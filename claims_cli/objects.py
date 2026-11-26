"""claims objects: put a list of DAPPER objects into an object collection (kind: objects) of a deployed service,
signed in with `claims login` (docs/depositing-objects.md).

    claims objects register <collection dir> --api qa [--replace]
    claims objects deposit  <collection dir> <version> --api qa --from <dir | s3://prefix/>
                            [--manifest <tsv> --role provenance] [--skip-group c2m2_files ...]
                            [--state-file PATH] [--workers 4] [--limit N] [--close] [--workdir DIR]
    claims objects close    <collection dir> <version> --api qa
    claims objects status   <collection dir> <version> --api qa

`register` sends the collection's definition (manifest, query templates, vocabulary files). `deposit` registers it
when it is not registered yet (and stops when another definition is), opens the release (opening an open release
changes nothing), then sends each selected DAPPER document as deposits into it, in parts within the service's
limits (1,000 objects, 5,000 edges, 16 MB). A document is recorded in the state file once all its parts are in,
and a resent part changes nothing, so a run that stops is resumed by running the same command again. The release
is closed only with --close (or `claims objects close`): it then becomes the collection's current release.
"""
from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from . import auth

# The service takes 1,000 objects, 5,000 edges and 16 MB per deposit; parts stay well inside (as the tested
# reference client did), since sizes are estimated here from compact JSON.
MAX_OBJECTS, MAX_EDGES, MAX_BYTES = 800, 4000, 10_000_000
ALWAYS_SKIPPED = {"prefixes", "id"}
RETRIES = 6
TIMEOUT = 600
PROGRESS_EVERY = 200   # documents between progress lines (and at least every PROGRESS_SECONDS)
PROGRESS_SECONDS = 60
STATE_HEADER = "# claims objects deposit"

try:
    Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover - PyYAML without libyaml
    Loader = yaml.SafeLoader


