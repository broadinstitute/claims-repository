"""Publish a collection release: manifest + release spec + result files in,
a DAPPER ResultCollection and its datasets, rows, edges, metrics and
mintable claims out.

Claims are not stored. Every claim a row implies is rendered once here to
learn its id; result_claims records that id with the mint context that
re-renders it, and the service mints the claim when it first serves it.
What IS stored is the data plane the claims reference: Files, Activities,
Datasets, the ResultCollection, GeneSet entities, Propositions, ClaimScores,
ValueLocators and EvidenceItems.

Datasets are content-addressed and shared by every release that lists them:
a dataset already ingested is not read again, and a (template, file) pairing
already rendered is not rendered again.

A release may hold only part of its result files (`where` in the release
specification: strict lower bounds per row kind and metric). A row below a
bound is not ingested: no result row, proposition, evidence or claim. A
minted entity (a gene set) exists only for a kept row, with its whole roster:
every edge that ends at it is ingested, whatever the other end's score, since
its roster claim counts them. The bounds are named in the Dataset record, so
the same files under other bounds are a different dataset; they enter no claim
id. Result files may be given by upstream location with their sha256 and size
instead of a local path; each dataset is then fetched when it is ingested and
checked against that sha256.

Each dataset is ingested, rendered and stored in its own transaction, and the
release is recorded last, so an interrupted publish resumes where it stopped
and nothing is served from a release that is not complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import engine
from . import identity as idn
from .manifest import Collection, ManifestError, SourceFile
from .traits import TraitRegistry, resolve_dimension
from ..refs import dapper_refs


class PublishError(ValueError):
    """A release that cannot be published as specified."""


@dataclass
class Publication:
    release_id: str
    version: str
    document: dict
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # False when the publish stopped at max_ingest: datasets are stored, the release is not recorded.
    complete: bool = True
    # dry_run only: what a real publish would write with (datasets, template
    # File docs, registry, the version already stored), for the admin check.
    plan: dict | None = None


# Reading the state of a directory an admin chose must not run commands its
# repository configures (fsmonitor, hooks) or rewrite its .git/index.
_GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


def _git(*args: str) -> str:
    return subprocess.run([*_GIT, *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout.strip()


def _git_info(path: Path) -> tuple[str | None, bool, str | None]:
    """(commit, dirty, path relative to the repository root) for a file."""
    try:
        top = _git("-C", str(path.parent), "rev-parse", "--show-toplevel")
        commit = _git("-C", top, "rev-parse", "HEAD")
        dirty = bool(_git("-C", top, "status", "--porcelain", "--", str(path.parent)))
        return commit, dirty, str(path.resolve().relative_to(Path(top).resolve()))
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None, False, None


def _source_file_doc(src: SourceFile, description: str) -> dict:
    _, _, rel = _git_info(src.path)
    return idn.mint("File", {
        "filename": src.path.name,
        "description": description,
        "mime_type": "application/yaml",
        "sha256": src.sha256,
        "size_in_bytes": len(src.data),
        "location": rel or src.rel,
    })


_PY_NONE = re.compile(rb'(?<=": )None(?=[,}])')


def _ndjson(path: Path, python_none: bool = False):
    """(row, line bytes, absolute byte offset) per non-blank line.
    python_none: read a bare Python `None` value as null. Some aggregator runs
    write their membership file that way; it is accepted only for edge files,
    whose values are never ingested (only the two ends of each edge are)."""
    offset = 0
    with open(path, "rb") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if stripped:
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    if not python_none:
                        raise PublishError(f"{path.name} line {lineno}: not valid JSON ({exc.msg})") from exc
                    try:
                        row = json.loads(_PY_NONE.sub(b"null", stripped))
                    except json.JSONDecodeError as exc2:
                        raise PublishError(f"{path.name} line {lineno}: not valid JSON ({exc2.msg})") from exc2
                if not isinstance(row, dict):
                    raise PublishError(f"{path.name} line {lineno}: expected a JSON object, "
                                       f"got {type(row).__name__}")
                yield row, line, offset
            offset += len(line)


class _Entities:
    """Identifiers for the collection's entity types within one dataset."""

    def __init__(self, col: Collection, dims: dict):
        self.col, self.dims = col, dims
        self.minted: dict[tuple[str, str], dict] = {}   # (entity type, value) -> DAPPER doc
        self.libraries: dict[str, dict] = {}

    def spec(self, entity: str) -> dict:
        return self.col.doc["entities"][entity]

    def ident(self, entity: str, value: str) -> tuple[str, str]:
        """(id, label) for a value of an entity type."""
        spec = self.spec(entity)
        if "dimension" in spec:
            dim = self.dims[spec["dimension"]]
            return dim["value"], dim["label"]
        if "id" in spec:
            return spec["id"].format(value=value), value
        doc = self.minted.get((entity, value))
        if doc is None:
            raise PublishError(f"{entity} {value!r} was not minted for this dataset")
        return doc["id"], value

    def library(self, entity: str) -> dict:
        lib = self.dims[self.spec(entity)["library_dimension"]]["value"]
        if lib not in self.libraries:
            self.libraries[lib] = engine.gene_set_collection(lib)
        return self.libraries[lib]


