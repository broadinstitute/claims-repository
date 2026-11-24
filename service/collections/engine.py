"""The claim template engine: one row (or one group of edges) plus a template
becomes Propositions, ClaimScores, ValueLocators, EvidenceItems and Claims,
each minted as DAPPER-ID-1.

Rendering is a pure function of (manifest minting section, template, file
ids, dataset, row values and byte spans). The ingester calls it to learn the
ids and store the data plane; the service calls it again when a claim is
served and refuses the claim unless the result has the same id. No owner
code runs here: templates are `{placeholder}` text plus the fixed functions
below (classify, direction, locate, number formatting, grouping).
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field

from . import identity as idn

# Bumping this re-addresses every claim: it is part of the minting Activity.
ENGINE_VERSION = "1"
SOFTWARE = "claims-repository collections engine"


class TemplateError(ValueError):
    """A placeholder or function the row cannot satisfy."""


class _Ns:
    """Dotted read-only access for str.format: {subject.label}, {metric.beta.text}."""

    def __init__(self, data: dict, path: str):
        self._data, self._path = data, path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._data:
            raise TemplateError(f"unknown placeholder {self._path}.{name}")
        value = self._data[name]
        return _Ns(value, f"{self._path}.{name}") if isinstance(value, dict) else value

    def __format__(self, spec):
        raise TemplateError(f"placeholder {self._path} names a group of fields, not a value")


class _Formatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if key not in kwargs:
            raise TemplateError(f"unknown placeholder {key}")
        value = kwargs[key]
        return _Ns(value, key) if isinstance(value, dict) else value


_FMT = _Formatter()


def fmt(text: str | None, ns: dict) -> str | None:
    if text is None:
        return None
    try:
        return _FMT.vformat(text, (), ns)
    except (KeyError, IndexError, ValueError) as exc:
        if isinstance(exc, TemplateError):
            raise
        raise TemplateError(f"cannot render {text!r}: {exc}") from exc


# --- fixed functions --------------------------------------------------------

def classify(value: float, rule: dict) -> str:
    """Tier label: tiers evaluate top-down, first match wins, `min` inclusive."""
    for tier in rule["tiers"]:
        if "min" not in tier or value >= tier["min"]:
            return tier["label"]
    raise TemplateError("tier rule has no catch-all tier")


def direction(value: float, rule: dict) -> str:
    spec = rule["direction"]
    return "SUPPORTS" if value >= spec["supports_min"] else spec["otherwise"]


_VALUE_RE: dict[str, re.Pattern] = {}


def locate(line: bytes, key: str) -> tuple[int, int, str] | None:
    """(start, length, text) of the serialized number for `key` in one NDJSON
    line, relative to the line start. text is the file's own serialization."""
    pattern = _VALUE_RE.get(key)
    if pattern is None:
        pattern = _VALUE_RE[key] = re.compile(rb'"' + re.escape(key.encode()) + rb'"\s*:\s*(-?[0-9][0-9eE+.\-]*)')
    match = pattern.search(line)
    if match is None:
        return None
    return match.start(1), match.end(1) - match.start(1), match.group(1).decode()


# --- minting context --------------------------------------------------------

@dataclass
class MintContext:
    """Everything one (template, result file) pairing needs to render claims.
    Its Activity is the claims' was_generated_by, so the claims trace to the
    exact template, file and manifest semantics that produced them."""
    minting: dict
    minting_digest: str
    template: dict
    template_file_id: str
    result_file_id: str
    file_name: str
    media_type: str
    activity: dict = field(init=False)

    def __post_init__(self):
        t = self.template
        self.activity = idn.mint("Activity", {
            "name": f"Mint {self.minting['collection']} {t['template']} claims from {self.file_name}",
            "description": (
                f"Rendered claim template {t['template']} v{t['version']} over {self.file_name} with the "
                f"{SOFTWARE} v{ENGINE_VERSION}. Same template, file and manifest semantics give the "
                "same claims, whichever collection release lists them."
            ),
            "activity_type": "ClaimMinting",
            "software_name": SOFTWARE,
            "software_version": ENGINE_VERSION,
            "command": (
                f"claims-repository mint --collection {self.minting['collection']} "
                f"--template {self.template_file_id} --file {self.result_file_id} "
                f"--minting-sha256 {self.minting_digest}"
            ),
        })

    @property
    def agent(self) -> str:
        return self.minting["attribution"]["agent"]

    def relation(self, key: str) -> str:
        return (self.minting.get("relations") or {}).get(key, key)

    def rule(self, name: str) -> dict:
        return self.minting["tier_rules"][name]

    def as_record(self) -> dict:
        """What the service stores to re-render later (mint_contexts row)."""
        return {"minting": self.minting, "minting_digest": self.minting_digest, "template": self.template,
                "template_file_id": self.template_file_id, "result_file_id": self.result_file_id,
                "file_name": self.file_name, "media_type": self.media_type}

    @classmethod
    def from_record(cls, rec: dict) -> "MintContext":
        return cls(**rec)


@dataclass
class Rendered:
    claims: dict[str, dict]                       # claim key -> Claim document
    objects: dict[str, tuple[str, dict]]          # id -> (class, document): the data plane
    propositions: dict[str, str]                  # claim key -> proposition id


def _base_ns(ctx: MintContext, dataset: dict) -> dict:
    return {
        "dataset": dataset,
        "file": {"name": ctx.file_name, "id": ctx.result_file_id},
        "collection": {"key": ctx.minting["collection"]},
    }


