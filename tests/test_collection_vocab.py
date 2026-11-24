"""A collection's own mappings (mappings/<dimension>.tsv) and its policy for
values that resolve nowhere (`unmapped`), in publishing and in queries; and
the vocab check's suggestions and value checks."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from claims_cli import vocab
from service import db as service_db
from service.collections import serve as cserve
from service.collections.manifest import ManifestError, load_collection
from service.collections.publish import PublishError, publish
from service.collections.traits import MappingError, read_mapping

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
ROLES = {"gene_stats": "combined_gene_stats.json", "gene_set_stats": "combined_gene_set_stats.json",
         "membership": "gene_gene_set_stats.json"}


def _collection(tmp: Path, mappings: str | None, unmapped: str | None) -> Path:
    col = tmp / "pigean"
    shutil.copytree(FIX / "collections" / "pigean", col)
    doc = (col / "collection.yaml").read_text()
    extra = ""
    if mappings is not None:
        (col / "mappings").mkdir(exist_ok=True)  # replaces the fixture's own mapping
        (col / "mappings" / "trait.tsv").write_text(mappings)
        extra += "\nmappings:\n  trait: mappings/trait.tsv\n"
    if unmapped:
        extra += f"\nunmapped:\n  trait: {unmapped}\n"
    (col / "collection.yaml").write_text(doc + extra)
    return col


def _files(tmp: Path, phenotype: str) -> Path:
    out = tmp / "files" / phenotype
    out.mkdir(parents=True)
    for name in ROLES.values():
        rows = [json.loads(l) for l in (FIX / "pigean" / name).read_text().splitlines() if l.strip()]
        for r in rows:
            r["phenotype"] = phenotype
        (out / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def _spec(tmp: Path, phenotypes) -> dict:
    folders = {p: _files(tmp, p) for p in phenotypes}
    return {"version": "0.5.0-vocab", "datasets": [
        {"dimensions": {"trait": p, "library": "fixture-v1"},
         "files": {role: {"path": str(folders[p] / name), "location": f"s3://b/{p}/{name}"}
                   for role, name in ROLES.items()}} for p in phenotypes]}


def _publish(tmp: Path, col: Path, spec: dict):
    path = str(tmp / "c.db")
    saved = service_db.DB_PATH
    service_db.DB_PATH = path
    con = service_db.connect()
    try:
        return publish(con, load_collection(col), spec, tmp), path
    finally:
        con.close()
        service_db.DB_PATH = saved


MAPPING = "key\tid\tlabel\nFIXPHENO\tKPN.TRAIT:9999999\tFixture phenotype\nOTHERPHENO\t\t\n"


def test_a_mapping_sets_the_dataset_value_and_queries_accept_the_key(tmp_path):
    col = _collection(tmp_path, MAPPING, None)
    result, path = _publish(tmp_path, col, _spec(tmp_path, ["FIXPHENO", "OTHERPHENO"]))
    dims = {d["name"]: [v["dimension_value"] for v in d["dimension_values"] if v["dimension"] == "trait"][0]
            for d in result.document["datasets"]}
    assert sorted(dims.values()) == ["KPN.TRAIT:9999999", "OTHERPHENO"]  # the blank id leaves OTHERPHENO as given
    assert any(f.get("mime_type") == "text/tab-separated-values" for f in result.document["files"])
    con = service_db.connect(path)
    try:
        record = json.loads(con.execute("SELECT manifest FROM collection_releases").fetchone()["manifest"])
        assert record["mappings"] == {"trait": {"FIXPHENO": ["KPN.TRAIT:9999999", "Fixture phenotype"]}}
        out = cserve.run_query(con, "pigean", "top-genes-with-sets", {"trait": "FIXPHENO", "library": "fixture-v1"},
                               claims=[])
    finally:
        con.close()
    assert out["rows"] and all(r["object"] == {"id": "KPN.TRAIT:9999999", "label": "Fixture phenotype"}
                               for r in out["rows"])


def test_the_collections_vocabulary_is_what_dimension_values_serve(tmp_path):
    """Attributes and cross-references come from the collection's own files; the service's
    trait registry (kpn_traits) is never read, so its contents change nothing."""
    from service.collections import catalog

    mapping = ("key\tid\tlabel\tcategory\tconcept_id\nFIXPHENO\tKPN.TRAIT:9999999\tFixture phenotype\tdigestive\t"
               "EFO:9999991\n")
    col = _collection(tmp_path, mapping, None)
    (col / "mappings" / "trait.sssom.tsv").write_text(
        "subject_id\tpredicate_id\tobject_id\tobject_label\tconfidence\n"
        "KPN.TRAIT:9999999\tskos:exactMatch\tEFO:9999991\tfixture disease\t0.9\n"
        "KPN.TRAIT:9999999\tskos:exactMatch\tMONDO:9999992\t\t\n")
    (col / "collection.yaml").write_text((col / "collection.yaml").read_text() + "\nxrefs:\n  trait: mappings/trait.sssom.tsv\n")
    path = str(tmp_path / "c.db")
    con = service_db.connect(path)
    try:
        with con:  # a registry row that would map FIXPHENO elsewhere: ignored
            con.execute("INSERT INTO kpn_traits (trait_id, legacy_id, name) VALUES ('KPN.TRAIT:0000001', 'FIXPHENO', 'Wrong')")
    finally:
        con.close()
    _, path = _publish(tmp_path, col, _spec(tmp_path, ["FIXPHENO"]))
    con = service_db.connect(path)
    try:
        values = catalog.dimension_values(con, "pigean", "trait")["values"]
        by_xref = catalog.dimension_values(con, "pigean", "trait", search="mondo:9999992")["values"]
    finally:
        con.close()
    [v] = values
    assert v["value"] == "KPN.TRAIT:9999999" and v["label"] == "Fixture phenotype" and v["also_accepts"] == ["FIXPHENO"]
    assert v["category"] == "digestive" and v["concept_id"] == "EFO:9999991"
    assert [(x["target_id"], x["confidence"]) for x in v["xrefs"]] == [("EFO:9999991", 0.9), ("MONDO:9999992", None)]
    assert [x["value"] for x in by_xref] == ["KPN.TRAIT:9999999"]
    # Cross-references must describe mapped ids.
    (col / "mappings" / "trait.sssom.tsv").write_text("subject_id\tpredicate_id\tobject_id\nKPN.TRAIT:1\tskos:exactMatch\tEFO:1\n")
    with pytest.raises(MappingError, match="not ids of mappings.trait"):
        from service.collections.traits import collection_vocabulary
        collection_vocabulary(load_collection(col).doc, col)


def test_unmapped_refuse_and_skip(tmp_path):
    refuse = _collection(tmp_path / "a", MAPPING, "refuse")
    with pytest.raises(PublishError, match="OTHERPHENO"):
        _publish(tmp_path / "a", refuse, _spec(tmp_path / "a", ["FIXPHENO", "OTHERPHENO"]))
    skip = _collection(tmp_path / "b", MAPPING, "skip")
    result, _ = _publish(tmp_path / "b", skip, _spec(tmp_path / "b", ["FIXPHENO", "OTHERPHENO"]))
    assert len(result.document["datasets"]) == 1
    assert any("left out" in w and "OTHERPHENO" in w for w in result.warnings)


def test_mapping_files_are_checked(tmp_path):
    p = tmp_path / "m.tsv"
    p.write_text("key\tid\nA\tnot a curie\n")
    with pytest.raises(MappingError, match="not a CURIE"):
        read_mapping(p)
    p.write_text("key\tid\nA\tX:1\nA\tX:2\n")
    with pytest.raises(MappingError, match="mapped twice"):
        read_mapping(p)
    p.write_text("name\tvalue\n")
    with pytest.raises(MappingError, match="header"):
        read_mapping(p)
    col = _collection(tmp_path / "c", None, None)
    (col / "collection.yaml").write_text((col / "collection.yaml").read_text() + "\nmappings:\n  nosuch: x.tsv\n")
    with pytest.raises(ManifestError, match="mappings.nosuch"):
        load_collection(col)


def test_labels_are_kept_exactly_as_written(tmp_path):
    # A dataset's label is part of its id: trimming an upstream name with stray spaces re-addresses its dataset.
    p = tmp_path / "m.tsv"
    p.write_text("key\tid\tlabel\nA\tX:1\t Spaced name \nB\tX:2\t  \n")
    assert read_mapping(p) == {"A": ("X:1", " Spaced name "), "B": ("X:2", "B")}


def test_suggestions_and_value_checks():
    class Registry:
        names = {"KPN.TRAIT:1": ("migraine disorder", "gcat_trait"), "KPN.TRAIT:2": ("Autism", "KPN"),
                 "KPN.TRAIT:3": ("Hypertrophic cardiomyopathy", "KPN")}
        aliases = {"KPN.TRAIT:2": ["KPN.TRAIT:2", "Autism"], "KPN.TRAIT:3": ["KPN.TRAIT:3", "Cardiomyopathy"]}

    s = vocab.Suggester(Registry())
    assert s.suggest("Autism")[0][:2] == (1.0, "KPN.TRAIT:2")
    assert s.suggest("HypertrophicCardiomyopathy(MTAG)")[0][1] == "KPN.TRAIT:3"
    assert s.suggest("Migraine")[0][1] == "KPN.TRAIT:1"
    assert s.suggest("ZZZ") == []
    issues = vocab.value_issues({"BRCA1", " TP53", "bad symbol", "x" * 300, "ÄBC"}, "HGNC.SYMBOL:{value}")
    assert issues["leading or trailing spaces"] == [" TP53"]
    assert issues["does not look like HGNC.SYMBOL"] == ["bad symbol"]
    assert issues["longer than 255 characters as an id"] == ["x" * 300]
    assert issues["not ASCII"] == ["ÄBC"]