class Refused(Exception):
    """The service refused a request (a 4xx other than 401)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


# --- the service ---------------------------------------------------------------------------------------------

class Api:
    """A deployed service, with a login token (Authorization: Bearer) or a deposit key (X-API-Key)."""

    def __init__(self, base: str, token: str | None = None, key: str | None = None):
        self.base, self.token, self.key = base, token, key

    def send(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """(status, JSON) of a request that the service answered below 500, tried again on a 5xx (honouring
        Retry-After) or a dropped connection; every write here is idempotent, so a retry repeats nothing."""
        headers = {"Accept": "application/json", "User-Agent": auth.CLIENT}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body, separators=(",", ":")).encode(), "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.key:
            headers["X-API-Key"] = self.key
        url = self.base + path
        for attempt in range(1, RETRIES + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    return response.status, json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                try:
                    answer = json.loads(exc.read() or b"{}")
                except ValueError:
                    answer = {}
                detail = answer.get("detail") if isinstance(answer, dict) else None
                if exc.code == 401:
                    how = ("the deposit key is not valid (unknown or revoked); check it" if self.key else
                           f"the token for {self.base} is expired or revoked. Run `claims login --api {self.base}`")
                    sys.exit(f"claims: {how}, then the same command again.")
                if exc.code < 500:
                    return exc.code, answer if isinstance(answer, dict) else {}
                if attempt == RETRIES:
                    raise Refused(exc.code, str(detail or exc.reason))
                reason = f"{exc.code} {detail or exc.reason}"
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == RETRIES:
                    sys.exit(f"claims: cannot reach {self.base}: {getattr(exc, 'reason', exc)}")
                reason, retry_after = str(getattr(exc, "reason", exc)), None
            wait = min(60, 5 * 2 ** (attempt - 1))
            if retry_after and retry_after.isdigit():
                wait = max(wait, min(120, int(retry_after)))
            print(f"  {reason}; trying again in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def ok(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        status, answer = self.send(method, path, body)
        if status >= 400:
            raise Refused(status, _detail(answer))
        return status, answer


def _detail(answer: dict) -> str:
    detail = answer.get("detail") if isinstance(answer, dict) else None
    return detail if isinstance(detail, str) else json.dumps(answer)[:500]


def _api_base(args) -> str:
    return auth.api_base(auth.QA if args.api == "qa" else args.api)


def _login(args) -> Api:
    base = _api_base(args)
    token = auth.token_for(base)
    if not token:
        sys.exit(f"claims: not signed in to {base}. Run `claims login --api {base}` first.")
    return Api(base, token=token)


def _writer(args) -> Api:
    """The credential deposits are sent with: the login, or a deposit key from $<--key-env>."""
    if args.key_env:
        key = os.environ.get(args.key_env)
        if not key:
            sys.exit(f"claims: {args.key_env} is not set (--key-env names the variable holding a deposit key)")
        return Api(_api_base(args), key=key)
    return _login(args)


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


# --- the collection -------------------------------------------------------------------------------------------

def _collection(path: str):
    """The collection directory, checked as `claims collection validate` checks it; it must be an object
    collection."""
    from service.collections.manifest import ManifestError, load_collection

    try:
        col = load_collection(path)
    except ManifestError as exc:
        sys.exit(f"invalid: {exc}")
    if not col.is_objects:
        sys.exit(f"claims: {col.key} is a file collection; publish it with `claims release publish --api`")
    return col


def _definition(col) -> dict:
    """What `register` sends: the collection's files, its path in its repository and its git commit (with
    -dirty when it has uncommitted changes, as scripts/publish_collection.py records it)."""
    from service.collections.publish import PublishError, _git_info, repository_prefix

    from .push import collection_files

    try:
        files = collection_files(col)
    except PublishError as exc:
        sys.exit(f"claims: {exc}")
    commit, dirty, _ = _git_info(col.manifest.path)
    return {"files": files, "prefix": repository_prefix(col),
            "git_commit": f"{commit}{'-dirty' if dirty else ''}" if commit else None}


def _register(api: Api, col, replace: bool = False) -> dict:
    body = _definition(col)
    if body["git_commit"] is None or body["git_commit"].endswith("-dirty"):
        print(f"warning      {col.key} is {'not in git' if body['git_commit'] is None else 'uncommitted'}: the "
              "definition is registered as it is on disk", flush=True)
    status, answer = api.send("POST", f"/v1/publish/objects/{_q(col.key)}:register", {**body, "replace": replace})
    if status == 409:
        sys.exit(f"refused      {col.key}: {_detail(answer)}")
    if status >= 400:
        sys.exit(f"claims: registering {col.key} failed ({status}): {_detail(answer)}")
    return answer


def _exit_on_refusal(fn):
    """A command whose request still failed after its retries ends with a message, not a traceback."""
    def run(args):
        try:
            return fn(args)
        except Refused as exc:
            sys.exit(f"claims: the service failed ({exc.status}): {exc.detail}. Run the same command again later.")
    return run


def cmd_register(args) -> None:
    col = _collection(args.collection)
    api = _login(args)
    answer = _register(api, col, args.replace)
    print(f"{answer['status']:<12} {col.key} at {api.base}: queries {', '.join(answer['queries']) or 'none'}, "
          f"{answer['files']} file(s), commit {answer.get('git_commit') or '-'}")


# --- documents -------------------------------------------------------------------------------------------------

def parts(document: dict, skip: set[str], max_bytes: int | None = None):
    """A document's node and edge groups as deposits within the limits: ({group: [items]}, objects, edges) each.
    Skipped: the groups named in `skip`, `prefixes` and `id`, and any value that is not a list. A deposit holds at
    least one object, so a part that would hold edges only repeats the document's first object (not counted in
    the part's objects); depositing an object again changes nothing."""
    groups = [(g, items) for g, items in document.items()
              if g not in skip and g not in ALWAYS_SKIPPED and isinstance(items, list) and items]
    anchor = next(((g, items[0]) for g, items in groups if not g.endswith("_edges")), None)
    part, objects, edges, size = {}, 0, 0, 0

    def done():
        if not objects:
            if anchor is None:
                raise ValueError("the document holds edges but no objects; a deposit needs at least one object")
            return {anchor[0]: [anchor[1]], **part}, 0, edges  # the repeated anchor is not counted again
        return part, objects, edges

    for group, items in groups:
        is_edge = group.endswith("_edges")
        for item in items:
            item_size = len(json.dumps(item, separators=(",", ":")).encode()) + 2  # sized as sent (ensure_ascii, as _call)
            if part and (size + item_size > (max_bytes or MAX_BYTES) or (objects >= MAX_OBJECTS and not is_edge)
                         or (edges >= MAX_EDGES and is_edge)):
                yield done()
                part, objects, edges, size = {}, 0, 0, 0
            part.setdefault(group, []).append(item)
            size += item_size
            objects, edges = objects + (not is_edge), edges + is_edge
    if part:
        yield done()


