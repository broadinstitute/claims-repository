"""Object collections (`kind: objects`): collections whose releases are built
from deposits of DAPPER objects rather than from result files.

A file-based collection mints its claims from rows with its own templates, so
it cannot hold objects someone else minted. An object collection holds exactly
the objects deposited into it, under their own DAPPER-ID-1 ids: the deposit
route stores them as it stores any deposit (all or nothing, an object already
held left as stored), and membership in the release is a record beside them.

Life of a release:
1. The collection's definition (manifest, query templates, vocabulary files)
   is registered from its directory: `claims objects register` with a login
   (POST /v1/publish/objects/{c}:register), or scripts/publish_collection.py
   on the database.
2. A key granted the collection, or a `claims login` (any collection), opens a
   release: POST /v1/collections/{c}/releases {"version": ...}.
3. Deposits naming the collection and that release add their objects (any
   number of parts; resending a part changes nothing).
4. Closing it mints the release's DAPPER ResultCollection (its members, the
   manifest and query Files), and it becomes the collection's current release.

Queries read the closed release named, or the current one. They are plain
reads: object queries serve stored documents and assert no claim.
"""
from __future__ import annotations

import hashlib
import json

from .. import dapper_validation as dval
from ..refs import DAPPER_ID_RE, dapper_refs
from . import identity as idn
from .manifest import Collection, ManifestError, bind_params
from .serve import CollectionNotFound, _json, release_row
from .traits import MappingError, collection_vocabulary, vocabulary_files

FILTER_OPS = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
MAX_VALUE = 255     # release_object_fields.value is VARCHAR(255)
CARRY_BATCH = 500   # stored documents read per query when re-indexing carried members
MAX_JOIN = 200      # objects one step returns per anchor, at most


class ReleaseError(ValueError):
    """A release request that conflicts with the release's state (409)."""


class NotGranted(PermissionError):
    """The key was not granted the collection (403). A login needs no grant."""


def is_objects(record: dict) -> bool:
    return record.get("kind") == "objects"


# --- the definition ----------------------------------------------------------------------------------------

def build_definition(col: Collection, prefix: str | None = None) -> tuple[dict, dict]:
    """(the definition record an object collection's directory makes, its File records by id): the manifest,
    query templates and vocabulary as parsed, each file a DAPPER File. `prefix` is the collection directory's
    path in its repository, as a push sends it (each File's location is `prefix/relpath`); None asks git, for
    a directory on this machine. A File's id does not depend on its location."""
    from .publish import SourceFile, _source_file_doc

    if not col.is_objects:
        raise ManifestError(f"{col.key} is not an object collection (kind: objects)")
    try:
        vocab = collection_vocabulary(col.doc, col.root)
    except MappingError as exc:
        raise ManifestError(str(exc)) from exc
    files: dict[str, tuple[str, dict]] = {}

    def keep(doc):
        files[doc["id"]] = ("File", doc)
        return doc["id"]

    def at(rel: str) -> str | None:
        return None if prefix is None else (f"{prefix}/{rel}" if prefix else rel)

    manifest_file = keep(_source_file_doc(col.manifest, f"{col.key} collection manifest.",
                                          location=at(col.manifest.rel)))
    queries = {name: {"file_id": keep(_source_file_doc(q, f"{col.key} query template {name}.", location=at(q.rel))),
                      "path": q.rel, "doc": q.doc} for name, q in col.queries.items()}
    vocab_files = []
    for block, dim, rel in vocabulary_files(col.doc):
        path = (col.root / rel).resolve()
        what = "mapping" if block == "mappings" else "cross-references"
        vocab_files.append(keep(_source_file_doc(SourceFile(path=path, rel=rel, data=path.read_bytes(), doc={}),
                                                 f"{col.key} {what} for {dim}.", "text/tab-separated-values",
                                                 location=at(rel))))
    # object_kinds and queries are optional now, but a build from before that (QA and prod share the database and
    # deploy apart) reads manifest["object_kinds"] / ["queries"] of every current release: store them, empty.
    manifest = {**col.doc, "object_kinds": col.doc.get("object_kinds") or {}, "queries": col.doc.get("queries") or []}
    record = {"kind": "objects", "manifest": manifest, "templates": [], "queries": queries,
              "manifest_file_id": manifest_file, "vocabulary_file_ids": vocab_files,
              "mappings": {dim: {k: list(v) for k, v in m.items()} for dim, m in vocab.mappings.items()},
              "vocabulary": vocab.entries}
    return json.loads(json.dumps(record)), files


