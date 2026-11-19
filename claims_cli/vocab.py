"""`claims vocab check`: how a collection's values resolve to identifiers.

- Dimension values (from the paths): through the collection's own mapping
  files, or not at all. A collection's vocabulary is written into its own
  files (mappings/<dimension>.tsv and mappings/<dimension>.sssom.tsv) from a
  KPN trait release file of broadinstitute/kpn-data-models: a key the release
  names exactly is filled in with its id, label, category, type, primary
  concept and cross-references; any other key gets the closest release traits
  as suggestions (never applied automatically: a wrong mapping is a wrong
  claim). The service never reads the release: the collection's files are its
  whole vocabulary.
- Entity values (from a few sample files): empty, padded, non-ASCII or
  over-long values, which the index's identifier columns cannot hold, and
  values that do not look like their identifier vocabulary.
"""
from __future__ import annotations

import csv
import difflib
import io
import os
import re
from pathlib import Path

import yaml

ID_LIMIT = 255  # result_rows.subject_id / result_edges ids: ascii VARCHAR(255)
SHAPES = {  # what a value of a known vocabulary looks like
    "HGNC.SYMBOL": re.compile(r"^[A-Za-z0-9][A-Za-z0-9.@_/-]*$"),
    "ENSEMBL": re.compile(r"^ENS[A-Z]*[GTP]\d{11}(\.\d+)?$"),
    "NCBIGene": re.compile(r"^\d+$"),
}


DEFAULT_TRAIT_RELEASE = Path(os.environ.get("CLAIMS_TRAIT_RELEASE") or Path.home() / "src" / "research"
                             / "portal-data-models" / "versions" / "trait" / "v0.0.2" / "kpn_trait_collection.yaml")
ATTRIBUTES = ("category", "trait_type", "concept_id", "concept_label")
MAPPING_COLUMNS = ("key", "id", "label", *ATTRIBUTES, "suggested_id", "suggested_name", "score")
XREF_COLUMNS = ("subject_id", "predicate_id", "object_id", "object_label", "confidence", "mapping_justification",
                "source")


class TraitRelease:
    """A KPN trait release file (kpn-data-models versions/trait/<v>/kpn_trait_collection.yaml): every
    trait's id, the keys it replaces (legacy and PIGEAN keys), name, category, type, primary concept and
    cross-references. What a collection's trait vocabulary is written from."""

    def __init__(self, path: Path):
        self.path = Path(path)
        p = self.path.resolve()
        self.label = (f"kpn-data-models trait release {p.parent.name}" if p.parent.parent.name == "trait"
                      else p.name)  # what generated files say they came from (never a local path)
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        data = yaml.load(self.path.read_bytes(), Loader=loader)
        traits = data["phenotypes"] if isinstance(data, dict) else data
        self.by_key: dict[str, tuple[str, str]] = {}
        self.names: dict[str, tuple[str, str]] = {}
        self.aliases: dict[str, list[str]] = {}
        self.attributes: dict[str, dict[str, str]] = {}
        self.xrefs: dict[str, list[dict]] = {}
        self.malformed: list[tuple[str, str]] = []  # (trait id, target) cross-references whose target is no CURIE
        for t in traits:  # legacy keys first, so a PIGEAN key wins a clash (as the service resolved them)
            self.by_key[t["legacy_id"]] = (t["portal_id"], t["phenotype_name"])
        for t in traits:
            tid, name = t["portal_id"], t["phenotype_name"]
            if t.get("pigean_id"):
                self.by_key[t["pigean_id"]] = (tid, name)
            self.by_key[tid] = (tid, name)
            self.names[tid] = (name, t.get("gwas_source_category") or "")
            self.aliases[tid] = [k for k in (tid, t.get("pigean_id"), t["legacy_id"]) if k]
            primary = t.get("primary_mapping") or {}
            self.attributes[tid] = {k: str(v) for k, v in (
                ("category", t.get("trait_group")), ("trait_type", t.get("trait_type")),
                ("concept_id", primary.get("target_id")), ("concept_label", primary.get("target_label"))) if v}
            self.xrefs[tid] = []
            for m in t.get("mappings") or []:
                target = str(m.get("target_id") or "")
                if ":" not in target or " " in target or "," in target:
                    self.malformed.append((tid, target))
                    continue
                self.xrefs[tid].append({
                    "subject_id": tid, "predicate_id": m["mapping_predicate"], "object_id": target,
                    "object_label": m.get("target_label") or "",
                    "confidence": "" if m.get("confidence") is None else str(m["confidence"]),
                    "mapping_justification": m.get("mapping_justification") or "", "source": m.get("source") or ""})


def load_release(path: Path | None) -> TraitRelease | None:
    path = Path(path) if path else DEFAULT_TRAIT_RELEASE
    return TraitRelease(path) if path.is_file() else None


