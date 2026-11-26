"""Registering a collection from the API machine's disk: the admin console's
Add collection flow (routes in service/admin.py).

Off unless the API process sets CLAIMS_COLLECTION_ROOTS, an os.pathsep-
separated list of directories, read at call time. Every path the flow reads
(collection directory, manifest, templates, queries, release file, result
files) must resolve, symlinks and `..` followed, inside one of the roots.

`check` reads files and writes nothing: publish() runs as a dry run for the
release id and version state, and sample claims are rendered with the
ingester's own building blocks (read_dataset, assemble_entities, assemble_rows, row_inputs,
mint_context_for), so a sample's id is the id a real publish records.
`start_job` runs the real publish on a background thread with its own
connection, one job at a time. Jobs are kept in this process's memory only
(the newest MAX_JOBS): a restart forgets them. The release itself is in the
database, and each job's start and finish are audited in admin_auth_events.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

import yaml

from .. import db
from . import engine
from . import identity as idn
from . import publish as pub
from .manifest import Collection, ManifestError, load_collection, load_release
from .traits import MappingError, collection_vocabulary, is_resolved, resolve_dimension

ENV = "CLAIMS_COLLECTION_ROOTS"
SCAN_DEPTH = 5
SCAN_CAP = 200
SKIP_DIRS = {"node_modules", ".venv", "__pycache__", "vendor"}
MAX_JOBS = 20
DISABLED = "Collection registration is not enabled on this deployment (CLAIMS_COLLECTION_ROOTS is unset)."


class OutsideRoots(ValueError):
    """A path the flow would read resolves outside CLAIMS_COLLECTION_ROOTS."""


class UnusablePath(OutsideRoots):
    """A path in the request that cannot be resolved at all (a NUL byte, an
    unknown ~user, a symlink loop)."""


class JobRunning(RuntimeError):
    pass


def roots() -> list[Path]:
    """The configured roots, resolved; empty when registration is off."""
    raw = os.environ.get(ENV, "")
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def _inside(path: Path, allowed: list[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in allowed)


def contained(raw: str, allowed: list[Path]) -> Path:
    """`raw` resolved, or OutsideRoots naming it. Relative paths are refused
    rather than resolved against the server's working directory."""
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise OutsideRoots(f"{raw} is not an absolute path")
        resolved = path.resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        if isinstance(exc, OutsideRoots):
            raise
        raise UnusablePath(f"{raw!r} is not a usable path") from exc
    if not _inside(resolved, allowed):
        raise OutsideRoots(f"{raw} resolves to {resolved}, outside the collection roots")
    return resolved


def _escapes(collection_dir: Path, release_file: Path | None, allowed: list[Path]) -> list[str]:
    """Paths load_collection and publish would read that resolve outside the
    roots. Files that cannot be parsed are left to the loaders to report."""
    out = []
    manifest = (collection_dir / "collection.yaml").resolve()
    if not _inside(manifest, allowed):
        return [f"{manifest} is outside the collection roots"]
    try:
        doc = yaml.safe_load(manifest.read_bytes())
    except (OSError, yaml.YAMLError):
        doc = None
    if isinstance(doc, dict):
        for rel in [*(doc.get("templates") or []), *(doc.get("queries") or [])]:
            if isinstance(rel, str) and not _inside((collection_dir / rel).resolve(), allowed):
                out.append(f"{rel} resolves to {(collection_dir / rel).resolve()}, outside the collection roots")
    if release_file is not None:
        try:
            spec, base = load_release(release_file)
        except (OSError, yaml.YAMLError, ManifestError):
            return out
        for ds in spec["datasets"]:
            for f in ds["files"].values():
                path = (base / f["path"]).resolve()
                if not _inside(path, allowed):
                    out.append(f"{f['path']} resolves to {path}, outside the collection roots")
    return out


# --- sources ------------------------------------------------------------------

def _yaml_field(path: Path, *keys):
    try:
        doc = yaml.safe_load(path.read_bytes())
        return [doc.get(k) if isinstance(doc.get(k), str) else None for k in keys]
    except Exception:  # any unreadable or malformed file is listed with nulls
        return [None] * len(keys)


