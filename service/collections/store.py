"""Where a collection's result data lives: the rows, metrics, edges and claim
index that the publisher derives from the result files and the executor and
claim rendering read back.

Two implementations share one interface:

- SqlStore: the result_rows / result_metrics / result_edges / result_claims
  tables in the service database (MySQL on QA, SQLite locally). The default.
- ParquetStore: Parquet files under a directory (CLAIMS_INDEX_DIR), queried
  with DuckDB. The service database keeps only the control plane (collections,
  releases, datasets, files, mint contexts) and the claims actually served.

Claim ids never depend on the store: both hold the same row values and byte
spans, and claims are rendered from them by the same engine.

Callers pass dataset ids, never a release: resolving a release to its datasets
and mint contexts is control-plane work (serve.release_datasets).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

FILTER_OPS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=", "ne": "!="}
ROW_FIELDS = ("row_id", "dataset_id", "kind", "subject_id", "subject_value", "subject_label", "object_id",
              "object_label", "file_id", "row_json", "spans")
CLAIM_FIELDS = ("claim_id", "activity_id", "claim_key", "dataset_id", "row_id", "edge_kind", "group_id",
                "proposition_id")


def _marks(n: int) -> str:
    return ",".join("?" * n)


def compile_where(where: dict | None, bind) -> list[tuple[str, str, float]]:
    """[(metric, SQL operator, value)] for a step's metric conditions; a
    condition whose variable is unset (None) is dropped."""
    out = []
    for metric, conds in sorted((where or {}).items()):
        for op, raw in sorted(conds.items()):
            value = bind(raw)
            if value is None:
                continue
            if op not in FILTER_OPS:
                raise ValueError(f"unknown filter operator {op!r}")
            out.append((metric, FILTER_OPS[op], value))
    return out


def _metric_joins(alias: str, conds, metrics_table: str):
    joins, params = [], []
    for i, (metric, op, value) in enumerate(conds):
        a = f"{alias}w{i}"
        joins.append(f"JOIN {metrics_table} {a} ON {a}.row_id = {alias}.row_id AND {a}.metric = ? AND {a}.value {op} ?")
        params.extend([metric, value])
    return joins, params


class _SqlShape:
    """The read queries, written once over table expressions so both stores
    run the same SQL: ordering, ties and windows are identical by construction."""

    def tables(self, dataset_ids):  # -> (rows, metrics, edges) table expressions
        raise NotImplementedError

    def query(self, sql, params):  # -> list[dict]
        raise NotImplementedError

    def anchor(self, dataset_ids, kind, conds, order_metric, direction, limit, subjects=None):
        """Rows of one kind, filtered, ordered and limited. `subjects` keeps only rows whose subject value or
        id is one of them (one gene across every dataset, say)."""
        if not dataset_ids:
            return []
        rows, metrics, _ = self.tables(dataset_ids)
        joins, params = _metric_joins("r", conds, metrics)
        subject_sql, subject_params = "", []
        if subjects:
            subject_sql = (f" AND (r.subject_value IN ({_marks(len(subjects))}) OR "
                           f"r.subject_id IN ({_marks(len(subjects))}))")
            subject_params = [*subjects, *subjects]
        sql = (f"SELECT r.* FROM {rows} r JOIN {metrics} o ON o.row_id = r.row_id AND o.metric = ? "
               + " ".join(joins)
               + f" WHERE r.kind = ? AND r.dataset_id IN ({_marks(len(dataset_ids))}){subject_sql} "
               f"ORDER BY o.value {direction}, r.subject_value ASC, r.dataset_id ASC LIMIT ?")
        return self.query(sql, [order_metric, *params, kind, *dataset_ids, *subject_params, limit])

    def has_edges(self, dataset_id, edge_kind):
        _, _, edges = self.tables([dataset_id])
        return bool(self.query(f"SELECT 1 AS x FROM {edges} e WHERE e.dataset_id = ? AND e.kind = ? LIMIT 1",
                               [dataset_id, edge_kind]))

    def join(self, dataset_id, edge_kind, from_ids, kind, conds, order_metric, direction, limit):
        if not from_ids:
            return []
        rows, metrics, edges = self.tables([dataset_id])
        joins, params = _metric_joins("t", conds, metrics)
        sql = ("SELECT * FROM (SELECT e.from_id AS e_from, e.to_id AS e_to, e.from_value AS e_from_value, "
               "e.to_value AS e_to_value, t.*, ROW_NUMBER() OVER (PARTITION BY e.from_id "
               f"ORDER BY o.value {direction}, t.subject_value ASC) AS e_rank "
               f"FROM {edges} e "
               f"JOIN {rows} t ON t.dataset_id = e.dataset_id AND t.kind = ? AND t.subject_id = e.to_id "
               f"JOIN {metrics} o ON o.row_id = t.row_id AND o.metric = ? " + " ".join(joins) +
               f" WHERE e.dataset_id = ? AND e.kind = ? AND e.from_id IN ({_marks(len(from_ids))})) x "
               "WHERE e_rank <= ? ORDER BY e_from, e_rank")
        return self.query(sql, [kind, order_metric, *params, dataset_id, edge_kind, *from_ids, limit])

    def group(self, dataset_id, edge_kind, group_id):
        """(label, member count) of the edges ending at group_id, or None."""
        _, _, edges = self.tables([dataset_id])
        got = self.query(f"SELECT e.to_value AS label, COUNT(*) AS n FROM {edges} e WHERE e.dataset_id = ? "
                         "AND e.kind = ? AND e.to_id = ? GROUP BY e.to_value", [dataset_id, edge_kind, group_id])
        return (got[0]["label"], got[0]["n"]) if got else None

    def members(self, dataset_id, edge_kind, group_id):
        _, _, edges = self.tables([dataset_id])
        return [r["from_id"] for r in self.query(
            f"SELECT e.from_id FROM {edges} e WHERE e.dataset_id = ? AND e.kind = ? AND e.to_id = ? ORDER BY e.from_id",
            [dataset_id, edge_kind, group_id])]

    def row(self, dataset_id, row_id):
        rows, _, _ = self.tables([dataset_id])
        got = self.query(f"SELECT * FROM {rows} r WHERE r.row_id = ?", [row_id])
        return got[0] if got else None


class SqlStore(_SqlShape):
    """Today's tables in the service database."""
    kind = "sql"
    lazy_objects = False

    def __init__(self, con):
        self.con = con

    def tables(self, dataset_ids):
        return "result_rows", "result_metrics", "result_edges"

    def query(self, sql, params):
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def row(self, dataset_id, row_id):
        r = self.con.execute("SELECT * FROM result_rows WHERE row_id = ?", (row_id,)).fetchone()
        return dict(r) if r else None

    def claims_for_rows(self, row_ids, dataset_ids=None):
        out = []
        ids = sorted(set(row_ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            out += self.query(f"SELECT * FROM result_claims WHERE row_id IN ({_marks(len(chunk))})", chunk)
        return out

    def claims_for_group(self, dataset_id, edge_kind, group_id):
        return self.query("SELECT * FROM result_claims WHERE dataset_id = ? AND edge_kind = ? AND group_id = ?",
                          [dataset_id, edge_kind, group_id])

    def claim(self, claim_id):
        got = self.query("SELECT * FROM result_claims WHERE claim_id = ?", [claim_id])
        return got[0] if got else None

    def claims_for_proposition(self, proposition_id):
        return self.query("SELECT * FROM result_claims WHERE proposition_id = ?", [proposition_id])

    def counts(self, dataset_id):
        out = {r["kind"]: r["n"] for r in self.query(
            "SELECT kind, COUNT(*) AS n FROM result_rows WHERE dataset_id = ? GROUP BY kind", [dataset_id])}
        out.update({r["kind"]: r["n"] for r in self.query(
            "SELECT kind, COUNT(*) AS n FROM result_edges WHERE dataset_id = ? GROUP BY kind", [dataset_id])})
        return out

    def claim_count(self, dataset_ids, activity_ids):
        if not dataset_ids or not activity_ids:
            return 0
        return self.query(f"SELECT COUNT(*) AS n FROM result_claims WHERE activity_id IN ({_marks(len(activity_ids))})",
                          list(activity_ids))[0]["n"]


# --- Parquet ----------------------------------------------------------------

def stable_row_id(dataset_id: str, kind: str, subject_id: str) -> int:
    """A row's id in a Parquet index: derived from its natural key, so it is
    the same whichever process or order wrote it (63 bits, positive)."""
    digest = hashlib.sha256(f"{dataset_id}\x1f{kind}\x1f{subject_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


class ParquetStore(_SqlShape):
    """Result data as Parquet under `root`:

        datasets/<dataset>/rows.parquet       one row per result row (values as canonical JSON, byte spans)
        datasets/<dataset>/metrics.parquet    (row_id, metric, value) for filtering and ordering
        datasets/<dataset>/edges.parquet      edges, e.g. gene member_of gene set
        datasets/<dataset>/claims-<activity>.parquet   the claims one mint context implies
        datasets/<dataset>/counts.json        rows and edges per kind, claims per mint context
        lookup/claims.parquet                 every claim, sorted by claim id (rebuilt when a release is recorded)

    A dataset directory is written whole to a temporary name and renamed into
    place, so a reader never sees half a dataset."""
    kind = "parquet"
    lazy_objects = True
    _lock = threading.Lock()

    def __init__(self, root: str | os.PathLike):
        import duckdb  # only this store needs it; the SQL store and the QA build do not

        self.root = Path(root)
        (self.root / "datasets").mkdir(parents=True, exist_ok=True)
        (self.root / "lookup").mkdir(parents=True, exist_ok=True)
        self._db = duckdb.connect()
        self._db.execute("SET threads TO 4")

    # paths
    def dataset_dir(self, dataset_id: str) -> Path:
        return self.root / "datasets" / _safe(dataset_id)

    def _files(self, dataset_ids, name):
        paths = [self.dataset_dir(d) / name for d in dataset_ids]
        return [str(p) for p in paths if p.exists()]

    def tables(self, dataset_ids):
        def expr(name, empty):
            files = self._files(dataset_ids, name)
            return f"read_parquet({files!r})" if files else f"(SELECT * FROM ({empty}) WHERE false)"
        return (expr("rows.parquet", "SELECT 0::BIGINT AS row_id, ''::VARCHAR AS dataset_id, ''::VARCHAR AS kind, "
                     "''::VARCHAR AS subject_id, ''::VARCHAR AS subject_value, ''::VARCHAR AS subject_label, "
                     "''::VARCHAR AS object_id, ''::VARCHAR AS object_label, ''::VARCHAR AS file_id, "
                     "''::VARCHAR AS row_json, ''::VARCHAR AS spans"),
                expr("metrics.parquet", "SELECT 0::BIGINT AS row_id, ''::VARCHAR AS metric, 0.0::DOUBLE AS value"),
                expr("edges.parquet", "SELECT ''::VARCHAR AS dataset_id, ''::VARCHAR AS kind, ''::VARCHAR AS from_id, "
                     "''::VARCHAR AS to_id, ''::VARCHAR AS from_value, ''::VARCHAR AS to_value"))

    def query(self, sql, params):
        cur = self._db.cursor()
        try:
            res = cur.execute(sql, params)
            cols = [d[0] for d in res.description]
            return [dict(zip(cols, r)) for r in res.fetchall()]
        finally:
            cur.close()

    # claims
    def _claim_files(self, dataset_ids):
        out = []
        for d in dataset_ids:
            out += sorted(str(p) for p in self.dataset_dir(d).glob("claims-*.parquet"))
        return out

    def _lookup(self):
        p = self.root / "lookup" / "claims.parquet"
        return str(p) if p.exists() else None

    def claims_for_rows(self, row_ids, dataset_ids=None):
        files = self._claim_files(dataset_ids) if dataset_ids else ([self._lookup()] if self._lookup() else [])
        ids = sorted(set(row_ids))
        if not files or not ids:
            return []
        return self.query(f"SELECT * FROM read_parquet({files!r}) WHERE row_id IN ({_marks(len(ids))})", ids)

    def claims_for_group(self, dataset_id, edge_kind, group_id):
        files = self._claim_files([dataset_id])
        if not files:
            return []
        return self.query(f"SELECT * FROM read_parquet({files!r}) WHERE edge_kind = ? AND group_id = ?",
                          [edge_kind, group_id])

    def claim(self, claim_id):
        lookup = self._lookup()
        if not lookup:
            return None
        got = self.query(f"SELECT * FROM read_parquet('{lookup}') WHERE claim_id = ?", [claim_id])
        return got[0] if got else None

    def claims_for_proposition(self, proposition_id):
        lookup = self._lookup()
        if not lookup:
            return []
        return self.query(f"SELECT * FROM read_parquet('{lookup}') WHERE proposition_id = ?", [proposition_id])

    def counts(self, dataset_id):
        p = self.dataset_dir(dataset_id) / "counts.json"
        if not p.exists():
            return {}
        c = json.loads(p.read_text())
        return {**c.get("rows", {}), **c.get("edges", {})}

    def claim_count(self, dataset_ids, activity_ids):
        total = 0
        for d in dataset_ids:
            p = self.dataset_dir(d) / "counts.json"
            if p.exists():
                c = json.loads(p.read_text()).get("claims", {})
                total += sum(n for a, n in c.items() if a in activity_ids)
        return total

    def has_dataset(self, dataset_id):
        return (self.dataset_dir(dataset_id) / "counts.json").exists()

    def size_bytes(self):
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    # writing
    def write_dataset(self, dataset_id: str, rows: list[dict], metrics: list[tuple], edges: list[tuple],
                      claims: dict[str, list[tuple]]) -> None:
        """rows: dicts with ROW_FIELDS; metrics: (row_id, metric, value);
        edges: (dataset_id, kind, from_id, to_id, from_value, to_value);
        claims: activity id -> [CLAIM_FIELDS tuples]."""
        final = self.dataset_dir(dataset_id)
        tmp = final.with_name(final.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        cur = self._db.cursor()
        try:
            self._write(cur, tmp / "rows.parquet",
                        "row_id BIGINT, dataset_id VARCHAR, kind VARCHAR, subject_id VARCHAR, subject_value VARCHAR, "
                        "subject_label VARCHAR, object_id VARCHAR, object_label VARCHAR, file_id VARCHAR, "
                        "row_json VARCHAR, spans VARCHAR",
                        [tuple(r[f] for f in ROW_FIELDS) for r in rows], "kind, subject_value")
            self._write(cur, tmp / "metrics.parquet", "row_id BIGINT, metric VARCHAR, value DOUBLE", metrics,
                        "metric, value")
            self._write(cur, tmp / "edges.parquet",
                        "dataset_id VARCHAR, kind VARCHAR, from_id VARCHAR, to_id VARCHAR, from_value VARCHAR, "
                        "to_value VARCHAR", edges, "kind, to_id, from_id")
            for activity_id, items in claims.items():
                self._write(cur, tmp / f"claims-{_safe(activity_id)}.parquet",
                            "claim_id VARCHAR, activity_id VARCHAR, claim_key VARCHAR, dataset_id VARCHAR, "
                            "row_id BIGINT, edge_kind VARCHAR, group_id VARCHAR, proposition_id VARCHAR",
                            items, "claim_id")
        finally:
            cur.close()
        counts = {"rows": {}, "edges": {}, "claims": {a: len(v) for a, v in claims.items()}}
        for r in rows:
            counts["rows"][r["kind"]] = counts["rows"].get(r["kind"], 0) + 1
        for e in edges:
            counts["edges"][e[1]] = counts["edges"].get(e[1], 0) + 1
        (tmp / "counts.json").write_text(json.dumps(counts, sort_keys=True))
        with self._lock:
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)

    @staticmethod
    def _write(cur, path: Path, columns: str, values: list[tuple], order: str) -> None:
        """Write rows to Parquet. Rows go through a temporary CSV that DuckDB
        reads in bulk: row-by-row inserts are far too slow for 10^5 edges."""
        import csv
        import tempfile

        cols = [c.strip().split(" ", 1) for c in columns.split(",")]
        spec = "{" + ", ".join(f"'{name}': '{typ}'" for name, typ in cols) + "}"
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            for row in values:
                writer.writerow(["\\N" if v is None else v for v in row])
            tmp = fh.name
        try:
            cur.execute(
                f"COPY (SELECT * FROM read_csv('{tmp}', columns={spec}, header=false, delim=',', quote='\"', "
                f"escape='\"', nullstr='\\N', auto_detect=false) ORDER BY {order}) "
                f"TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            os.unlink(tmp)

    def add_claims(self, dataset_id: str, claims: dict[str, list[tuple]]) -> None:
        """Claims of a new mint context over an already ingested dataset."""
        d = self.dataset_dir(dataset_id)
        cur = self._db.cursor()
        try:
            for activity_id, items in claims.items():
                self._write(cur, d / f"claims-{_safe(activity_id)}.parquet",
                            "claim_id VARCHAR, activity_id VARCHAR, claim_key VARCHAR, dataset_id VARCHAR, "
                            "row_id BIGINT, edge_kind VARCHAR, group_id VARCHAR, proposition_id VARCHAR",
                            items, "claim_id")
        finally:
            cur.close()
        p = d / "counts.json"
        counts = json.loads(p.read_text())
        counts["claims"].update({a: len(v) for a, v in claims.items()})
        p.write_text(json.dumps(counts, sort_keys=True))

    def rebuild_lookup(self, dataset_ids) -> int:
        """lookup/claims.parquet: every claim of these datasets, sorted by id."""
        files = self._claim_files(dataset_ids)
        target = self.root / "lookup" / "claims.parquet"
        tmp = target.with_suffix(".tmp")
        if not files:
            return 0
        cur = self._db.cursor()
        try:
            cur.execute(f"COPY (SELECT * FROM read_parquet({files!r}) ORDER BY claim_id) TO '{tmp}' "
                        "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000)")
            n = cur.execute(f"SELECT COUNT(*) FROM read_parquet('{tmp}')").fetchone()[0]
        finally:
            cur.close()
        os.replace(tmp, target)
        return n


_PARQUET: dict[str, ParquetStore] = {}


def index_dir() -> str | None:
    return os.environ.get("CLAIMS_INDEX_DIR") or None


def get_store(con):
    """The store for this deployment: Parquet under CLAIMS_INDEX_DIR when it
    is set, else the service database's tables."""
    root = index_dir()
    if not root:
        return SqlStore(con)
    store = _PARQUET.get(root)
    if store is None:
        store = _PARQUET[root] = ParquetStore(root)
    return store
