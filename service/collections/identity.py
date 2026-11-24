"""DAPPER-ID-1 minting through dapper's own identity module.

vendor/dapper holds dapper's `dapper_identity.py` and schema at a pinned
commit (scripts/vendor_dapper.py, VENDOR.json), so every id minted here is the
id dapper's tooling computes for the same content. Documents are plain DAPPER
nodes: no `class_name` key. The class is part of the id
(`dapper:{Class}.{digest}`) and is stored beside the payload.
"""
from __future__ import annotations

import json
import re
import sys
import threading
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "dapper"
sys.path.insert(0, str(VENDOR / "schema" / "identity"))
import dapper_identity as _dapper  # noqa: E402

PROFILE = _dapper.PROFILE  # "DAPPER-ID-1"
VENDOR_INFO = json.loads((VENDOR / "VENDOR.json").read_text())
DAPPER_VERSION = re.search(r"(?m)^version:\s*(\S+)", (VENDOR / "schema" / "dapper.yaml").read_text()).group(1)
ID_RE = re.compile(r"^dapper:([A-Za-z]+)\.([A-Za-z0-9_-]{32})\Z")

_lock = threading.Lock()


@lru_cache(maxsize=1)
def schema_view():
    """dapper's SchemaView, built once (it parses the whole schema)."""
    with _lock:
        return _dapper.load_schema(_dapper.SCHEMA_PATH)


@lru_cache(maxsize=None)
def slots_of(class_name: str) -> frozenset[str]:
    return frozenset(s.name for s in schema_view().class_induced_slots(class_name))


def class_of(object_id: str) -> str | None:
    match = ID_RE.match(object_id or "")
    return match.group(1) if match else None


class MintError(ValueError):
    """A document that cannot be minted as written."""


def mint(class_name: str, payload: dict) -> dict:
    """The document with its DAPPER-ID-1 id first. None values are dropped.

    Every key must be a slot of the class: the digest covers hashable slots
    only, so a misspelled field would otherwise vanish from identity silently.
    """
    content = {k: v for k, v in payload.items() if k != "id" and v is not None}
    unknown = sorted(set(content) - slots_of(class_name))
    if unknown:
        raise MintError(f"{class_name} has no slot(s) {unknown}")
    return {"id": _dapper.compute_id(content, class_name, schema_view()), **content}


def verify(document: dict) -> bool:
    """True when the document's id is the DAPPER-ID-1 address of its content."""
    cls = class_of(document.get("id", ""))
    if cls is None:
        return False
    try:
        return mint(cls, document)["id"] == document["id"]
    except MintError:
        return False


def num(value) -> float:
    """Canonical float for scores: ints and floats alike, -0.0 as 0.0, and
    non-finite values refused rather than hashed into unreproducible claims."""
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise MintError(f"non-finite metric value {value!r}")
    return 0.0 if v == 0 else v


def canonical(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))