def store_definition(con, col: Collection, record: dict, files: dict, git_commit: str | None) -> dict:
    """Record a definition (build_definition) as the collection's: replaces any registered one. No commit."""
    from .publish import _store_objects

    _store_objects(con, files)
    con.execute("DELETE FROM object_collections WHERE collection_key = ?", (col.key,))
    con.execute("INSERT INTO object_collections (collection_key, definition, git_commit) VALUES (?, ?, ?)",
                (col.key, json.dumps(record), git_commit))
    if con.execute("SELECT 1 FROM collections WHERE collection_key = ?", (col.key,)).fetchone():
        con.execute("UPDATE collections SET title = ?, description = ? WHERE collection_key = ?",
                    (col.doc["title"], col.doc.get("description"), col.key))
    else:
        con.execute("INSERT INTO collections (collection_key, title, description, current_release_id) "
                    "VALUES (?, ?, ?, NULL)", (col.key, col.doc["title"], col.doc.get("description")))
    return {"collection": col.key, "queries": sorted(record["queries"]), "files": len(files)}


def register(con, col: Collection, git_commit: str | None = None, prefix: str | None = None) -> dict:
    """Record an object collection's definition from its directory: the manifest, query templates and
    vocabulary as parsed, each file stored as a DAPPER File. Registering again replaces the definition;
    releases already opened keep the one they were opened with."""
    record, files = build_definition(col, prefix)
    with con:
        return store_definition(con, col, record, files, git_commit)


def definition(con, key: str) -> dict | None:
    row = con.execute("SELECT definition FROM object_collections WHERE collection_key = ?", (key,)).fetchone()
    return _json(row["definition"]) if row else None


# --- releases ----------------------------------------------------------------------------------------------

def _release(con, key: str, version: str, lock: bool = False):
    return con.execute("SELECT * FROM object_releases WHERE collection_key = ? AND version = ?"
                       + (con.dialect.lock_share if lock else ""), (key, version)).fetchone()


def _state(row) -> dict:
    carried = _json(row["definition"]).get("carried_from") if row["definition"] else None
    return {"collection": row["collection_key"], "version": row["version"], "state": row["state"],
            "release_id": row["release_id"], "opened_at": str(row["opened_at"]),
            "closed_at": str(row["closed_at"]) if row["closed_at"] else None,
            "carried_from": carried}


def _granted(con, depositor: dict, key: str) -> None:
    from .. import api_keys

    if api_keys.is_login(depositor) and not api_keys.still_valid(con, depositor):
        # re-checked inside the open/close transaction, as a deposit re-checks it (api_keys.still_valid)
        raise NotGranted("this claims login was revoked or has expired: run `claims login` again")
    if not api_keys.has_grant(con, depositor, key):
        raise NotGranted(f"this key may not write into the collection {key!r}: grant it in the admin console")


def _audit(con, event: str, email: str, detail: str) -> None:
    from ..device_login import _audit as audit

    audit(con, event, email, detail)