def where_for(col: Collection, spec: dict, ds: dict) -> dict:
    """{row kind: {metric: bound}} for one dataset: its own `where`, else the
    release's. Checked against the manifest's row kinds and metrics."""
    where = ds.get("where") if "where" in ds else spec.get("where")
    out = {}
    for kind, bounds in (where or {}).items():
        if kind not in col.doc["row_kinds"]:
            raise PublishError(f"where names unknown row kind {kind!r}")
        for metric, bound in bounds.items():
            if metric not in col.doc["row_kinds"][kind]["metrics"]:
                raise PublishError(f"where.{kind} names {metric!r}, which is not a metric of that row kind")
            out.setdefault(kind, {})[metric] = float(bound["gt"])
    return out


def where_text(where: dict) -> str | None:
    """The bounds as they are written into a Dataset description, or None."""
    if not where:
        return None
    return "; ".join(f"{kind} rows with " + " and ".join(f"{m} > {b:g}" for m, b in sorted(bounds.items()))
                     for kind, bounds in sorted(where.items()))


def _kept(row: dict, bounds: dict | None) -> bool:
    return all(idn.num(row[m]) > b for m, b in (bounds or {}).items())


def _file_bytes(where: str, role: str, f: dict, base: Path) -> tuple[Path | None, str, int]:
    """(local path or None, sha256, size) of one result file of a release
    specification: read and hashed when it is on disk, else taken as declared
    (it is fetched and checked when its dataset is ingested)."""
    path = (base / f["path"]).resolve() if f.get("path") else None
    if path is not None and path.is_file():
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if f.get("sha256") and f["sha256"] != sha:
            raise PublishError(f"{where}: {role} file {path} has sha256 {sha}, the release declares {f['sha256']}")
        return path, sha, len(data)
    if f.get("location") and f.get("sha256") and f.get("size") is not None:
        return None, f["sha256"], int(f["size"])
    raise PublishError(f"{where}: {role} file {path or '(no path)'} does not exist, and the release does not "
                       "declare its location, sha256 and size")