def _claim_doc(ctx, spec, ns, prop_id, score_ids, evidence_ids, direction_value):
    return idn.mint("Claim", {
        "name": fmt(spec.get("name"), ns),
        "proposition": prop_id,
        "statement": fmt(spec["statement"], ns),
        "direction": direction_value,
        "has_score": score_ids or None,
        "has_evidence": evidence_ids or None,
        "was_generated_by": ctx.activity["id"],
        "was_attributed_to": [ctx.agent],
        "was_derived_from": [ctx.result_file_id],
        "source_locator": fmt(spec.get("source_locator"), ns),
    })


def _proposition(ctx, spec, ns, add):
    p = spec["proposition"]
    return add("Proposition", {
        "statement": fmt(p["statement"], ns),
        "subject_entity": fmt(p["subject"], ns),
        "relation": ctx.relation(p["relation"]),
        "object_entity": fmt(p["object"], ns),
        "scope": fmt(p.get("scope"), ns),
        "proposition_kind": p["kind"],
    })


def render_row(ctx: MintContext, *, dataset: dict, row: dict, subject: dict, obj: dict,
               metrics: list[str], spans: dict, selector: str) -> Rendered:
    """Claims for one result row. `spans[m]` is (byte_start, byte_length,
    value_text) of metric m in the result file, absolute offsets."""
    objects: dict[str, tuple[str, dict]] = {}

    def add(cls, payload):
        doc = idn.mint(cls, payload)
        objects[doc["id"]] = (cls, doc)
        return doc

    metric_ns = {}
    for m in metrics:
        if m not in spans:
            raise TemplateError(f"no byte span for metric {m!r} ({selector})")
        metric_ns[m] = {"name": m, "value": idn.num(row[m]), "text": spans[m][2]}
    base = {**_base_ns(ctx, dataset), "row": row, "subject": subject, "object": obj}
    metric_defs = ctx.minting["metrics"]
    claims, props = {}, {}
    for spec in ctx.template["claims"]:
        focus = spec.get("focus")
        ns = {**base, "claim": {"key": spec["key"]},
              "metric": {**metric_ns, **(metric_ns[focus] if focus else {})}}
        direction_value = None
        if spec.get("classify"):
            rule = ctx.rule(spec["classify"])
            value = metric_ns[focus]["value"]
            ns["tier"] = {"label": classify(value, rule)}
            ns["rule"] = {"name": spec["classify"], "version": rule["version"], "status": rule.get("status", "")}
            direction_value = direction(value, rule)
        prop = _proposition(ctx, spec, ns, add)
        score_ids = [add("ClaimScore", {
            "score_kind": metric_defs[m]["score_kind"],
            "metric": m,
            "value": metric_ns[m]["value"],
            "interpretation": " ".join(metric_defs[m]["interpretation"].split()),
        })["id"] for m in spec.get("scores", [])]
        evidence_ids = []
        ev = spec.get("evidence")
        if ev:
            for m in ev["per_metric"]:
                m = focus if m == "focus" else m
                start, length, text = spans[m]
                locator = add("ValueLocator", {
                    "file": ctx.result_file_id,
                    "byte_start": start,
                    "byte_length": length,
                    "value_text": text,
                    "selector": f"{selector}; key={m}",
                    "media_type": ctx.media_type,
                })
                ens = {**ns, "metric": {**metric_ns, **metric_ns[m]}}
                evidence_ids.append(add("EvidenceItem", {
                    "name": fmt(ev["name"], ens),
                    "value_locators": [locator["id"]],
                    "source_claims": [claims[k]["id"] for k in ev.get("source_claims", [])] or None,
                    "target_proposition": prop["id"],
                    "direction": direction_value,
                    "explanation": fmt(ev["explanation"], ens),
                    "context": fmt(ev.get("context"), ens),
                    "assumptions": [" ".join(a.split()) for a in ctx.minting.get("assumptions", [])]
                    if ev.get("assumptions") else None,
                    "evidence_source": fmt(ev.get("evidence_source"), ens),
                })["id"])
        claims[spec["key"]] = _claim_doc(ctx, spec, ns, prop["id"], score_ids, evidence_ids, direction_value)
        props[spec["key"]] = prop["id"]
    return Rendered(claims=claims, objects=objects, propositions=props)


def render_group(ctx: MintContext, *, dataset: dict, group: dict) -> Rendered:
    """Claims for one group of edges (e.g. a gene set's roster). `group` has
    id, label, count and parent."""
    objects: dict[str, tuple[str, dict]] = {}

    def add(cls, payload):
        doc = idn.mint(cls, payload)
        objects[doc["id"]] = (cls, doc)
        return doc

    base = {**_base_ns(ctx, dataset), "group": group}
    claims, props = {}, {}
    for spec in ctx.template["claims"]:
        ns = {**base, "claim": {"key": spec["key"]}}
        prop = _proposition(ctx, spec, ns, add)
        claims[spec["key"]] = _claim_doc(ctx, spec, ns, prop["id"], [], [], None)
        props[spec["key"]] = prop["id"]
    return Rendered(claims=claims, objects=objects, propositions=props)


# --- minted entities --------------------------------------------------------

def gene_set_collection(library: str) -> dict:
    return idn.mint("GeneSetCollection", {
        "name": library,
        "description": f"The {library} gene-set library.",
        "member_type": "gene_set",
    })


def gene_set(name: str, library_doc: dict, members: list[str], membership_file_id: str | None) -> dict:
    """A GeneSet as one dataset's membership file observed it. Datasets that
    observe the same members share one id; different rosters get different ids."""
    payload = {
        "name": name,
        "description": (
            f"Gene set {name} in the {library_doc['name']} library. Members are the genes the "
            f"dataset's membership file lists for it{'' if members else ' (none listed)'}."
        ),
        "member_type": "gene",
        "in_gene_set_collection": [library_doc["id"]],
    }
    if members:
        payload["members"] = members
        payload["was_derived_from"] = [membership_file_id]
    return idn.mint("GeneSet", payload)
