"""Validation of deposited DAPPER objects against the vendored DAPPER schema.

POST /v1/deposits accepts any DAPPER node class and the edges of a DAPPER
document. A node is accepted only when:

  1. its class is a DAPPER document class (dapper_identity.DOC_GROUPS), so
     mixins and abstract classes such as Agent cannot be deposited;
  2. every key is a slot of that class, every required slot is present,
     multivalued slots hold lists and single-valued slots do not, a value is
     a string, number or boolean as its slot's type says (dates and URIs are
     checked only to be strings) or one of its enum's values, and a `dapper:`
     reference names a class the slot's range allows and is written
     `dapper:{Class}.{digest}`;
  3. its id is the DAPPER-ID-1 address of its content, computed with dapper's
     own identity module (service/collections/identity.py).

Not checked here, because dapper's lint tooling is not vendored: the schema's
conditional `rules`, the provenance lint profiles (scientific-account,
result-collection, ...) and whether a reference resolves. The deposit route
reports unresolved references; it does not refuse them. The inside of an
inlined object (Dataset.dimension_values, ...) is hashed and stored as given
but not checked against its class.

Pure: no database, no HTTP. Every failure is an `Invalid` with a message a
depositor can act on.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .collections import identity as cidn
from .refs import DAPPER_ID_RE, MAX_ID_LENGTH

_dapper = cidn._dapper
REF_CLASS_RE = re.compile(r"^dapper:([A-Za-z]+)\.")
BARE_DIGEST_RE = re.compile(r"[A-Za-z0-9_-]{32}\Z")
MAX_ERRORS = 8  # per object: enough to fix a document in one pass, bounded for the response
# Values in one node, counted before dapper's identity code builds a graph from them. dapper's own bound: a node
# that dapper can mint is never refused here for its size.
MAX_VALUES = _dapper.MAX_TRIPLES


class Invalid(ValueError):
    """A deposited object or edge that is not valid DAPPER as written."""


@lru_cache(maxsize=1)
def node_classes() -> frozenset[str]:
    return frozenset(_dapper.DOC_GROUPS.values())


@lru_cache(maxsize=1)
def node_groups() -> dict[str, str]:
    """Document group key -> node class, as dapper's own tooling names them (`claims`, `evidence_items`, ...)."""
    return dict(_dapper.DOC_GROUPS)


@lru_cache(maxsize=1)
def edge_groups() -> dict[str, str]:
    """Document group key -> edge class: `Used` -> `used_edges`, `WasGeneratedBy` -> `was_generated_by_edges`."""
    view = cidn.schema_view()
    groups = {}
    for name, cls in view.all_classes().items():
        if name != "Edge" and not cls.abstract and not cls.mixin and "Edge" in view.class_ancestors(name):
            groups[re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() + "_edges"] = name
    return groups


def _base(view, range_name: str) -> str:
    """The JSON kind of a LinkML type: 'int', 'number', 'bool' or 'str'."""
    for name in view.type_ancestors(range_name):
        base = view.get_type(name).base
        if base:
            if base == "int": return "int"
            if base in ("float", "Decimal"): return "number"
            if base == "Bool": return "bool"
            return "str"
    return "str"


@lru_cache(maxsize=None)
def slots(class_name: str) -> dict[str, dict]:
    view = cidn.schema_view()
    result = {}
    for slot in view.class_induced_slots(class_name):
        ranges = [r for r in [slot.range, *[a.range for a in (slot.any_of or [])]] if r]
        classes = frozenset(r for r in ranges if view.get_class(r))
        enums = [r for r in ranges if view.get_enum(r)]
        types = [r for r in ranges if view.get_type(r)]
        result[slot.name] = {
            "required": bool(slot.required), "multivalued": bool(slot.multivalued),
            "inlined": bool(slot.inlined or slot.inlined_as_list), "classes": classes,
            "enum": frozenset(v for e in enums for v in view.get_enum(e).permissible_values) if enums else None,
            "kinds": frozenset(_base(view, t) for t in types),
        }
    return result


@lru_cache(maxsize=1)
def _schema_classes() -> frozenset[str]:
    return frozenset(cidn.schema_view().all_classes())


