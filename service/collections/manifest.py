"""Load and check a collection: its manifest, claim templates and query
templates (api/collections/*.schema.json), plus release specifications.

Everything a template or query could get wrong is caught here, before any
data is read: unknown metrics, rules, relations or entities, and queries that
filter or order one kind of row by another kind's metric (the no-blend rule).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "api" / "collections"
RESERVED_METRIC_NAMES = {"name", "value", "text"}


class ManifestError(ValueError):
    """A collection, template, query or release that cannot be used as written."""


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def _validate(doc, schema_name: str, where: str) -> None:
    validator = jsonschema.Draft202012Validator(_schema(schema_name))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [f"  {'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}" for e in errors[:10]]
        raise ManifestError(f"{where} does not match {schema_name}.schema.json:\n" + "\n".join(lines))


@dataclass
class SourceFile:
    """A manifest, template or query file: its parsed content and exact bytes."""
    path: Path
    rel: str
    data: bytes
    doc: dict

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Collection:
    root: Path
    manifest: SourceFile
    templates: list[SourceFile] = field(default_factory=list)
    queries: dict[str, SourceFile] = field(default_factory=dict)

    @property
    def doc(self) -> dict:
        return self.manifest.doc

    @property
    def key(self) -> str:
        return self.doc["collection"]

    @property
    def is_objects(self) -> bool:
        """A collection whose releases are built from deposits (kind: objects), not from result files."""
        return self.doc.get("kind") == "objects"

    def minting_section(self) -> dict:
        """The manifest content claims depend on. Adding a query or editing
        the title does not re-address any claim; changing a metric's
        interpretation or a tier rule does."""
        keys = ("collection", "methods", "attribution", "prefixes", "relations", "dataset_dimensions",
                "entities", "row_kinds", "edge_kinds", "file_roles", "metrics", "tier_rules", "assumptions")
        return {k: self.doc[k] for k in keys if k in self.doc}

    def minting_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.minting_section(), sort_keys=True).encode()).hexdigest()

    def kind_spec(self, kind: str) -> tuple[str, dict]:
        """('row' | 'edge', spec) for a row kind or edge kind."""
        if kind in self.doc["row_kinds"]:
            return "row", self.doc["row_kinds"][kind]
        if kind in (self.doc.get("edge_kinds") or {}):
            return "edge", self.doc["edge_kinds"][kind]
        raise ManifestError(f"unknown row or edge kind {kind!r}")

    def role_for(self, kind: str) -> str:
        roles = [r for r, spec in self.doc["file_roles"].items() if spec["yields"] == kind]
        if len(roles) != 1:
            raise ManifestError(f"kind {kind!r} must be yielded by exactly one file role, found {roles}")
        return roles[0]

    def relation(self, key: str) -> str:
        return (self.doc.get("relations") or {}).get(key, key)


def _read(path: Path, root: Path, schema_name: str) -> SourceFile:
    path = path.resolve()  # `..` and links are followed before the check, never after
    if not path.is_relative_to(root):
        raise ManifestError(f"{path} is outside the collection directory {root}; "
                            "template and query paths must be inside it")
    if not path.is_file():
        raise ManifestError(f"missing file {path}")
    data = path.read_bytes()
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not valid YAML: {exc}") from exc
    rel = str(path.relative_to(root))
    _validate(doc, schema_name, rel)
    return SourceFile(path=path, rel=rel, data=data, doc=doc)


def load_collection(root: Path | str) -> Collection:
    root = Path(root).resolve()
    path = root / "collection.yaml"
    try:
        objects = isinstance(raw := yaml.safe_load(path.read_bytes()), dict) and raw.get("kind") == "objects"
    except (OSError, yaml.YAMLError):
        objects = False  # _read reports it
    manifest = _read(path, root, "object-collection" if objects else "collection")
    col = Collection(root=root, manifest=manifest)
    for rel in manifest.doc.get("templates") or []:
        col.templates.append(_read(root / rel, root, "template"))
    for rel in manifest.doc.get("queries") or []:
        q = _read(root / rel, root, "object-query" if objects else "query")
        if q.doc["query"] in col.queries:
            raise ManifestError(f"query {q.doc['query']!r} is defined twice")
        col.queries[q.doc["query"]] = q
    (check_object_collection if objects else check_collection)(col)
    return col


def object_fields(doc: dict) -> set[str]:
    """Every field name an object collection's kinds declare (what its mappings may be keyed by)."""
    return {f for k in (doc.get("object_kinds") or {}).values() for f in k["fields"]}