def open_release(con, depositor: dict, key: str, version: str, carry_from: str | None = None) -> tuple[dict, bool]:
    """(release state, whether this call opened it). Opening an open release again is a no-op. Who opened it is
    recorded: a key's id, or a login's token id and email (audited as object_release_opened).

    `carry_from` names a closed release of the same collection whose members the new release starts with, so
    a revision deposits only what is new or changed: its membership is copied in the same transaction, keeping
    each member's original deposit, and its indexed fields are copied where the class's field spec is unchanged
    and recomputed from the stored documents under the new definition otherwise. The release records what it
    carried (`carried_from`: version and count)."""
    from .. import api_keys

    if not isinstance(version, str) or not version or len(version) > 64 or not version.isascii() or " " in version:
        raise ValueError("version must be a short ASCII string without spaces, e.g. 2026.11.0")
    if carry_from is not None and (not isinstance(carry_from, str) or not carry_from or carry_from == version):
        raise ValueError("carry_from names another, closed release of this collection, e.g. 2026.11.0")
    with con:
        _granted(con, depositor, key)
        defn = con.execute("SELECT definition FROM object_collections WHERE collection_key = ?", (key,)).fetchone()
        if defn is None:
            raise CollectionNotFound(f"no object collection {key!r} is registered")
        row = _release(con, key, version, lock=True)
        if row is not None:
            if row["state"] != "open":
                raise ReleaseError(f"release {version} of {key} is closed; open a new version")
            recorded = (_json(row["definition"]).get("carried_from") or {}).get("version")
            if carry_from is not None and recorded != carry_from:
                raise ReleaseError(f"release {version} of {key} is already open"
                                   + (f", carried from {recorded}" if recorded else " without carried members"))
            return _state(row), False
        if con.execute("SELECT 1 FROM collection_releases WHERE collection_key = ? AND version = ?",
                       (key, version)).fetchone():
            raise ReleaseError(f"{key} already has a release {version}")
        definition = _json(defn["definition"])
        source = None
        if carry_from is not None:
            source = _release(con, key, carry_from, lock=True)
            if source is None:
                raise ValueError(f"{key} has no release {carry_from} to carry members from")
            if source["state"] != "closed":
                raise ValueError(f"release {carry_from} of {key} is still open; close it before carrying from it")
            count = con.execute("SELECT COUNT(*) AS n FROM release_objects WHERE collection_key = ? AND version = ?",
                                (key, carry_from)).fetchone()["n"]
            definition["carried_from"] = {"version": carry_from, "objects": int(count)}
        key_id, token_id, email = api_keys.writer_columns(depositor)
        con.execute(f"INSERT INTO object_releases (collection_key, version, state, definition, opened_at, opened_by, "
                    f"opened_by_token_id, opened_by_email) VALUES (?, ?, 'open', ?, {con.dialect.now}, ?, ?, ?)",
                    (key, version, json.dumps(definition), key_id, token_id, email))
        if source is not None:
            carry_members(con, key, carry_from, version, _json(source["definition"]), definition)
        if token_id:
            _audit(con, "object_release_opened", email, f"{key} {version} with {token_id}"
                   + (f", carrying {carry_from}" if source is not None else ""))
        return _state(_release(con, key, version)), True


def carry_members(con, key: str, source: str, version: str, old_defn: dict, new_defn: dict) -> None:
    """Copy release `source`'s members into `version`, and their indexed fields under `new_defn`."""
    con.execute(f"{con.dialect.insert_or_ignore} release_objects (collection_key, version, object_id, class_name, "
                "deposit_id) SELECT collection_key, ?, object_id, class_name, deposit_id FROM release_objects "
                "WHERE collection_key = ? AND version = ?", (version, key, source))
    old_kinds = old_defn["manifest"].get("object_kinds") or {}
    new_kinds = new_defn["manifest"].get("object_kinds") or {}
    classes = [r["class_name"] for r in con.execute(
        "SELECT DISTINCT class_name FROM release_objects WHERE collection_key = ? AND version = ?", (key, version))]
    for cls in classes:
        spec = new_kinds.get(cls)
        if not spec or not spec.get("fields"):
            continue
        old = old_kinds.get(cls) or {}
        if old.get("fields") == spec["fields"]:
            con.execute(f"{con.dialect.insert_or_ignore} release_object_fields (collection_key, version, object_id, "
                        "field, value, num) SELECT f.collection_key, ?, f.object_id, f.field, f.value, f.num "
                        "FROM release_object_fields f JOIN release_objects o ON o.collection_key = f.collection_key "
                        "AND o.version = f.version AND o.object_id = f.object_id "
                        "WHERE f.collection_key = ? AND f.version = ? AND o.class_name = ?", (version, key, source, cls))
            continue
        ids = [r["object_id"] for r in con.execute(
            "SELECT object_id FROM release_objects WHERE collection_key = ? AND version = ? AND class_name = ?",
            (key, version, cls))]
        for start in range(0, len(ids), CARRY_BATCH):
            batch = ids[start:start + CARRY_BATCH]
            rows = con.execute("SELECT id, payload FROM dapper_objects WHERE id IN (" + ",".join("?" * len(batch))
                               + ")", batch).fetchall()
            fields = sorted({f for r in rows for f in field_rows(key, version, spec, r["id"], json.loads(r["payload"]))})
            if fields:
                con.executemany(f"{con.dialect.insert_or_ignore} release_object_fields (collection_key, version, "
                                "object_id, field, value, num) VALUES (?, ?, ?, ?, ?, ?)", fields)