def skipped_counts(document: dict, skip: set[str]) -> dict[str, int]:
    """Items left out of a document, by group: the --skip-group groups, and any group whose value is not a list."""
    out = {}
    for group, items in document.items():
        if group in ALWAYS_SKIPPED:
            continue
        if group in skip:
            out[group] = len(items) if isinstance(items, list) else 1
        elif not isinstance(items, list):
            out[group] = 1
    return out


# --- where the documents are ----------------------------------------------------------------------------------

def default_workdir() -> Path:
    """Stable across runs, so the default state file resumes: $XDG_CACHE_HOME/claims/objects, else
    ~/.cache/claims/objects."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "claims" / "objects"


def s3_sync(prefix: str, dest: Path, includes: list[str] | None) -> None:
    """Mirror an s3:// prefix (only the files matching `includes`, when given) into dest, with the aws CLI; files
    already there and unchanged are not downloaded again."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["aws", "s3", "sync", "--only-show-errors", prefix, str(dest)]
    if includes:
        cmd += ["--exclude", "*", *(x for pattern in includes for x in ("--include", pattern))]
    for attempt in range(1, 5):
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode == 0:
            return
        if attempt < 4:
            time.sleep(5 * attempt)
    sys.exit(f"claims: aws s3 sync {prefix} failed (exit {out.returncode}): {out.stderr.strip()[:500]}")


def s3_fetch(location: str, dest: Path) -> None:
    from .sources import SourceError, fetch

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        fetch(location, dest)
    except SourceError as exc:
        sys.exit(f"claims: {exc}")


def _mirror(prefix: str, workdir: Path) -> Path:
    bucket, _, key = prefix[5:].partition("/")
    return workdir / "source" / bucket / key.strip("/")


