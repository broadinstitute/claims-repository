"""Publish a collection release: manifest + release spec + result files in,
a DAPPER ResultCollection and its datasets, rows, edges, metrics and mint
contexts out.

No claim is rendered here. A mint context (one template over one result
file) is all a release records about its claims: the service renders a
row's claims from it when the row is served, and stores a claim (with the
propositions, scores and evidence it references) the first time it is
served. A template or wording change therefore costs one mint context per
dataset, not a pass over every row. What publishing stores is the data
plane rows come from: Files, Activities, Datasets, the ResultCollection and
GeneSet entities.

Datasets are content-addressed and shared by every release that lists them:
a dataset already ingested is not read again, and a (template, file) pairing
already rendered is not rendered again.

A release may hold only part of its result files (`where` in the release
specification: strict lower bounds per row kind and metric). A row below a
bound is not ingested: no result row, proposition, evidence or claim. A
minted entity (a gene set) exists only for a kept row, with its whole roster:
every edge that ends at it is ingested, whatever the other end's score, since
its roster claim counts them. The bounds are named in the Dataset record, so
the same files under other bounds are a different dataset; they enter no claim
id. Result files may be given by upstream location with their sha256 and size
instead of a local path; each dataset is then fetched when it is ingested and
checked against that sha256.

Each dataset is ingested, rendered and stored in its own transaction, and the
release is recorded last, so an interrupted publish resumes where it stopped
and nothing is served from a release that is not complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import engine
from . import identity as idn
from .manifest import Collection, ManifestError, SourceFile
from .traits import MappingError, Vocabulary, collection_vocabulary, resolve_dimension, unmapped_policy, vocabulary_files
from ..refs import dapper_refs
from .. import text_index


class PublishError(ValueError):
    """A release that cannot be published as specified."""


@dataclass
class Publication:
    release_id: str
    version: str
    document: dict
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # False when the publish stopped at max_ingest: datasets are stored, the release is not recorded.
    complete: bool = True
    # dry_run only: what a real publish would write with (datasets, template
    # File docs, vocab, the version already stored), for the admin check.
    plan: dict | None = None


# Reading the state of a directory an admin chose must not run commands its
# repository configures (fsmonitor, hooks) or rewrite its .git/index.
_GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


def _git(*args: str) -> str:
    return subprocess.run([*_GIT, *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout.strip()


def _git_info(path: Path) -> tuple[str | None, bool, str | None]:
    """(commit, dirty, path relative to the repository root) for a file."""
    try:
        top = _git("-C", str(path.parent), "rev-parse", "--show-toplevel")
        commit = _git("-C", top, "rev-parse", "HEAD")
        dirty = bool(_git("-C", top, "status", "--porcelain", "--", str(path.parent)))
        return commit, dirty, str(path.resolve().relative_to(Path(top).resolve()))
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None, False, None


def _source_file_doc(src: SourceFile, description: str, mime_type: str = "application/yaml",
                     location: str | None = None) -> dict:
    """A collection file's File record. Its location is the file's path in its git repository (`location`
    gives it when there is no repository to ask, as on the API for a push), else its collection path."""
    if location is None:
        _, _, location = _git_info(src.path)
    return idn.mint("File", {
        "filename": src.path.name,
        "description": description,
        "mime_type": mime_type,
        "sha256": src.sha256,
        "size_in_bytes": len(src.data),
        "location": location or src.rel,
    })


def vocabulary_file_docs(col: Collection, prefix: str | None = None) -> list[dict]:
    """The File records of the collection's mapping and cross-reference files (prefix: as collection_file_docs)."""
    out = []
    for block, dim, rel in vocabulary_files(col.doc):
        path = (col.root / rel).resolve()
        what = "mapping" if block == "mappings" else "cross-references"
        location = None if prefix is None else (f"{prefix}/{rel}" if prefix else rel)
        out.append(_source_file_doc(SourceFile(path=path, rel=rel, data=path.read_bytes(), doc={}),
                                    f"{col.key} {what} for {dim}.", "text/tab-separated-values", location=location))
    return out


def publication_doc(col: Collection, version: str, code_version: str) -> dict:
    """The Activity that publishes a release."""
    return idn.mint("Activity", {
        "name": f"Publish {col.key} {version}",
        "description": (f"Built release {version} of the {col.key} collection from its manifest, "
                        "checked each result file's sha256, and recorded the release."),
        "activity_type": "ResultCollectionPublication",
        "software_name": engine.SOFTWARE,
        "software_version": engine.ENGINE_VERSION,
        "code_version": code_version,
    })


def release_doc(col: Collection, version: str, members: list[str], publication_id: str, previous_id: str | None,
                manifest_file: dict, template_files: dict, query_files: dict) -> dict:
    """A release's ResultCollection record."""
    doc = col.doc
    return idn.mint("ResultCollection", {
        "name": f"{doc['title']} {version}",
        "collection_key": col.key,
        "version": version,
        "description": doc.get("description"),
        "methods": doc["methods"],
        "dataset_dimensions": [d["name"] for d in doc["dataset_dimensions"]],
        "members": members,
        "n_members": len(members),
        "has_claim_template": [template_files[t.doc["template"]]["id"] for t in col.templates],
        "has_query_template": [query_files[n]["id"] for n in col.queries] or None,
        "has_manifest_file": manifest_file["id"],
        "was_generated_by": publication_id,
        "was_revision_of": previous_id,
    })


def repository_prefix(col: Collection) -> str:
    """The collection directory's path in its git repository ("" when it is not in one): what a push sends so
    the API computes the same File records for the collection's files (collection_file_docs)."""
    _, _, rel = _git_info(col.manifest.path)
    return rel.rsplit("/", 1)[0] if rel and "/" in rel else ""


def collection_file_docs(col: Collection, prefix: str | None = None) -> tuple[dict, dict, dict]:
    """The File records of a collection's manifest, its templates (by template name) and its queries (by
    name). `prefix` is the collection directory's path in its repository (repository_prefix); None asks git."""
    def doc(src: SourceFile, description: str) -> dict:
        location = None if prefix is None else (f"{prefix}/{src.rel}" if prefix else src.rel)
        return _source_file_doc(src, description, location=location)
    return (doc(col.manifest, f"{col.key} collection manifest."),
            {t.doc["template"]: doc(t, f"{col.key} claim template {t.doc['template']} v{t.doc['version']}.")
             for t in col.templates},
            {name: doc(q, f"{col.key} query template {name}.") for name, q in col.queries.items()})


_PY_NONE = re.compile(rb'(?<=": )None(?=[,}])')


def _ndjson(path: Path, python_none: bool = False):
    """(row, line bytes, absolute byte offset) per non-blank line.
    python_none: read a bare Python `None` value as null. Some aggregator runs
    write their membership file that way; it is accepted only for edge files,
    whose values are never ingested (only the two ends of each edge are)."""
    offset = 0
    with open(path, "rb") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if stripped:
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    if not python_none:
                        raise PublishError(f"{path.name} line {lineno}: not valid JSON ({exc.msg})") from exc
                    try:
                        row = json.loads(_PY_NONE.sub(b"null", stripped))
                    except json.JSONDecodeError as exc2:
                        raise PublishError(f"{path.name} line {lineno}: not valid JSON ({exc2.msg})") from exc2
                if not isinstance(row, dict):
                    raise PublishError(f"{path.name} line {lineno}: expected a JSON object, "
                                       f"got {type(row).__name__}")
                yield row, line, offset
            offset += len(line)