def check_object_collection(col: Collection) -> None:
    """Cross-checks the object-collection schema cannot express: classes are DAPPER classes, and every query
    names declared classes, fields and links, and declares every variable it uses."""
    from .. import dapper_validation as dval

    doc = col.doc
    problems: list[str] = []
    known = dval.node_classes()
    kinds = doc["object_kinds"]
    for cls, spec in kinds.items():
        if cls not in known:
            problems.append(f"object_kinds: {cls!r} is not a class of the vendored DAPPER schema")
            continue
        slots = dval.slots(cls)
        for block in ("fields", "links"):
            for name, ref in (spec.get(block) or {}).items():
                if ref["slot"] not in slots:
                    problems.append(f"object_kinds.{cls}.{block}.{name}: {cls} has no slot {ref['slot']!r}")
    for block in ("mappings", "xrefs"):
        for f in doc.get(block) or {}:
            if f not in object_fields(doc):
                problems.append(f"{block}.{f}: not a field of any object kind")
    for name, q in col.queries.items():
        where = q.rel
        params = q.doc["params"]
        for v in _var_names(q.doc["plan"]):
            if v not in params:
                problems.append(f"{where}: plan uses ${v}, which params does not declare")

        def check_step(label, cls, step):
            spec = kinds.get(cls)
            if spec is None:
                problems.append(f"{where} {label}: {cls!r} is not one of the collection's object kinds")
                return
            used = [*(step.get("where") or {}), *([step["order"]["field"]] if step.get("order") else [])]
            for f in used:
                if f not in spec["fields"]:
                    problems.append(f"{where} {label}: {f!r} is not a field of {cls}")

        anchor = q.doc["plan"][0]["anchor"]
        check_step("anchor", anchor["class"], anchor)
        prev = anchor["class"]
        for step in q.doc["plan"][1:]:
            if "follow" in step:
                f = step["follow"]
                if f["link"] not in (kinds.get(prev) or {}).get("links", {}):
                    problems.append(f"{where} follow {f['as']}: {f['link']!r} is not a link of {prev}")
                if f.get("class"):
                    check_step(f"follow {f['as']}", f["class"], f)
            else:
                r = step["referenced_by"]
                if r["link"] not in (kinds.get(r["class"]) or {}).get("links", {}):
                    problems.append(f"{where} referenced_by {r['as']}: {r['link']!r} is not a link of {r['class']}")
                check_step(f"referenced_by {r['as']}", r["class"], r)
        try:
            bind_params(q.doc, q.doc["example"])
        except ManifestError as exc:
            problems.append(f"{where} example: {exc}")
    if problems:
        raise ManifestError("collection checks failed:\n" + "\n".join(f"  - {p}" for p in problems))


def _var_names(value):
    if isinstance(value, str) and value.startswith("$"):
        yield value[1:]
    elif isinstance(value, dict):
        for v in value.values():
            yield from _var_names(v)
    elif isinstance(value, list):
        for v in value:
            yield from _var_names(v)