def _manifest_paths(args, source: str, root: Path, workdir: Path) -> list[str]:
    """The documents a manifest TSV selects: its relative_path column, the rows whose role column is --role (every
    row without --role). The manifest is an s3:// URI, a local file, or a path relative to --from."""
    m = args.manifest
    if m.startswith("s3://"):
        local = _mirror(m.rsplit("/", 1)[0] + "/", workdir) / m.rsplit("/", 1)[1]
        s3_fetch(m, local)
    elif Path(m).is_file():
        local = Path(m)
    elif source.startswith("s3://"):
        local = root / m
        s3_fetch(source.rstrip("/") + "/" + m, local)
    else:
        local = root / m
    if not local.is_file():
        sys.exit(f"claims: no manifest at {m} (a local file, an s3:// URI, or a path under --from)")
    with open(local, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if "relative_path" not in (reader.fieldnames or []) or (args.role and "role" not in reader.fieldnames):
            sys.exit(f"claims: {m} needs a relative_path column{' and a role column' if args.role else ''}")
        rows = [r for r in reader if not args.role or r.get("role") == args.role]
    return sorted({r["relative_path"] for r in rows if r.get("relative_path")})


def select(args, workdir: Path) -> tuple[Path, list[str]]:
    """(the local directory documents are read from, their sorted relative paths): what the manifest selects, or
    every *.dapper.yaml under --from. An s3:// --from is mirrored under the workdir first."""
    source = args.source
    if source.startswith("s3://"):
        prefix = source.rstrip("/") + "/"
        root = _mirror(prefix, workdir)
        if args.manifest:
            paths = _manifest_paths(args, prefix, root, workdir)
            names = sorted({p.rsplit("/", 1)[-1] for p in paths})
            print(f"downloading  {len(paths):,} document(s) from {prefix} into {root}", flush=True)
            s3_sync(prefix, root, [f"*{n}" for n in names] if len(names) <= 50 else None)
        else:
            print(f"downloading  the *.dapper.yaml files under {prefix} into {root}", flush=True)
            s3_sync(prefix, root, ["*.dapper.yaml"])
            paths = sorted(str(p.relative_to(root)) for p in root.rglob("*.dapper.yaml"))
    else:
        root = Path(source).resolve()
        if not root.is_dir():
            sys.exit(f"claims: --from {source} is not a directory or an s3:// prefix")
        paths = (_manifest_paths(args, source, root, workdir) if args.manifest
                 else sorted(str(p.relative_to(root)) for p in root.rglob("*.dapper.yaml")))
    missing = [p for p in paths if not (root / p).is_file()]
    if missing:
        sys.exit(f"claims: {len(missing)} selected document(s) are not under {root}, e.g. {missing[0]}")
    return root, paths


# --- the state file --------------------------------------------------------------------------------------------

class State:
    """The documents of one release already deposited, one relative path per line, after a header naming the
    service, collection and release, so a state file is never reused for another."""

    def __init__(self, path: Path, api: str, key: str, version: str, source: str = "", skip: tuple = ()):
        # the source and the groups left out are part of what was sent: a run with others must not count as done
        self.path = path
        self.header = f"{STATE_HEADER} {api} {key} {version} from={source} skip={','.join(sorted(skip)) or '-'}"
        self.done: set[str] = set()
        self.lock = threading.Lock()
        if path.exists():
            lines = path.read_text().splitlines()
            if not lines or lines[0] != self.header:
                sys.exit(f"claims: {path} is the state of another deposit ({lines[0] if lines else 'empty'}); "
                         "name another with --state-file")
            self.done = {line.strip() for line in lines[1:] if line.strip()}
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.header + "\n")

    def add(self, rel: str) -> None:
        with self.lock, open(self.path, "a") as fh:
            fh.write(rel + "\n")
            fh.flush()
        self.done.add(rel)


# --- deposit ---------------------------------------------------------------------------------------------------

def _open(api: Api, key: str, version: str) -> None:
    status, answer = api.send("POST", f"/v1/collections/{_q(key)}/releases", {"version": version})
    if status in (200, 201):
        print(f"{'opened' if status == 201 else 'open':<12} {key} {version} at {api.base}", flush=True)
        return
    sys.exit(f"claims: cannot open {key} {version} ({status}): {_detail(answer)}")


def _close(api: Api, key: str, version: str) -> dict:
    status, answer = api.send("POST", f"/v1/collections/{_q(key)}/releases/{_q(version)}:close")
    if status != 200:
        sys.exit(f"claims: cannot close {key} {version} ({status}): {_detail(answer)}")
    return answer


def _print_release(answer: dict, closing: bool) -> None:
    classes = answer.get("classes") or {}
    print(f"{answer.get('state', '?'):<12} {answer.get('collection')} {answer.get('version')}"
          + (f" as {answer['release_id']}" if answer.get("release_id") else ""))
    print(f"  objects: {answer.get('objects', 0):,} in {answer.get('deposits', 0):,} deposit(s)")
    for cls, n in sorted(classes.items()):
        print(f"    {cls}: {n:,}")
    if closing:
        unresolved = answer.get("unresolved_references")
        if unresolved is None:
            print("  unresolved_references: (closed before; not reported again)")
        else:
            print(f"  unresolved_references: {len(unresolved)}" + (f", e.g. {unresolved[0]}" if unresolved else ""))