class _Entities:
    """Identifiers for the collection's entity types within one dataset."""

    def __init__(self, col: Collection, dims: dict):
        self.col, self.dims = col, dims
        self.minted: dict[tuple[str, str], dict] = {}   # (entity type, value) -> DAPPER doc
        self.libraries: dict[str, dict] = {}

    def spec(self, entity: str) -> dict:
        return self.col.doc["entities"][entity]

    def ident(self, entity: str, value: str) -> tuple[str, str]:
        """(id, label) for a value of an entity type."""
        spec = self.spec(entity)
        if "dimension" in spec:
            dim = self.dims[spec["dimension"]]
            return dim["value"], dim["label"]
        if "id" in spec:
            return spec["id"].format(value=value), value
        doc = self.minted.get((entity, value))
        if doc is None:
            raise PublishError(f"{entity} {value!r} was not minted for this dataset")
        return doc["id"], value

    def library(self, entity: str) -> dict:
        lib = self.dims[self.spec(entity)["library_dimension"]]["value"]
        if lib not in self.libraries:
            self.libraries[lib] = engine.gene_set_collection(lib)
        return self.libraries[lib]


def result_file_doc(col: Collection, role: str, name: str, sha256: str, size: int, location: str) -> dict:
    """The File record of one result file."""
    role_spec = col.doc["file_roles"][role]
    return idn.mint("File", {"filename": name, "description": role_spec.get("description"),
                             "mime_type": role_spec.get("media_type"), "sha256": sha256, "size_in_bytes": size,
                             "location": location})


def _trait(dims: dict) -> str | None:
    # DAPPER's trait slot takes a CURIE (e.g. a KPN.TRAIT id from the collection's
    # mapping); an unmapped bare key stays only a dimension value.
    return dims["trait"]["value"] if "trait" in dims and ":" in dims["trait"]["value"] else None


def _trait_contexts(trait: str | None) -> list | None:
    """DAPPER 0.3 keeps a record's trait in its typed contexts (TraitContext), not in a `trait` slot."""
    return [{"context_type": "TraitContext", "trait": trait}] if trait else None


def dataset_doc(col: Collection, dims: dict, files: dict, bounds: dict, run_id: str) -> dict:
    """The Dataset record of one dataset: `dims` = {name: {value, label}}, `files` = {role: File record} in
    role order, `bounds` as where_for gives them, `run_id` the Activity that wrote the files."""
    doc = col.doc
    names = [d["name"] for d in doc["dataset_dimensions"]]
    label = ", ".join(f"{n}={dims[n]['label']}" for n in names)
    return idn.mint("Dataset", {
        "name": f"{doc['title']}: {label}",
        "description": f"{doc['title']} for {label}." + (f" Holds only {where_text(bounds)}." if bounds else ""),
        "contexts": _trait_contexts(_trait(dims)),
        "has_file": [f["id"] for f in files.values()],
        "dimension_values": [{"dimension": n, "dimension_value": dims[n]["value"]} for n in names],
        "file_roles": [{"role": r, "file": f["id"]} for r, f in files.items()],
        "was_generated_by": run_id,
    })


def where_for(col: Collection, spec: dict, ds: dict) -> dict:
    """{row kind: {metric: bound}} for one dataset: its own `where`, else the
    release's. Checked against the manifest's row kinds and metrics."""
    where = ds.get("where") if "where" in ds else spec.get("where")
    out = {}
    for kind, bounds in (where or {}).items():
        if kind not in col.doc["row_kinds"]:
            raise PublishError(f"where names unknown row kind {kind!r}")
        for metric, bound in bounds.items():
            if metric not in col.doc["row_kinds"][kind]["metrics"]:
                raise PublishError(f"where.{kind} names {metric!r}, which is not a metric of that row kind")
            out.setdefault(kind, {})[metric] = float(bound["gt"])
    return out


def where_text(where: dict) -> str | None:
    """The bounds as they are written into a Dataset description, or None."""
    if not where:
        return None
    return "; ".join(f"{kind} rows with " + " and ".join(f"{m} > {b:g}" for m, b in sorted(bounds.items()))
                     for kind, bounds in sorted(where.items()))


def _kept(row: dict, bounds: dict | None) -> bool:
    return all(idn.num(row[m]) > b for m, b in (bounds or {}).items())


def _file_bytes(where: str, role: str, f: dict, base: Path) -> tuple[Path | None, str, int]:
    """(local path or None, sha256, size) of one result file of a release
    specification: read and hashed when it is on disk, else taken as declared
    (it is fetched and checked when its dataset is ingested)."""
    path = (base / f["path"]).resolve() if f.get("path") else None
    if path is not None and path.is_file():
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if f.get("sha256") and f["sha256"] != sha:
            raise PublishError(f"{where}: {role} file {path} has sha256 {sha}, the release declares {f['sha256']}")
        return path, sha, len(data)
    if f.get("location") and f.get("sha256") and f.get("size") is not None:
        return None, f["sha256"], int(f["size"])
    raise PublishError(f"{where}: {role} file {path or '(no path)'} does not exist, and the release does not "
                       "declare its location, sha256 and size")