def sources() -> dict:
    """collection.yaml files under the roots (bounded scan) and the release
    files in each collection's releases/ directory."""
    allowed = roots()
    found: list[dict] = []

    def walk(directory: Path, depth: int):
        if len(found) >= SCAN_CAP:
            return
        manifest = directory / "collection.yaml"
        if manifest.is_file() and _inside(manifest.resolve(), allowed):
            key, title = _yaml_field(manifest, "collection", "title")
            releases = []
            rdir = directory / "releases"
            if rdir.is_dir():
                for p in sorted([*rdir.glob("*.yaml"), *rdir.glob("*.yml")]):
                    if p.is_file() and _inside(p.resolve(), allowed):
                        releases.append({"path": str(p.resolve()), "version": _yaml_field(p, "version")[0]})
            found.append({"path": str(directory), "key": key, "title": title, "releases": releases})
        if depth >= SCAN_DEPTH:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS or not e.is_dir(follow_symlinks=False):
                continue
            walk(Path(e.path), depth + 1)

    for root in allowed:
        if root.is_dir():
            walk(root, 0)
    return {"enabled": bool(allowed), "roots": [str(r) for r in allowed], "collections": found[:SCAN_CAP]}


# --- the check ------------------------------------------------------------------

STEPS = [("manifest", "Collection manifest, templates and queries"), ("git", "Git state"),
         ("release", "Release file and version"), ("datasets", "Datasets and result files"),
         ("samples", "Sample claims")]


def manifest_facets(doc: dict) -> dict:
    """The parts of a manifest the console shows, camelCased at the top level."""
    if doc.get("kind") == "objects":  # an object collection: classes and their fields instead of files and rows
        return {"kind": "objects", "dimensions": [], "entities": {}, "rowKinds": {}, "edgeKinds": {}, "fileRoles": {},
                "metrics": [], "tierRules": {}, "objectKinds": doc.get("object_kinds") or {}}
    return {
        "kind": "files",
        "dimensions": [{"name": d["name"], "resolve": d.get("resolve"), "description": d.get("description")}
                       for d in doc["dataset_dimensions"]],
        "entities": doc["entities"],
        "rowKinds": doc["row_kinds"],
        "edgeKinds": doc.get("edge_kinds") or {},
        "fileRoles": doc["file_roles"],
        "metrics": [{"name": name, "scoreKind": m["score_kind"], "scale": m.get("scale"), "better": m.get("better"),
                     "description": m.get("description"),
                     "interpretation": " ".join((m.get("interpretation") or "").split()) or None}
                    for name, m in doc["metrics"].items()],
        "tierRules": doc.get("tier_rules") or {},
    }


def _collection_view(col: Collection) -> dict:
    doc = col.doc
    files = [("manifest", col.key, col.manifest)] + [("template", t.doc["template"], t) for t in col.templates] \
        + [("query", name, q) for name, q in col.queries.items()]
    return {
        "key": col.key, "title": doc["title"], "description": doc.get("description"), "methods": doc["methods"],
        "owners": doc.get("owners", []), "dapperProfile": doc["dapper_profile"], **manifest_facets(doc),
        "templates": [{"name": t.doc["template"], "version": t.doc["version"], "for": t.doc["for"],
                       "claims": [c["key"] for c in t.doc["claims"]]} for t in col.templates],
        "queries": sorted(col.queries),
        "sourceFiles": [{"kind": kind, "name": name, "path": f.rel, "sha256": f.sha256, "sizeBytes": len(f.data)}
                        for kind, name, f in files],
        "mintingDigest": None if col.is_objects else col.minting_digest(),
    }


