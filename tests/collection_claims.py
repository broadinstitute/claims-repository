"""Every claim a published release implies, rendered now. Nothing records a claim before it is served, so
tests that need "all of a release's claims" render them from its rows and edge groups."""
from __future__ import annotations

from service.collections import serve as cserve
from service.collections.store import get_store


def implied_claims(con, release_id: str | None = None) -> list[dict]:
    """Claim records (claim_id, activity_id, claim_key, dataset_id, row_id or edge group, proposition_id) for
    every row and edge group of a release: the collection's current one when no release is given."""
    if release_id is None:
        release_id = con.execute("SELECT current_release_id FROM collections WHERE current_release_id IS NOT NULL "
                                 "ORDER BY collection_key LIMIT 1").fetchone()["current_release_id"]
    store = get_store(con)
    datasets = [r["dataset_id"] for r in con.execute(
        "SELECT dataset_id FROM release_datasets WHERE release_id = ? ORDER BY dataset_id", (release_id,))]
    rows_t, _, edges_t = store.tables(datasets)
    out = []
    for dataset_id in datasets:
        rows = store.query(f"SELECT * FROM {rows_t} r WHERE r.dataset_id = ? ORDER BY r.row_id", [dataset_id])
        out += cserve.implied_row_claims(con, release_id, rows)
        for g in store.query(f"SELECT DISTINCT e.kind, e.to_id FROM {edges_t} e WHERE e.dataset_id = ? "
                             "ORDER BY e.kind, e.to_id", [dataset_id]):
            out += cserve.implied_group_claims(con, release_id, dataset_id, g["kind"], g["to_id"])
    return [{k: v for k, v in rc.items() if k != "_rendered"} for rc in out]


def implied_ids(con, release_id: str | None = None) -> set[str]:
    return {rc["claim_id"] for rc in implied_claims(con, release_id)}