def publish(con, col: Collection, spec: dict, base: Path, *, allow_dirty: bool = True, dry_run: bool = False,
            progress: Callable[[str], None] | None = None,
            fetch: Callable[[str, Path], None] | None = None, workdir: Path | None = None,
            max_ingest: int | None = None, shard: tuple[int, int] | None = None,
            on_hash: Callable[[str, str, int], None] | None = None,
            provenance_fetch: Callable[[str], bytes] | None = None, target=None) -> Publication:
    """Publish a release. `dry_run` computes the release id, its DAPPER
    document (less the mint-context Activities) and warnings, and returns
    before anything is written; a version already published with other
    content is then reported in `plan`, not raised. `progress` is called with
    a short phase name as work proceeds. `fetch(location, destination)`
    downloads a result file the release gives by location; files land in
    `workdir` and are removed once their dataset is stored. `max_ingest`
    stops after that many datasets have been ingested by this call, without
    recording the release (nothing is served from it until a later call
    finishes it): a first batch can be checked before the rest goes in.
    `shard` = (i, n) handles only every n-th dataset starting at the i-th, so n
    processes can ingest one release side by side; a last unsharded call
    records the release. A file given by location WITHOUT a sha256 is
    hashed when its dataset is fetched (its dataset's id depends on it), and
    `on_hash(location, sha256, size)` is told, so a caller can remember it
    for the next call; the release is recorded only once every file is known.

    `target` is where the release goes: by default the service database `con`
    opens (SqlTarget); a push to a deployed service passes its own (the CLI's
    RemoteTarget), and `con` is then None."""
    step = progress or (lambda phase: None)
    target = target or SqlTarget(con)
    doc = col.doc
    dims_spec = doc["dataset_dimensions"]
    dim_names = [d["name"] for d in dims_spec]
    # Dimension values resolve through the collection's own vocabulary files alone.
    try:
        vocab = collection_vocabulary(doc, col.root)
    except MappingError as exc:
        raise PublishError(str(exc)) from exc
    refused: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    objects: dict[str, tuple[str, dict]] = {}

    def keep(cls, document):
        objects[document["id"]] = (cls, document)
        return document

    commit, dirty, _ = _git_info(col.manifest.path)
    if dirty:
        msg = f"{col.root} has uncommitted changes; this release records commit {commit} as dirty"
        if not allow_dirty:
            raise PublishError(msg)
        warnings.append(msg)
    code_version = f"{commit}{'-dirty' if dirty else ''}" if commit else "uncommitted"

    manifest_file, template_files, query_files = collection_file_docs(col)
    for f in (manifest_file, *template_files.values(), *query_files.values()):
        keep("File", f)

    # --- provenance --------------------------------------------------------
    from . import provenance as prov_mod
    try:
        prov = prov_mod.Index([prov_mod.load(e, base, **({"fetch": provenance_fetch} if provenance_fetch else {}))
                               for e in spec.get("provenance") or []])
    except prov_mod.ProvenanceError as exc:
        raise PublishError(f"provenance: {exc}") from exc
    for s_ in prov.sources:
        if s_.stored:
            for cls, node in s_.nodes.values():
                keep(cls, node)

    # --- datasets --------------------------------------------------------
    step("Hashing result files")
    datasets = []
    used_edges, generated_edges = [], []

    def make_dataset(entry: dict, facts: dict) -> None:
        """The File, run Activity and Dataset records of one dataset, once
        every file's bytes are known: facts = {role: (path, sha256, size)}."""
        ds, dims = entry["spec"], entry["dims"]
        files = {}
        for role, f in sorted(ds["files"].items()):
            path, sha, size = facts[role]
            name = path.name if path is not None else f["location"].rstrip("/").rsplit("/", 1)[-1]
            files[role] = {"path": path, "name": name, "location": f.get("location"), "doc": keep("File", result_file_doc(
                col, role, name, sha, size, f.get("location") or name))}
        bounds = where_for(col, spec, ds)
        label = ", ".join(f"{n}={dims[n]['label']}" for n in dim_names)
        trait = _trait(dims)
        # The run as the release describes it. It is part of the Dataset's identity, so it never depends on
        # provenance documents: adding or correcting provenance later re-addresses no dataset and re-reads no file.
        run = keep("Activity", idn.mint("Activity", {
            "name": f"{' / '.join(doc['methods'])} run: {label}",
            "description": ds.get("run") or f"The {' / '.join(doc['methods'])} run that wrote these result files.",
            "software_name": doc["methods"][0] if len(doc["methods"]) == 1 else None,
            "contexts": _trait_contexts(trait),
        }))
        try:
            documented = prov.run_for({role: (f["doc"]["sha256"], f["location"]) for role, f in files.items()})
        except prov_mod.ProvenanceError as exc:
            raise PublishError(f"provenance: {exc}") from exc
        # A provenance document's run, when one names it, is linked from the files and the dataset by edges
        # in the release's graph (DAPPER WasGeneratedBy: Dataset/File -> Activity), not by content.
        for f in files.values():
            generated_edges.append({"subject": f["doc"]["id"], "predicate": "prov:wasGeneratedBy",
                                    "object": documented or run["id"]})
        dataset = keep("Dataset", dataset_doc(col, dims, {r: f["doc"] for r, f in files.items()}, bounds, run["id"]))
        if documented:
            generated_edges.append({"subject": dataset["id"], "predicate": "prov:wasGeneratedBy", "object": documented})
        entry.update({"doc": dataset, "files": files, "where": bounds, "deferred": False, "run": documented})
    for i, ds in enumerate(spec["datasets"]):
        where = f"dataset {i + 1}"
        raw_dims = ds["dimensions"]
        if set(raw_dims) != set(dim_names):
            raise PublishError(f"{where}: dimensions must be exactly {dim_names}, got {sorted(raw_dims)}")
        dims = {}
        unresolved = []
        for d in dims_spec:
            value, label = resolve_dimension(vocab, d, raw_dims[d["name"]])
            dims[d["name"]] = {"value": value, "label": label, "raw": raw_dims[d["name"]]}
            if d["name"] in vocab.mappings and vocab.how(d["name"], raw_dims[d["name"]]) == "unresolved":
                unresolved.append((d["name"], raw_dims[d["name"]]))
        policy = {name: unmapped_policy(doc, name) for name, _ in unresolved}
        if any(p == "refuse" for p in policy.values()):
            refused += [f"{n}={v}" for n, v in unresolved if policy[n] == "refuse"]
            continue
        if any(p == "skip" for p in policy.values()):
            skipped += [f"{n}={v}" for n, v in unresolved if policy[n] == "skip"]
            continue
        unknown_roles = sorted(set(ds["files"]) - set(doc["file_roles"]))
        if unknown_roles:
            raise PublishError(f"{where}: unknown file role(s) {unknown_roles}")
        entry = {"spec": ds, "dims": dims, "deferred": False}
        if any(not (f.get("path") and (base / f["path"]).is_file()) and not f.get("sha256")
               for f in ds["files"].values()):
            for role, f in ds["files"].items():
                if not f.get("location"):
                    raise PublishError(f"{where}: {role} file has neither a readable path nor a location")
            entry["deferred"] = True
        else:
            make_dataset(entry, {role: _file_bytes(where, role, f, base) for role, f in ds["files"].items()})
        datasets.append(entry)

    if refused:
        raise PublishError(f"{len(refused)} dimension value(s) resolve through no mapping and the "
                           f"manifest's `unmapped` policy refuses them: {', '.join(sorted(refused)[:10])}"
                           f"{', ...' if len(refused) > 10 else ''}. Map them (claims vocab check --write-template).")
    if skipped:
        warnings.append(f"{len(skipped)} dataset(s) left out: their dimension values resolve through no mapping "
                        f"(`unmapped: skip`): {', '.join(sorted(skipped)[:5])}{', ...' if len(skipped) > 5 else ''}")
    for vocab_file in vocabulary_file_docs(col):
        keep("File", vocab_file)
        used_edges.append({"subject": "__publication__", "predicate": "prov:used", "object": vocab_file["id"]})

    # --- the release -----------------------------------------------------
    def build_release():
        """The release record and its DAPPER document, from every dataset."""
        # Re-publishing a version keeps the predecessor it was first published with.
        existing = target.existing_release(col.key, spec["version"])
        previous_id = existing["was_revision_of"] if existing else target.current_release_id(col.key)
        publication = keep("Activity", publication_doc(col, spec["version"], code_version))
        used_edges.append({"subject": publication["id"], "predicate": "prov:used", "object": manifest_file["id"]})
        for e in used_edges:
            if e["subject"] == "__publication__":
                e["subject"] = publication["id"]
        release = release_doc(col, spec["version"], [d["doc"]["id"] for d in datasets], publication["id"],
                              previous_id, manifest_file, template_files, query_files)
        keep("ResultCollection", release)
        if existing and existing["release_id"] != release["id"] and not dry_run:
            raise PublishError(f"{col.key} {spec['version']} is already published as {existing['release_id']} "
                               "with different content; publish a new version")

        def group(cls):
            return [d for c, d in objects.values() if c == cls]
        document = {
            "prefixes": doc.get("prefixes") or {},
            "files": group("File"),
            "activities": group("Activity"),
            "datasets": group("Dataset"),
            "result_collections": [release],
            "used_edges": used_edges,
            "was_generated_by_edges": generated_edges,
        }
        for s_ in prov.sources:  # a stored provenance document's own edges belong to the release's graph
            if s_.stored:
                for group_name, rows in s_.edges.items():
                    known = {(e.get("subject"), e.get("object")) for e in document.get(group_name, [])}
                    document.setdefault(group_name, []).extend(
                        e for e in rows if (e.get("subject"), e.get("object")) not in known)
        return release, document, existing, previous_id

    deferred = sum(1 for d in datasets if d["deferred"])
    if deferred and dry_run:
        raise PublishError(f"{deferred} dataset(s) have files without a sha256; a dry run needs every file's bytes")
    release = document = existing = previous_id = None
    if not deferred:
        release, document, existing, previous_id = build_release()

    stats = {"datasets": len(datasets), "datasets_ingested": 0, "rows": 0, "edges": 0,
             "mint_contexts_recorded": 0}
    if dry_run:
        if vocab.unresolved:
            warnings.append(_unresolved_warning(vocab))
        return Publication(release_id=release["id"], version=spec["version"], document=document, stats=stats,
                           warnings=warnings, plan={
                               "datasets": datasets, "template_files": template_files, "vocab": vocab,
                               "existing_release_id": existing["release_id"] if existing else None,
                               "previous_release_id": previous_id, "commit": commit, "dirty": dirty,
                               "code_version": code_version})
    minting, digest = col.minting_section(), col.minting_digest()

    def contexts_of(ds: dict) -> list:
        out = []
        for t in col.templates:
            ctx = mint_context_for(col, t, template_files[t.doc["template"]]["id"], ds, minting, digest)
            if ctx is not None:
                out.append(ctx)
                keep("Activity", ctx.activity)
        return out
    known = [d for d in datasets if not d["deferred"]]
    for d in known:
        d["contexts"] = contexts_of(d)
    target.prefetch([d["doc"]["id"] for d in known], [c.activity["id"] for d in known for c in d["contexts"]])
    stats["objects"] = 0
    # One transaction per dataset: its rows, its claims' data plane and its
    # mint contexts land together or not at all, and memory holds one dataset.
    unfinished = 0
    for n, ds in enumerate(datasets, 1):
        in_scope = shard is None or (n - 1) % shard[1] == shard[0]
        if ds["deferred"]:
            if not in_scope or (max_ingest is not None and stats["datasets_ingested"] >= max_ingest):
                unfinished += 1
                continue
            ds["prefetched"] = _fetch_and_hash(ds, fetch, workdir, f"dataset {n}", on_hash)
            make_dataset(ds, {role: v[:3] for role, v in ds["prefetched"].items()})
            for role, (path, _, _, _) in ds["prefetched"].items():
                ds["files"][role]["path"] = path
        dataset_id = ds["doc"]["id"]
        if "contexts" not in ds:
            ds["contexts"] = contexts_of(ds)
        ingest = not target.dataset_stored(dataset_id)
        pending = [ctx for ctx in ds["contexts"] if not target.context_recorded(ctx.activity["id"])]
        if not ingest and not pending:
            continue
        if not ds.get("prefetched") and (not in_scope or
                                          (ingest and max_ingest is not None and stats["datasets_ingested"] >= max_ingest)):
            unfinished += 1   # another shard's, or past this call's limit
            continue
        if ingest:
            target.refuse_other_slice(col, ds)
        fetched = _fetch_files(ds, fetch, workdir, f"dataset {n}") if ingest else []
        fetched += [v[3] for v in (ds.get("prefetched") or {}).values()]
        if target.kind == "parquet":
            try:
                step(f"Ingesting dataset {n} of {len(datasets)} into the Parquet index")
                n_rows, n_edges = _ingest_parquet(target.con, col, ds, pending, vocab, target.store, ingest)
            finally:
                _remove(fetched)
            stats["datasets_ingested"] += int(ingest)
            stats["rows"] += n_rows
            stats["edges"] += n_edges
            stats["mint_contexts_recorded"] += len(pending)
            continue
        try:
            if ingest:
                step(f"Ingesting dataset {n} of {len(datasets)}")
            extract = read_dataset(col, ds, vocab) if ingest else None
        finally:
            _remove(fetched)
        done = target.put_dataset(col, ds, vocab, extract, pending, step=lambda m: step(f"{m} on dataset {n}"))
        stats["datasets_ingested"] += int(ingest)
        stats["rows"] += done["rows"]
        stats["edges"] += done["edges"]
        stats["mint_contexts_recorded"] += done["contexts"]
        stats["objects"] += done["objects"]
    if unfinished:
        warnings.append(f"{unfinished} of {len(datasets)} dataset(s) are not stored yet (outside this call's shard "
                        "or limit); the release is not recorded. Run again to continue.")
        return Publication(release_id=release["id"] if release else None, version=spec["version"],
                           document=document, stats=stats, warnings=warnings, complete=False)
    if release is None:
        release, document, existing, previous_id = build_release()
    if target.kind == "parquet":
        stats["index_bytes"] = target.store.size_bytes()
    step("Recording the release")
    target.record_release(col, release, document, objects, [d["doc"]["id"] for d in datasets],
                          [(ctx.activity["id"], d["doc"]["id"]) for d in datasets for ctx in d["contexts"]],
                          prov.record(), vocab, template_files, query_files, code_version)
    if vocab.unresolved:
        warnings.append(_unresolved_warning(vocab))
    stats["objects"] += len(objects)
    return Publication(release_id=release["id"], version=spec["version"], document=document,
                       stats=stats, warnings=warnings)