@lru_cache(maxsize=None)
def _ancestors(class_name: str) -> frozenset[str]:
    return frozenset(cidn.schema_view().class_ancestors(class_name, mixins=True))


def _is_a(class_name: str) -> frozenset[str]:
    # Only the schema's own class names reach the cache: a caller's reference cannot grow it.
    return _ancestors(class_name) if class_name in _schema_classes() else frozenset()


@lru_cache(maxsize=None)
def hashable_slots(class_name: str) -> frozenset[str]:
    """The slots dapper's identity hashes for this class."""
    return frozenset(_dapper.hashable_slot_names(cidn.schema_view(), class_name))


def _count(value) -> int:
    if isinstance(value, dict):
        return 1 + sum(_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_count(item) for item in value)
    return 1


def _spelling_errors(value, slot: str, errors: list[str]) -> None:
    """Every string in a node, however deeply nested: a DAPPER reference has one spelling here.

    dapper's identity replaces any reference it recognises (the CURIE, the full namespace URI, `dapper:.digest`)
    by its bare digest before hashing, so those spellings hash alike. Only `dapper:{Class}.{digest}` is accepted,
    which keeps the reference index and the stored document in step with the address."""
    if isinstance(value, dict):
        for item in value.values():
            _spelling_errors(item, slot, errors)
    elif isinstance(value, list):
        for item in value:
            _spelling_errors(item, slot, errors)
    elif isinstance(value, str) and _dapper.digest_of(value) is not None:
        if not DAPPER_ID_RE.match(value):
            errors.append(f"{slot} holds a DAPPER reference not written as dapper:{{Class}}.{{digest}}")
        elif len(value) > MAX_ID_LENGTH:
            errors.append(f"{slot} references an id longer than {MAX_ID_LENGTH} characters")


def _value_error(slot: str, info: dict, value) -> str | None:
    """Why one (non-list) value does not fit its slot, or None."""
    if isinstance(value, dict):
        return None if info["inlined"] and info["classes"] else f"{slot} must not be a nested object"
    if isinstance(value, list):
        return f"{slot} must not hold a list inside a list"
    if isinstance(value, str):
        ref = REF_CLASS_RE.match(value)
        if ref and DAPPER_ID_RE.match(value):
            if len(value) > MAX_ID_LENGTH:
                return f"{slot} references an id longer than {MAX_ID_LENGTH} characters"
            if info["classes"] and not (_is_a(ref.group(1)) & info["classes"]):
                return f"{slot} must reference {' or '.join(sorted(info['classes']))}, not {ref.group(1)}"
            return None
        if info["classes"] and BARE_DIGEST_RE.match(value):
            # A bare digest hashes like the reference it abbreviates but is not one: nothing could resolve it.
            return f"{slot} must reference {' or '.join(sorted(info['classes']))} as dapper:{{Class}}.{{digest}}, not a bare digest"
        if info["enum"] is not None and not info["classes"] and not info["kinds"]:
            return None if value in info["enum"] else f"{slot} must be one of {', '.join(sorted(info['enum']))}"
        if info["classes"] or info["enum"] is not None or "str" in info["kinds"] or not info["kinds"]:
            return None
        return f"{slot} must be a {' or '.join(sorted(info['kinds']))}, not a string"
    if isinstance(value, bool):
        return None if "bool" in info["kinds"] else f"{slot} must not be a boolean"
    if isinstance(value, int):
        return None if info["kinds"] & {"int", "number"} else f"{slot} must not be a number"
    if isinstance(value, float):
        if "number" in info["kinds"] or ("int" in info["kinds"] and value.is_integer()):
            return None
        return f"{slot} must be a whole number" if "int" in info["kinds"] else f"{slot} must not be a number"
    return f"{slot} has an unsupported value"


def _shape_errors(class_name: str, content: dict) -> list[str]:
    known = slots(class_name)
    errors = [f"{class_name} has no slot {key!r}" for key in sorted(set(content) - set(known))]
    errors += [f"{name} is required" for name, info in sorted(known.items())
               if info["required"] and name != "id" and name not in content]
    for key, value in content.items():
        info = known.get(key)
        if info is None:
            continue
        if info["multivalued"]:
            if not isinstance(value, list):
                errors.append(f"{key} must be a list")
                continue
            values = value
        else:
            if isinstance(value, list):
                errors.append(f"{key} must be a single value, not a list")
                continue
            values = [value]
        for item in values:
            problem = _value_error(key, info, item)
            if problem:
                errors.append(problem)
                break
    return errors


