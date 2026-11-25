"""Publishing a release into a deployed service: `claims release publish ... --api URL`
(docs/publishing-to-a-deployment.md).

RemoteTarget is where publish() sends a release instead of a database: the files are read and checked
here, and each dataset's values go to the API, which computes every identifier itself and stores them.
Every request is idempotent, so a push that stops is resumed by running the same command again.
"""
from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from service.collections.publish import PublishError, repository_prefix
from service.collections.traits import vocabulary_files

from . import auth

RETRIES = 6   # transient failures (a 5xx while the service's task is replaced, a dropped connection)
TIMEOUT = 300


def collection_files(col) -> dict[str, str]:
    """The files a push sends: the manifest, templates, queries and vocabulary files, by path in the collection."""
    rels = ["collection.yaml", *(t.rel for t in col.templates), *(q.rel for q in col.queries.values()),
            *(rel for _, _, rel in vocabulary_files(col.doc))]
    out = {}
    for rel in rels:
        try:
            out[rel] = (col.root / rel).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise PublishError(f"{rel} is not UTF-8 text")
    return out


class RemoteTarget:
    """publish()'s target for a deployed service (see publish.SqlTarget for the same calls on a database)."""

    kind = "remote"

    def __init__(self, api: str, token: str, col, version: str):
        self.api, self.token = api, token
        self.base = f"{api}/v1/publish/{urllib.parse.quote(col.key, safe='')}/{urllib.parse.quote(version, safe='')}"
        begun = self._send("POST", ":begin", {"files": collection_files(col), "prefix": repository_prefix(col)})
        self.bundle = begun["bundle"]
        self._existing = ({"release_id": begun["existing_release_id"], "was_revision_of": begun["existing_was_revision_of"]}
                          if begun.get("existing_release_id") else None)
        self._current = begun.get("current_release_id")
        self._stored: set[str] = set()
        self._recorded: set[str] = set()
        self._asked: set[str] = set()

    # --- HTTP --------------------------------------------------------------------
    def _send(self, method: str, path: str, body: dict) -> dict:
        data = gzip.compress(json.dumps(body, separators=(",", ":")).encode(), compresslevel=6)
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Content-Encoding": "gzip", "Accept": "application/json", "User-Agent": auth.CLIENT}
        url = self.base + (path if path.startswith(":") else "/" + path)
        for attempt in range(1, RETRIES + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read() or b"{}").get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if exc.code == 401:
                    sys.exit(f"claims: the token for {self.api} is expired or revoked. Run `claims login --api "
                             f"{self.api}`, then the same command again.")
                if exc.code < 500 or attempt == RETRIES:
                    raise PublishError(f"{self.api} refused it ({exc.code}): {detail or exc.reason}")
                reason = f"{exc.code} {detail or exc.reason}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == RETRIES:
                    raise PublishError(f"cannot reach {self.api}: {getattr(exc, 'reason', exc)}")
                reason = str(getattr(exc, "reason", exc))
            wait = min(60, 5 * 2 ** (attempt - 1))
            print(f"  {reason}; trying again in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
        raise AssertionError("unreachable")

    # --- what publish() asks ------------------------------------------------------
    def existing_release(self, key: str, version: str) -> dict | None:
        return self._existing

    def current_release_id(self, key: str) -> str | None:
        return self._current

    def prefetch(self, dataset_ids: list[str], activity_ids: list[str]) -> None:
        for i in range(0, max(len(dataset_ids), len(activity_ids)), 20_000):
            datasets, contexts = dataset_ids[i:i + 20_000], activity_ids[i:i + 20_000]
            got = self._send("POST", ":check", {"datasets": datasets, "contexts": contexts})
            self._stored.update(got["stored"])
            self._recorded.update(got["recorded"])
            self._asked.update(datasets + contexts)

    def dataset_stored(self, dataset_id: str) -> bool:
        if dataset_id not in self._asked:
            self.prefetch([dataset_id], [])
        return dataset_id in self._stored

    def context_recorded(self, activity_id: str) -> bool:
        if activity_id not in self._asked:
            self.prefetch([], [activity_id])
        return activity_id in self._recorded

    def refuse_other_slice(self, ds: dict) -> None:
        """The API checks this when the dataset arrives."""

    def put_dataset(self, col, ds: dict, vocab, extract: dict | None, pending: list, step=None) -> dict:
        dataset = {"raw_dimensions": {n: d["raw"] for n, d in ds["dims"].items()},
                   "files": {role: f["doc"] for role, f in ds["files"].items()},
                   "where": ds.get("where") or {}, "was_generated_by": ds["doc"]["was_generated_by"]}
        done = self._send("PUT", f"datasets/{urllib.parse.quote(ds['doc']['id'], safe=':')}",
                          {"bundle": self.bundle, "dataset": dataset, "extract": extract})
        if done.get("dataset_id") != ds["doc"]["id"]:
            raise PublishError(f"{self.api} stored {done.get('dataset_id')}, not {ds['doc']['id']}")
        self._stored.add(ds["doc"]["id"])
        return {k: done[k] for k in ("rows", "edges", "contexts", "objects")}

    def record_release(self, col, release: dict, document: dict, objects: dict, dataset_ids: list[str],
                       contexts: list, provenance: list, vocab, template_files: dict, query_files: dict,
                       code_version: str) -> None:
        in_document = {d["id"] for group in ("files", "activities", "datasets", "result_collections")
                       for d in document.get(group) or []}
        extra = [d for _, d in objects.values() if d["id"] not in in_document]
        done = self._send("POST", ":finish", {"bundle": self.bundle, "release": release, "document": document,
                                              "objects": extra, "provenance": provenance,
                                              "code_version": code_version})
        if done.get("release_id") != release["id"]:
            raise PublishError(f"{self.api} recorded {done.get('release_id')}, not {release['id']}")
        self.recorded_release = done