def _groups(col: Collection, ds: dict, ents, edges: dict, kind: str, group_by: str) -> list[dict]:
    """render_group's `group` per edge group, in the ingester's order, from
    the edges as read (mirrors publish.group_inputs over result_edges)."""
    spec = col.doc["edge_kinds"][kind]
    rows = [(from_id, ents.ident(spec["to"]["entity"], to_value)[0], to_value)
            for (_, to_value), (from_id, _, _) in edges.get(kind, {}).items()]
    entity = col.doc["entities"][spec["to"]["entity"]]
    parent = ""
    if "library_dimension" in entity:
        parent = engine.gene_set_collection(ds["dims"][entity["library_dimension"]]["value"])["id"]
    out = []
    for gid in sorted({r[1] if group_by == "to" else r[0] for r in rows}):
        members = [r for r in rows if r[1] == gid]
        if not members:
            raise pub.PublishError(f"edge group {gid} has no {kind} edges ending at it")
        out.append({"id": gid, "label": min(r[2] for r in members), "count": len(members), "parent": parent})
    return out


def check(con, collection_dir: str, release_file: str, sample: int = 3) -> dict:
    """What publishing this collection and release would do, read-only.
    Raises OutsideRoots for a collection directory or release file outside
    the roots; every other problem is reported in the result."""
    allowed = roots()
    cdir, rfile = contained(collection_dir, allowed), contained(release_file, allowed)
    steps = {sid: {"id": sid, "title": title, "status": "skipped", "messages": []} for sid, title in STEPS}
    out = {"ok": False, "steps": list(steps.values()), "collection": None, "git": None, "release": None,
           "datasets": [], "samples": [], "warnings": []}

    def done(sid, status, *messages):
        steps[sid].update(status=status, messages=[*steps[sid]["messages"], *messages])
        return status != "error"

    if not _manifest_step(cdir, allowed, out, done):
        return _finish(out)
    col = out.pop("_col")

    commit, dirty, _ = pub._git_info(col.manifest.path)
    out["git"] = {"commit": commit, "dirty": dirty,
                  "codeVersion": f"{commit}{'-dirty' if dirty else ''}" if commit else "uncommitted"}
    if dirty:
        msg = f"{col.root} has uncommitted changes; a publish records commit {commit} as dirty"
        out["warnings"].append(msg)
        done("git", "warning", msg)
    else:
        done("git", "ok", f"commit {commit}" if commit else
             "not in a git repository; the release records code version 'uncommitted'")

    try:
        spec, base = load_release(rfile)
    except (OSError, yaml.YAMLError, ManifestError) as exc:
        done("release", "error", str(exc))
        return _finish(out)
    datasets, problems, vocab = _inspect_datasets(con, col, spec, base, allowed)
    current = con.execute("SELECT r.version FROM collections c LEFT JOIN collection_releases r ON r.release_id = "
                          "c.current_release_id WHERE c.collection_key = ?", (col.key,)).fetchone()
    stored = con.execute("SELECT release_id FROM collection_releases WHERE collection_key = ? AND version = ?",
                         (col.key, spec["version"])).fetchone()
    release = {"version": spec["version"], "releaseId": None, "state": None, "previousReleaseId": None,
               "currentVersion": current["version"] if current else None}
    out["release"] = release
    out["datasets"] = datasets  # reported whatever the release state
    plan = None
    if not problems:
        try:
            result = pub.publish(con, col, spec, base, allow_dirty=True, dry_run=True)
        except (pub.PublishError, ManifestError, OSError, ValueError) as exc:
            problems.append(str(exc))
        else:
            plan = result.plan
            release.update(releaseId=result.release_id, previousReleaseId=plan["previous_release_id"])
            # The git step already reported a dirty tree, in its own words.
            dirty_msg = f"{col.root} has uncommitted changes;"
            out["warnings"].extend(w for w in result.warnings
                                   if w not in out["warnings"] and not w.startswith(dirty_msg))
            for i, ds in enumerate(plan["datasets"]):
                view = datasets[i]
                view["datasetId"] = ds["doc"]["id"]
                view["alreadyIngested"] = con.execute("SELECT 1 FROM dataset_files WHERE dataset_id = ? LIMIT 1",
                                                      (ds["doc"]["id"],)).fetchone() is not None
                for f in view["files"]:
                    doc = ds["files"][f["role"]]["doc"]
                    f.update(sha256=doc["sha256"], location=doc["location"])
    if current is None:
        release["state"] = "new-collection"
    elif stored is None:
        release["state"] = "new-version"
    elif release["releaseId"] is None:
        release["state"] = "unknown"  # stored, but this content cannot be compared with it yet
    elif release["releaseId"] == stored["release_id"]:
        release["state"] = "already-published"
    else:
        release["state"] = "conflict"
    if release["state"] == "conflict":
        done("release", "error", f"{col.key} {spec['version']} is already published as {stored['release_id']} "
             "with different content; publish would refuse it. Bump the version.")
        return _finish(out)
    msgs = {"new-collection": f"{col.key} is not registered yet; {spec['version']} would be its first release",
            "new-version": f"{spec['version']} would be a new release of {col.key} (current: {release['currentVersion']})",
            "already-published": f"{col.key} {spec['version']} is already published with this content; "
                                 "publishing it again changes nothing",
            "unknown": f"{col.key} {spec['version']} is already published as "
                       f"{stored['release_id'] if stored else None}"}
    if plan is None:
        done("release", "warning", msgs[release["state"]],
             "its content cannot be compared with the stored release until the dataset problems below are fixed"
             if release["state"] == "unknown" else
             "the release id cannot be computed until the dataset problems below are fixed")
    else:
        done("release", "ok", msgs[release["state"]])

    sample_rows = []
    if plan is not None:
        for i, ds in enumerate(plan["datasets"]):
            try:
                sample_rows.append(_read_dataset(col, ds, plan["vocab"], sample))
            except KeyError as exc:
                problems.append(f"dataset {i + 1}: a row has no column {exc}")
            except (pub.PublishError, ManifestError, ValueError, OSError) as exc:
                problems.append(f"dataset {i + 1}: {exc}")
    unresolved = sorted(set().union(*(r.unresolved for r in (vocab, plan and plan["vocab"]) if r)))
    if unresolved:
        msg = (f"{len(unresolved)} key(s) resolve through no mapping and would be kept as given: "
               f"{unresolved[:5]}")
        if not any(w.startswith(f"{len(unresolved)} key(s)") for w in out["warnings"]):
            out["warnings"].append(msg)
        steps["datasets"]["messages"].append(msg)
    if problems:
        done("datasets", "error", *problems)
        return _finish(out)
    done("datasets", "warning" if unresolved else "ok",
         *([] if unresolved else [f"{len(datasets)} dataset(s); every result file is present and readable"]))

    try:
        out["samples"] = _samples(col, plan, sample_rows, sample)
    except (engine.TemplateError, idn.MintError, pub.PublishError, ManifestError, KeyError, ValueError) as exc:
        done("samples", "error", f"{type(exc).__name__}: {exc}")
        return _finish(out)
    done("samples", "ok", f"rendered {sum(len(s['claims']) for s in out['samples'])} claim(s) from "
                          f"{len(out['samples'])} row(s) or group(s)")
    return _finish(out)