def _other_collections(con, dataset_id: str) -> set[str]:
    """The collections a stored dataset is known to belong to: those of releases listing it, and the one it was
    pushed for (dataset_indexes)."""
    keys = {r["collection_key"] for r in con.execute(
        "SELECT DISTINCT r.collection_key FROM release_datasets d JOIN collection_releases r ON r.release_id = "
        "d.release_id WHERE d.dataset_id = ?", (dataset_id,)).fetchall()}
    keys |= {r["collection_key"] for r in con.execute(
        "SELECT collection_key FROM dataset_indexes WHERE dataset_id = ?", (dataset_id,)).fetchall()}
    return keys


def _refuse_other_slice(con, ds: dict, collection: str | None = None) -> None:
    """The same result files already ingested as another dataset of the same
    collection and dimensions, so under other `where` bounds: claims are shared
    by id, so two slices of one file cannot both be served, and adding rows to
    an ingested dataset is not supported. (A file listed under the wrong
    dimensions is caught, with a clearer message, when its rows are read.)
    Another collection may hold the same files: its claim ids differ, since the
    collection is part of every claim's minting context. A dataset known to
    belong only to other collections is therefore no conflict."""
    mine = {n: d["value"] for n, d in ds["dims"].items()}
    for f in ds["files"].values():
        for other in con.execute("SELECT DISTINCT dataset_id FROM dataset_files WHERE file_id = ? AND dataset_id <> ?",
                                 (f["doc"]["id"], ds["doc"]["id"])).fetchall():
            if collection is not None:
                owners = _other_collections(con, other["dataset_id"])
                if owners and collection not in owners:
                    continue
            theirs = {r["dimension"]: r["value"] for r in con.execute(
                "SELECT dimension, value FROM dataset_dimensions WHERE dataset_id = ?", (other["dataset_id"],)).fetchall()}
            if theirs == mine:
                raise PublishError(
                    f"result file {f['name']} ({f['doc']['id']}) is already ingested as dataset "
                    f"{other['dataset_id']} under other `where` bounds; one file cannot be served as two "
                    "datasets, and adding rows to an ingested dataset is not supported")


