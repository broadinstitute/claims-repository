"""The claims CLI's pure parts: path patterns, row bounds, and the deployment guard."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claims_cli import sources as src
from claims_cli.main import _parse_where

ROOT = Path(__file__).resolve().parent.parent


def test_path_pattern_matches_repeated_placeholders():
    rx = src.pattern_regex("{kind}/{library}/portal/{trait}/{kind}.json")
    m = rx.match("combined_gene_stats/mouse_msigdb/portal/IBD/combined_gene_stats.json")
    assert m and m.groupdict() == {"kind": "combined_gene_stats", "library": "mouse_msigdb", "trait": "IBD"}
    assert not rx.match("combined_gene_stats/mouse_msigdb/portal/IBD/gene_gene_set_stats.json")  # {kind} must repeat
    assert not rx.match("combined_gene_stats/mouse_msigdb/portal/IBD/_SUCCESS")
    assert src.placeholders("{kind}/{library}/portal/{trait}/{kind}.json") == ["kind", "library", "trait"]
    m = rx.match("gene_gene_set_stats/mouse_msigdb/portal/HypertrophicCardiomyopathy(MTAG)/gene_gene_set_stats.json")
    assert m and m.group("trait") == "HypertrophicCardiomyopathy(MTAG)"


def test_where_parses_strict_lower_bounds_only():
    assert _parse_where(["gene_phenotype: combined > 0.5", "gene_set_phenotype: beta_uncorrected > 0.01"], None) == {
        "gene_phenotype": {"combined": {"gt": 0.5}}, "gene_set_phenotype": {"beta_uncorrected": {"gt": 0.01}}}
    assert _parse_where(None, {"x": 1}) == {"x": 1}
    with pytest.raises(SystemExit):
        _parse_where(["gene_phenotype: combined < 0.5"], None)


def test_cli_refuses_a_shared_mysql_target_and_an_unnamed_deployment(tmp_path):
    base = [sys.executable, "-m", "claims_cli.main"]
    env = {**os.environ, "CLAIMS_MYSQL_HOST": "example.invalid"}
    out = subprocess.run(base + ["--db", str(tmp_path / "c.db"), "--index-dir", str(tmp_path / "i"), "release", "status",
                                 str(tmp_path), "1"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "CLAIMS_MYSQL_HOST" in out.stderr
    env = {k: v for k, v in os.environ.items() if k not in ("CLAIMS_MYSQL_HOST", "CLAIMS_DB", "CLAIMS_INDEX_DIR")}
    out = subprocess.run(base + ["release", "status", str(tmp_path), "1"], cwd=ROOT, env=env, capture_output=True,
                         text=True)
    assert out.returncode != 0 and "--db" in out.stderr
    out = subprocess.run(base + ["--db", str(ROOT / "claims.db"), "--index-dir", str(tmp_path / "i"), "release",
                                 "status", str(tmp_path), "1"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "working database" in out.stderr


def _layout(tmp_path: Path) -> Path:
    """The PIGEAN fixture files laid out like the aggregator's output: {kind}/{library}/portal/{trait}/{kind}.json."""
    import json

    src_dir = tmp_path / "out"
    for name in ("combined_gene_stats", "combined_gene_set_stats", "gene_gene_set_stats"):
        rows = [json.loads(l) for l in (ROOT / "tests" / "fixtures" / "pigean" / f"{name}.json").read_text().splitlines() if l.strip()]
        folder = src_dir / name / "fixture-v1" / "portal" / "FIXPHENO"
        folder.mkdir(parents=True)
        (folder / f"{name}.json").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return src_dir