def publish(con, col: Collection, spec: dict, base: Path, *, allow_dirty: bool = True, dry_run: bool = False,
            progress: Callable[[str], None] | None = None,
            fetch: Callable[[str, Path], None] | None = None, workdir: Path | None = None,
            max_ingest: int | None = None, shard: tuple[int, int] | None = None) -> Publication:
    """Publish a release. `dry_run` computes the release id, its DAPPER
    document (less the mint-context Activities) and warnings, and returns
    before anything is written; a version already published with other
    content is then reported in `plan`, not raised. `progress` is called with
    a short phase name as work proceeds. `fetch(location, destination)`
    downloads a result file the release gives by location; files land in
    `workdir` and are removed once their dataset is stored. `max_ingest`
    stops after that many datasets have been ingested by this call, without
    recording the release (nothing is served from it until a later call
    finishes it): a first batch can be checked before the rest goes in.
    `shard` = (i, n) handles only every n-th dataset starting at the i-th, so n
    processes can ingest one release side by side; a last unsharded call
    records the release."""
    step = progress or (lambda phase: None)
    doc = col.doc
    dims_spec = doc["dataset_dimensions"]
    dim_names = [d["name"] for d in dims_spec]
    registry = TraitRegistry(con) if any(d.get("resolve") == "kpn-traits" for d in dims_spec) else None
    warnings: list[str] = []
    objects: dict[str, tuple[str, dict]] = {}

    def keep(cls, document):
        objects[document["id"]] = (cls, document)
        return document

    commit, dirty, _ = _git_info(col.manifest.path)
    if dirty:
        msg = f"{col.root} has uncommitted changes; this release records commit {commit} as dirty"
        if not allow_dirty:
            raise PublishError(msg)
        warnings.append(msg)
    code_version = f"{commit}{'-dirty' if dirty else ''}" if commit else "uncommitted"

    manifest_file = keep("File", _source_file_doc(col.manifest, f"{col.key} collection manifest."))
    template_files = {t.doc["template"]: keep("File", _source_file_doc(
        t, f"{col.key} claim template {t.doc['template']} v{t.doc['version']}.")) for t in col.templates}
    query_files = {name: keep("File", _source_file_doc(q, f"{col.key} query template {name}."))
                   for name, q in col.queries.items()}

    # --- datasets --------------------------------------------------------
    step("Hashing result files")
    datasets = []
    used_edges, generated_edges = [], []
    for i, ds in enumerate(spec["datasets"]):
        where = f"dataset {i + 1}"
        raw_dims = ds["dimensions"]
        if set(raw_dims) != set(dim_names):
            raise PublishError(f"{where}: dimensions must be exactly {dim_names}, got {sorted(raw_dims)}")
        dims = {}
        for d in dims_spec:
            value, label = resolve_dimension(registry, d, raw_dims[d["name"]])
            dims[d["name"]] = {"value": value, "label": label, "raw": raw_dims[d["name"]]}
        unknown_roles = sorted(set(ds["files"]) - set(doc["file_roles"]))
        if unknown_roles:
            raise PublishError(f"{where}: unknown file role(s) {unknown_roles}")
        files = {}
        for role, f in sorted(ds["files"].items()):
            path, sha, size = _file_bytes(where, role, f, base)
            name = path.name if path is not None else f["location"].rstrip("/").rsplit("/", 1)[-1]
            role_spec = doc["file_roles"][role]
            files[role] = {"path": path, "name": name, "location": f.get("location"), "doc": keep("File", idn.mint("File", {
                "filename": name,
                "description": role_spec.get("description"),
                "mime_type": role_spec.get("media_type"),
                "sha256": sha,
                "size_in_bytes": size,
                "location": f.get("location") or name,
            }))}
        where = where_for(col, spec, ds)
        label = ", ".join(f"{n}={dims[n]['label']}" for n in dim_names)
        # DAPPER's trait slot takes a CURIE (e.g. a registry-resolved
        # KPN.TRAIT id); an unregistered bare key stays only a dimension value.
        trait = dims["trait"]["value"] if "trait" in dims and ":" in dims["trait"]["value"] else None
        run = keep("Activity", idn.mint("Activity", {
            "name": f"{' / '.join(doc['methods'])} run: {label}",
            "description": ds.get("run") or f"The {' / '.join(doc['methods'])} run that wrote these result files.",
            "software_name": doc["methods"][0] if len(doc["methods"]) == 1 else None,
            "trait": trait,
        }))
        for f in files.values():
            generated_edges.append({"subject": f["doc"]["id"], "predicate": "prov:wasGeneratedBy",
                                    "object": run["id"]})
        dataset = keep("Dataset", idn.mint("Dataset", {
            "name": f"{doc['title']}: {label}",
            "description": f"{doc['title']} for {label}." + (f" Holds only {where_text(where)}." if where else ""),
            "trait": trait,
            "has_file": [f["doc"]["id"] for f in files.values()],
            "dimension_values": [{"dimension": n, "dimension_value": dims[n]["value"]} for n in dim_names],
            "file_roles": [{"role": r, "file": f["doc"]["id"]} for r, f in files.items()],
            "was_generated_by": run["id"],
        }))
        datasets.append({"doc": dataset, "dims": dims, "files": files, "where": where})

    # --- the release -----------------------------------------------------
    # Re-publishing a version keeps the predecessor it was first published with.
    existing = con.execute("SELECT release_id, document FROM collection_releases WHERE collection_key = ? "
                           "AND version = ?", (col.key, spec["version"])).fetchone()
    if existing:
        previous_id = json.loads(existing["document"])["result_collections"][0].get("was_revision_of")
    else:
        current = con.execute("SELECT current_release_id FROM collections WHERE collection_key = ?",
                              (col.key,)).fetchone()
        previous_id = current["current_release_id"] if current else None
    publication = keep("Activity", idn.mint("Activity", {
        "name": f"Publish {col.key} {spec['version']}",
        "description": (f"Built release {spec['version']} of the {col.key} collection from its manifest, "
                        "checked each result file's sha256, and recorded the release."),
        "activity_type": "ResultCollectionPublication",
        "software_name": engine.SOFTWARE,
        "software_version": engine.ENGINE_VERSION,
        "code_version": code_version,
    }))
    used_edges.append({"subject": publication["id"], "predicate": "prov:used", "object": manifest_file["id"]})
    release = idn.mint("ResultCollection", {
        "name": f"{doc['title']} {spec['version']}",
        "collection_key": col.key,
        "version": spec["version"],
        "description": doc.get("description"),
        "methods": doc["methods"],
        "dataset_dimensions": dim_names,
        "members": [d["doc"]["id"] for d in datasets],
        "n_members": len(datasets),
        "has_claim_template": [template_files[t.doc["template"]]["id"] for t in col.templates],
        "has_query_template": [query_files[n]["id"] for n in col.queries] or None,
        "has_manifest_file": manifest_file["id"],
        "was_generated_by": publication["id"],
        "was_revision_of": previous_id,
    })
    keep("ResultCollection", release)
    if existing and existing["release_id"] != release["id"] and not dry_run:
        raise PublishError(f"{col.key} {spec['version']} is already published as {existing['release_id']} "
                           "with different content; publish a new version")

    def group(cls):
        return [d for c, d in objects.values() if c == cls]
    document = {
        "prefixes": doc.get("prefixes") or {},
        "files": group("File"),
        "activities": group("Activity"),
        "datasets": group("Dataset"),
        "result_collections": [release],
        "used_edges": used_edges,
        "was_generated_by_edges": generated_edges,
    }

    stats = {"datasets": len(datasets), "datasets_ingested": 0, "rows": 0, "edges": 0,
             "mint_contexts_rendered": 0, "claims": 0}
    if dry_run:
        if registry is not None and registry.unresolved:
            warnings.append(_unresolved_warning(registry))
        return Publication(release_id=release["id"], version=spec["version"], document=document, stats=stats,
                           warnings=warnings, plan={
                               "datasets": datasets, "template_files": template_files, "registry": registry,
                               "existing_release_id": existing["release_id"] if existing else None,
                               "previous_release_id": previous_id, "commit": commit, "dirty": dirty,
                               "code_version": code_version})
    minting, digest = col.minting_section(), col.minting_digest()
    stats["objects"] = 0
    # One transaction per dataset: its rows, its claims' data plane and its
    # mint contexts land together or not at all, and memory holds one dataset.
    unfinished = 0
    for n, ds in enumerate(datasets, 1):
        dataset_id = ds["doc"]["id"]
        ds["contexts"] = []
        for t in col.templates:
            ctx = mint_context_for(col, t, template_files[t.doc["template"]]["id"], ds, minting, digest)
            if ctx is not None:
                ds["contexts"].append(ctx)
                keep("Activity", ctx.activity)
        ingest = not con.execute("SELECT 1 FROM dataset_files WHERE dataset_id = ? LIMIT 1", (dataset_id,)).fetchone()
        pending = [ctx for ctx in ds["contexts"] if not con.execute(
            "SELECT 1 FROM mint_contexts WHERE activity_id = ?", (ctx.activity["id"],)).fetchone()]
        if not ingest and not pending:
            continue
        if (shard is not None and (n - 1) % shard[1] != shard[0]) or \
                (ingest and max_ingest is not None and stats["datasets_ingested"] >= max_ingest):
            unfinished += 1   # another shard's, or past this call's limit
            continue
        if ingest:
            _refuse_other_slice(con, ds)
        fetched = _fetch_files(ds, fetch, workdir, f"dataset {n}") if ingest else []
        try:
            for attempt in range(1, 4):
                ds_objects: dict[str, tuple[str, dict]] = {}
                n_rows = n_edges = n_claims = 0
                try:
                    with con:
                        if ingest:
                            step(f"Ingesting dataset {n} of {len(datasets)}")
                            n_rows, n_edges = _ingest_dataset(con, col, ds, ds_objects, registry)
                        for ctx in pending:
                            step(f"Rendering template {ctx.template['template']} (dataset {n} of {len(datasets)})")
                            n_claims += _render_context(con, col, ctx, ds, ds_objects)
                        _store_objects(con, ds_objects)
                    break
                except Exception as exc:
                    # Shards share a few rows (scores, the library record): a lock clash rolls this
                    # dataset back whole, so it is simply tried again.
                    if attempt == 3 or not any(code in str(exc) for code in ("1213", "1205", "Deadlock")):
                        raise
                    step(f"Lock clash on dataset {n}; retrying")
        finally:
            _remove(fetched)
        stats["datasets_ingested"] += int(ingest)
        stats["rows"] += n_rows
        stats["edges"] += n_edges
        stats["claims"] += n_claims
        stats["mint_contexts_rendered"] += len(pending)
        stats["objects"] += len(ds_objects)
    if unfinished:
        warnings.append(f"{unfinished} of {len(datasets)} dataset(s) are not stored yet (outside this call's shard "
                        "or limit); the release is not recorded. Run again to continue.")
        return Publication(release_id=release["id"], version=spec["version"], document=document, stats=stats,
                           warnings=warnings, complete=False)
    with con:
        step("Storing objects")
        _store_objects(con, objects)
        step("Recording the release")
        con.execute(
            f"{con.dialect.insert_or_ignore} collection_releases "
            "(release_id, collection_key, version, manifest, document, git_commit) VALUES (?, ?, ?, ?, ?, ?)",
            (release["id"], col.key, spec["version"], json.dumps(_manifest_record(col, template_files, query_files)),
             json.dumps(document), code_version),
        )
        con.executemany(f"{con.dialect.insert_or_ignore} release_datasets (release_id, dataset_id) VALUES (?, ?)",
                        [(release["id"], d["doc"]["id"]) for d in datasets])
        con.executemany(
            f"{con.dialect.insert_or_ignore} release_mint_contexts (release_id, activity_id, dataset_id) VALUES (?, ?, ?)",
            [(release["id"], ctx.activity["id"], d["doc"]["id"]) for d in datasets for ctx in d["contexts"]])
        con.execute(
            f"{con.dialect.insert_or_replace} collections (collection_key, title, description, current_release_id) "
            "VALUES (?, ?, ?, ?)", (col.key, doc["title"], doc.get("description"), release["id"]))
    if registry is not None and registry.unresolved:
        warnings.append(_unresolved_warning(registry))
    stats["objects"] += len(objects)
    return Publication(release_id=release["id"], version=spec["version"], document=document,
                       stats=stats, warnings=warnings)