def _finish(out: dict) -> dict:
    out.pop("_col", None)
    out["ok"] = not any(s["status"] == "error" for s in out["steps"])
    return out


def _manifest_step(cdir: Path, allowed, out, done) -> bool:
    escaped = _escapes(cdir, None, allowed)
    if escaped:
        return done("manifest", "error", *escaped)
    try:
        col = load_collection(cdir)
    except (ManifestError, OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        return done("manifest", "error", str(exc))
    out["_col"] = col
    out["collection"] = _collection_view(col)
    return done("manifest", "ok", f"{col.key}: {len(col.templates)} template(s), {len(col.queries)} query "
                                  f"template(s), schema and semantic checks pass")


def _inspect_datasets(con, col: Collection, spec: dict, base: Path, allowed) -> tuple[list, list, object]:
    """Dimensions and result files as the release names them, without
    reading any file: (dataset views, problems, the collection's vocabulary)."""
    doc = col.doc
    dims_spec = doc["dataset_dimensions"]
    dim_names = [d["name"] for d in dims_spec]
    views, problems = [], []
    try:
        vocab = collection_vocabulary(doc, col.root)
    except MappingError as exc:
        vocab = None
        problems.append(str(exc))
    for i, ds in enumerate(spec["datasets"]):
        where = f"dataset {i + 1}"
        raw_dims = ds["dimensions"]
        if set(raw_dims) != set(dim_names):
            problems.append(f"{where}: dimensions must be exactly {dim_names}, got {sorted(raw_dims)}")
        dims = {}
        for d in dims_spec:
            if d["name"] not in raw_dims:
                continue
            raw = raw_dims[d["name"]]
            value, label = resolve_dimension(vocab, d, raw)
            resolved = is_resolved(vocab, d, raw)
            dims[d["name"]] = {"raw": raw, "value": value, "label": label, "resolved": resolved}
        unknown = sorted(set(ds["files"]) - set(doc["file_roles"]))
        if unknown:
            problems.append(f"{where}: unknown file role(s) {unknown}")
        files = []
        for role, f in sorted(ds["files"].items()):
            path = (base / f["path"]).resolve()
            inside = _inside(path, allowed)
            exists = inside and path.is_file()
            if not inside:
                problems.append(f"{where}: {role} file {path} is outside the collection roots")
            elif not exists:
                problems.append(f"{where}: {role} file {path} does not exist")
            files.append({"role": role, "path": str(path), "exists": exists, "insideRoots": inside,
                          "sizeBytes": path.stat().st_size if exists else None, "sha256": None, "location": None})
        views.append({"index": i, "dimensions": dims, "datasetId": None, "alreadyIngested": False, "files": files})
    return views, problems, vocab


def _read_dataset(col: Collection, ds: dict, vocab, sample: int) -> dict:
    """Read every file of a dataset as the ingester would (so a bad row is
    reported now, not at publish), keeping the first `sample` rows per kind."""
    extract = pub.read_dataset(col, ds, vocab)
    ents, edges = pub.assemble_entities(col, ds, extract, {})
    rows = {}
    for kind in col.doc["row_kinds"]:
        if col.role_for(kind) not in ds["files"]:
            continue
        it = pub.assemble_rows(col, ds, ents, kind, extract, vocab)
        rows[kind] = list(islice(it, sample))
        for _ in it:  # validate the rest
            pass
    return {"ents": ents, "edges": edges, "rows": rows}


def _samples(col: Collection, plan: dict, read: list[dict], sample: int) -> list[dict]:
    if sample <= 0:
        return []
    minting, digest = col.minting_section(), col.minting_digest()
    out = []
    for i, (ds, got) in enumerate(zip(plan["datasets"], read)):
        dataset = {"id": ds["doc"]["id"], **{n: d["value"] for n, d in ds["dims"].items()}}
        for t in col.templates:
            ctx = pub.mint_context_for(col, t, plan["template_files"][t.doc["template"]]["id"], ds, minting, digest)
            if ctx is None:
                continue
            kind = t.doc["for"]
            if kind in col.doc["row_kinds"]:
                for r in got["rows"].get(kind, []):
                    rendered = engine.render_row(ctx, dataset=dataset, **pub.row_inputs(ctx.minting, kind, r))
                    out.append(_sample(t, kind, i, r["subject_value"], rendered))
            else:
                for g in _groups(col, ds, got["ents"], got["edges"], kind, t.doc["group_by"])[:sample]:
                    out.append(_sample(t, kind, i, g["label"], engine.render_group(ctx, dataset=dataset, group=g)))
    return out


def _sample(t, kind, index, subject, rendered) -> dict:
    return {"template": t.doc["template"], "kind": kind, "dataset": index, "subject": subject,
            "claims": [{"key": key, "claimId": c["id"], "statement": c.get("statement"), "name": c.get("name")}
                       for key, c in rendered.claims.items()]}


# --- jobs -----------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(event: str, email: str | None, detail: str) -> None:
    con = db.connect()
    try:
        with con:
            con.execute("INSERT INTO admin_auth_events (at, event, email, detail) VALUES (?, ?, ?, ?)",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), event, email, detail[:255]))
    finally:
        con.close()


