"""Publishing a release into this service with a `claims login` token (docs/publishing-to-a-deployment.md).

The publishing machine reads and checks the result files (publish.read_dataset) and sends what they hold,
one dataset per request. This module computes everything else itself (every identifier, gene set, metric
and mint context, and the collection's own File records) and stores through publish.SqlTarget, the same
write path scripts/publish_collection.py takes, so a pushed release and a directly published one are the
same rows. A request that would change something recorded is refused; one repeated after a lost answer
changes nothing.

    POST /v1/publish/{collection}/{version}:begin                 the collection's files -> bundle digest, state
    POST /v1/publish/{collection}/{version}:check                 which datasets and mint contexts are stored
    PUT  /v1/publish/{collection}/{version}/datasets/{dataset_id}  one dataset
    POST /v1/publish/{collection}/{version}:finish                the release: recorded, and made current
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import db, device_login
from .collections import identity as idn
from .collections import publish as pub
from .collections.manifest import Collection, ManifestError, load_collection
from .collections.traits import MappingError, collection_vocabulary, resolve_dimension, unmapped_policy, vocabulary_files

router = APIRouter(prefix="/v1/publish", tags=["Publish"])

MAX_BODY = 16 * 1024 * 1024        # as sent (gzip allowed)
MAX_INFLATED = 160 * 1024 * 1024   # after gunzip
MAX_BUNDLE_FILES = 200
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_CHECK = 50_000
KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
PATH_PART_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
DOCUMENT_GROUPS = {"files": "File", "activities": "Activity", "datasets": "Dataset"}


class Conflict(pub.PublishError):
    """The push contradicts something already recorded (409)."""


# --- the request -------------------------------------------------------------------

async def _body(request: Request) -> dict:
    """The JSON body: at most MAX_BODY bytes as sent, gzip (Content-Encoding) allowed up to MAX_INFLATED."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(status_code=413, detail=f"A publish request is at most {MAX_BODY:,} bytes as sent.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(status_code=413, detail=f"A publish request is at most {MAX_BODY:,} bytes as sent.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding == "gzip":
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = inflater.decompress(raw, MAX_INFLATED)
        except zlib.error:
            raise HTTPException(status_code=400, detail="The body is not valid gzip.")
        if inflater.unconsumed_tail:
            raise HTTPException(status_code=413, detail=f"A publish request is at most {MAX_INFLATED:,} bytes unzipped.")
    elif encoding not in ("", "identity"):
        raise HTTPException(status_code=415, detail="Send the body as JSON, gzip-encoded or not.")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="The body is not JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="The body is a JSON object.")
    return body


def _names(collection: str, version: str) -> None:
    if not KEY_RE.match(collection):
        raise HTTPException(status_code=404, detail=f"{collection!r} is not a collection key.")
    if not VERSION_RE.match(version):
        raise HTTPException(status_code=400, detail=f"{version!r} is not a release version.")


def _call(fn, *args):
    """Run fn(con, *args) on its own connection; publish errors become 400 or 409."""
    con = db.connect()
    try:
        return fn(con, *args)
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (pub.PublishError, ManifestError, MappingError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        con.close()


# --- the collection a push names -----------------------------------------------------

@dataclass
class Bundle:
    digest: str
    col: Collection
    prefix: str
    manifest_file: dict
    template_files: dict
    query_files: dict

    def vocab(self):
        """A fresh vocabulary (it records unresolved keys as it is used)."""
        return collection_vocabulary(self.col.doc, self.col.root)


_BUNDLES: OrderedDict[str, Bundle] = OrderedDict()
_BUNDLES_LOCK = threading.Lock()
_BUNDLE_DIR = Path(tempfile.mkdtemp(prefix="claims-publish-bundles-"))


def _bundle_files(body: dict) -> tuple[dict, str]:
    files, prefix = body.get("files"), body.get("prefix", "")
    if not isinstance(files, dict) or not files or len(files) > MAX_BUNDLE_FILES:
        raise pub.PublishError(f"send the collection's files as {{relative path: text}} (1 to {MAX_BUNDLE_FILES})")
    total = 0
    for rel, text in files.items():
        if not (isinstance(rel, str) and isinstance(text, str) and len(rel) <= 255
                and all(PATH_PART_RE.match(part) and part not in (".", "..") for part in rel.split("/"))):
            raise pub.PublishError(f"{rel!r}: a collection file is a relative path (letters, digits, . _ -) and its text")
        total += len(text.encode())
    if total > MAX_BUNDLE_BYTES:
        raise pub.PublishError(f"the collection's files are {total:,} bytes; at most {MAX_BUNDLE_BYTES:,}")
    if "collection.yaml" not in files:
        raise pub.PublishError("the collection's files hold no collection.yaml")
    if not (isinstance(prefix, str) and len(prefix) <= 255 and
            (prefix == "" or all(PATH_PART_RE.match(p) and p not in (".", "..") for p in prefix.split("/")))):
        raise pub.PublishError("prefix is the collection directory's path in its repository, or empty")
    return files, prefix


def _digest(files: dict, prefix: str) -> str:
    return hashlib.sha256(json.dumps({"files": files, "prefix": prefix}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _load(digest: str, files: dict, prefix: str) -> Bundle:
    """Load a bundle's collection from its files (written under a directory of its own)."""
    root = _BUNDLE_DIR / digest
    if not root.exists():
        tmp = Path(tempfile.mkdtemp(dir=_BUNDLE_DIR))
        for rel, text in files.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text)
        try:
            tmp.rename(root)
        except OSError:  # another request wrote it first
            pass
    col = load_collection(root)
    if col.is_objects:
        raise pub.PublishError(f"{col.key} is an object collection: its releases are built from deposits")
    names = {"collection.yaml", *(t.rel for t in col.templates), *(q.rel for q in col.queries.values()),
             *(rel for _, _, rel in vocabulary_files(col.doc))}
    extra = sorted(set(files) - names)
    if extra:
        raise pub.PublishError(f"files the collection does not use: {extra[:5]}")
    collection_vocabulary(col.doc, col.root)  # the mappings load
    manifest_file, template_files, query_files = pub.collection_file_docs(col, prefix)
    return Bundle(digest, col, prefix, manifest_file, template_files, query_files)


def _remember(bundle: Bundle) -> Bundle:
    with _BUNDLES_LOCK:
        _BUNDLES[bundle.digest] = bundle
        _BUNDLES.move_to_end(bundle.digest)
        while len(_BUNDLES) > 8:
            _BUNDLES.popitem(last=False)
    return bundle


def _bundle(con, collection: str, digest) -> Bundle:
    """The bundle a request names (stored by :begin)."""
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise pub.PublishError("name the push's bundle (the digest :begin answered)")
    with _BUNDLES_LOCK:
        cached = _BUNDLES.get(digest)
    if cached is None:
        row = con.execute("SELECT collection_key, files FROM publish_bundles WHERE bundle_sha256 = ?",
                          (digest,)).fetchone()
        if row is None:
            raise pub.PublishError(f"no bundle {digest}: start the push with :begin")
        stored = json.loads(row["files"])
        cached = _remember(_load(digest, stored["files"], stored["prefix"]))
    if cached.col.key != collection:
        raise pub.PublishError(f"bundle {digest[:12]} is the collection {cached.col.key}, not {collection}")
    return cached


# --- what a push sends -------------------------------------------------------------

def _verified(document, cls: str | None = None) -> dict:
    if not isinstance(document, dict) or not idn.verify(document):
        what = document.get("id") if isinstance(document, dict) else document
        raise pub.PublishError(f"{str(what)[:120]!r} is not a DAPPER document whose id is its content address")
    if cls and idn.class_of(document["id"]) != cls:
        raise pub.PublishError(f"{document['id']} is not a {cls}")
    return document


def _where(col: Collection, where) -> dict:
    """The dataset's bounds, as where_for gives them: {row kind: {metric: number}}."""
    if where in (None, {}):
        return {}
    if not isinstance(where, dict):
        raise pub.PublishError("where is {row kind: {metric: bound}}")
    out = {}
    for kind, bounds in where.items():
        rk = col.doc["row_kinds"].get(kind)
        if rk is None or not isinstance(bounds, dict) or not bounds:
            raise pub.PublishError(f"where names unknown row kind {kind!r}")
        for metric, bound in bounds.items():
            if metric not in rk["metrics"] or isinstance(bound, bool) or not isinstance(bound, (int, float)) \
                    or not math.isfinite(bound):
                raise pub.PublishError(f"where.{kind}.{metric}: not a metric of that row kind with a finite bound")
            out.setdefault(kind, {})[metric] = float(bound)
    return out


def _dataset(bundle: Bundle, vocab, dataset_id: str, sent) -> dict:
    """The dataset a push describes, as publish() holds it, once its Dataset and File records are what this
    collection makes of the values sent: dimensions through the collection's own mappings, the bounds, and
    each file's name, sha256, size and location."""
    col = bundle.col
    if not isinstance(sent, dict):
        raise pub.PublishError("dataset is {raw_dimensions, files, where, was_generated_by}")
    raw = sent.get("raw_dimensions")
    names = [d["name"] for d in col.doc["dataset_dimensions"]]
    if not isinstance(raw, dict) or sorted(raw) != sorted(names) or not all(isinstance(v, str) for v in raw.values()):
        raise pub.PublishError(f"raw_dimensions must be exactly {names}, as text")
    dims = {}
    for d in col.doc["dataset_dimensions"]:
        value, label = resolve_dimension(vocab, d, raw[d["name"]])
        if d["name"] in vocab.mappings and vocab.how(d["name"], raw[d["name"]]) == "unresolved" \
                and unmapped_policy(col.doc, d["name"]) in ("refuse", "skip"):
            raise pub.PublishError(f"{d['name']}={raw[d['name']]!r} resolves through no mapping, and the manifest's "
                                   "`unmapped` policy leaves it out")
        dims[d["name"]] = {"value": value, "label": label, "raw": raw[d["name"]]}
    files_sent = sent.get("files")
    if not isinstance(files_sent, dict) or not files_sent or set(files_sent) - set(col.doc["file_roles"]):
        raise pub.PublishError(f"files is {{role: File record}} over the roles {sorted(col.doc['file_roles'])}")
    files = {}
    for role in sorted(files_sent):
        f = _verified(files_sent[role], "File")
        try:
            expected = pub.result_file_doc(col, role, f["filename"], f["sha256"], f["size_in_bytes"], f["location"])
        except (KeyError, idn.MintError) as exc:
            raise pub.PublishError(f"the {role} file's record is incomplete ({exc})") from exc
        if expected["id"] != f["id"]:
            raise pub.PublishError(f"the {role} file's record is not this collection's record of that file")
        files[role] = {"doc": f, "name": f["filename"], "path": None, "location": f["location"]}
    bounds = _where(col, sent.get("where"))
    run_id = sent.get("was_generated_by")
    if not isinstance(run_id, str) or idn.class_of(run_id) != "Activity":
        raise pub.PublishError("was_generated_by is the id of the Activity that wrote the files")
    doc = pub.dataset_doc(col, dims, {r: f["doc"] for r, f in files.items()}, bounds, run_id)
    if doc["id"] != dataset_id:
        raise pub.PublishError(f"{dataset_id} is not the dataset these values make ({doc['id']})")
    return {"doc": doc, "dims": dims, "files": files, "where": bounds}


def _contexts(bundle: Bundle, ds: dict) -> list:
    col = bundle.col
    minting, digest = col.minting_section(), col.minting_digest()
    out = []
    for t in col.templates:
        ctx = pub.mint_context_for(col, t, bundle.template_files[t.doc["template"]]["id"], ds, minting, digest)
        if ctx is not None:
            out.append(ctx)
    return out


def _extract(sent) -> dict | None:
    if sent is None:
        return None
    if not (isinstance(sent, dict) and isinstance(sent.get("edges", {}), dict) and isinstance(sent.get("rows", {}), dict)
            and all(isinstance(v, list) for v in (*sent.get("edges", {}).values(), *sent.get("rows", {}).values()))):
        raise pub.PublishError("extract is {edges: {kind: [[from, to], ...]}, rows: {kind: [row, ...]}}")
    return sent


# --- the steps ---------------------------------------------------------------------

def begin(con, collection: str, version: str, body: dict, publisher: dict) -> dict:
    files, prefix = _bundle_files(body)
    digest = _digest(files, prefix)
    bundle = _load(digest, files, prefix)
    if bundle.col.key != collection:
        raise pub.PublishError(f"collection.yaml is the collection {bundle.col.key!r}, not {collection!r}")
    _remember(bundle)
    target = pub.SqlTarget(con)
    existing = target.existing_release(collection, version)
    with con:
        con.execute(f"{con.dialect.insert_or_ignore} publish_bundles (bundle_sha256, collection_key, files, "
                    "created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                    (digest, collection, json.dumps({"files": files, "prefix": prefix}), publisher["email"],
                     device_login._fmt(device_login._now())))
        device_login._audit(con, "release_publish_started", publisher["email"],
                            f"{collection} {version} with {publisher['token_id']}, bundle {digest[:12]}")
    return {"bundle": digest, "collection": collection, "version": version,
            "existing_release_id": existing["release_id"] if existing else None,
            "existing_was_revision_of": existing["was_revision_of"] if existing else None,
            "current_release_id": target.current_release_id(collection)}


def check(con, body: dict) -> dict:
    datasets, contexts = body.get("datasets") or [], body.get("contexts") or []
    if not (isinstance(datasets, list) and isinstance(contexts, list) and len(datasets) <= MAX_CHECK
            and len(contexts) <= MAX_CHECK and all(isinstance(x, str) and len(x) <= 128 for x in datasets + contexts)):
        raise pub.PublishError(f"send {{datasets: [ids], contexts: [activity ids]}}, at most {MAX_CHECK:,} each")
    stored, recorded = set(), set()
    for i in range(0, len(datasets), 500):
        part = datasets[i:i + 500]
        stored.update(r["dataset_id"] for r in con.execute(
            f"SELECT DISTINCT dataset_id FROM dataset_files WHERE dataset_id IN ({','.join('?' * len(part))})",
            part).fetchall())
    for i in range(0, len(contexts), 500):
        part = contexts[i:i + 500]
        recorded.update(r["activity_id"] for r in con.execute(
            f"SELECT activity_id FROM mint_contexts WHERE activity_id IN ({','.join('?' * len(part))})",
            part).fetchall())
    return {"stored": sorted(stored), "recorded": sorted(recorded)}


def put_dataset(con, collection: str, dataset_id: str, body: dict, publisher: dict) -> dict:
    bundle = _bundle(con, collection, body.get("bundle"))
    vocab = bundle.vocab()
    ds = _dataset(bundle, vocab, dataset_id, body.get("dataset"))
    extract = _extract(body.get("extract"))
    target = pub.SqlTarget(con, publisher["email"], publisher["token_id"])
    if target.kind != "sql":
        raise pub.PublishError("this deployment keeps result data in a Parquet index; push to one that keeps it in "
                               "its database")
    stored = target.dataset_stored(dataset_id)
    if stored:
        extract = None  # a repeat: the rows are in
    elif extract is None:
        raise pub.PublishError(f"{dataset_id} is not stored yet: send what its files hold (extract)")
    else:
        try:
            target.refuse_other_slice(ds)
        except pub.PublishError as exc:
            raise Conflict(str(exc)) from exc
    pending = [ctx for ctx in _contexts(bundle, ds) if not target.context_recorded(ctx.activity["id"])]
    done = target.put_dataset(bundle.col, ds, vocab, extract, pending)
    return {"dataset_id": dataset_id, "already_stored": stored, **done}


def finish(con, collection: str, version: str, body: dict, publisher: dict) -> dict:
    bundle = _bundle(con, collection, body.get("bundle"))
    col = bundle.col
    release = _verified(body.get("release"), "ResultCollection")
    if release.get("collection_key") != collection or release.get("version") != version:
        raise pub.PublishError(f"the release is {release.get('collection_key')} {release.get('version')}, "
                               f"not {collection} {version}")
    document = body.get("document")
    if not isinstance(document, dict) or document.get("result_collections") != [release]:
        raise pub.PublishError("document is the release's DAPPER document, holding this release")
    objects = {release["id"]: ("ResultCollection", release)}
    for group, cls in DOCUMENT_GROUPS.items():
        if not isinstance(document.get(group, []), list):
            raise pub.PublishError(f"document.{group} is a list")
        for d in document.get(group, []):
            objects[_verified(d, cls)["id"]] = (cls, d)
    extra = body.get("objects") or []
    if not isinstance(extra, list):
        raise pub.PublishError("objects is a list of DAPPER documents")
    for d in extra:
        _verified(d)
        objects[d["id"]] = (idn.class_of(d["id"]), d)
    for f in (bundle.manifest_file, *bundle.template_files.values(), *bundle.query_files.values()):
        objects[f["id"]] = ("File", f)

    target = pub.SqlTarget(con, publisher["email"], publisher["token_id"])
    existing = target.existing_release(collection, version)
    if existing and existing["release_id"] != release["id"]:
        raise Conflict(f"{collection} {version} is already published as {existing['release_id']} with different "
                       "content; publish a new version")
    if existing:
        return {"release_id": release["id"], "collection": collection, "version": version, "already_recorded": True}
    previous = target.current_release_id(collection)
    expected = {
        "has_manifest_file": bundle.manifest_file["id"],
        "has_claim_template": [bundle.template_files[t.doc["template"]]["id"] for t in col.templates],
        "has_query_template": [bundle.query_files[n]["id"] for n in col.queries] or None,
        "was_revision_of": previous,
        "collection_key": collection,
        "methods": col.doc["methods"],
    }
    for slot, value in expected.items():
        if release.get(slot) != value:
            raise pub.PublishError(f"the release's {slot} is {release.get(slot)!r}; this push makes it {value!r}"
                                   + (" (another release became current meanwhile: run the push again)"
                                      if slot == "was_revision_of" else ""))
    publication = objects.get(release.get("was_generated_by"), (None, {}))[1]
    if publication.get("activity_type") != "ResultCollectionPublication":
        raise pub.PublishError("the release's was_generated_by is its publication Activity, in the document")
    members = release.get("members") or []
    if not members or release.get("n_members") != len(members) or len(set(members)) != len(members):
        raise pub.PublishError("the release lists its member datasets once each")
    missing = [m for m in members if objects.get(m, ("",))[0] != "Dataset"]
    if missing:
        raise pub.PublishError(f"{len(missing)} member(s) have no Dataset record in the document, e.g. {missing[0]}")
    stored = set(check(con, {"datasets": members})["stored"]) if len(members) <= MAX_CHECK else set()
    unstored = [m for m in members if m not in stored]
    if unstored:
        raise pub.PublishError(f"{len(unstored)} member dataset(s) are not stored yet, e.g. {unstored[0]}: "
                               "push them first")
    contexts = []
    for m in members:
        dataset = objects[m][1]
        files = {}
        for entry in dataset.get("file_roles") or []:
            f = objects.get(entry.get("file"), (None, None))
            if f[0] != "File":
                raise pub.PublishError(f"dataset {m}: its {entry.get('role')} file has no File record in the document")
            files[entry["role"]] = {"doc": f[1], "name": f[1]["filename"]}
        for ctx in _contexts(bundle, {"doc": dataset, "files": files}):
            contexts.append((ctx.activity["id"], m))
            objects[ctx.activity["id"]] = ("Activity", ctx.activity)
    recorded = set(check(con, {"contexts": [a for a, _ in contexts]})["recorded"]) if len(contexts) <= MAX_CHECK \
        else {a for a, _ in contexts if target.context_recorded(a)}
    unrecorded = [a for a, _ in contexts if a not in recorded]
    if unrecorded:
        raise pub.PublishError(f"{len(unrecorded)} mint context(s) are not recorded yet, e.g. {unrecorded[0]}: "
                               "push their datasets again")
    provenance = body.get("provenance")
    code_version = body.get("code_version")
    if not (isinstance(provenance, list) and all(isinstance(p, dict) for p in provenance)) \
            or not (isinstance(code_version, str) and 0 < len(code_version) <= 128):
        raise pub.PublishError("send the release's provenance record (a list) and code_version")
    vocab = bundle.vocab()
    target.record_release(col, release, document, objects, members, contexts, provenance, vocab,
                          bundle.template_files, bundle.query_files, code_version)
    with con:
        device_login._audit(con, "release_published", publisher["email"],
                            f"{collection} {version} as {release['id']} with {publisher['token_id']}")
    return {"release_id": release["id"], "collection": collection, "version": version, "already_recorded": False,
            "published_by": publisher["email"]}


# --- routes ------------------------------------------------------------------------

@router.post("/{collection}/{version}:begin")
async def begin_route(collection: str, version: str, request: Request,
                      publisher: dict = Depends(device_login.require_publisher)):
    """Start (or resume) a push: send the collection's files; answers the bundle digest later requests name,
    and the version's state."""
    _names(collection, version)
    body = await _body(request)
    return await run_in_threadpool(_call, begin, collection, version, body, publisher)


@router.post("/{collection}/{version}:check")
async def check_route(collection: str, version: str, request: Request,
                      publisher: dict = Depends(device_login.require_publisher)):
    """Which of these datasets are stored and which of these mint contexts are recorded."""
    _names(collection, version)
    body = await _body(request)
    return await run_in_threadpool(_call, check, body)


@router.put("/{collection}/{version}/datasets/{dataset_id}")
async def dataset_route(collection: str, version: str, dataset_id: str, request: Request,
                        publisher: dict = Depends(device_login.require_publisher)):
    """Store one dataset from what its files hold, in one transaction. Repeating it changes nothing."""
    _names(collection, version)
    if idn.class_of(dataset_id) != "Dataset":
        raise HTTPException(status_code=404, detail=f"{dataset_id!r} is not a Dataset id.")
    body = await _body(request)
    return await run_in_threadpool(_call, put_dataset, collection, dataset_id, body, publisher)


@router.post("/{collection}/{version}:finish")
async def finish_route(collection: str, version: str, request: Request,
                       publisher: dict = Depends(device_login.require_publisher)):
    """Record the release, once every member dataset is stored, and make it the collection's current release."""
    _names(collection, version)
    body = await _body(request)
    return await run_in_threadpool(_call, finish, collection, version, body, publisher)
