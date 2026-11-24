"""Scaffolding a collection: `claims collection init` (a draft manifest,
source.yaml, templates, a query and a GUIDE.md from sample files) and
`claims templates generate` (default claim templates from a manifest).

Everything written here is a draft for a person to finish. Values the files
cannot tell us are marked TODO, and `claims collection validate` lists every
TODO left.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from pathlib import Path

import yaml

TODO = "TODO"
# Column names whose values are known identifier vocabularies.
KNOWN_IDS = {
    "gene": "HGNC.SYMBOL", "gene_symbol": "HGNC.SYMBOL", "symbol": "HGNC.SYMBOL", "hgnc_symbol": "HGNC.SYMBOL",
    "ensembl_gene_id": "ENSEMBL", "ensembl_id": "ENSEMBL", "entrez_id": "NCBIGene", "ncbi_gene_id": "NCBIGene",
}
KNOWN_PREFIXES = {
    "HGNC.SYMBOL": "https://identifiers.org/hgnc.symbol:",
    "ENSEMBL": "https://identifiers.org/ensembl:",
    "NCBIGene": "https://identifiers.org/ncbigene:",
    "KPN.TRAIT": "https://broadinstitute.github.io/kpn-data-models/kpn.trait/",
    "biolink": "https://w3id.org/biolink/vocab/",
}
_PY_NONE = re.compile(r'(?<=": )None(?=[,}])')


# --- reading samples -----------------------------------------------------------

def file_format(name: str) -> str:
    low = name.lower()
    if low.endswith((".json", ".jsonl", ".ndjson")):
        return "ndjson"
    if low.endswith((".tsv", ".txt", ".out")):
        return "tsv"
    if low.endswith(".csv"):
        return "csv"
    return "ndjson"


def read_sample(data: bytes, fmt: str, truncated: bool) -> tuple[list[dict], dict]:
    """Rows of a sample (the last line dropped when the read was truncated),
    and notes: repaired Python None lines, unparseable lines."""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[:-1]
    notes = {"python_none": 0, "bad_lines": 0}
    rows = []
    if fmt == "ndjson":
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                try:
                    rows.append(json.loads(_PY_NONE.sub("null", line)))
                    notes["python_none"] += 1
                except json.JSONDecodeError:
                    notes["bad_lines"] += 1
    else:
        reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t" if fmt == "tsv" else ",")
        for r in reader:
            rows.append({k: _number(v) for k, v in r.items()})
    return rows, notes


def _number(v):
    if v in (None, "", "NA", "NaN", "nan"):
        return None
    try:
        return int(v) if re.fullmatch(r"-?\d+", v) else float(v)
    except (TypeError, ValueError):
        return v


def profile(rows: list[dict]) -> dict[str, dict]:
    """Per column: how often present, type, distinct values, uniqueness."""
    cols: dict[str, dict] = {}
    n = len(rows)
    for r in rows:
        for k, v in r.items():
            c = cols.setdefault(k, {"present": 0, "types": Counter(), "values": set()})
            if v is not None:
                c["present"] += 1
                c["types"]["number" if isinstance(v, (int, float)) and not isinstance(v, bool) else "string"] += 1
                if len(c["values"]) <= n:
                    c["values"].add(v if not isinstance(v, (dict, list)) else json.dumps(v))
    out = {}
    for k, c in cols.items():
        kind = c["types"].most_common(1)[0][0] if c["types"] else "string"
        out[k] = {"type": kind, "present": c["present"], "rows": n, "distinct": len(c["values"]),
                  "unique": len(c["values"]) == n == c["present"],
                  "constant": next(iter(c["values"])) if len(c["values"]) == 1 else None,
                  "example": next(iter(c["values"]), None)}
    return out


# --- naming ----------------------------------------------------------------------

def entity_name(column: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", column) if part) or "Entity"


def entity_words(entity: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", entity).lower()


def kind_name(*parts: str) -> str:
    return "_".join(re.sub(r"[^a-z0-9]+", "_", p.lower()).strip("_") for p in parts if p)


# --- templates ---------------------------------------------------------------------

def generate_templates(doc: dict) -> dict[str, dict]:
    """{file name: template} for a manifest: a measurement claim per row kind,
    and a roster claim per edge kind that defines a minted entity's members."""
    method = (doc.get("methods") or ["The method"])[0]
    entities = doc.get("entities") or {}
    out = {}
    for kind, rk in (doc.get("row_kinds") or {}).items():
        subj = rk["subject"]["entity"]
        words = entity_words(subj)
        column = rk["subject"].get("column", "id")
        metrics = list(rk["metrics"])
        figures = "; ".join(f"{m} {{metric.{m}.text}}" for m in metrics)
        out[f"{kind}.yaml"] = {
            "template": kind.replace("_", "-"),
            "version": "1.0.0",
            "for": kind,
            "claims": [{
                "key": "measurement",
                "proposition": {
                    "statement": f"The {words} {{subject.label}} is associated with {{object.label}}.",
                    "subject": "{subject.id}",
                    "relation": "associated_with",
                    "object": "{object.id}",
                    "scope": f"{method} {words}-level assessment.",
                    "kind": "RESULT",
                },
                "name": f"{method} measurement: {{subject.label}} - {{object.label}}",
                "statement": f"{method} assessed the association of {words} {{subject.label}} with {{object.label}} "
                             f"({figures}).",
                "scores": metrics,
                "evidence": {
                    "per_metric": metrics,
                    "name": f"{method} {{metric.name}} evidence: {{subject.label}} - {{object.label}}",
                    "explanation": f"{method} reported {{metric.name}} = {{metric.text}} for this association in "
                                   "{file.name}; the value locator pins the serialized number's byte span, so the "
                                   "measurement is re-verifiable against the file's sha256.",
                    "evidence_source": f"{method} result file {{file.name}}",
                },
                "source_locator": f"{{file.name}}#{column}={{subject.label}}",
            }],
        }
    for kind, ek in (doc.get("edge_kinds") or {}).items():
        to = ek["to"]["entity"]
        spec = entities.get(to) or {}
        if "mint" not in spec or spec.get("members_from") != kind:
            continue
        words = entity_words(to)
        library = spec.get("library_dimension")
        where = f"the {{dataset.{library}}} library" if library else "this collection"
        out[f"{kind}-roster.yaml"] = {
            "template": f"{kind.replace('_', '-')}-roster",
            "version": "1.0.0",
            "for": kind,
            "group_by": "to",
            "claims": [{
                "key": "roster",
                "proposition": {
                    "statement": f"In {where}, the {words} {{group.label}} comprises {{group.count}} members; the "
                                 f"exact roster is recorded in its content-addressed {to} entity.",
                    "subject": "{group.id}",
                    "relation": "has_members",
                    "object": "{group.parent}",
                    "scope": f"{method} {words} membership (observed roster).",
                    "kind": "RESULT",
                },
                "name": f"{method} roster: {{group.label}}",
                "statement": f"{where[0].upper() + where[1:]} used by {method} defines the membership of "
                             f"{{group.label}}: {{group.count}} members observed in {{file.name}}.",
                "source_locator": f"{{file.name}}#{ek['to']['column']}={{group.label}}",
            }],
        }
    return out