def target(con, depositor: dict, key: str, version: str | None) -> tuple[str, dict]:
    """Inside a deposit's transaction: the open release a deposit naming `key` writes into, and its
    definition. Checks the grant and the release state as locking reads, so a release closed (or a grant
    withdrawn) before this point takes no part, and one closing after it waits for the part."""
    _granted(con, depositor, key)
    if version is None:
        rows = con.execute("SELECT version FROM object_releases WHERE collection_key = ? AND state = 'open'",
                           (key,)).fetchall()
        if len(rows) != 1:
            raise ReleaseError(f"{key} has {len(rows)} open releases; name one with `release`" if rows else
                               f"{key} has no open release: open one (POST /v1/collections/{key}/releases) "
                               "before depositing into it")
        version = rows[0]["version"]
    row = _release(con, key, version, lock=True)
    if row is None:
        raise ReleaseError(f"{key} has no release {version}: open it first")
    if row["state"] != "open":
        raise ReleaseError(f"release {version} of {key} is closed; it takes no more deposits")
    return version, _json(row["definition"])


def _values(value) -> list:
    if isinstance(value, list):
        return [v for x in value for v in _values(x)]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    if isinstance(value, (str, int, float)):
        return [value]
    return []


def field_rows(key: str, version: str, spec: dict, object_id: str, document: dict) -> list[tuple]:
    """The release_object_fields rows one member's document gives under its kind's field spec."""
    rows = []
    for name, ref in spec["fields"].items():
        for v in _values(document.get(ref["slot"])):
            num = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            rows.append((key, version, object_id, name, str(v)[:MAX_VALUE], num))
    return rows


def add_objects(con, key: str, version: str, defn: dict, deposit_id: str, items: list[dict]) -> int:
    """Record deposited objects as members of the release, and index the fields its object kinds declare.
    Idempotent: a member already recorded keeps its first deposit."""
    kinds = defn["manifest"].get("object_kinds") or {}  # none: members only, no indexed fields
    members, fields = [], []
    for item in items:
        members.append((key, version, item["id"], item["class_name"], deposit_id))
        spec = kinds.get(item["class_name"])
        if spec is None:
            continue
        fields += field_rows(key, version, spec, item["id"], item["document"])
    con.executemany(f"{con.dialect.insert_or_ignore} release_objects (collection_key, version, object_id, class_name, "
                    "deposit_id) VALUES (?, ?, ?, ?, ?)", members)
    if fields:
        con.executemany(f"{con.dialect.insert_or_ignore} release_object_fields (collection_key, version, object_id, "
                        "field, value, num) VALUES (?, ?, ?, ?, ?, ?)", sorted(set(fields)))
    return len(members)


def release_status(con, key: str, version: str) -> dict:
    row = _release(con, key, version)
    if row is None:
        raise CollectionNotFound(f"{key} has no release {version}")
    counts = {r["class_name"]: r["n"] for r in con.execute(
        "SELECT class_name, COUNT(*) AS n FROM release_objects WHERE collection_key = ? AND version = ? "
        "GROUP BY class_name ORDER BY class_name", (key, version))}
    deposits = con.execute("SELECT COUNT(*) AS n FROM deposits WHERE collection_key = ? AND release_version = ?",
                           (key, version)).fetchone()["n"]
    return {**_state(row), "objects": sum(counts.values()), "classes": counts, "deposits": deposits}