def _remove(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def _fetch_and_hash(ds: dict, fetch, workdir: Path | None, where: str, on_hash) -> dict:
    """Fetch a deferred dataset's files and hash them: {role: (path, sha256,
    size, path to remove)}."""
    if fetch is None or workdir is None:
        raise PublishError(f"{where}: its files are given by location without a sha256; publishing it needs a "
                           "fetcher and a working directory")
    out = {}
    for role, f in sorted(ds["spec"]["files"].items()):
        name = f["location"].rstrip("/").rsplit("/", 1)[-1]
        dest = workdir / hashlib.sha256(f["location"].encode()).hexdigest()[:16] / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        fetch(f["location"], dest)
        data = dest.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if f.get("size") is not None and int(f["size"]) != len(data):
            _remove([dest])
            raise PublishError(f"{where}: {f['location']} is {len(data)} bytes, the release says {f['size']}")
        if on_hash is not None:
            on_hash(f["location"], sha, len(data))
        out[role] = (dest, sha, len(data), dest)
    return out


def _fetch_files(ds: dict, fetch, workdir: Path | None, where: str) -> list[Path]:
    """Download the dataset's files that the release gave by location, and
    check each against its declared sha256. Returns the paths to remove."""
    fetched = []
    for role, f in ds["files"].items():
        if f["path"] is not None:
            continue
        if fetch is None or workdir is None:
            raise PublishError(f"{where}: {role} file is given by location ({f['location']}); "
                               "publishing it needs a fetcher and a working directory")
        dest = workdir / f["doc"]["sha256"][:16] / f["name"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        fetched.append(dest)
        fetch(f["location"], dest)
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        if sha != f["doc"]["sha256"]:
            _remove(fetched)
            raise PublishError(f"{where}: {f['location']} has sha256 {sha}, the release declares {f['doc']['sha256']}")
        f["path"] = dest
    return fetched


def _unresolved_warning(vocab: Vocabulary) -> str:
    return (f"{len(vocab.unresolved)} key(s) resolve through no mapping and were "
            f"kept as given: {sorted(vocab.unresolved)[:5]}")


def mint_context_for(col: Collection, t: SourceFile, template_file_id: str, ds: dict, minting: dict,
                     digest: str) -> engine.MintContext | None:
    """The (template, result file) pairing for one dataset, or None when the
    dataset has no file of the role the template reads."""
    role = col.role_for(t.doc["for"])
    if role not in ds["files"]:
        return None
    result = ds["files"][role]
    return engine.MintContext(
        minting=minting, minting_digest=digest, template=t.doc, template_file_id=template_file_id,
        result_file_id=result["doc"]["id"], file_name=result["name"],
        media_type=col.doc["file_roles"][role].get("media_type") or "application/octet-stream")


def _manifest_record(col: Collection, template_files: dict, query_files: dict) -> dict:
    """What discovery serves for a release: the manifest, templates and
    queries as parsed, with the DAPPER File id of each."""
    return {
        "manifest": col.doc,
        "templates": [{"file_id": template_files[t.doc["template"]]["id"], "path": t.rel, "doc": t.doc}
                      for t in col.templates],
        "queries": {name: {"file_id": query_files[name]["id"], "path": q.rel, "doc": q.doc}
                    for name, q in col.queries.items()},
    }


def _check_dimension_columns(col, role, row, ds, vocab, where):
    for dim, column in (col.doc["file_roles"][role].get("dimension_columns") or {}).items():
        if column not in row:
            raise PublishError(f"{where}: column {column!r} (dimension {dim}) is missing")
        spec = next(d for d in col.doc["dataset_dimensions"] if d["name"] == dim)
        value, _ = resolve_dimension(vocab, spec, str(row[column]))
        if value != ds["dims"][dim]["value"]:
            raise PublishError(f"{where}: {column}={row[column]!r} does not match the dataset's {dim} "
                               f"{ds['dims'][dim]['raw']!r}; is this file in the right dataset?")


def read_dataset(col: Collection, ds: dict, vocab) -> dict:
    """What a dataset's result files hold, checked line by line: the values a
    dataset is built from, and all that a push to a deployed service sends.

        {"edges": {kind: [[from value, to value], ...]},
         "rows":  {kind: [{"subject_value", "object_value", "row_json", "spans"}, ...]}}

    Rows below the release's bounds are left out. Edges are kept whole: which
    of them survive the bounds is decided when entities are minted
    (assemble_entities), from the rows."""
    doc = col.doc
    edges: dict[str, list] = {}
    for kind, spec in (doc.get("edge_kinds") or {}).items():
        role = col.role_for(kind)
        if role not in ds["files"]:
            continue
        f = ds["files"][role]
        pairs: dict[tuple, None] = {}
        for n, (row, _, _) in enumerate(_ndjson(f["path"], python_none=True), 1):
            _check_dimension_columns(col, role, row, ds, vocab, f"{f['path'].name} line {n}")
            pairs[(str(row[spec["from"]["column"]]), str(row[spec["to"]["column"]]))] = None
        edges[kind] = [list(p) for p in pairs]
    rows = {}
    for kind, rk in doc["row_kinds"].items():
        role = col.role_for(kind)
        if role not in ds["files"]:
            continue
        f = ds["files"][role]
        bounds = (ds.get("where") or {}).get(kind)
        out, seen = [], set()
        for n, (row, line, offset) in enumerate(_ndjson(f["path"]), 1):
            where = f"{f['path'].name} line {n}"
            _check_dimension_columns(col, role, row, ds, vocab, where)
            if not _kept(row, bounds):
                continue
            subject_value = str(row[rk["subject"]["column"]])
            if subject_value in seen:
                raise PublishError(f"{where}: {subject_value!r} appears twice in {f['path'].name}")
            seen.add(subject_value)
            spans = {}
            for m in rk["metrics"]:
                located = engine.locate(line, m)
                if located is None or m not in row:
                    raise PublishError(f"{where}: metric {m!r} is missing")
                start, length, text = located
                spans[m] = [offset + start, length, text]
                idn.num(row[m])  # refuse non-finite values at ingest, not at serve
            out.append({"subject_value": subject_value,
                        "object_value": str(row[rk["object"]["column"]]) if "column" in rk["object"] else "",
                        "row_json": idn.canonical(row), "spans": idn.canonical(spans)})
        rows[kind] = out
    return {"edges": edges, "rows": rows}


def assemble_entities(col: Collection, ds: dict, extract: dict, objects: dict) -> tuple[_Entities, dict]:
    """Mint a dataset's entities from what its files hold (read_dataset):
    (entities, edges by kind as {(from value, to value): (from id, from value,
    to value)}). Minted entities (gene sets) take their members from the whole
    edge file, so their ids are known only after every edge has been seen.
    Every id is computed here, never taken from the extract."""
    doc = col.doc
    ents = _Entities(col, ds["dims"])
    edge_kinds = doc.get("edge_kinds") or {}

    # Edges first: minted entities (gene sets) take their members from them.
    edges: dict[str, dict[tuple, tuple]] = {}
    members: dict[str, dict[str, set]] = {}     # minted entity type -> value -> member ids
    for kind, pairs in (extract.get("edges") or {}).items():
        spec = edge_kinds.get(kind)
        if spec is None or col.role_for(kind) not in ds["files"]:
            raise PublishError(f"edges of kind {kind!r}: not an edge kind this dataset's files yield")
        if "mint" in ents.spec(spec["from"]["entity"]):
            raise PublishError(f"edge kind {kind}: a minted entity can only be an edge's `to` end")
        to_entity = spec["to"]["entity"]
        collects = "mint" in ents.spec(to_entity) and ents.spec(to_entity)["members_from"] == kind
        rows = edges.setdefault(kind, {})
        for pair in pairs:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(v, str) for v in pair)):
                raise PublishError(f"edges of kind {kind!r}: each edge is [from value, to value]")
            from_value, to_value = pair
            from_id, _ = ents.ident(spec["from"]["entity"], from_value)
            rows[(from_value, to_value)] = (from_id, from_value, to_value)
            if collects:
                members.setdefault(to_entity, {}).setdefault(to_value, set()).add(from_id)

    # Minted entities: every value seen as an edge end or a row subject.
    for entity, spec in doc["entities"].items():
        if "mint" not in spec:
            continue
        row_values, bounded = set(), False
        for kind, rk in doc["row_kinds"].items():
            if rk["subject"]["entity"] == entity and col.role_for(kind) in ds["files"]:
                bounded = bounded or bool((ds.get("where") or {}).get(kind))
                row_values.update(r["subject_value"] for r in (extract.get("rows") or {}).get(kind) or [])
        if bounded:
            # Under bounds an entity exists only for a kept row: not for one seen only as an
            # edge end, and no edge ends at a dropped one. A kept entity keeps its whole roster.
            values = row_values
            for edge_kind, edge_spec in edge_kinds.items():
                if edge_spec["to"]["entity"] == entity and edge_kind in edges:
                    edges[edge_kind] = {pair: v for pair, v in edges[edge_kind].items() if pair[1] in values}
            members[entity] = {v: m for v, m in members.get(entity, {}).items() if v in values}
        else:
            values = set(members.get(entity, {})) | row_values
        library = ents.library(entity)
        objects[library["id"]] = ("GeneSetCollection", library)
        membership_role = col.role_for(spec["members_from"])
        membership_file = ds["files"].get(membership_role, {}).get("doc", {}).get("id")
        for value in sorted(values):
            entity_doc = engine.gene_set(value, library, sorted(members.get(entity, {}).get(value, ())),
                                         membership_file)
            ents.minted[(entity, value)] = entity_doc
            objects[entity_doc["id"]] = ("GeneSet", entity_doc)
    return ents, edges


