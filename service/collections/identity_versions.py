"""Every DAPPER version this service can mint with, side by side.

vendor/dapper is the current version: deposits are minted and checked with it
(service/collections/identity.py). vendor/dapper-archive/<version>/ keeps each
version vendored before it, with its own dapper_identity.py and schema, so an
id minted under an older version can still be recomputed and compared with
what the current version mints (scripts/rescan_identity.py,
docs/dapper-version-migration.md).

DAPPER-ID-1 hashes the class name and, for each hashable slot present, the
slot's name and value. So two versions mint the same id for a payload exactly
when they agree on which of its slots are hashed. `hashed` returns that
slot -> value map, and comparing it between versions says which slots changed.
"""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT = ROOT / "vendor" / "dapper"
ARCHIVE = ROOT / "vendor" / "dapper-archive"


@dataclass
class DapperVersion:
    root: Path
    version: str = field(init=False)
    _cache: dict = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self):
        self.version = re.search(r"(?m)^version:\s*(\S+)", (self.root / "schema" / "dapper.yaml").read_text()).group(1)

    @cached_property
    def module(self):
        # 0.3 keeps identity/ beside schema/; 0.2 kept it inside schema/.
        path = next(p for p in (self.root / "identity" / "dapper_identity.py",
                                self.root / "schema" / "identity" / "dapper_identity.py") if p.exists())
        name = "dapper_identity_" + re.sub(r"\W", "_", self.version)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @cached_property
    def schema(self):
        return self.module.load_schema(self.module.SCHEMA_PATH)

    @cached_property
    def commit(self) -> str | None:
        info = self.root / "VENDOR.json"
        return json.loads(info.read_text()).get("commit") if info.exists() else None

    def _cached(self, key, compute):
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def has_class(self, class_name: str) -> bool:
        return self._cached(("class", class_name), lambda: self.schema.get_class(class_name) is not None)

    def slots(self, class_name: str) -> set[str]:
        return self._cached(("slots", class_name),
                            lambda: {s.name for s in self.schema.class_induced_slots(class_name)})

    def hashable(self, class_name: str) -> set[str]:
        return self._cached(("hashable", class_name),
                            lambda: set(self.module.hashable_slot_names(self.schema, class_name)))

    def unknown_slots(self, class_name: str, payload: dict) -> list[str]:
        return sorted(k for k in payload if k != "id" and k not in self.slots(class_name))

    def hashed(self, class_name: str, payload: dict) -> dict:
        """The slot -> value pairs this version puts into the id."""
        keep = self.hashable(class_name)
        return {k: v for k, v in payload.items() if k in keep and v is not None}

    def mint_id(self, class_name: str, payload: dict) -> str:
        """The id this version mints for the payload, as the service mints it (no self id)."""
        content = {k: v for k, v in payload.items() if k != "id" and v is not None}
        return self.module.compute_id(content, class_name, self.schema)


def current() -> DapperVersion:
    return DapperVersion(CURRENT)


def archived() -> list[DapperVersion]:
    """Older vendored versions, oldest first by directory name."""
    if not ARCHIVE.exists():
        return []
    return [DapperVersion(p) for p in sorted(ARCHIVE.iterdir()) if (p / "schema" / "dapper.yaml").exists()]