def close_release(con, depositor: dict, key: str, version: str) -> tuple[dict, bool]:
    """(release status, whether this call closed it). Closing mints the release's ResultCollection, records
    the release, and makes it the collection's current one. Closing a closed release again is a no-op. A login's
    close records its email and token id as the release's publisher (audited as object_release_closed); a key's
    records its id on the object release."""
    from .. import api_keys
    from .publish import _store_objects

    with con:
        _granted(con, depositor, key)
        # Flip the state first: the update takes the release row's write lock, so a part in flight (which holds
        # a share lock on it) finishes before the members are read, and a part arriving later sees it closed.
        key_id, token_id, email = api_keys.writer_columns(depositor)
        flipped = con.execute(f"UPDATE object_releases SET state = 'closed', closed_at = {con.dialect.now}, "
                              "closed_by = ?, closed_by_token_id = ?, closed_by_email = ? "
                              "WHERE collection_key = ? AND version = ? AND state = 'open'",
                              (key_id, token_id, email, key, version)).rowcount
        row = _release(con, key, version)
        if row is None:
            raise CollectionNotFound(f"{key} has no release {version}")
        if not flipped:
            return release_status(con, key, version), False
        defn = _json(row["definition"])
        members = [r["object_id"] for r in con.execute(
            "SELECT object_id FROM release_objects WHERE collection_key = ? AND version = ? ORDER BY object_id",
            (key, version))]
        if not members:
            raise ReleaseError(f"release {version} of {key} holds no objects; deposit into it before closing")
        current = con.execute("SELECT current_release_id FROM collections WHERE collection_key = ?",
                              (key,)).fetchone()
        m = defn["manifest"]
        # DAPPER's ResultCollection holds Datasets of files. Here each Dataset is one class of member objects,
        # and its File is the sorted list of their ids (served at the File's location), pinned by its sha256.
        by_class: dict[str, list[str]] = {}
        for r in con.execute("SELECT object_id, class_name FROM release_objects WHERE collection_key = ? AND "
                             "version = ? ORDER BY class_name, object_id", (key, version)):
            by_class.setdefault(r["class_name"], []).append(r["object_id"])
        datasets, new = [], {}
        for cls, ids in sorted(by_class.items()):
            data = members_file(ids)
            f = idn.mint("File", {"filename": f"{key}-{version}-{cls}.txt", "mime_type": "text/plain",
                                  "description": f"The ids of the {cls} objects in {key} {version}, one per line.",
                                  "sha256": hashlib.sha256(data).hexdigest(), "size_in_bytes": len(data),
                                  "location": f"/v1/collections/{key}/releases/{version}/members?class={cls}"})
            ds = idn.mint("Dataset", {"name": f"{m['title']} {version}: {cls}", "version": version,
                                      "description": f"The {len(ids):,} {cls} objects deposited into {key} {version}.",
                                      "has_file": [f["id"]],
                                      "dimension_values": [{"dimension": "class", "dimension_value": cls}],
                                      "file_roles": [{"role": "members", "file": f["id"]}]})
            new[f["id"]], new[ds["id"]] = ("File", f), ("Dataset", ds)
            datasets.append(ds)
        rc = idn.mint("ResultCollection", {
            "name": f"{m['title']} {version}",
            "description": m.get("description"),
            "collection_key": key,
            "version": version,
            "methods": m["methods"],
            "dataset_dimensions": ["class"],
            "members": [d["id"] for d in datasets],
            "n_members": len(datasets),
            "has_manifest_file": defn["manifest_file_id"],
            "has_query_template": sorted(q["file_id"] for q in (defn.get("queries") or {}).values()) or None,
            "was_revision_of": (current["current_release_id"] if current else None) or None,
        })
        for doc in (rc, *datasets, *(d for c, d in new.values() if c == "File")):
            dval.validate_node(doc)
        files = [json.loads(r["payload"]) for r in con.execute(
            "SELECT payload FROM dapper_objects WHERE id IN (" + ",".join("?" * (1 + len(defn["queries"]) +
                                                                               len(defn["vocabulary_file_ids"])))
            + ")", [defn["manifest_file_id"], *(q["file_id"] for q in defn["queries"].values()),
                    *defn["vocabulary_file_ids"]])]
        files += [d for c, d in new.values() if c == "File"]
        document = {"result_collections": [rc], "datasets": sorted(datasets, key=lambda d: d["id"]),
                    "files": sorted(files, key=lambda f: f["id"])}
        _store_objects(con, {rc["id"]: ("ResultCollection", rc), **new})
        con.execute("INSERT INTO collection_releases (release_id, collection_key, version, manifest, document, "
                    "git_commit, published_by, publisher_token_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rc["id"], key, version, json.dumps({**defn, "objects": len(members)}), json.dumps(document),
                     None, email, token_id))
        if token_id:
            _audit(con, "object_release_closed", email, f"{key} {version} as {rc['id']} with {token_id}")
        con.execute("UPDATE object_releases SET release_id = ? WHERE collection_key = ? AND version = ?",
                    (rc["id"], key, version))
        con.execute("UPDATE collections SET current_release_id = ? WHERE collection_key = ?", (rc["id"], key))
        status = release_status(con, key, version)
        missing = _unresolved(con, members)
        return {**status, "unresolved_references": missing}, True