def assemble_rows(col: Collection, ds: dict, ents: _Entities, kind: str, extract: dict, vocab):
    """The result_rows column values of one row kind, from what its file holds
    (read_dataset): identifiers and labels computed here, and each row's values
    checked again (its dimension columns, the release's bounds, every metric a
    finite number with a byte span), so rows that did not come from read_dataset
    on this machine are held to the same rules."""
    rk = col.doc["row_kinds"][kind]
    role = col.role_for(kind)
    if role not in ds["files"]:
        raise PublishError(f"rows of kind {kind!r}: this dataset has no {role} file")
    f = ds["files"][role]
    bounds = (ds.get("where") or {}).get(kind)
    seen = set()
    for n, r in enumerate((extract.get("rows") or {}).get(kind) or [], 1):
        where = f"{f['name']} row {n}"
        try:
            row, spans = json.loads(r["row_json"]), json.loads(r["spans"])
            subject_value, object_value = r["subject_value"], r["object_value"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PublishError(f"{where}: not a row as read_dataset writes it ({exc})") from exc
        if not (isinstance(row, dict) and isinstance(spans, dict) and idn.canonical(row) == r["row_json"]
                and isinstance(subject_value, str) and isinstance(object_value, str)
                and subject_value == str(row.get(rk["subject"]["column"]))
                and object_value == (str(row.get(rk["object"]["column"])) if "column" in rk["object"] else "")):
            raise PublishError(f"{where}: its values do not match its row")
        _check_dimension_columns(col, role, row, ds, vocab, where)
        if not _kept(row, bounds):
            raise PublishError(f"{where}: below the release's bounds")
        for m in rk["metrics"]:
            span = spans.get(m)
            if m not in row or not (isinstance(span, list) and len(span) == 3):
                raise PublishError(f"{where}: metric {m!r} is missing")
            try:
                value = idn.num(row[m])
            except (TypeError, ValueError) as exc:
                raise PublishError(f"{where}: metric {m!r} is not a finite number") from exc
            start, length, text = span
            # The span is what a claim quotes as the file's own bytes: it must be this value's text.
            try:
                quoted = idn.num(json.loads(text)) if isinstance(text, str) else None
            except (TypeError, ValueError):
                quoted = None
            if not (type(start) is int and type(length) is int and start >= 0 and isinstance(text, str)
                    and length == len(text.encode()) and quoted == value):
                raise PublishError(f"{where}: the byte span of {m!r} is not the text of its value")
        subject_id, subject_label = ents.ident(rk["subject"]["entity"], subject_value)
        if subject_id in seen:
            raise PublishError(f"{where}: {subject_value!r} appears twice in {f['name']}")
        seen.add(subject_id)
        object_id, object_label = ents.ident(rk["object"]["entity"], object_value)
        yield {"subject_id": subject_id, "subject_value": subject_value, "subject_label": subject_label,
               "object_id": object_id, "object_label": object_label, "file_id": f["doc"]["id"],
               "row_json": r["row_json"], "spans": r["spans"]}


ROW_COLUMNS = ("subject_id", "subject_value", "subject_label", "object_id", "object_label", "file_id",
               "row_json", "spans")


def store_dataset(con, col: Collection, ds: dict, extract: dict, objects: dict, vocab) -> tuple[int, int]:
    """Store one dataset in the service database (SQL store) from what its
    files hold: rows, metrics, edges, dimensions and files, in the caller's
    transaction. Its gene sets go into `objects` for the caller to store."""
    doc = col.doc
    dataset_id = ds["doc"]["id"]
    edge_kinds = doc.get("edge_kinds") or {}
    ents, edges = assemble_entities(col, ds, extract, objects)

    edge_rows = []
    for kind, pairs in edges.items():
        spec = edge_kinds[kind]
        for (from_value, to_value), (from_id, _, _) in sorted(pairs.items()):
            to_id, _ = ents.ident(spec["to"]["entity"], to_value)
            edge_rows.append((dataset_id, kind, from_id, to_id, from_value, to_value))

    unknown = sorted(set(extract.get("rows") or {}) - set(doc["row_kinds"]))
    if unknown:
        raise PublishError(f"rows of unknown kind(s) {unknown}")
    n_rows = 0
    for kind, rk in doc["row_kinds"].items():
        role = col.role_for(kind)
        if role not in ds["files"]:
            continue
        batch_rows = [(dataset_id, kind, *(r[c] for c in ROW_COLUMNS))
                      for r in assemble_rows(col, ds, ents, kind, extract, vocab)]
        batch_metrics = []
        con.executemany(
            "INSERT INTO result_rows (dataset_id, kind, subject_id, subject_value, subject_label, object_id, "
            "object_label, file_id, row_json, spans) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch_rows)
        n_rows += len(batch_rows)
        for r in con.execute("SELECT row_id, row_json FROM result_rows WHERE dataset_id = ? AND kind = ?",
                             (dataset_id, kind)).fetchall():
            row = json.loads(r["row_json"])
            batch_metrics.extend((r["row_id"], dataset_id, kind, m, idn.num(row[m])) for m in rk["metrics"])
        con.executemany(
            "INSERT INTO result_metrics (row_id, dataset_id, kind, metric, value) VALUES (?, ?, ?, ?, ?)",
            batch_metrics)
    con.executemany(
        "INSERT INTO result_edges (dataset_id, kind, from_id, to_id, from_value, to_value) VALUES (?, ?, ?, ?, ?, ?)",
        edge_rows)
    con.executemany("INSERT INTO dataset_dimensions (dataset_id, dimension, value, label) VALUES (?, ?, ?, ?)",
                    [(dataset_id, n, d["value"], d["label"]) for n, d in ds["dims"].items()])
    con.executemany("INSERT INTO dataset_files (dataset_id, role, file_id) VALUES (?, ?, ?)",
                    [(dataset_id, role, f["doc"]["id"]) for role, f in ds["files"].items()])
    return n_rows, len(edge_rows)


def request_memo(con) -> dict:
    """Lookups of stored, immutable records (a dataset's dimensions and files, its gene sets' edges) made while
    one request is served: kept on its connection, so each is read once per request."""
    memo = getattr(con, "memo", None)
    if memo is None:
        memo = con.memo = {}
    return memo


def dataset_dimensions(con, dataset_id: str) -> dict:
    """{dimension: canonical value} of a stored dataset."""
    memo = request_memo(con)
    key = ("dimensions", dataset_id)
    if key not in memo:
        memo[key] = {r["dimension"]: r["value"] for r in con.execute(
            "SELECT dimension, value FROM dataset_dimensions WHERE dataset_id = ?", (dataset_id,)).fetchall()}
    return memo[key]


def dataset_role_files(con, dataset_id: str) -> dict:
    """{role: file id} of a stored dataset."""
    memo = request_memo(con)
    key = ("files", dataset_id)
    if key not in memo:
        memo[key] = {r["role"]: r["file_id"] for r in con.execute(
            "SELECT role, file_id FROM dataset_files WHERE dataset_id = ?", (dataset_id,)).fetchall()}
    return memo[key]


def prefetch_groups(con, dataset_id: str, edge_kind: str, group_ids) -> None:
    """Read many edge groups (gene sets) of one dataset in two queries, for group_info."""
    from .store import get_store

    memo = request_memo(con)
    wanted = [g for g in set(group_ids) if ("group", dataset_id, edge_kind, g) not in memo]
    if not wanted:
        return
    found = get_store(con, [dataset_id]).groups(dataset_id, edge_kind, wanted)
    for g in wanted:
        memo[("group", dataset_id, edge_kind, g)] = found.get(g)


def group_info(con, dataset_id: str, edge_kind: str, group_id: str) -> tuple | None:
    """(label, member count, member ids) of the edges ending at group_id, or None."""
    prefetch_groups(con, dataset_id, edge_kind, [group_id])
    return request_memo(con)[("group", dataset_id, edge_kind, group_id)]


def dataset_ns(con, dataset_id: str) -> dict:
    """The {dataset.*} placeholders: each dimension's canonical value."""
    return {"id": dataset_id, **dataset_dimensions(con, dataset_id)}


def row_inputs(col_minting: dict, kind: str, r) -> dict:
    """render_row keyword arguments for one stored result row."""
    rk = col_minting["row_kinds"][kind]
    return {
        "row": json.loads(r["row_json"]),
        "subject": {"id": r["subject_id"], "label": r["subject_label"]},
        "obj": {"id": r["object_id"], "label": r["object_label"]},
        "metrics": rk["metrics"],
        "spans": {m: tuple(v) for m, v in json.loads(r["spans"]).items()},
        "selector": f"{rk['subject']['column']}={r['subject_value']}",
    }


def group_inputs(con, minting: dict, dataset_id: str, kind: str, group_id: str,
                 found: tuple[str, int] | None = None, library: str | None = None) -> dict:
    """render_group's `group` for one edge group (all edges ending at group_id).
    `found` = (label, member count) and `library` when the caller already has
    them (the Parquet ingester, before anything is stored)."""
    spec = minting["edge_kinds"][kind]
    if found is None:
        info = group_info(con, dataset_id, kind, group_id)
        found = (info[0], info[1]) if info else None
    label, count = found
    entity = minting["entities"][spec["to"]["entity"]]
    parent = ""
    if "library_dimension" in entity:
        if library is None:
            library = dataset_dimensions(con, dataset_id)[entity["library_dimension"]]
        parent = engine.gene_set_collection(library)["id"]
    return {"id": group_id, "label": label, "count": count, "parent": parent}


def _record_context(con, ctx: engine.MintContext) -> None:
    """Record one (template, file) pairing: what the service renders the dataset's claims from."""
    con.execute("INSERT INTO mint_contexts (activity_id, template_file_id, result_file_id, context) VALUES (?, ?, ?, ?)",
                (ctx.activity["id"], ctx.template_file_id, ctx.result_file_id, json.dumps(ctx.as_record())))


# Where a release goes ------------------------------------------------------

class SqlTarget:
    """The service database `con` opened, SQLite or MySQL: where
    scripts/publish_collection.py and the CLI's local deployment publish, and
    what the API stores a pushed release through (service/publish_api.py), so
    a pushed release and a directly published one are the same rows.
    `published_by` and `token_id` name the publisher of a push."""

    def __init__(self, con, published_by: str | None = None, token_id: str | None = None):
        from .store import get_store

        self.con, self.published_by, self.token_id = con, published_by, token_id
        self.store = get_store(con)
        self.kind = self.store.kind  # "sql", or "parquet" for a local deployment

    def existing_release(self, key: str, version: str) -> dict | None:
        """The version's recorded release: {release_id, was_revision_of}, or None."""
        row = self.con.execute("SELECT release_id, document FROM collection_releases WHERE collection_key = ? "
                               "AND version = ?", (key, version)).fetchone()
        if row is None:
            return None
        return {"release_id": row["release_id"],
                "was_revision_of": json.loads(row["document"])["result_collections"][0].get("was_revision_of")}

    def current_release_id(self, key: str) -> str | None:
        row = self.con.execute("SELECT current_release_id FROM collections WHERE collection_key = ?",
                               (key,)).fetchone()
        return row["current_release_id"] if row else None

    def prefetch(self, dataset_ids: list[str], activity_ids: list[str]) -> None:
        """A remote target asks about these in one request; the database is asked one at a time."""

    def dataset_stored(self, dataset_id: str) -> bool:
        return bool(self.con.execute("SELECT 1 FROM dataset_files WHERE dataset_id = ? LIMIT 1",
                                     (dataset_id,)).fetchone())

    def context_recorded(self, activity_id: str) -> bool:
        return bool(self.con.execute("SELECT 1 FROM mint_contexts WHERE activity_id = ?", (activity_id,)).fetchone())

    def refuse_other_slice(self, col: Collection, ds: dict) -> None:
        _refuse_other_slice(self.con, ds, col.key)

    def put_dataset(self, col: Collection, ds: dict, vocab, extract: dict | None, pending: list,
                    step: Callable[[str], None] | None = None, on_store: Callable | None = None) -> dict:
        """Store a dataset from what its files hold (None when it is stored already) and record its pending
        mint contexts, in one transaction; `on_store(con)` runs in it when the dataset is stored."""
        con = self.con
        for attempt in range(1, 4):
            objects: dict[str, tuple[str, dict]] = {}
            n_rows = n_edges = 0
            try:
                with con:
                    if extract is not None:
                        n_rows, n_edges = store_dataset(con, col, ds, extract, objects, vocab)
                        if on_store:
                            on_store(con)
                    for ctx in pending:
                        _record_context(con, ctx)
                    _store_objects(con, objects)
                break
            except Exception as exc:
                # Shards share a few rows (scores, the library record): a lock clash rolls this
                # dataset back whole, so it is simply tried again.
                if attempt == 3 or not any(code in str(exc) for code in ("1213", "1205", "Deadlock")):
                    raise
                if step:
                    step("Lock clash; retrying")
        return {"rows": n_rows, "edges": n_edges, "contexts": len(pending), "objects": len(objects)}

    def record_release(self, col: Collection, release: dict, document: dict, objects: dict, dataset_ids: list[str],
                       contexts: list[tuple[str, str]], provenance: list, vocab, template_files: dict,
                       query_files: dict, code_version: str) -> None:
        """Store the release's objects and record it as the collection's current release, in one transaction.
        `contexts` = [(mint context activity id, dataset id)]."""
        con = self.con
        manifest = {**_manifest_record(col, template_files, query_files), "provenance": provenance,
                    "mappings": {dim: {k: list(v) for k, v in m.items()} for dim, m in vocab.mappings.items()},
                    "vocabulary": vocab.entries}
        with con:
            _store_objects(con, objects)
            con.execute(
                f"{con.dialect.insert_or_ignore} collection_releases (release_id, collection_key, version, manifest, "
                "document, git_commit, published_by, publisher_token_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (release["id"], col.key, release["version"], json.dumps(manifest), json.dumps(document),
                 code_version, self.published_by, self.token_id))
            con.executemany(f"{con.dialect.insert_or_ignore} release_datasets (release_id, dataset_id) VALUES (?, ?)",
                            [(release["id"], d) for d in dataset_ids])
            con.executemany(f"{con.dialect.insert_or_ignore} release_mint_contexts (release_id, activity_id, "
                            "dataset_id) VALUES (?, ?, ?)", [(release["id"], a, d) for a, d in contexts])
            con.execute(
                f"{con.dialect.insert_or_replace} collections (collection_key, title, description, current_release_id) "
                "VALUES (?, ?, ?, ?)", (col.key, col.doc["title"], col.doc.get("description"), release["id"]))


# Parquet ingest ------------------------------------------------------------

# Stored when the dataset is ingested even with a lazy store: small, and what
# lazily stored objects point at (a GeneSet's library).
EAGER_CLASSES = {"File", "Activity", "Dataset", "ResultCollection", "GeneSetCollection"}


def parquet_dataset(col: Collection, ds: dict, extract: dict, objects: dict, vocab) -> tuple[list, list, list]:
    """A dataset's Parquet index content from what its files hold (read_dataset): (rows, metrics, edges), every
    identifier computed here. `objects` receives the entities it mints (the gene-set library, gene sets)."""
    from .store import stable_row_id

    doc = col.doc
    dataset_id = ds["doc"]["id"]
    rows, metrics, edge_rows = [], [], []
    ents, edges = assemble_entities(col, ds, extract, objects)
    for kind, pairs in edges.items():
        spec = doc["edge_kinds"][kind]
        for (from_value, to_value), (from_id, _, _) in sorted(pairs.items()):
            to_id, _ = ents.ident(spec["to"]["entity"], to_value)
            edge_rows.append((dataset_id, kind, from_id, to_id, from_value, to_value))
    unknown = sorted(set(extract.get("rows") or {}) - set(doc["row_kinds"]))
    if unknown:
        raise PublishError(f"rows of unknown kind(s) {unknown}")
    for kind, rk in doc["row_kinds"].items():
        if col.role_for(kind) not in ds["files"]:
            continue
        for r in assemble_rows(col, ds, ents, kind, extract, vocab):
            row = {**r, "dataset_id": dataset_id, "kind": kind,
                   "row_id": stable_row_id(dataset_id, kind, r["subject_id"])}
            rows.append(row)
            values = json.loads(row["row_json"])
            metrics.extend((row["row_id"], m, idn.num(values[m])) for m in rk["metrics"])
    return rows, metrics, edge_rows


def record_index_dataset(con, ds: dict, pending: list, objects: dict, stored: bool) -> None:
    """A Parquet-indexed dataset's control rows, in the caller's transaction: dimensions and files (when it is
    stored now), pending mint contexts, and the eagerly stored objects (gene sets are rebuilt when served)."""
    dataset_id = ds["doc"]["id"]
    if stored:
        con.executemany("INSERT INTO dataset_dimensions (dataset_id, dimension, value, label) VALUES (?, ?, ?, ?)",
                        [(dataset_id, n, d["value"], d["label"]) for n, d in ds["dims"].items()])
        con.executemany("INSERT INTO dataset_files (dataset_id, role, file_id) VALUES (?, ?, ?)",
                        [(dataset_id, role, f["doc"]["id"]) for role, f in ds["files"].items()])
    for ctx in pending:
        _record_context(con, ctx)
    _store_objects(con, {oid: v for oid, v in objects.items() if v[0] in EAGER_CLASSES})


def read_parquet_extract(col: Collection, directory: Path) -> tuple[dict, list, list, list]:
    """What a dataset's Parquet index says its files hold (the values a push is checked by), and the index's
    own (rows, metrics, edges) as tuples, read with DuckDB."""
    import duckdb

    db = duckdb.connect()
    try:
        rows = [dict(zip(("row_id", "dataset_id", "kind", "subject_id", "subject_value", "subject_label", "object_id",
                          "object_label", "file_id", "row_json", "spans"), r)) for r in db.execute(
            "SELECT row_id, dataset_id, kind, subject_id, subject_value, subject_label, object_id, object_label, "
            "file_id, row_json, spans FROM read_parquet(?)", [str(directory / "rows.parquet")]).fetchall()]
        metrics = db.execute("SELECT row_id, metric, value FROM read_parquet(?)",
                             [str(directory / "metrics.parquet")]).fetchall()
        edges = db.execute("SELECT dataset_id, kind, from_id, to_id, from_value, to_value FROM read_parquet(?)",
                           [str(directory / "edges.parquet")]).fetchall()
    finally:
        db.close()
    extract: dict = {"edges": {}, "rows": {}}
    for e in edges:
        extract["edges"].setdefault(e[1], []).append([e[4], e[5]])
    for r in rows:
        rk = col.doc["row_kinds"].get(r["kind"])
        if rk is None:
            raise PublishError(f"the index holds rows of unknown kind {r['kind']!r}")
        values = json.loads(r["row_json"])
        extract["rows"].setdefault(r["kind"], []).append({
            "subject_value": r["subject_value"],
            "object_value": str(values.get(rk["object"]["column"])) if "column" in rk["object"] else "",
            "row_json": r["row_json"], "spans": r["spans"]})
    return extract, rows, metrics, edges


def verify_parquet_dataset(col: Collection, ds: dict, vocab, directory: Path, objects: dict) -> dict:
    """Check a pushed dataset's Parquet index against what its values make: every identifier, label, metric
    and edge is computed again here and must be exactly what the index holds. Its counts."""
    extract, rows, metrics, edges = read_parquet_extract(col, directory)
    want_rows, want_metrics, want_edges = parquet_dataset(col, ds, extract, objects, vocab)
    key = lambda r: tuple(r[f] for f in ("row_id", "dataset_id", "kind", "subject_id", "subject_value",  # noqa: E731
                                         "subject_label", "object_id", "object_label", "file_id", "row_json", "spans"))
    if sorted(map(key, rows)) != sorted(map(key, want_rows)):
        raise PublishError("the index's rows are not what their values make (identifiers, labels or files differ)")
    if sorted(metrics) != sorted(want_metrics):
        raise PublishError("the index's metrics are not the rows' values")
    if sorted(edges) != sorted(want_edges):
        raise PublishError("the index's edges are not what their values make")
    counts = {"rows": {}, "edges": {}}
    for r in want_rows:
        counts["rows"][r["kind"]] = counts["rows"].get(r["kind"], 0) + 1
    for e in want_edges:
        counts["edges"][e[1]] = counts["edges"].get(e[1], 0) + 1
    return counts


def _ingest_parquet(con, col: Collection, ds: dict, pending: list, vocab, store, ingest: bool) -> tuple[int, int]:
    """Ingest one dataset into the Parquet index (rows, metrics and edges,
    written whole), then record its control rows (dimensions, files, pending
    mint contexts) in the service database. Gene sets are not stored here: the
    service rebuilds and stores one the first time a claim naming it is served."""
    objects: dict[str, tuple[str, dict]] = {}
    rows, edge_rows = [], []
    if ingest:
        rows, metrics, edge_rows = parquet_dataset(col, ds, read_dataset(col, ds, vocab), objects, vocab)
        store.write_dataset(ds["doc"]["id"], rows, metrics, edge_rows)
    with con:
        record_index_dataset(con, ds, pending, objects, ingest)
    return len(rows), len(edge_rows)


def _store_objects(con, objects: dict) -> None:
    """Upsert the data plane (re-minting an existing address counts it) and
    index every reference for /v1/objects/search."""
    rows = [(oid, cls, idn.PROFILE, idn.canonical(d)) for oid, (cls, d) in sorted(objects.items())]
    con.executemany(con.dialect.upsert_object, rows)
    refs = sorted({(oid, ref) for oid, (_, d) in objects.items() for ref in dapper_refs(d)})
    con.executemany(con.dialect.insert_ref, refs)
    text_index.index_objects(con, [(oid, cls, d) for oid, (cls, d) in sorted(objects.items())])