def _refuse_other_slice(con, ds: dict) -> None:
    """The same result files already ingested as another dataset of the same
    dimensions, so under other `where` bounds: claims are shared by id, so two
    slices of one file cannot both be served, and adding rows to an ingested
    dataset is not supported. (A file listed under the wrong dimensions is
    caught, with a clearer message, when its rows are read.)"""
    mine = {n: d["value"] for n, d in ds["dims"].items()}
    for f in ds["files"].values():
        for other in con.execute("SELECT DISTINCT dataset_id FROM dataset_files WHERE file_id = ? AND dataset_id <> ?",
                                 (f["doc"]["id"], ds["doc"]["id"])).fetchall():
            theirs = {r["dimension"]: r["value"] for r in con.execute(
                "SELECT dimension, value FROM dataset_dimensions WHERE dataset_id = ?", (other["dataset_id"],)).fetchall()}
            if theirs == mine:
                raise PublishError(
                    f"result file {f['name']} ({f['doc']['id']}) is already ingested as dataset "
                    f"{other['dataset_id']} under other `where` bounds; one file cannot be served as two "
                    "datasets, and adding rows to an ingested dataset is not supported")


def _remove(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def _fetch_files(ds: dict, fetch, workdir: Path | None, where: str) -> list[Path]:
    """Download the dataset's files that the release gave by location, and
    check each against its declared sha256. Returns the paths to remove."""
    fetched = []
    for role, f in ds["files"].items():
        if f["path"] is not None:
            continue
        if fetch is None or workdir is None:
            raise PublishError(f"{where}: {role} file is given by location ({f['location']}); "
                               "publishing it needs a fetcher and a working directory")
        dest = workdir / f["doc"]["sha256"][:16] / f["name"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        fetched.append(dest)
        fetch(f["location"], dest)
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        if sha != f["doc"]["sha256"]:
            _remove(fetched)
            raise PublishError(f"{where}: {f['location']} has sha256 {sha}, the release declares {f['doc']['sha256']}")
        f["path"] = dest
    return fetched


def _unresolved_warning(registry: TraitRegistry) -> str:
    return (f"{len(registry.unresolved)} trait key(s) are not in the KPN trait registry and were "
            f"kept as given: {sorted(registry.unresolved)[:5]}")


def mint_context_for(col: Collection, t: SourceFile, template_file_id: str, ds: dict, minting: dict,
                     digest: str) -> engine.MintContext | None:
    """The (template, result file) pairing for one dataset, or None when the
    dataset has no file of the role the template reads."""
    role = col.role_for(t.doc["for"])
    if role not in ds["files"]:
        return None
    result = ds["files"][role]
    return engine.MintContext(
        minting=minting, minting_digest=digest, template=t.doc, template_file_id=template_file_id,
        result_file_id=result["doc"]["id"], file_name=result["name"],
        media_type=col.doc["file_roles"][role].get("media_type") or "application/octet-stream")


def _manifest_record(col: Collection, template_files: dict, query_files: dict) -> dict:
    """What discovery serves for a release: the manifest, templates and
    queries as parsed, with the DAPPER File id of each."""
    return {
        "manifest": col.doc,
        "templates": [{"file_id": template_files[t.doc["template"]]["id"], "path": t.rel, "doc": t.doc}
                      for t in col.templates],
        "queries": {name: {"file_id": query_files[name]["id"], "path": q.rel, "doc": q.doc}
                    for name, q in col.queries.items()},
    }


def _check_dimension_columns(col, role, row, ds, registry, where):
    for dim, column in (col.doc["file_roles"][role].get("dimension_columns") or {}).items():
        if column not in row:
            raise PublishError(f"{where}: column {column!r} (dimension {dim}) is missing")
        spec = next(d for d in col.doc["dataset_dimensions"] if d["name"] == dim)
        value, _ = resolve_dimension(registry, spec, str(row[column]))
        if value != ds["dims"][dim]["value"]:
            raise PublishError(f"{where}: {column}={row[column]!r} does not match the dataset's {dim} "
                               f"{ds['dims'][dim]['raw']!r}; is this file in the right dataset?")


def dataset_entities(col: Collection, ds: dict, objects: dict, registry) -> tuple[_Entities, dict]:
    """Read a dataset's edge files and mint its entities: (entities, edges by
    kind as {(from value, to value): (from id, from value, to value)}).
    Minted entities (gene sets) take their members from the whole edge file,
    so their ids are known only after every edge has been read."""
    doc = col.doc
    ents = _Entities(col, ds["dims"])
    edge_kinds = doc.get("edge_kinds") or {}

    # Edges first: minted entities (gene sets) take their members from them.
    edges: dict[str, dict[tuple, tuple]] = {}
    members: dict[str, dict[str, set]] = {}     # minted entity type -> value -> member ids
    for kind, spec in edge_kinds.items():
        role = col.role_for(kind)
        if role not in ds["files"]:
            continue
        f = ds["files"][role]
        rows = edges.setdefault(kind, {})
        for n, (row, _, _) in enumerate(_ndjson(f["path"], python_none=True), 1):
            where = f"{f['path'].name} line {n}"
            _check_dimension_columns(col, role, row, ds, registry, where)
            from_value, to_value = str(row[spec["from"]["column"]]), str(row[spec["to"]["column"]])
            if "mint" in ents.spec(spec["from"]["entity"]):
                raise PublishError(f"edge kind {kind}: a minted entity can only be an edge's `to` end")
            from_id, _ = ents.ident(spec["from"]["entity"], from_value)
            rows[(from_value, to_value)] = (from_id, from_value, to_value)
            to_entity = spec["to"]["entity"]
            if "mint" in ents.spec(to_entity) and ents.spec(to_entity)["members_from"] == kind:
                members.setdefault(to_entity, {}).setdefault(to_value, set()).add(from_id)

    # Minted entities: every value seen as an edge end or a row subject.
    for entity, spec in doc["entities"].items():
        if "mint" not in spec:
            continue
        row_values, bounded = set(), False
        for kind, rk in doc["row_kinds"].items():
            role = col.role_for(kind)
            if rk["subject"]["entity"] == entity and role in ds["files"]:
                bounds = (ds.get("where") or {}).get(kind)
                bounded = bounded or bool(bounds)
                row_values.update(str(r[rk["subject"]["column"]]) for r, _, _ in _ndjson(ds["files"][role]["path"])
                                  if _kept(r, bounds))
        if bounded:
            # Under bounds an entity exists only for a kept row: not for one seen only as an
            # edge end, and no edge ends at a dropped one. A kept entity keeps its whole roster.
            values = row_values
            for edge_kind, edge_spec in edge_kinds.items():
                if edge_spec["to"]["entity"] == entity and edge_kind in edges:
                    edges[edge_kind] = {pair: v for pair, v in edges[edge_kind].items() if pair[1] in values}
            members[entity] = {v: m for v, m in members.get(entity, {}).items() if v in values}
        else:
            values = set(members.get(entity, {})) | row_values
        library = ents.library(entity)
        objects[library["id"]] = ("GeneSetCollection", library)
        membership_role = col.role_for(spec["members_from"])
        membership_file = ds["files"].get(membership_role, {}).get("doc", {}).get("id")
        for value in sorted(values):
            entity_doc = engine.gene_set(value, library, sorted(members.get(entity, {}).get(value, ())),
                                         membership_file)
            ents.minted[(entity, value)] = entity_doc
            objects[entity_doc["id"]] = ("GeneSet", entity_doc)
    return ents, edges


def dataset_rows(col: Collection, ds: dict, ents: _Entities, kind: str, registry):
    """(subject value, result_rows column values) per row of one row kind,
    in file order, checked as the ingester checks them."""
    rk = col.doc["row_kinds"][kind]
    role = col.role_for(kind)
    f = ds["files"][role]
    bounds = (ds.get("where") or {}).get(kind)
    seen = set()
    for n, (row, line, offset) in enumerate(_ndjson(f["path"]), 1):
        where = f"{f['path'].name} line {n}"
        _check_dimension_columns(col, role, row, ds, registry, where)
        if not _kept(row, bounds):
            continue
        subject_value = str(row[rk["subject"]["column"]])
        subject_id, subject_label = ents.ident(rk["subject"]["entity"], subject_value)
        if subject_id in seen:
            raise PublishError(f"{where}: {subject_value!r} appears twice in {f['path'].name}")
        seen.add(subject_id)
        object_value = str(row[rk["object"]["column"]]) if "column" in rk["object"] else ""
        object_id, object_label = ents.ident(rk["object"]["entity"], object_value)
        spans = {}
        for m in rk["metrics"]:
            located = engine.locate(line, m)
            if located is None or m not in row:
                raise PublishError(f"{where}: metric {m!r} is missing")
            start, length, text = located
            spans[m] = [offset + start, length, text]
            idn.num(row[m])  # refuse non-finite values at ingest, not at serve
        yield {"subject_id": subject_id, "subject_value": subject_value, "subject_label": subject_label,
               "object_id": object_id, "object_label": object_label, "file_id": f["doc"]["id"],
               "row_json": idn.canonical(row), "spans": idn.canonical(spans)}


ROW_COLUMNS = ("subject_id", "subject_value", "subject_label", "object_id", "object_label", "file_id",
               "row_json", "spans")


def _ingest_dataset(con, col: Collection, ds: dict, objects: dict, registry) -> tuple[int, int]:
    doc = col.doc
    dataset_id = ds["doc"]["id"]
    edge_kinds = doc.get("edge_kinds") or {}
    ents, edges = dataset_entities(col, ds, objects, registry)

    edge_rows = []
    for kind, pairs in edges.items():
        spec = edge_kinds[kind]
        for (from_value, to_value), (from_id, _, _) in sorted(pairs.items()):
            to_id, _ = ents.ident(spec["to"]["entity"], to_value)
            edge_rows.append((dataset_id, kind, from_id, to_id, from_value, to_value))

    n_rows = 0
    for kind, rk in doc["row_kinds"].items():
        role = col.role_for(kind)
        if role not in ds["files"]:
            continue
        batch_rows = [(dataset_id, kind, *(r[c] for c in ROW_COLUMNS))
                      for r in dataset_rows(col, ds, ents, kind, registry)]
        batch_metrics = []
        con.executemany(
            "INSERT INTO result_rows (dataset_id, kind, subject_id, subject_value, subject_label, object_id, "
            "object_label, file_id, row_json, spans) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch_rows)
        n_rows += len(batch_rows)
        for r in con.execute("SELECT row_id, row_json FROM result_rows WHERE dataset_id = ? AND kind = ?",
                             (dataset_id, kind)).fetchall():
            row = json.loads(r["row_json"])
            batch_metrics.extend((r["row_id"], dataset_id, kind, m, idn.num(row[m])) for m in rk["metrics"])
        con.executemany(
            "INSERT INTO result_metrics (row_id, dataset_id, kind, metric, value) VALUES (?, ?, ?, ?, ?)",
            batch_metrics)
    con.executemany(
        "INSERT INTO result_edges (dataset_id, kind, from_id, to_id, from_value, to_value) VALUES (?, ?, ?, ?, ?, ?)",
        edge_rows)
    con.executemany("INSERT INTO dataset_dimensions (dataset_id, dimension, value, label) VALUES (?, ?, ?, ?)",
                    [(dataset_id, n, d["value"], d["label"]) for n, d in ds["dims"].items()])
    con.executemany("INSERT INTO dataset_files (dataset_id, role, file_id) VALUES (?, ?, ?)",
                    [(dataset_id, role, f["doc"]["id"]) for role, f in ds["files"].items()])
    return n_rows, len(edge_rows)


def dataset_ns(con, dataset_id: str) -> dict:
    """The {dataset.*} placeholders: each dimension's canonical value."""
    rows = con.execute("SELECT dimension, value FROM dataset_dimensions WHERE dataset_id = ?",
                       (dataset_id,)).fetchall()
    return {"id": dataset_id, **{r["dimension"]: r["value"] for r in rows}}


def row_inputs(col_minting: dict, kind: str, r) -> dict:
    """render_row keyword arguments for one stored result row."""
    rk = col_minting["row_kinds"][kind]
    return {
        "row": json.loads(r["row_json"]),
        "subject": {"id": r["subject_id"], "label": r["subject_label"]},
        "obj": {"id": r["object_id"], "label": r["object_label"]},
        "metrics": rk["metrics"],
        "spans": {m: tuple(v) for m, v in json.loads(r["spans"]).items()},
        "selector": f"{rk['subject']['column']}={r['subject_value']}",
    }


def group_inputs(con, minting: dict, dataset_id: str, kind: str, group_id: str) -> dict:
    """render_group's `group` for one edge group (all edges ending at group_id)."""
    spec = minting["edge_kinds"][kind]
    r = con.execute("SELECT to_value, COUNT(*) AS n FROM result_edges WHERE dataset_id = ? AND kind = ? "
                    "AND to_id = ? GROUP BY to_value", (dataset_id, kind, group_id)).fetchone()
    entity = minting["entities"][spec["to"]["entity"]]
    parent = ""
    if "library_dimension" in entity:
        library = con.execute("SELECT value FROM dataset_dimensions WHERE dataset_id = ? AND dimension = ?",
                              (dataset_id, entity["library_dimension"])).fetchone()
        parent = engine.gene_set_collection(library["value"])["id"]
    return {"id": group_id, "label": r["to_value"], "count": r["n"], "parent": parent}


def _render_context(con, col: Collection, ctx: engine.MintContext, ds: dict, objects: dict) -> int:
    """Render every claim of one (template, file) pairing over one dataset."""
    dataset_id = ds["doc"]["id"]
    kind = ctx.template["for"]
    dataset = dataset_ns(con, dataset_id)
    claim_rows = []
    if kind in col.doc["row_kinds"]:
        for r in con.execute("SELECT * FROM result_rows WHERE dataset_id = ? AND kind = ? ORDER BY row_id",
                             (dataset_id, kind)).fetchall():
            out = engine.render_row(ctx, dataset=dataset, **row_inputs(ctx.minting, kind, r))
            objects.update(out.objects)
            claim_rows.extend((c["id"], ctx.activity["id"], key, dataset_id, r["row_id"], None, None,
                               out.propositions[key]) for key, c in out.claims.items())
    else:
        side = "to_id" if ctx.template["group_by"] == "to" else "from_id"
        for g in con.execute(f"SELECT DISTINCT {side} AS gid FROM result_edges WHERE dataset_id = ? AND kind = ? "
                             f"ORDER BY {side}", (dataset_id, kind)).fetchall():
            out = engine.render_group(ctx, dataset=dataset,
                                      group=group_inputs(con, ctx.minting, dataset_id, kind, g["gid"]))
            objects.update(out.objects)
            claim_rows.extend((c["id"], ctx.activity["id"], key, dataset_id, None, kind, g["gid"],
                               out.propositions[key]) for key, c in out.claims.items())
    con.executemany(
        f"{con.dialect.insert_or_ignore} result_claims (claim_id, activity_id, claim_key, dataset_id, row_id, "
        "edge_kind, group_id, proposition_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", claim_rows)
    con.execute("INSERT INTO mint_contexts (activity_id, template_file_id, result_file_id, context) VALUES (?, ?, ?, ?)",
                (ctx.activity["id"], ctx.template_file_id, ctx.result_file_id, json.dumps(ctx.as_record())))
    return len(claim_rows)


def _store_objects(con, objects: dict) -> None:
    """Upsert the data plane (re-minting an existing address counts it) and
    index every reference for /v1/objects/search."""
    rows = [(oid, cls, idn.PROFILE, idn.canonical(d)) for oid, (cls, d) in sorted(objects.items())]
    con.executemany(con.dialect.upsert_object, rows)
    refs = sorted({(oid, ref) for oid, (_, d) in objects.items() for ref in dapper_refs(d)})
    con.executemany(con.dialect.insert_ref, refs)
