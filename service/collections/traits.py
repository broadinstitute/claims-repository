"""Dimension values through the KPN trait registry (kpn_traits, loaded by
scripts/load_kpn_traits.py): a legacy or PIGEAN key such as IBD resolves to
its KPN.TRAIT id and name. Unregistered keys pass through unchanged."""
from __future__ import annotations


class TraitRegistry:
    def __init__(self, con):
        self.by_key: dict[str, tuple[str, str]] = {}
        self.aliases: dict[str, list[str]] = {}
        rows = con.execute("SELECT trait_id, legacy_id, pigean_id, name FROM kpn_traits").fetchall()
        for r in rows:  # legacy first, so a pigean_id wins a clash
            self.by_key[r["legacy_id"]] = (r["trait_id"], r["name"])
        for r in rows:
            if r["pigean_id"]:
                self.by_key[r["pigean_id"]] = (r["trait_id"], r["name"])
            self.by_key[r["trait_id"]] = (r["trait_id"], r["name"])
            self.aliases[r["trait_id"]] = [k for k in (r["trait_id"], r["pigean_id"], r["legacy_id"]) if k]
        self.unresolved: set[str] = set()

    def resolve(self, key: str) -> tuple[str, str]:
        """(canonical value, label)."""
        if key in self.by_key:
            return self.by_key[key]
        self.unresolved.add(key)
        return key, key


def resolve_dimension(registry: TraitRegistry | None, spec: dict, raw: str) -> tuple[str, str]:
    if spec.get("resolve") == "kpn-traits" and registry is not None:
        return registry.resolve(raw)
    return raw, raw