def check_collection(col: Collection) -> None:
    doc = col.doc
    problems: list[str] = []
    metrics = doc["metrics"]
    entities = doc["entities"]
    rules = doc.get("tier_rules") or {}
    dims = [d["name"] for d in doc["dataset_dimensions"]]
    edge_kinds = doc.get("edge_kinds") or {}

    if len(set(dims)) != len(dims):
        problems.append("dataset_dimensions names must be distinct")
    for block in ("mappings", "unmapped"):
        for dim in doc.get(block) or {}:
            if dim not in dims:
                problems.append(f"{block}.{dim}: not a dataset dimension ({dims})")
    for name in metrics:
        if name in RESERVED_METRIC_NAMES:
            problems.append(f"metric name {name!r} is reserved (name, value and text are placeholder fields)")
    for name, spec in entities.items():
        modes = [k for k in ("id", "mint", "dimension") if k in spec]
        if len(modes) != 1:
            problems.append(f"entity {name}: give exactly one of id, mint or dimension")
        if spec.get("dimension") and spec["dimension"] not in dims:
            problems.append(f"entity {name}: unknown dimension {spec['dimension']!r}")
        if spec.get("mint"):
            if spec.get("members_from") not in edge_kinds:
                problems.append(f"entity {name}: members_from must name an edge kind")
            if spec.get("library_dimension") not in dims:
                problems.append(f"entity {name}: library_dimension must name a dataset dimension")
    for kind, spec in {**doc["row_kinds"], **edge_kinds}.items():
        for end in ("subject", "object", "from", "to"):
            ref = spec.get(end)
            if ref is None:
                continue
            ent = entities.get(ref["entity"])
            if ent is None:
                problems.append(f"{kind}.{end}: unknown entity {ref['entity']!r}")
            elif "dimension" not in ent and "column" not in ref:
                problems.append(f"{kind}.{end}: entity {ref['entity']} needs a column")
        for m in spec.get("metrics", []):
            if m not in metrics:
                problems.append(f"{kind}: unknown metric {m!r}")
    for role, spec in doc["file_roles"].items():
        if spec["yields"] not in doc["row_kinds"] and spec["yields"] not in edge_kinds:
            problems.append(f"file role {role}: yields unknown kind {spec['yields']!r}")
    for kind in [*doc["row_kinds"], *edge_kinds]:
        try:
            col.role_for(kind)
        except ManifestError as exc:
            problems.append(str(exc))
    for rule_name, rule in rules.items():
        if "min" in rule["tiers"][-1]:
            problems.append(f"tier rule {rule_name}: the last tier must be the catch-all (no min)")

    seen_templates = set()
    for t in col.templates:
        problems.extend(_check_template(col, t, seen_templates))
    for q in col.queries.values():
        problems.extend(_check_query(col, q))
    if problems:
        raise ManifestError(f"collection {col.key!r} has {len(problems)} problem(s):\n  "
                            + "\n  ".join(problems))


def _check_template(col: Collection, t: SourceFile, seen: set) -> list[str]:
    doc, where = t.doc, t.rel
    out = []
    if doc["template"] in seen:
        out.append(f"{where}: template name {doc['template']!r} is used twice")
    seen.add(doc["template"])
    try:
        shape, spec = col.kind_spec(doc["for"])
    except ManifestError as exc:
        return [f"{where}: {exc}"]
    if shape == "edge" and doc.get("group_by") is None:
        out.append(f"{where}: an edge-kind template needs group_by")
    if shape == "row" and doc.get("group_by") is not None:
        out.append(f"{where}: group_by applies only to edge kinds")
    metrics = spec.get("metrics", [])
    keys = []
    for c in doc["claims"]:
        cw = f"{where} claim {c['key']}"
        if c["key"] in keys:
            out.append(f"{cw}: key used twice")
        if c.get("focus") and c["focus"] not in metrics:
            out.append(f"{cw}: focus {c['focus']!r} is not a metric of {doc['for']}")
        if c.get("classify"):
            if c["classify"] not in (col.doc.get("tier_rules") or {}):
                out.append(f"{cw}: unknown tier rule {c['classify']!r}")
            if not c.get("focus"):
                out.append(f"{cw}: classify needs a focus metric")
        for m in c.get("scores", []):
            if m not in metrics:
                out.append(f"{cw}: score {m!r} is not a metric of {doc['for']}")
        ev = c.get("evidence")
        if ev:
            if shape == "edge":
                out.append(f"{cw}: edge groups carry no metrics to ground evidence in")
            for m in ev["per_metric"]:
                if m == "focus" and not c.get("focus"):
                    out.append(f"{cw}: evidence names focus, but the claim has none")
                elif m != "focus" and m not in metrics:
                    out.append(f"{cw}: evidence metric {m!r} is not a metric of {doc['for']}")
            for src in ev.get("source_claims", []):
                if src not in keys:
                    out.append(f"{cw}: source claim {src!r} must be an earlier claim in this template")
            if ev.get("source_claims") and not (c.get("classify") and ev.get("context")):
                out.append(f"{cw}: evidence with source_claims needs a direction (classify) and a context")
        rel = c["proposition"]["relation"]
        if rel not in (col.doc.get("relations") or {}) and ":" not in rel:
            out.append(f"{cw}: relation {rel!r} is neither in relations nor a CURIE")
        keys.append(c["key"])
    return out


