"""Provenance for a release: DAPPER provenance documents that say how the
result files were produced (the run Activity, its software and parameters, the
files it wrote).

A release lists them under `provenance`, each one either

- `path:` a document kept with the collection. Its nodes are validated and
  stored with the release, so they resolve here; or
- `url:` a document hosted elsewhere, such as the provenance indexer
  (`.../v1/documents/<id>`). It is fetched, checked against its pinned
  sha256 and validated, but not stored: its ids resolve where it is hosted
  (`resolver`, by default the indexer's base URL).

Either way the publisher matches the release's result files to the
document's File nodes (by sha256, then by location) and links each dataset
and each of its files to the Activity that generated them, with WasGeneratedBy
edges in the release's DAPPER graph. Edges are not content: no dataset id and
no claim id depends on provenance, so it can be added or corrected in any later
release without re-reading a file.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import identity as idn


class ProvenanceError(ValueError):
    pass


@dataclass
class Source:
    entry: dict
    stored: bool                      # path: stored here; url: referenced
    sha256: str
    resolver: str | None
    nodes: dict = field(default_factory=dict)        # id -> (class, document)
    edges: dict = field(default_factory=dict)        # group -> [edge]

    @property
    def label(self) -> str:
        return self.entry.get("path") or self.entry["url"]


def default_resolver(url: str) -> str | None:
    """The provenance indexer serves documents at <base>/v1/documents/<id> and
    entities at <base>/v1/entities/<id>."""
    marker = "/v1/documents/"
    return url.split(marker, 1)[0] if marker in url else None


def _parse(data: bytes, where: str) -> dict:
    try:
        doc = json.loads(data)
    except json.JSONDecodeError:
        try:
            doc = yaml.safe_load(data)
        except yaml.YAMLError as exc:
            raise ProvenanceError(f"{where}: neither JSON nor YAML ({exc})") from exc
    if isinstance(doc, dict) and "payload" in doc and isinstance(doc["payload"], dict):
        doc = doc["payload"]  # a provenance indexer document response
    if not isinstance(doc, dict):
        raise ProvenanceError(f"{where}: not a DAPPER document")
    return doc


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json, application/yaml;q=0.9"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def load(entry: dict, base: Path, fetch=fetch_url) -> Source:
    """Read, check and validate one provenance entry of a release."""
    from .. import dapper_validation as dv

    if "path" in entry:
        path = (base / entry["path"]).resolve()
        if not path.is_file():
            raise ProvenanceError(f"provenance file {path} does not exist")
        data, stored, resolver = path.read_bytes(), True, None
    elif "url" in entry:
        data = fetch(entry["url"])
        stored, resolver = False, entry.get("resolver") or default_resolver(entry["url"])
    else:
        raise ProvenanceError("a provenance entry needs `path` or `url`")
    sha = hashlib.sha256(data).hexdigest()
    if entry.get("sha256") and entry["sha256"] != sha:
        raise ProvenanceError(f"{entry.get('path') or entry['url']} has sha256 {sha}, the release pinned "
                              f"{entry['sha256']}: the document changed since the release was planned")
    doc = _parse(data, entry.get("path") or entry["url"])
    src = Source(entry=entry, stored=stored, sha256=sha, resolver=resolver)
    try:
        nodes, edges = dv.document_items({k: v for k, v in doc.items() if k not in ("prefixes", "id")})
    except dv.Invalid as exc:
        raise ProvenanceError(f"{src.label}: {exc}") from exc
    for cls, node in nodes:
        if not isinstance(node, dict) or not idn.verify(node):
            raise ProvenanceError(f"{src.label}: {cls} {str((node or {}).get('id'))[:80]} is not a true DAPPER-ID-1 "
                                  "address of its content")
        src.nodes[node["id"]] = (cls, node)
    groups = {v: k for k, v in dv.edge_groups().items()}
    for cls, edge in edges:
        src.edges.setdefault(groups[cls], []).append(edge)
    return src


class Index:
    """Which provenance Activity generated a result file."""

    def __init__(self, sources: list[Source]):
        self.sources = sources
        self.by_sha: dict[str, str] = {}
        self.by_location: dict[str, str] = {}
        self.explicit = None
        for s in sources:
            files = {nid: doc for nid, (cls, doc) in s.nodes.items() if cls == "File"}
            generated = {e["subject"]: e["object"] for e in s.edges.get("was_generated_by_edges", [])
                         if e.get("subject") in files}
            for fid, doc in files.items():
                act = generated.get(fid)
                if act is None:
                    continue
                if doc.get("sha256"):
                    self.by_sha[doc["sha256"]] = act
                if doc.get("location"):
                    self.by_location[doc["location"]] = act
            if s.entry.get("run"):
                if self.explicit and self.explicit != s.entry["run"]:
                    raise ProvenanceError("two provenance entries name different `run` activities")
                self.explicit = s.entry["run"]

    def run_for(self, files: dict[str, tuple]) -> str | None:
        """The one Activity that generated these files ({role: (sha256, location)}), if the documents say."""
        found = set()
        for sha, location in files.values():
            act = self.by_sha.get(sha) or self.by_location.get(location)
            if act:
                found.add(act)
        if len(found) > 1:
            raise ProvenanceError(f"a dataset's files were generated by several activities: {sorted(found)}")
        return found.pop() if found else self.explicit

    def known(self, activity_id: str) -> bool:
        return any(activity_id in s.nodes for s in self.sources)

    def record(self) -> list[dict]:
        """What a release records about its provenance (discovery and resolution)."""
        return [{**({"path": s.entry["path"]} if s.stored else {"url": s.entry["url"]}),
                 "sha256": s.sha256, "stored": s.stored, "resolver": s.resolver,
                 "ids": sorted(s.nodes)} for s in self.sources]