def members_file(ids: list[str]) -> bytes:
    """The bytes of a release's members File for one class: sorted ids, one per line."""
    return "".join(f"{i}\n" for i in sorted(ids)).encode()


def member_ids(con, key: str, version: str, cls: str) -> list[str]:
    return [r["object_id"] for r in con.execute(
        "SELECT object_id FROM release_objects WHERE collection_key = ? AND version = ? AND class_name = ? "
        "ORDER BY object_id", (key, version, cls))]


def _unresolved(con, members: list[str]) -> list[str]:
    """References from the release's members to ids this repository does not hold (the first 100), as a deposit
    receipt reports them."""
    targets = set()
    for i in range(0, len(members), 500):
        chunk = members[i:i + 500]
        targets |= {r["to_id"] for r in con.execute(
            f"SELECT DISTINCT to_id FROM object_refs WHERE from_id IN ({','.join('?' * len(chunk))})", chunk)}
    targets = sorted(targets)
    held = set()
    for i in range(0, len(targets), 500):
        chunk = targets[i:i + 500]
        held |= {r["id"] for r in con.execute(
            f"SELECT id FROM dapper_objects WHERE id IN ({','.join('?' * len(chunk))})", chunk)}
    return sorted(set(targets) - held)[:100]


# --- discovery ---------------------------------------------------------------------------------------------

def describe(con, key: str, rel, record: dict) -> dict:
    m = record["manifest"]
    counts = {r["class_name"]: r["n"] for r in con.execute(
        "SELECT class_name, COUNT(*) AS n FROM release_objects WHERE collection_key = ? AND version = ? "
        "GROUP BY class_name ORDER BY class_name", (key, rel["version"]))}
    releases = [_state(r) for r in con.execute(
        "SELECT * FROM object_releases WHERE collection_key = ? ORDER BY opened_at, version", (key,))]
    return {
        "id": key, "kind": "objects", "title": m["title"], "description": m.get("description"),
        "owners": m.get("owners", []), "methods": m["methods"], "dapper_profile": m["dapper_profile"],
        "release": {"id": rel["release_id"], "version": rel["version"], "git_commit": rel["git_commit"]},
        "releases": releases,
        "object_kinds": m.get("object_kinds") or {},
        "objects": {"total": sum(counts.values()), "classes": counts},
        "queries": sorted(record["queries"]),
        "vocabulary": sorted((record.get("mappings") or {})),
        "dapper_document": f"/v1/collections/{key}/releases/{rel['version']}/document",
    }


def field_values(con, key: str, rel, record: dict, field: str, search: str | None, first: int | None,
                 max_values: int) -> dict:
    if field not in {f for k in (record["manifest"].get("object_kinds") or {}).values() for f in k["fields"]}:
        raise CollectionNotFound(f"collection {key!r} has no field {field!r}")
    rows = con.execute("SELECT value, COUNT(*) AS n FROM release_object_fields WHERE collection_key = ? AND "
                       "version = ? AND field = ? GROUP BY value ORDER BY value", (key, rel["version"], field)).fetchall()
    aliases: dict[str, set] = {}
    for k, target in (record.get("mappings") or {}).get(field, {}).items():
        aliases.setdefault(target[0], set()).add(k)
    entries = (record.get("vocabulary") or {}).get(field, {})
    out, needle = [], (search or "").lower()
    for r in rows:
        entry = entries.get(r["value"]) or {}
        xrefs = entry.get("xrefs") or []
        also = sorted(aliases.get(r["value"], ()))
        hay = (r["value"], entry.get("label"), *also, *(x["target_id"] for x in xrefs), *(x["label"] for x in xrefs))
        if needle and not any(needle in (s or "").lower() for s in hay):
            continue
        item = {"value": r["value"], "label": entry.get("label") or r["value"], "objects": r["n"],
                **{k: v for k, v in entry.items() if k not in ("label", "xrefs")}}
        if xrefs:
            item["xrefs"] = xrefs
        if also:
            item["also_accepts"] = also
        out.append(item)
    limit = min(first if first is not None else max_values, max_values)
    return {"collection": key, "release": {"id": rel["release_id"], "version": rel["version"]},
            "dimension": field, "count": len(out), "values": out[:limit], "truncated": len(out) > limit}