def _check_query(col: Collection, q: SourceFile) -> list[str]:
    doc, where = q.doc, q.rel
    out = []
    params = doc["params"]
    dims = {d["name"] for d in col.doc["dataset_dimensions"]}

    def var_names(value):
        if isinstance(value, str) and value.startswith("$"):
            yield value[1:]
        elif isinstance(value, dict):
            for v in value.values():
                yield from var_names(v)
        elif isinstance(value, list):
            for v in value:
                yield from var_names(v)

    for name in var_names(doc["plan"]):
        if name not in params:
            out.append(f"{where}: plan uses ${name}, which params does not declare")

    def own_metrics(step_where, kind, step):
        if kind not in col.doc["row_kinds"]:
            out.append(f"{step_where}: {kind!r} is not a row kind")
            return
        mine = set(col.doc["row_kinds"][kind]["metrics"])
        used = [step["order"]["metric"], *(step.get("where") or {})]
        for m in used:
            if m not in mine:
                out.append(f"{step_where}: {m!r} is not a metric of {kind}; a step may only filter and "
                           "order on its own kind's metrics (no blending across kinds)")

    anchor = doc["plan"][0]["anchor"]
    own_metrics(f"{where} anchor", anchor["kind"], anchor)
    for d in anchor.get("dimensions") or {}:
        if d not in dims:
            out.append(f"{where} anchor: unknown dimension {d!r}")
    prev_kind = anchor["kind"]
    for step in doc["plan"][1:]:
        j = step["join"]
        own_metrics(f"{where} join {j['as']}", j["kind"], j)
        edge = (col.doc.get("edge_kinds") or {}).get(j["via"])
        if edge is None:
            out.append(f"{where} join {j['as']}: unknown edge kind {j['via']!r}")
            continue
        rows = col.doc["row_kinds"]
        if prev_kind in rows and rows[prev_kind]["subject"]["entity"] != edge["from"]["entity"]:
            out.append(f"{where} join {j['as']}: {j['via']} starts at {edge['from']['entity']}, "
                       f"but {prev_kind} rows are about {rows[prev_kind]['subject']['entity']}")
        if j["kind"] in rows and rows[j["kind"]]["subject"]["entity"] != edge["to"]["entity"]:
            out.append(f"{where} join {j['as']}: {j['via']} ends at {edge['to']['entity']}, "
                       f"but {j['kind']} rows are about {rows[j['kind']]['subject']['entity']}")
    if len(doc["plan"]) > 2:
        out.append(f"{where}: plans support one anchor and one join for now")
    try:
        bind_params(doc, doc["example"])
    except ManifestError as exc:
        out.append(f"{where} example: {exc}")
    return out


def bind_params(query_doc: dict, variables: dict | None) -> dict:
    """Variables checked against the query's params, with defaults filled."""
    variables = dict(variables or {})
    params = query_doc["params"]
    unknown = sorted(set(variables) - set(params))
    if unknown:
        raise ManifestError(f"unknown variable(s) {unknown}; this query takes {sorted(params)}")
    out = {}
    for name, spec in params.items():
        value = variables.get(name, spec.get("default"))
        if value is None:
            if spec.get("required"):
                raise ManifestError(f"variable {name!r} is required")
            out[name] = None
            continue
        t = spec["type"]
        ok = {"string": isinstance(value, str),
              "number": isinstance(value, (int, float)) and not isinstance(value, bool),
              "integer": isinstance(value, int) and not isinstance(value, bool),
              "boolean": isinstance(value, bool)}[t]
        if not ok:
            raise ManifestError(f"variable {name!r} must be a {t}, got {value!r}")
        if "min" in spec and value < spec["min"]:
            raise ManifestError(f"variable {name!r} must be at least {spec['min']}, got {value}")
        if "max" in spec and value > spec["max"]:
            raise ManifestError(f"variable {name!r} must be at most {spec['max']}, got {value}")
        out[name] = value
    return out


def load_release(path: Path | str) -> tuple[dict, Path]:
    """A release specification and the directory its file paths are relative to."""
    path = Path(path).resolve()
    doc = yaml.safe_load(path.read_bytes())
    _validate(doc, "release", path.name)
    return doc, path.parent