def default_query(doc: dict) -> tuple[str, dict] | None:
    """A top-N query over the first row kind, joined through an edge kind to
    another row kind when one connects them."""
    row_kinds = doc.get("row_kinds") or {}
    if not row_kinds:
        return None
    dims = [d["name"] for d in doc.get("dataset_dimensions") or []]
    anchor_kind, anchor = next(iter(row_kinds.items()))
    join = None
    for ek_name, ek in (doc.get("edge_kinds") or {}).items():
        if ek["from"]["entity"] == anchor["subject"]["entity"]:
            target = next((k for k, rk in row_kinds.items() if rk["subject"]["entity"] == ek["to"]["entity"]), None)
            if target:
                join = (ek_name, target)
                break
    metric = anchor["metrics"][0]
    params = {d: {"type": "string", "required": True, "description": f"Dataset {d}."} for d in dims}
    params["top"] = {"type": "integer", "default": 20, "min": 0, "max": 200}
    plan = [{"anchor": {"kind": anchor_kind, "dimensions": {d: f"${d}" for d in dims},
                        "order": {"metric": metric, "dir": "desc"}, "limit": "$top"}}]
    name = f"top-{anchor_kind.replace('_', '-')}"
    if join:
        params["perRow"] = {"type": "integer", "default": 5, "min": 0, "max": 50}
        target_metric = row_kinds[join[1]]["metrics"][0]
        plan.append({"join": {"as": "joined", "via": join[0], "kind": join[1],
                              "order": {"metric": target_metric, "dir": "desc"}, "limit_per_anchor": "$perRow"}})
        name += f"-with-{join[1].replace('_', '-')}"
    return name, {
        "query": name,
        "description": f"Top {anchor_kind.replace('_', ' ')} rows by {metric}"
                       + (f", each with its top {join[1].replace('_', ' ')} rows through {join[0]}." if join else "."),
        "params": params,
        "plan": plan,
        "example": {**{d: TODO for d in dims}, "top": 10, **({"perRow": 3} if join else {})},
    }


def set_templates_list(manifest_path: Path, names: list[str]) -> None:
    """Replace the manifest's `templates:` block (keeping everything else)."""
    text = manifest_path.read_text()
    block = "templates:\n" + "".join(f"  - templates/{n}\n" for n in names)
    m = re.search(r"(?m)^templates:\n(?:[ \t]+.*\n|\n)*", text)
    text = text[:m.start()] + block + text[m.end():] if m else text.rstrip("\n") + "\n\n" + block
    manifest_path.write_text(text)


def todos(collection_dir: Path) -> list[str]:
    """Every TODO left in a collection's own files, as 'file:line: text'."""
    out = []
    for p in sorted(collection_dir.rglob("*")):
        if p.is_file() and p.suffix in (".yaml", ".yml") and "releases" not in p.parts:
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if TODO in line and not line.lstrip().startswith("#"):
                    out.append(f"{p.relative_to(collection_dir)}:{i}: {line.strip()[:110]}")
    return out


# --- writing YAML with comments ------------------------------------------------------

def dump(doc, comments: dict[str, str] | None = None) -> str:
    """YAML with a comment line above chosen top-level keys."""
    text = yaml.safe_dump(doc, sort_keys=False, width=110, allow_unicode=True)
    for key, comment in (comments or {}).items():
        text = re.sub(rf"(?m)^{re.escape(key)}:", "\n" + "\n".join(f"# {c}" for c in comment.splitlines())
                      + f"\n{key}:", text, count=1)
    return text
