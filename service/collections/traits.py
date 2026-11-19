"""A collection's own dimension vocabularies: how its keys become ids, and
what each id means. Nothing here reads the service's database: a collection
is resolved from its own files alone.

- `mappings/<dimension>.tsv` (the manifest's `mappings` block): one row per
  key, with columns key, id (a CURIE) and label, and optionally more columns
  describing the id (for traits: category, trait_type, concept_id,
  concept_label). Rows without an id leave the key unmapped.
- `mappings/<dimension>.sssom.tsv` (the manifest's `xrefs` block): the ids'
  cross-references to other vocabularies, SSSOM style: subject_id,
  predicate_id, object_id, and optionally object_label, confidence,
  mapping_justification, source.

A key that no mapping resolves is handled by the manifest's `unmapped` policy
for that dimension: `keep-raw` (the default: kept as given), `refuse`
(publishing fails, listing them), or `skip` (the dataset is left out).

Mappings and cross-references are not part of the manifest's minting
section: a mapping changes the dimension values of the datasets it maps, and
so the ids of those datasets' claims, and nothing else; cross-references
change no claim.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

UNMAPPED_POLICIES = ("keep-raw", "refuse", "skip")
XREF_REQUIRED = ("subject_id", "predicate_id", "object_id")


class MappingError(ValueError):
    pass


class Vocabulary:
    """A collection's mappings ({dimension: {key: (id, label)}}) and what each
    mapped id means ({dimension: {id: {label, attributes..., xrefs}}})."""

    def __init__(self, mappings: dict | None = None, entries: dict | None = None):
        self.mappings: dict[str, dict[str, tuple[str, str]]] = mappings or {}
        self.entries: dict[str, dict[str, dict]] = entries or {}
        self.unresolved: set[str] = set()

    def how(self, dimension: str, key: str) -> str:
        """'mapping' or 'unresolved' for one value of a dimension."""
        return "mapping" if key in self.mappings.get(dimension, {}) else "unresolved"


def resolve_dimension(vocab: Vocabulary | None, spec: dict, raw: str) -> tuple[str, str]:
    """(value, label) of a raw dimension value: its mapping, else as given."""
    if vocab is not None:
        mapped = vocab.mappings.get(spec["name"], {}).get(raw)
        if mapped is not None:
            return mapped
        if spec["name"] in vocab.mappings:
            vocab.unresolved.add(raw)  # the collection maps this dimension, but not this key
    return raw, raw


def is_resolved(vocab: Vocabulary | None, spec: dict, raw: str) -> bool:
    """Whether a value resolves: through the collection's mapping, or because
    the collection maps nothing for its dimension (values are taken as given)."""
    if vocab is None or spec["name"] not in vocab.mappings:
        return True
    return raw in vocab.mappings[spec["name"]]


def _rows(path: Path) -> tuple[list[str], list[dict]]:
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t")
    return list(reader.fieldnames or []), list(reader)


def _curie(value: str) -> bool:
    return ":" in value and " " not in value


def _attribute_columns(fields: list[str]) -> list[str]:
    return [f for f in fields if f not in ("key", "id", "label", "score") and not f.startswith("suggested_")]


def read_mapping(path: Path) -> dict[str, tuple[str, str]]:
    """{key: (id, label)} from a TSV with a header row: key, id, and optionally
    label (more columns describe the id; suggested_* columns and score are
    hints and are ignored). Rows without an id are left unmapped. An id must
    be a CURIE; a missing label is the key."""
    fields, rows = _rows(path)
    if "key" not in fields or "id" not in fields:
        raise MappingError(f"{path}: the header must name at least the columns key and id")
    out: dict[str, tuple[str, str]] = {}
    for n, r in enumerate(rows, 2):
        key, ident = (r.get("key") or "").strip(), (r.get("id") or "").strip()
        if not key or not ident:
            continue
        if not _curie(ident):
            raise MappingError(f"{path} line {n}: id {ident!r} for {key!r} is not a CURIE")
        if key in out and out[key][0] != ident:
            raise MappingError(f"{path} line {n}: {key!r} is mapped twice, to {out[key][0]} and {ident}")
        out[key] = (ident, (r.get("label") or "").strip() or key)
    return out


def read_entries(path: Path) -> dict[str, dict]:
    """{id: {label, attribute: value}} from a mapping file's id, label and
    attribute columns (the first row naming an id wins)."""
    fields, rows = _rows(path)
    attrs = _attribute_columns(fields)
    out: dict[str, dict] = {}
    for r in rows:
        ident = (r.get("id") or "").strip()
        if ident and ident not in out:
            entry = {"label": (r.get("label") or "").strip() or None}
            entry.update({a: (r.get(a) or "").strip() for a in attrs if (r.get(a) or "").strip()})
            out[ident] = entry
    return out


def read_xrefs(path: Path) -> dict[str, list[dict]]:
    """{subject id: [{predicate, target_id, label, confidence, justification,
    source}]} from an SSSOM-style TSV, in file order."""
    fields, rows = _rows(path)
    missing = [c for c in XREF_REQUIRED if c not in fields]
    if missing:
        raise MappingError(f"{path}: the header must name the columns {list(XREF_REQUIRED)}; missing {missing}")
    out: dict[str, list[dict]] = {}
    for n, r in enumerate(rows, 2):
        subject, predicate, target = ((r.get(c) or "").strip() for c in XREF_REQUIRED)
        if not (subject and predicate and target):
            raise MappingError(f"{path} line {n}: subject_id, predicate_id and object_id are all required")
        for c, v in (("subject_id", subject), ("predicate_id", predicate), ("object_id", target)):
            if not _curie(v):
                raise MappingError(f"{path} line {n}: {c} {v!r} is not a CURIE")
        conf = (r.get("confidence") or "").strip()
        try:
            confidence = float(conf) if conf else None
        except ValueError:
            raise MappingError(f"{path} line {n}: confidence {conf!r} is not a number") from None
        out.setdefault(subject, []).append({
            "predicate": predicate, "target_id": target, "label": (r.get("object_label") or "").strip() or None,
            "confidence": confidence, "justification": (r.get("mapping_justification") or "").strip() or None,
            "source": (r.get("source") or "").strip() or None,
        })
    return out


def _file(doc: dict, root: Path, block: str, dim: str, rel: str) -> Path:
    dims = {d["name"] for d in doc.get("dataset_dimensions") or []} | {
        f for k in (doc.get("object_kinds") or {}).values() for f in k["fields"]}
    if dim not in dims:
        raise MappingError(f"{block}.{dim}: not a dataset dimension or object field ({sorted(dims)})")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise MappingError(f"{block}.{dim}: {rel} is outside the collection directory")
    if not path.is_file():
        raise MappingError(f"{block}.{dim}: {rel} does not exist")
    return path


def vocabulary_files(doc: dict) -> list[tuple[str, str, str]]:
    """(block, dimension, relative path) for every vocabulary file the manifest names."""
    return [(block, dim, rel) for block in ("mappings", "xrefs") for dim, rel in sorted((doc.get(block) or {}).items())]


def collection_vocabulary(doc: dict, root: Path) -> Vocabulary:
    """Every mapping and cross-reference file the manifest names."""
    mappings, entries = {}, {}
    for dim, rel in (doc.get("mappings") or {}).items():
        path = _file(doc, root, "mappings", dim, rel)
        mappings[dim] = read_mapping(path)
        entries[dim] = read_entries(path)
    for dim, rel in (doc.get("xrefs") or {}).items():
        if dim not in mappings:
            raise MappingError(f"xrefs.{dim}: give the dimension's mapping file too (mappings.{dim})")
        xrefs = read_xrefs(_file(doc, root, "xrefs", dim, rel))
        unknown = sorted(set(xrefs) - set(entries[dim]))
        if unknown:
            raise MappingError(f"xrefs.{dim}: {len(unknown)} subject id(s) are not ids of mappings.{dim}, "
                               f"e.g. {unknown[:3]}")
        for ident, refs in xrefs.items():
            entries[dim][ident]["xrefs"] = refs
    return Vocabulary(mappings, entries)


def unmapped_policy(doc: dict, dimension: str) -> str:
    return (doc.get("unmapped") or {}).get(dimension, "keep-raw")