# --- the executor ------------------------------------------------------------------------------------------

def _bind(value, bound):
    if isinstance(value, str) and value.startswith("$"):
        return bound[value[1:]]
    return value


def _resolve(record: dict, field: str, raw):
    mapped = (record.get("mappings") or {}).get(field, {}).get(raw) if isinstance(raw, str) else None
    return mapped[0] if mapped else raw


def _conditions(con, alias: str, where: dict | None, bound: dict, record: dict) -> tuple[list[str], list]:
    """SQL conditions (one EXISTS per field condition) for a step's `where`; unset variables drop theirs."""
    sql, params = [], []
    for field, conds in sorted((where or {}).items()):
        for op, raw in sorted(conds.items()):
            value = _bind(raw, bound)
            if value is None:
                continue
            base = (f"SELECT 1 FROM release_object_fields f WHERE f.collection_key = {alias}.collection_key AND "
                    f"f.version = {alias}.version AND f.object_id = {alias}.object_id AND f.field = ?")
            if op == "contains":
                from ..db import like_escape

                sql.append(f"EXISTS ({base} AND {con.dialect.like_ci('f.value')})")
                params += [field, f"%{like_escape(str(value))}%"]
            elif op in ("eq", "ne"):
                value = _resolve(record, field, value)
                clause = f"EXISTS ({base} AND f.value = ?)"
                sql.append(clause if op == "eq" else f"NOT {clause}")
                params += [field, str(value)]
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ManifestError(f"{field} {op} needs a number, got {value!r}")
                sql.append(f"EXISTS ({base} AND f.num {FILTER_OPS[op]} ?)")
                params += [field, value]
    return sql, params


def _order(alias: str, order: dict | None) -> tuple[str, list]:
    if not order:
        return f"{alias}.object_id ASC", []
    d = "DESC" if order.get("dir") == "desc" else "ASC"
    sub = (f"FROM release_object_fields f WHERE f.collection_key = {alias}.collection_key AND f.version = "
           f"{alias}.version AND f.object_id = {alias}.object_id AND f.field = ?")
    return (f"(SELECT MIN(f.num) {sub}) {d}, (SELECT MIN(f.value) {sub}) {d}, {alias}.object_id ASC",
            [order["field"], order["field"]])


def _view(payload: dict, cls: str, record: dict) -> dict:
    from ..objects import present

    spec = (record["manifest"].get("object_kinds") or {}).get(cls) or {}
    return {"class": cls, "id": payload["id"],
            "fields": {n: payload.get(r["slot"]) for n, r in spec.get("fields", {}).items()},
            "object": present(payload)}


def _payloads(con, ids) -> dict:
    from ..objects import _payloads as fetch

    return fetch(con, ids)


