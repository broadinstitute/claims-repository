#!/usr/bin/env python3
"""Load the KPN trait registry (2026-11-09, per Chase) from a pinned release of
broadinstitute/kpn-data-models into kpn_traits / kpn_trait_mappings.

Each trait has a canonical KPN.TRAIT ID (e.g. KPN.TRAIT:0000505), the legacy
portal key it replaces (legacy_id, e.g. IBD), the PIGEAN key where that
differs (pigean_id), and its cross-ontology mappings (SSSOM predicates to
EFO, MONDO, MESH, DOID, HP, ORPHANET, ...) with the primary mapping marked.
PIGEAN results are keyed by the legacy / PIGEAN key; scripts/load_pigean.py
resolves them through this table so claims name the KPN.TRAIT ID.

The release is a full snapshot: loading replaces the registry tables in one
transaction. Load it BEFORE load_pigean.py. Source of record:
  https://github.com/broadinstitute/kpn-data-models  (versions/trait/<version>/)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402

UPSTREAM = "https://github.com/broadinstitute/kpn-data-models"
DEFAULT_CHECKOUT = Path.home() / "src" / "research" / "portal-data-models"
DEFAULT_VERSION = "v0.0.2"
COLLECTION_FILE = "kpn_trait_collection.yaml"


def release_path(checkout: Path, version: str) -> Path:
    return checkout / "versions" / "trait" / version / COLLECTION_FILE


LS_REMOTE_TIMEOUT = 20  # seconds; an unreachable or silent remote is "not advertised"
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}


def _git(checkout: Path, *args: str, timeout: float | None = None) -> str | None:
    """git output, or None on any failure (missing git, error, timeout).
    Never prompts; ref names need not be UTF-8 (only SHAs are compared)."""
    try:
        out = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, check=True,
                             timeout=timeout, env=_GIT_ENV)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out.stdout.decode("utf-8", errors="replace").strip()


def _is_local_url(url: str) -> bool:
    """git's rule (url_is_local_not_ssh): a file:// URL, or a path with no
    ':' before its first '/' ('.', '/abs', '../clone'), is local; scp-style
    'host:path' and other scheme URLs are not."""
    if "://" in url:
        return url.startswith("file://")
    colon, slash = url.find(":"), url.find("/")
    return colon == -1 or (slash != -1 and slash < colon)


def _published(checkout: Path, commit: str) -> bool:
    """`commit` is contained in a remote-tracking branch of, or advertised
    as a branch or tag (peeled tags included) by, an eligible remote: one
    whose every configured fetch and push URL is non-local. Remote-tracking
    branches are trusted as last fetched (or pushed) and are checked first,
    for every eligible remote, without network access; only if none
    contains the commit is each eligible remote asked (git ls-remote,
    bounded by LS_REMOTE_TIMEOUT, failing closed). A local-only commit or
    tag, a remote with any local URL, and FETCH_HEAD never count."""
    remotes = (_git(checkout, "remote") or "").split()
    eligible = []
    for remote in remotes:
        urls = [u for key in (f"remote.{remote}.url", f"remote.{remote}.pushurl")
                for u in (_git(checkout, "config", "--get-all", key) or "").splitlines() if u]
        if urls and not any(_is_local_url(u) for u in urls):
            eligible.append(remote)
    for remote in eligible:
        # Exactly this remote's namespace, not that of a remote named "<remote>/x".
        nested = [f"refs/remotes/{o}/" for o in remotes if o != remote and o.startswith(remote + "/")]
        refs = (_git(checkout, "for-each-ref", "--contains", commit, "--format=%(refname)",
                     f"refs/remotes/{remote}/") or "").splitlines()
        if any(r and not any(r.startswith(n) for n in nested) for r in refs):
            return True
    for remote in eligible:
        listing = _git(checkout, "ls-remote", "--heads", "--tags", remote, timeout=LS_REMOTE_TIMEOUT) or ""
        if any(line.split("\t")[0] == commit for line in listing.splitlines()):
            return True
    return False


def git_commit(checkout: Path, path: Path, allow_unpublished: bool = False) -> tuple[str | None, bool]:
    """(commit, published) for the release file. Published means the file
    is exactly the committed blob at HEAD, and HEAD is contained in a
    remote-tracking branch of, or advertised (branch or tag) by, a configured
    non-local remote (see _published) — so a clone, including a tag-pinned
    or shallow one, qualifies; a local-only commit or tag does not. This
    proves the commit was published somewhere, not which repository (see
    docs §7). Anything else — a modified or untracked file, an unpublished
    commit, no git checkout — is refused unless allow_unpublished, and then
    recorded as unpublished (with no commit when the file is not the
    committed blob)."""
    checkout, path = checkout.resolve(), path.resolve()
    commit = _git(checkout, "rev-parse", "HEAD")
    if commit is None:
        if allow_unpublished:
            return None, False
        raise SystemExit(f"{checkout} is not a git checkout; cannot verify the release (--allow-unpublished to load anyway)")
    rel = path.relative_to(checkout).as_posix()
    committed = _git(checkout, "rev-parse", f"HEAD:{rel}")
    on_disk = _git(checkout, "hash-object", "--", rel)
    if committed is None or on_disk != committed:
        if allow_unpublished:
            return None, False  # HEAD does not contain this content: name no commit
        raise SystemExit(f"{path} is not the committed release file at {commit[:12]} (modified or untracked); "
                         "load only a published release (--allow-unpublished to load anyway)")
    published = _published(checkout, commit)
    if not published and not allow_unpublished:
        raise SystemExit(f"commit {commit[:12]} is not shown published by any configured non-local remote (no "
                         "remote-tracking branch contains it and no remote advertises a branch or tag at it); "
                         f"load a published release: clone {UPSTREAM} (e.g. --branch <tag>), or add it as a "
                         "remote (--allow-unpublished to load anyway)")
    return commit, published


def parse_release(path: Path) -> list[dict]:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path) as handle:
        data = yaml.load(handle, Loader=loader)
    traits = data["phenotypes"] if isinstance(data, dict) else data
    # One combined key space: a key may name only one trait, whether it is a
    # trait id, a legacy id, or a PIGEAN id — else resolution is ambiguous.
    owner: dict[str, str] = {}
    for t in traits:
        for key in (t["portal_id"], t["legacy_id"], t.get("pigean_id")):
            if key is None:
                continue
            if owner.setdefault(key, t["portal_id"]) != t["portal_id"]:
                raise ValueError(f"release key {key!r} names both {owner[key]} and {t['portal_id']}: "
                                 "trait resolution would be ambiguous")
    return traits


def orphaned_traits(con, traits: list[dict]) -> list[str]:
    """Stored result keys the new release would resolve differently: a key
    (KPN.TRAIT id, or a legacy / PIGEAN key the CURRENT registry resolves)
    whose trait the release drops, or that the release unmaps or moves to
    another trait. Uses the same combined key space parse_release enforces."""
    new = {k: t["portal_id"] for t in traits for k in (t["portal_id"], t["legacy_id"], t.get("pigean_id")) if k}
    current = {}
    for r in con.execute("SELECT trait_id, legacy_id, pigean_id FROM kpn_traits").fetchall():
        for k in (r["trait_id"], r["legacy_id"], r["pigean_id"]):
            if k:
                current[k] = r["trait_id"]
    changed = set()
    for table in ("pigean_gene_associations", "pigean_gene_set_associations", "claim_memberships"):
        for r in con.execute(f"SELECT DISTINCT phenotype FROM {table}").fetchall():
            key = r["phenotype"]
            trait = current.get(key) or (key if key.startswith("KPN.TRAIT:") else None)
            if trait and new.get(key) != trait:
                changed.add(f"{trait} (key {key!r} -> {new.get(key)})" if key != trait else trait)
    return sorted(changed)


def load(con, path: Path, version: str, commit: str | None, force: bool = False, published: bool = False) -> dict:
    data = path.read_bytes()
    traits = parse_release(path)
    orphans = orphaned_traits(con, traits)
    if orphans and not force:
        raise SystemExit(f"refusing: {len(orphans)} stored result key(s) would resolve differently under this "
                         f"release (dropped, unmapped, or moved), e.g. {orphans[:3]}; replacing the registry "
                         "would orphan them (--force to proceed)")
    trait_rows, mapping_rows = [], []
    for t in traits:
        primary = t.get("primary_mapping") or {}
        trait_rows.append((
            t["portal_id"], t["legacy_id"], t.get("pigean_id"), t.get("gwas_source_category"),
            t["phenotype_name"], t.get("description"), t.get("legacy_trait_group"), t.get("trait_group"),
            t.get("trait_type"), primary.get("target_id"), primary.get("target_label"),
            primary.get("mapping_predicate"),
        ))
        for m in t.get("mappings") or []:
            mapping_rows.append((
                t["portal_id"], m["target_id"], m["target_ontology"], m["mapping_predicate"],
                m.get("target_label"), m.get("confidence"), m.get("mapping_justification"), m.get("source"),
            ))
    if published:
        source_url = f"{UPSTREAM}/blob/{commit}/versions/trait/{version}/{COLLECTION_FILE}"
    else:  # never present an unverified file as an upstream release
        source_url = f"unpublished:{commit or 'uncommitted'}:versions/trait/{version}/{COLLECTION_FILE}"
    with con:
        replace = con.dialect.insert_or_replace
        con.execute("DELETE FROM kpn_trait_mappings")
        con.execute("DELETE FROM kpn_traits")
        con.executemany(
            "INSERT INTO kpn_traits (trait_id, legacy_id, pigean_id, source_category, name, description, "
            "legacy_trait_group, trait_group, trait_type, primary_concept_id, primary_concept_label, "
            "primary_predicate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            trait_rows,
        )
        con.executemany(
            f"{replace} kpn_trait_mappings (trait_id, target_id, ontology, predicate, label, confidence, "
            "justification, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            mapping_rows,
        )
        con.execute(
            f"{replace} kpn_trait_release (release_key, version, commit_sha, source_url, file_sha256, "
            "trait_count, mapping_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("current", version, commit, source_url, hashlib.sha256(data).hexdigest(),
             len(trait_rows), len(mapping_rows)),
        )
    stats = {"traits": len(trait_rows), "mappings": len(mapping_rows), "version": version, "commit": commit,
             "published": published, "source_url": source_url}
    print(f"KPN traits {version} ({(commit or 'no git')[:12]}{'' if published else ', UNPUBLISHED'}): "
          f"{stats['traits']} traits, {stats['mappings']} mappings")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT,
                        help=f"local clone of {UPSTREAM} (default: %(default)s)")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="release version (default: %(default)s)")
    parser.add_argument("--db", default=None, help="SQLite path (default: service default)")
    parser.add_argument("--force", action="store_true", help="replace the registry even if it orphans loaded results")
    parser.add_argument("--allow-unpublished", action="store_true",
                        help="load a release file that is not a published commit; recorded with an "
                             "'unpublished:' source_url instead of the upstream URL")
    args = parser.parse_args()
    path = release_path(args.checkout.resolve(), args.version)
    if not path.exists():
        raise SystemExit(f"release file not found: {path}")
    commit, published = git_commit(args.checkout, path, allow_unpublished=args.allow_unpublished)
    con = db.connect(args.db)
    try:
        load(con, path, args.version, commit, force=args.force, published=published)
    finally:
        con.close()


if __name__ == "__main__":
    main()