def test_collection_init_drafts_a_valid_collection_from_samples(tmp_path):
    import yaml

    from service.collections.manifest import load_collection

    src_dir = _layout(tmp_path)
    out = tmp_path / "draft"
    traits = tmp_path / "traits.yaml"  # a KPN trait release file naming the fixture's trait key
    traits.write_text(yaml.safe_dump({"phenotypes": [{
        "portal_id": "KPN.TRAIT:9999999", "legacy_id": "FIXPHENO", "phenotype_name": "Fixture phenotype",
        "trait_group": "digestive", "trait_type": "disease",
        "primary_mapping": {"target_id": "EFO:9999991", "target_label": "fixture disease"},
        "mappings": [{"target_id": "EFO:9999991", "target_ontology": "EFO", "mapping_predicate": "skos:exactMatch",
                      "target_label": "fixture disease", "confidence": 0.9, "mapping_justification": "curated"}]}]}))
    env = {k: v for k, v in os.environ.items() if k not in ("CLAIMS_MYSQL_HOST",)}
    run = subprocess.run([sys.executable, "-m", "claims_cli.main", "collection", "init", str(out), "--handle", "fixture",
                          "--traits", str(traits),
                          "--method", "PIGEAN", "--source", str(src_dir) + "/",
                          "--sample", "gene_stats=combined_gene_stats/fixture-v1/portal/FIXPHENO/combined_gene_stats.json",
                          "--sample", "gene_set_stats=combined_gene_set_stats/fixture-v1/portal/FIXPHENO/combined_gene_set_stats.json",
                          "--sample", "membership=gene_gene_set_stats/fixture-v1/portal/FIXPHENO/gene_gene_set_stats.json",
                          "--dimension", "trait=phenotype", "--dimension", "library=gene_set_size"],
                         cwd=ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    source = yaml.safe_load((out / "source.yaml").read_text())
    assert source["path"] == "{kind}/{library}/portal/{trait}/{kind}.json"
    assert source["roles"]["membership"] == {"kind": "gene_gene_set_stats", "requires": "gene_set_stats"}
    col = load_collection(out)  # schema-valid and cross-checked, TODOs and all
    doc = col.doc
    assert doc["entities"]["Gene"] == {"id": "HGNC.SYMBOL:{value}"}
    assert doc["entities"]["GeneSet"] == {"mint": "GeneSet", "members_from": "member_of", "library_dimension": "library"}
    assert doc["entities"]["Trait"] == {"dimension": "trait"}
    assert sorted(d["name"] for d in doc["dataset_dimensions"]) == ["library", "trait"]
    assert all("resolve" not in d for d in doc["dataset_dimensions"])
    # The trait vocabulary is the collection's own, started from the release file for the sampled key.
    assert doc["mappings"] == {"trait": "mappings/trait.tsv"} and doc["xrefs"] == {"trait": "mappings/trait.sssom.tsv"}
    assert doc["unmapped"] == {"trait": "keep-raw"}
    from service.collections.traits import collection_vocabulary
    vocabulary = collection_vocabulary(doc, out)
    assert vocabulary.mappings["trait"] == {"FIXPHENO": ("KPN.TRAIT:9999999", "Fixture phenotype")}
    entry = vocabulary.entries["trait"]["KPN.TRAIT:9999999"]
    assert entry["category"] == "digestive" and entry["concept_id"] == "EFO:9999991"
    assert [x["target_id"] for x in entry["xrefs"]] == ["EFO:9999991"]
    assert set(doc["row_kinds"]) == {"gene_trait", "gene_set_trait"}
    assert doc["row_kinds"]["gene_trait"]["subject"] == {"entity": "Gene", "column": "gene"}
    assert {"combined", "log_bf", "prior"} <= set(doc["row_kinds"]["gene_trait"]["metrics"])
    assert doc["edge_kinds"]["member_of"]["from"]["column"] == "gene"
    assert sorted(t.doc["template"] for t in col.templates) == ["gene-set-trait", "gene-trait", "member-of-roster"]
    assert list(col.queries) == ["top-gene-trait-with-gene-set-trait"]
    assert (out / "GUIDE.md").read_text().startswith("# Finishing the `fixture` collection")
    validate = subprocess.run([sys.executable, "-m", "claims_cli.main", "collection", "validate", str(out)],
                              cwd=ROOT, env=env, capture_output=True, text=True)
    assert validate.returncode == 0 and "TODO(s) left" in validate.stdout


def test_generated_templates_render_claims_for_every_kind(tmp_path):
    import yaml

    from service.collections.manifest import load_collection
    from claims_cli import scaffold

    col_dir = tmp_path / "pigean"
    import shutil
    shutil.copytree(ROOT / "tests" / "fixtures" / "collections" / "pigean", col_dir)
    for t in (col_dir / "templates").iterdir():
        t.unlink()
    doc = yaml.safe_load((col_dir / "collection.yaml").read_text())
    generated = scaffold.generate_templates(doc)
    assert set(generated) == {"gene_phenotype.yaml", "gene_set_phenotype.yaml", "member_of-roster.yaml"}
    for name, t in generated.items():
        (col_dir / "templates" / name).write_text(scaffold.dump(t))
    scaffold.set_templates_list(col_dir / "collection.yaml", sorted(generated))
    col = load_collection(col_dir)
    assert sorted(t.rel for t in col.templates) == [f"templates/{n}" for n in sorted(generated)]


def test_validate_reports_an_object_collection():
    """kind: objects has object kinds, not metrics or claim templates; validate must not assume them."""
    out = subprocess.run([sys.executable, "-m", "claims_cli.main", "collection", "validate",
                          str(ROOT / "tests" / "fixtures" / "collections" / "catalog-test")],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "catalog-test (objects): 4 object kind(s)" in out.stdout
