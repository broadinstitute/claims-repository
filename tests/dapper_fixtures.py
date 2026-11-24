"""Real DAPPER objects for tests, written the readable way.

POST /v1/deposits accepts only valid DAPPER nodes at their DAPPER-ID-1 addresses, and those addresses are
digests nobody can write by hand. Tests therefore describe a bundle with symbolic ids
("dapper:Claim.SYNTHG01") and `remint` turns it into the real thing: every object minted with dapper's own
identity code, in dependency order, with references rewritten to the minted ids. The symbolic names stay
usable through `R` (one id) and `ids` (every symbolic id inside a string, for GraphQL queries and URLs).

Nothing here rewrites what the service returns: a test compares responses with the real ids `R` gives it.
"""
from __future__ import annotations

import copy
import re

from service.collections import identity as cidn

IDS: dict[str, str] = {}          # symbolic id -> minted id, shared by every bundle in the session
_CONTENT: dict[str, dict] = {}    # symbolic id -> the plain minted node
ATTRIBUTION = "urn:claims-repository:test:depositor"
_ACTIVITY = cidn.mint("Activity", {"name": "claims-repository test deposit",
                                   "description": "Fixture activity: DAPPER requires every Claim to name what generated it."})


def R(symbolic: str) -> str:
    """The minted id a symbolic fixture id stands for."""
    return IDS[symbolic]


def ids(text: str) -> str:
    """`text` with every known symbolic id replaced by its minted id (longest names first)."""
    for symbolic in sorted(IDS, key=len, reverse=True):
        text = re.sub(re.escape(symbolic) + r"(?![A-Za-z0-9_-])", IDS[symbolic], text)
    return text


def _swap(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_swap(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _swap(item, mapping) for key, item in value.items()}
    return value


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def remint(bundle: dict, provenance: bool = True) -> dict:
    """The bundle as valid DAPPER: plain nodes at their DAPPER-ID-1 addresses, hints pointing at them.

    Objects carry `class_name` and a symbolic `id`. A Claim without `was_generated_by` / `was_attributed_to`
    (both required by DAPPER) gets the fixture Activity and attribution unless `provenance` is False; the
    Activity is then part of the bundle. Symbolic ids that no bundle defines (a File nobody deposits) are left
    as they are: a reference need not resolve."""
    objects = copy.deepcopy(bundle["objects"])
    pending = {obj["id"]: obj for obj in objects}
    assert len(pending) == len(objects), "a symbolic id is used twice in one bundle"
    minted, needs_activity = [], False
    while pending:
        ready = [symbolic for symbolic, obj in pending.items()
                 if not any(ref in pending and ref != symbolic for ref in _strings({k: v for k, v in obj.items() if k != "id"}))]
        assert ready, f"reference cycle among {sorted(pending)}"
        for symbolic in ready:
            obj = pending.pop(symbolic)
            class_name = obj["class_name"]
            content = _swap({k: v for k, v in obj.items() if k not in ("id", "class_name") and v is not None}, IDS)
            if class_name == "Claim" and provenance:
                if "was_generated_by" not in content:
                    content["was_generated_by"], needs_activity = _ACTIVITY["id"], True
                content.setdefault("was_attributed_to", [ATTRIBUTION])
            node = cidn.mint(class_name, content)
            previous = _CONTENT.get(symbolic)
            assert previous is None or previous == node, f"{symbolic} was minted earlier with different content"
            holder = next((name for name, real in IDS.items() if real == node["id"] and name != symbolic), None)
            # Content addressing merges identical objects. Two symbolic names for one object would silently change
            # what a test counts, so a fixture must make its objects differ in content.
            assert holder is None, f"{symbolic} and {holder} have identical content and so one id"
            IDS[symbolic], _CONTENT[symbolic] = node["id"], node
            minted.append(node)
    if needs_activity and all(node["id"] != _ACTIVITY["id"] for node in minted):
        minted.insert(0, dict(_ACTIVITY))
    result = {"objects": minted}
    if bundle.get("index_hints") is not None:
        result["index_hints"] = _swap(copy.deepcopy(bundle["index_hints"]), IDS)
    return result