def _send_document(api: Api, key: str, version: str, root: Path, rel: str, skip: set[str], stop: threading.Event,
                   max_bytes: int | None = None) -> dict:
    with open(root / rel) as fh:
        document = yaml.load(fh, Loader=Loader)
    if not isinstance(document, dict):
        raise Refused(0, f"{rel} is not a DAPPER document")
    out = {"parts": 0, "objects": 0, "edges": 0, "unresolved_references": 0,
           "skipped": skipped_counts(document, skip)}
    try:
        split = list(parts(document, skip, max_bytes))
    except ValueError as exc:
        raise Refused(0, f"{rel}: {exc}")
    for part, objects, edges in split:
        if stop.is_set():
            raise Refused(0, "stopped")
        _status, receipt = api.ok("POST", "/v1/deposits", {"collection": key, "release": version, "document": part})
        out["parts"] += 1
        out["objects"] += objects
        out["edges"] += edges
        out["unresolved_references"] += len(receipt.get("unresolved_references") or [])
    return out


def cmd_deposit(args) -> None:
    col = _collection(args.collection)
    key, version = col.key, args.version
    api = _writer(args)
    if api.token:
        answer = _register(api, col)
        print(f"{answer['status']:<12} {key}: queries {', '.join(answer['queries']) or 'none'}", flush=True)
    else:
        print(f"note         depositing with a key: the definition is not checked (register it with "
              f"`claims objects register` and a login)", flush=True)
    workdir = Path(args.workdir).resolve() if args.workdir else default_workdir()
    root, paths = select(args, workdir)
    if args.limit is not None:
        paths = paths[:args.limit]
    state = State(Path(args.state_file) if args.state_file else workdir / f"{key}-{version}.sent", api.base, key,
                  version, args.source, tuple(args.skip_group or ()))
    todo = [p for p in paths if p not in state.done]
    print(f"selected     {len(paths):,} document(s) under {root}; {len(paths) - len(todo):,} already deposited "
          f"(state {state.path})", flush=True)
    _open(api, key, version)

    skip = set(args.skip_group or [])
    if args.part_mb is not None and not 0.05 <= args.part_mb <= MAX_BYTES / 1e6:
        sys.exit(f"claims: --part-mb is between 0.05 and {MAX_BYTES / 1e6:g}")
    max_bytes = int(args.part_mb * 1e6) if args.part_mb is not None else None
    totals = {"documents": 0, "parts": 0, "objects": 0, "edges": 0, "unresolved_references": 0}
    skipped: dict[str, int] = {}
    started = last = time.time()
    stop = threading.Event()
    failure = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_send_document, api, key, version, root, rel, skip, stop, max_bytes): rel for rel in todo}
        try:
            for future in concurrent.futures.as_completed(futures):
                rel = futures[future]
                try:
                    got = future.result()
                except BaseException as exc:  # a refusal, an unreadable document, or a sys.exit in a worker
                    if failure is None:
                        failure = (rel, exc)
                        stop.set()
                        for f in futures:
                            f.cancel()
                    continue
                state.add(rel)
                totals["documents"] += 1
                for k in ("parts", "objects", "edges", "unresolved_references"):
                    totals[k] += got[k]
                for g, n in got["skipped"].items():
                    skipped[g] = skipped.get(g, 0) + n
                if totals["documents"] % PROGRESS_EVERY == 0 or time.time() - last > PROGRESS_SECONDS:
                    last = time.time()
                    print(f"  {totals['documents']:,} / {len(todo):,} documents, {totals['parts']:,} parts, "
                          f"{totals['objects']:,} objects, {totals['edges']:,} edges ({last - started:.0f}s)",
                          flush=True)
        except KeyboardInterrupt:  # stop at the next part; documents not finished are not recorded as sent
            stop.set()
            for f in futures:
                f.cancel()
            print("\nclaims: stopping after the parts in flight; run the same command again to continue",
                  file=sys.stderr, flush=True)
            raise
    print(f"deposited    {totals['documents']:,} document(s) in {totals['parts']:,} part(s) "
          f"({time.time() - started:.0f}s): {totals['objects']:,} objects, {totals['edges']:,} edges")
    for g, n in sorted(skipped.items()):
        print(f"  skipped {g}: {n:,}")
    print(f"  unresolved_references: {totals['unresolved_references']:,} (summed over the parts' receipts)")
    if failure is not None:
        rel, exc = failure
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise exc
        if isinstance(exc, Refused):
            why = f"refused ({exc.detail if exc.status == 0 else f'{exc.status}: {exc.detail}'})"
        else:
            why = f"not deposited ({type(exc).__name__}: {str(exc)[:300]})"
        sys.exit(f"claims: {rel} was {why}. Documents already deposited are in the state file; fix it and run the "
                 "same command again.")
    if len(state.done & set(paths)) < len(paths):  # pragma: no cover - every document either succeeded or failed
        sys.exit("claims: some documents were not deposited; run the same command again")
    if args.close:
        _print_release(_close(api, key, version), closing=True)
    else:
        print(f"open         {key} {version} stays open: close it with --close or `claims objects close`")


