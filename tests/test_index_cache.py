"""The index cache reads many datasets in parallel, refuses a file that is not the one recorded, and the bucket
store reads no dataset whose recorded counts hold none of the kind asked for."""
from __future__ import annotations

import pytest

from service.collections import index_bucket
from service.collections.index_bucket import FILES, Cache, IndexError_, sha256_file
from service.collections.store import BucketStore


def _bucket(tmp_path, n):
    entries = []
    for i in range(n):
        d = tmp_path / "bucket" / f"ds{i}"
        d.mkdir(parents=True)
        files = {}
        for name in FILES:
            (d / name).write_bytes(f"{name} of ds{i}".encode())
            files[name] = {"size": (d / name).stat().st_size, "sha256": sha256_file(d / name)}
        entries.append((f"ds{i}", {"location": f"file://{d}", "files": files,
                                   "counts": {"rows": {"gene_phenotype": 3} if i % 2 else {}, "edges": {}}}))
    return entries


def test_many_datasets_are_read_in_parallel_and_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_INDEX_ALLOW_FILE", "1")
    entries = _bucket(tmp_path, 40)
    reads = []
    real = index_bucket.download
    monkeypatch.setattr(index_bucket, "download", lambda *a: (reads.append(a[1]), real(*a)))
    cache = Cache(tmp_path / "cache")
    cache.ensure_many(entries)
    assert len(reads) == 40 * len(FILES)
    assert all((cache.dir(d) / ".checked").exists() for d, _ in entries)
    cache.ensure_many(entries)   # held: nothing is read again
    assert len(reads) == 40 * len(FILES)

    bad = _bucket(tmp_path / "other", 3)
    bad[1][1]["files"]["rows.parquet"]["sha256"] = "0" * 64
    other = Cache(tmp_path / "cache2")
    with pytest.raises(IndexError_, match="not the file recorded"):
        other.ensure_many(bad)
    # The others are read and checked; the tampered one never enters the cache.
    assert [(other.dir(d) / ".checked").exists() for d, _ in bad] == [True, False, True]


def test_the_bucket_store_skips_datasets_without_the_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_INDEX_ALLOW_FILE", "1")
    store = BucketStore(tmp_path / "cache")
    entries = _bucket(tmp_path, 6)
    store.known.update(dict(entries))
    assert store._holding([d for d, _ in entries], "gene_phenotype") == ["ds1", "ds3", "ds5"]
    assert store.anchor(["ds0", "ds2", "ds4"], "gene_phenotype", [], "combined", "DESC", 10) == []
    assert not any(store.cache.dir(d).exists() for d in ("ds0", "ds2", "ds4"))   # never read