def validate_node(node, expected_class: str | None = None) -> tuple[str, str, dict]:
    """(id, class, the document to store) for a valid DAPPER node, else Invalid.

    The stored document is the plain DAPPER node: no `class_name` envelope key (the class is in the id) and no
    null-valued slots (dapper's identity skips them, so they cannot distinguish two documents)."""
    if not isinstance(node, dict):
        raise Invalid("every object must be a JSON object")
    oid = node.get("id")
    if not isinstance(oid, str) or len(oid) > MAX_ID_LENGTH:
        raise Invalid(f"object id {str(oid)[:140]!r} is not a dapper identifier of at most {MAX_ID_LENGTH} characters")
    class_name = cidn.class_of(oid)
    if class_name is None:
        raise Invalid(f"{oid[:140]!r} is not a DAPPER-ID-1 identifier (dapper:{{Class}}.{{32-character digest}})")
    if class_name not in node_classes():
        raise Invalid(f"{oid}: {class_name} is not a DAPPER class that can be deposited")
    if expected_class is not None and class_name != expected_class:
        raise Invalid(f"{oid}: listed under {expected_class} but its id names {class_name}")
    if "class_name" in node and node["class_name"] != class_name:
        raise Invalid(f"{oid}: class_name {str(node['class_name'])[:60]!r} disagrees with the id's class {class_name}")
    content = {k: v for k, v in node.items() if k not in ("id", "class_name") and v is not None}
    if _count(content) > MAX_VALUES:
        raise Invalid(f"{oid}: an object holds at most {MAX_VALUES} values")
    errors = _shape_errors(class_name, content)
    for key, value in content.items():
        _spelling_errors(value, key, errors)
    if errors:
        more = f" (and {len(errors) - MAX_ERRORS} more)" if len(errors) > MAX_ERRORS else ""
        raise Invalid(f"{oid}: " + "; ".join(errors[:MAX_ERRORS]) + more)
    document = {"id": oid, **content}
    try:
        verified = cidn.verify(document)
    except Exception as exc:  # noqa: BLE001 — dapper's identity code rejects content it cannot canonicalize
        raise Invalid(f"{oid}: content cannot be given a DAPPER-ID-1 address ({type(exc).__name__}: {str(exc)[:200]})")
    if not verified:
        raise Invalid(f"{oid}: the id is not the DAPPER-ID-1 address of this content")
    return oid, class_name, document


def validate_edge(class_name: str, edge) -> dict:
    """The edge to store for a valid DAPPER edge of that class, else Invalid."""
    where = f"{class_name} edge"
    if not isinstance(edge, dict):
        raise Invalid(f"{where}: every edge must be a JSON object")
    content = {k: v for k, v in edge.items() if v is not None}
    errors = _shape_errors(class_name, content)
    for end in ("subject", "object"):
        if not isinstance(content.get(end), str) or not content[end].strip():
            errors.append(f"{end} is required")
    if errors:
        raise Invalid(f"{where} {str(edge.get('subject'))[:80]!r} -> {str(edge.get('object'))[:80]!r}: " + "; ".join(errors[:MAX_ERRORS]))
    return content


def document_items(document) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """([(node class, node)], [(edge class, edge)]) of a DAPPER document: lists of nodes and edges keyed by dapper's
    group names. An unknown key is refused, never skipped: a misspelled group would otherwise drop its objects."""
    if not isinstance(document, dict):
        raise Invalid("document must be a JSON object of DAPPER groups (claims, propositions, used_edges, ...)")
    nodes, edges = [], []
    for key, rows in document.items():
        node_class, edge_class = node_groups().get(key), edge_groups().get(key)
        if node_class is None and edge_class is None:
            raise Invalid(f"document has no DAPPER group {str(key)[:80]!r}")
        if not isinstance(rows, list):
            raise Invalid(f"document.{key} must be a list")
        for row in rows:
            (nodes if node_class else edges).append((node_class or edge_class, row))
    return nodes, edges