def cmd_close(args) -> None:
    col = _collection(args.collection)
    _print_release(_close(_writer(args), col.key, args.version), closing=True)


def cmd_status(args) -> None:
    col = _collection(args.collection)
    api = Api(_api_base(args))
    status, answer = api.send("GET", f"/v1/collections/{_q(col.key)}/releases/{_q(args.version)}")
    if status != 200:
        sys.exit(f"claims: {col.key} {args.version} ({status}): {_detail(answer)}")
    _print_release(answer, closing=False)


def add_commands(sub) -> None:
    group = sub.add_parser("objects", help="register and fill an object collection (kind: objects) of a deployed "
                                           "service, with `claims login`").add_subparsers(dest="cmd", required=True)
    api_help = "the deployed service: `qa`, or its base URL"
    key_help = ("send deposits, open and close with the deposit key in this environment variable (e.g. "
                "CLAIMS_API_KEY; the key needs a grant for the collection) instead of the login")

    p = group.add_parser("register", help="register (or check) the collection's definition")
    p.add_argument("collection", help="the collection directory")
    p.add_argument("--api", required=True, help=api_help)
    p.add_argument("--replace", action="store_true", help="replace a different registered definition (releases "
                                                          "already opened keep theirs)")
    p.set_defaults(func=_exit_on_refusal(cmd_register))

    p = group.add_parser("deposit", help="deposit DAPPER documents into a release; resumable")
    p.add_argument("collection", help="the collection directory")
    p.add_argument("version", help="the release, e.g. 2026.11.0")
    p.add_argument("--api", required=True, help=api_help)
    p.add_argument("--from", dest="source", required=True, help="a directory or an s3:// prefix holding the documents")
    p.add_argument("--manifest", help="a TSV selecting documents by its relative_path column (a local file, an "
                                      "s3:// URI, or a path under --from); default: every *.dapper.yaml under --from")
    p.add_argument("--role", help="with --manifest: only the rows whose role column is this, e.g. provenance")
    p.add_argument("--skip-group", action="append", metavar="NAME",
                   help="a document group to leave out, e.g. c2m2_files (repeatable)")
    p.add_argument("--state-file", help="documents already deposited (default <workdir>/<collection>-<version>.sent)")
    p.add_argument("--workers", type=int, default=4, help="documents deposited in parallel (default 4)")
    p.add_argument("--limit", type=int, help="the first N selected documents only (a trial)")
    p.add_argument("--part-mb", type=float, default=None,
                   help=f"at most this many MB of JSON per deposit (default {MAX_BYTES / 1e6:g}); smaller parts keep a "
                        "document with many indexed values (e.g. big gene sets) within a gateway's request timeout")
    p.add_argument("--close", action="store_true", help="close the release after the last document")
    p.add_argument("--workdir", help=f"where an s3:// source is mirrored and the state file kept (default "
                                     f"{default_workdir()})")
    p.add_argument("--key-env", metavar="VAR", help=key_help)
    p.set_defaults(func=_exit_on_refusal(cmd_deposit))

    p = group.add_parser("close", help="close a release: it becomes the collection's current release")
    p.add_argument("collection")
    p.add_argument("version")
    p.add_argument("--api", required=True, help=api_help)
    p.add_argument("--key-env", metavar="VAR", help=key_help)
    p.set_defaults(func=_exit_on_refusal(cmd_close))

    p = group.add_parser("status", help="a release's state, objects and deposits")
    p.add_argument("collection")
    p.add_argument("version")
    p.add_argument("--api", required=True, help=api_help)
    p.set_defaults(func=_exit_on_refusal(cmd_status))