def words(text: str) -> list[str]:
    """'HypertrophicCardiomyopathy(MTAG)' -> ['hypertrophic', 'cardiomyopathy', 'mtag']."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


class Suggester:
    """Best release matches for a key, by its words against trait names and keys."""

    def __init__(self, release):
        self.entries = []  # (normalized text, trait id, name, category)
        self.by_word: dict[str, set[int]] = {}
        for trait_id, (name, category) in release.names.items():
            for text in {name, *release.aliases.get(trait_id, [])}:
                if not text or text.startswith("KPN.TRAIT:"):
                    continue
                norm = " ".join(words(text))
                if not norm:
                    continue
                i = len(self.entries)
                self.entries.append((norm, trait_id, name, category))
                for w in set(norm.split()):
                    if len(w) > 2:
                        self.by_word.setdefault(w, set()).add(i)

    def suggest(self, key: str, n: int = 1) -> list[tuple[float, str, str]]:
        norm = " ".join(words(key))
        cands = set()
        for w in set(norm.split()):
            cands |= self.by_word.get(w, set())
        scored = {}
        for i in cands:
            text, trait_id, name, category = self.entries[i]
            score = difflib.SequenceMatcher(None, norm, text).ratio()
            shared = set(norm.split()) & set(text.split())
            score = 0.6 * score + 0.4 * len(shared) / max(len(set(norm.split()) | set(text.split())), 1)
            score += 0.03 if category == "KPN" else 0.0
            if score > scored.get(trait_id, (0,))[0]:
                scored[trait_id] = (score, name)
        best = sorted(((round(min(s, 1.0), 2), t, nm) for t, (s, nm) in scored.items()), key=lambda x: (-x[0], x[1]))
        return best[:n]


def value_issues(values: set[str], id_template: str | None) -> dict[str, list[str]]:
    """Values an identifier column cannot hold, or that do not look like their vocabulary."""
    prefix = id_template.split(":", 1)[0] if id_template and "{value}" in id_template else None
    shape = SHAPES.get(prefix or "")
    out: dict[str, list[str]] = {}
    for v in sorted(values):
        ident = id_template.replace("{value}", v) if id_template else v
        if v == "":
            out.setdefault("empty", []).append(v)
        elif v != v.strip():
            out.setdefault("leading or trailing spaces", []).append(v)
        elif not ident.isascii():
            out.setdefault("not ASCII", []).append(v)
        elif len(ident) > ID_LIMIT:
            out.setdefault(f"longer than {ID_LIMIT} characters as an id", []).append(v)
        elif shape and not shape.match(v):
            out.setdefault(f"does not look like {prefix}", []).append(v)
    return out


def _read_tsv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t"))


def _write_tsv(path: Path, header: list[str], columns: tuple, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    out.write("".join(f"# {h}\n" for h in header))
    w = csv.DictWriter(out, fieldnames=list(columns), delimiter="\t", lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: (r.get(c) or "") for c in columns})
    path.write_text(out.getvalue(), encoding="utf-8")


def write_vocabulary(mapping_path: Path, xrefs_path: Path | None, keys: list[str], release: TraitRelease | None,
                     suggester: Suggester | None = None, fill_at: float | None = None) -> dict:
    """Write a dimension's vocabulary files for `keys`. The mapping file gets one row per key: a key the
    release names exactly gets its id, label and attributes; any other key an empty id and the best
    suggestion (in the id column too when it scores at least `fill_at`, for review). Rows already in the
    file keep their id and label; their empty attribute cells are filled from the release. The
    cross-reference file is rewritten for every mapped id the release knows (rows for other ids are kept)."""
    rows = {r["key"]: r for r in _read_tsv(mapping_path) if r.get("key")}
    counts = {"kept": len(rows), "exact": 0, "suggested": 0, "blank": 0}
    for k in keys:
        if k in rows:
            continue
        if release and k in release.by_key:
            tid, name = release.by_key[k]
            rows[k] = {"key": k, "id": tid, "label": name}
            counts["exact"] += 1
            continue
        found = suggester.suggest(k) if suggester else []
        s = found[0] if found else None
        prefill = s[1] if s and fill_at is not None and s[0] >= fill_at else ""
        rows[k] = {"key": k, "id": prefill, "label": "", "suggested_id": s[1] if s else "",
                   "suggested_name": s[2] if s else "", "score": str(s[0]) if s else ""}
        counts["suggested" if prefill else "blank"] += 1
    for r in rows.values():
        ident = (r.get("id") or "").strip()
        if release and ident in release.attributes:
            if not (r.get("label") or "").strip():
                r["label"] = release.names[ident][0]
            for a, v in release.attributes[ident].items():
                if not (r.get(a) or "").strip():
                    r[a] = v
    source = f" from {release.label}" if release else ""
    _write_tsv(mapping_path, [
        "key -> id (a CURIE) and label, and what the id means (category, trait_type, primary concept).",
        f"Written by `claims vocab check --write-template`{source}. Rows with an empty id stay unmapped;",
        "suggested_* columns are hints: copy one into id only if it is right. This file is the collection's",
        "own vocabulary for the dimension: the service reads nothing else."], MAPPING_COLUMNS,
        sorted(rows.values(), key=lambda r: r["key"]))
    counts["mapped"] = sum(1 for r in rows.values() if (r.get("id") or "").strip())
    counts["xrefs"] = 0
    if xrefs_path is not None:
        ids = sorted({(r.get("id") or "").strip() for r in rows.values()} - {""})
        known = set(release.xrefs) if release else set()
        kept = [r for r in _read_tsv(xrefs_path) if r.get("subject_id") in ids and r["subject_id"] not in known]
        fresh = [x for i in ids if i in known for x in release.xrefs[i]]
        counts["malformed"] = [(t, x) for t, x in release.malformed if t in ids] if release else []
        _write_tsv(xrefs_path, [
            "Cross-references of the mapped ids to other vocabularies (SSSOM columns).",
            f"Written by `claims vocab check --write-template`{source}; rows for ids the release does not know are kept."],
            XREF_COLUMNS, kept + fresh)
        counts["xrefs"] = len(kept) + len(fresh)
    return counts