def run_query(con, collection: str, name: str, variables: dict | None, release: str | None, rel, record: dict) -> dict:
    if name not in record["queries"]:
        raise CollectionNotFound(f"collection {collection!r} has no query {name!r}; it has {sorted(record['queries'])}")
    qdoc = record["queries"][name]["doc"]
    bound = bind_params(qdoc, variables)
    kinds = record["manifest"].get("object_kinds") or {}
    version = rel["version"]
    a = qdoc["plan"][0]["anchor"]
    conds, params = _conditions(con, "ro", a.get("where"), bound, record)
    order_sql, order_params = _order("ro", a.get("order"))
    limit = _bind(a["limit"], bound)
    rows = con.execute(
        "SELECT ro.object_id, ro.class_name FROM release_objects ro WHERE ro.collection_key = ? AND ro.version = ? "
        "AND ro.class_name = ?" + "".join(f" AND {c}" for c in conds) + f" ORDER BY {order_sql} LIMIT ?",
        [collection, version, a["class"], *params, *order_params, limit]).fetchall()
    ids = [r["object_id"] for r in rows]
    docs = _payloads(con, ids)
    out = [_view(docs[i], a["class"], record) for i in ids if i in docs]
    warnings: list[str] = []
    if not out:
        warnings.append(f"no {a['class']} in {collection} release {version} matches; check the values with "
                        f"GET /v1/collections/{collection}/dimensions/<field>")
    for step in qdoc["plan"][1:]:
        if "follow" in step:
            s = step["follow"]
            cap = min(_bind(s.get("limit_per_anchor", MAX_JOIN), bound) or 0, MAX_JOIN)
            want = {}
            for row in out:
                slot = kinds[row["class"]]["links"][s["link"]]["slot"]
                refs = [v for v in _values(row["object"].get(slot)) if isinstance(v, str) and DAPPER_ID_RE.match(v)]
                want[row["id"]] = list(dict.fromkeys(refs))
            got = _payloads(con, [i for refs in want.values() for i in refs])
            members = _member_classes(con, collection, version, list(got))
            allowed = _filter_ids(con, collection, version, list(got), s.get("where"), bound, record)
            for row in out:
                items = []
                for i in want[row["id"]]:
                    if i not in got:
                        continue
                    cls = members.get(i) or idn.class_of(i)
                    if s.get("class") and cls != s["class"]:
                        continue
                    if s.get("where") and i not in allowed:
                        continue
                    items.append(_view(got[i], cls, record))
                row[s["as"]] = items[:cap]
        else:
            s = step["referenced_by"]
            cap = min(_bind(s.get("limit_per_anchor", MAX_JOIN), bound) or 0, MAX_JOIN)
            slot = kinds[s["class"]]["links"][s["link"]]["slot"]
            c2, p2 = _conditions(con, "ro", s.get("where"), bound, record)
            o2, op2 = _order("ro", s.get("order"))
            for row in out:
                found = con.execute(
                    "SELECT ro.object_id FROM release_objects ro JOIN object_refs r ON r.from_id = ro.object_id AND "
                    "r.to_id = ? WHERE ro.collection_key = ? AND ro.version = ? AND ro.class_name = ?"
                    + "".join(f" AND {c}" for c in c2) + f" ORDER BY {o2} LIMIT ?",
                    [row["id"], collection, version, s["class"], *p2, *op2, MAX_JOIN * 5]).fetchall()
                got = _payloads(con, [r["object_id"] for r in found])
                items = []
                for r in found:  # the reference must sit in the named link's slot
                    doc = got.get(r["object_id"])
                    if doc and row["id"] in _values(doc.get(slot)):
                        items.append(_view(doc, s["class"], record))
                row[s["as"]] = items[:cap]
    return {"collection": collection, "release": {"id": rel["release_id"], "version": version}, "query": name,
            "variables": bound, "rows": out, "warnings": warnings}


def _member_classes(con, key, version, ids) -> dict:
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update({r["object_id"]: r["class_name"] for r in con.execute(
            f"SELECT object_id, class_name FROM release_objects WHERE collection_key = ? AND version = ? AND "
            f"object_id IN ({','.join('?' * len(chunk))})", [key, version, *chunk])})
    return out


def _filter_ids(con, key, version, ids, where, bound, record) -> set:
    if not where or not ids:
        return set(ids)
    conds, params = _conditions(con, "ro", where, bound, record)
    out = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out |= {r["object_id"] for r in con.execute(
            "SELECT ro.object_id FROM release_objects ro WHERE ro.collection_key = ? AND ro.version = ? AND "
            f"ro.object_id IN ({','.join('?' * len(chunk))})" + "".join(f" AND {c}" for c in conds),
            [key, version, *chunk, *params])}
    return out


def current(con, key: str, release: str | None = None):
    """(release row, record) of an object collection's named or current closed release."""
    rel = release_row(con, key, release)
    return rel, _json(rel["manifest"])


__all__ = ["register", "definition", "open_release", "target", "add_objects", "close_release", "release_status",
           "describe", "field_values", "run_query", "current", "is_objects", "ReleaseError", "NotGranted",
           "dapper_refs"]