def _view(job: dict) -> dict:
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    end = job["_ended"] if job["_ended"] is not None else time.monotonic()
    out["seconds"] = round(end - job["_t0"], 1)
    return out


def jobs() -> list[dict]:
    with _lock:
        return [_view(j) for j in reversed(_jobs.values())]


def job(job_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(job_id)
        return _view(j) if j else None


def start_job(collection_dir: str, release_file: str, require_clean: bool, email: str) -> dict:
    """Start the real publish in the background. Raises OutsideRoots (any
    path it would read is outside the roots) or JobRunning."""
    allowed = roots()
    cdir, rfile = contained(collection_dir, allowed), contained(release_file, allowed)
    escaped = _escapes(cdir, rfile, allowed)
    if escaped:
        raise OutsideRoots(escaped[0])
    key = _yaml_field(cdir / "collection.yaml", "collection")[0] if (cdir / "collection.yaml").is_file() else None
    version = _yaml_field(rfile, "version")[0] if rfile.is_file() else None
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise JobRunning("A publish job is already running; wait for it to finish.")
        j = {"id": secrets.token_hex(8), "state": "running", "phase": "Starting", "collectionDir": str(cdir),
             "releaseFile": str(rfile), "requireClean": require_clean, "startedBy": email, "startedAt": _now(),
             "finishedAt": None, "collection": key, "version": version, "releaseId": None, "stats": None,
             "warnings": [], "error": None, "_t0": time.monotonic(), "_ended": None}
        _jobs[j["id"]] = j
        for old in [k for k, v in _jobs.items() if v["state"] != "running"][:max(0, len(_jobs) - MAX_JOBS)]:
            del _jobs[old]
    detail = f"{cdir.name} {version or '?'}"
    try:
        audit("collection_publish_started", email, detail)
    except Exception as exc:
        with _lock:
            j.update(state="failed", error=f"could not record the audit event: {exc}", phase=None,
                     finishedAt=_now(), _ended=time.monotonic())
        return job(j["id"])
    threading.Thread(target=_run, args=(j, cdir, rfile, allowed, detail), name=f"publish-{j['id']}",
                     daemon=True).start()
    return job(j["id"])


def _run(j: dict, cdir: Path, rfile: Path, allowed, detail: str) -> None:
    def phase(text: str):
        with _lock:
            j["phase"] = text

    result, error = {}, None
    con = None
    try:
        phase("Loading the collection")
        # Again, before reading anything: the files may have changed since the request.
        contained(str(cdir), allowed)
        contained(str(rfile), allowed)
        manifest = (cdir / "collection.yaml").resolve()
        if not _inside(manifest, allowed):
            raise OutsideRoots(f"{manifest} is outside the collection roots")
        col = load_collection(cdir)
        spec, base = load_release(rfile)
        escaped = _escapes(cdir, rfile, allowed)  # the paths the manifest and release name
        if escaped:
            raise OutsideRoots(escaped[0])
        with _lock:
            j.update(collection=col.key, version=spec["version"])
        detail = f"{cdir.name} {spec['version']}"
        con = db.connect()
        done = pub.publish(con, col, spec, base, allow_dirty=not j["requireClean"], progress=phase)
        result = {"releaseId": done.release_id, "stats": done.stats, "warnings": done.warnings}
    except Exception as exc:  # the job reports every failure; nothing propagates from a thread
        error = str(exc) if isinstance(exc, (ManifestError, pub.PublishError, OutsideRoots)) \
            else f"{type(exc).__name__}: {exc}"
    finally:
        if con is not None:
            con.close()
    try:
        audit("collection_publish_failed" if error else "collection_publish_succeeded", j["startedBy"], detail)
    except Exception as exc:
        result.setdefault("warnings", []).append(f"the audit event could not be recorded: {exc}")
    with _lock:
        j.update(result, phase=None if error else "Done", error=error, finishedAt=_now(), _ended=time.monotonic(),
                 state="failed" if error else "succeeded")
