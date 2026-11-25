"""claims: build, publish and query collections.

    claims sources check s3://bucket/prefix/
    claims collection init <new dir> --handle pigean --source s3://bucket/prefix/ --sample gene_stats=a/b/c.json ...
    claims templates generate <collection dir>
    claims vocab check <collection dir> [--version V] [--write-template]
    claims collection validate <collection dir>
    claims release plan <collection dir> --version 2026.11.1 [--only IBD,HEIGHT]
    claims release publish <collection dir> 2026.11.1 [--first 25] [--workers 4]
    claims release publish <collection dir> 2026.11.1 --api qa      (into a deployed service; needs claims login)
    claims release status <collection dir> 2026.11.1
    claims sources verify <collection dir> 2026.11.1
    claims query run pigean top-genes-with-sets --var trait=IBD --var topGenes=5
    claims serve --port 8100
    claims login [--api URL] [--no-wait | --continue]     (also: claims whoami, claims logout)

Every command that touches a deployment needs --db (the control-plane SQLite
file) and --index-dir (the Parquet index), or CLAIMS_DB and CLAIMS_INDEX_DIR.
It refuses to run when CLAIMS_MYSQL_HOST is set: this CLI is for local stacks.
`claims login` signs in to a deployed API instead (device code; docs/cli-login.md),
and `release publish --api` publishes into one with that sign-in, reading the files
here (docs/publishing-to-a-deployment.md).

A collection directory holds the manifest (collection.yaml), templates,
queries, and source.yaml, which says where the result files live:

    location: s3://dig-pigean-paper-regression-2/out/falcon/
    path: "{kind}/{library}/portal/{trait}/{kind}.json"
    roles:
      gene_stats: {kind: combined_gene_stats}
      gene_set_stats: {kind: combined_gene_set_stats, optional: true}
      membership: {kind: gene_gene_set_stats, requires: gene_set_stats}
    run: "One sentence on the run that wrote the files."
    where: {gene_phenotype: {combined: {gt: 0.5}}}

source.yaml is not part of the manifest's minting section, so where the files
live never changes a claim id. Releases are written to <collection>/releases/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _require_cli_packages():
    """Name the install command instead of failing mid-command with an ImportError."""
    import importlib.util

    missing = [m for m in ("yaml", "fastapi", "duckdb", "httpx") if importlib.util.find_spec(m) is None]
    if missing:
        repo = Path(__file__).resolve().parent.parent
        sys.exit(f"claims: missing Python packages ({', '.join(missing)}). Install the CLI's requirements:\n"
                 f"  {repo / '.venv/bin/pip'} install -r {repo / 'requirements-cli.txt'}")


_require_cli_packages()

import yaml  # noqa: E402

from . import auth  # noqa: E402
from . import scaffold  # noqa: E402
from . import vocab  # noqa: E402
from . import sources as src  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


# --- deployment -------------------------------------------------------------

def _deployment(args) -> tuple[Path, Path]:
    """Point the service modules at a local deployment, refusing anything else."""
    if os.environ.get("CLAIMS_MYSQL_HOST"):
        sys.exit("refusing: CLAIMS_MYSQL_HOST is set. This CLI writes only to a local control database and "
                 "Parquet index; unset it.")
    db = args.db or os.environ.get("CLAIMS_DB")
    index = args.index_dir or os.environ.get("CLAIMS_INDEX_DIR")
    if not db or not index:
        sys.exit("name the deployment: --db <control-plane SQLite file> and --index-dir <Parquet index directory> "
                 "(or CLAIMS_DB and CLAIMS_INDEX_DIR)")
    db, index = Path(db).resolve(), Path(index).resolve()
    if db == (REPO / "claims.db").resolve():
        sys.exit(f"refusing: {db} is the repository's working database; use a database of its own")
    db.parent.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    os.environ["CLAIMS_DB"], os.environ["CLAIMS_INDEX_DIR"] = str(db), str(index)
    sys.path.insert(0, str(REPO))
    from service import db as service_db  # noqa: E402  (reads CLAIMS_DB when imported)

    service_db.DB_PATH = str(db)
    con = service_db.connect()
    try:
        con.execute("PRAGMA journal_mode=WAL")  # parallel publish workers
    finally:
        con.close()
    return db, index


def _connect():
    from service import db as service_db

    return service_db.connect()


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


# --- source.yaml and datasets -----------------------------------------------

def _load_source(collection_dir: Path) -> dict:
    p = collection_dir / "source.yaml"
    if not p.exists():
        sys.exit(f"{p} not found: say where the result files live (see `claims --help`)")
    source = yaml.safe_load(p.read_text())
    for key in ("location", "path", "roles"):
        if key not in source:
            sys.exit(f"{p}: missing `{key}`")
    if not source["location"].endswith("/"):
        source["location"] += "/"
    return source


def _discover(source: dict, dims: list[str], versioned: bool) -> tuple[dict, list[str]]:
    """{dimension values tuple: {role: Listed}} from listing each role's
    prefix, plus notes about files the pattern skipped."""
    regex = src.pattern_regex(source["path"])
    found: dict[tuple, dict] = {}
    notes = []
    for role, spec in source["roles"].items():
        path = source["path"].replace("{kind}", spec["kind"])
        literal = path.split("{", 1)[0]
        listed = src.list_prefix(source["location"] + literal, versioned)
        for item in listed:
            rel = item.location[len(source["location"]):]
            m = regex.match(rel)
            if not m:
                continue
            if m.groupdict().get("kind", spec["kind"]) != spec["kind"]:
                continue
            key = tuple(m.group(d) for d in dims)
            found.setdefault(key, {})[role] = item
    return found, notes


def _parse_where(items: list[str] | None, default: dict | None) -> dict | None:
    """'gene_phenotype: combined > 0.5' -> {gene_phenotype: {combined: {gt: 0.5}}}."""
    if not items:
        return default
    ops = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
    out: dict = {}
    for item in items:
        m = re.fullmatch(r"\s*(\w+)\s*:\s*(\w+)\s*(>=|<=|>|<)\s*(-?[0-9.eE+-]+)\s*", item)
        if not m:
            sys.exit(f"--where {item!r}: write it as 'row_kind: metric > value'")
        kind, metric, op, value = m.groups()
        if op not in (">",):
            sys.exit(f"--where {item!r}: only '>' (a strict lower bound) is supported")
        out.setdefault(kind, {})[metric] = {ops[op]: float(value)}
    return out


def _release_paths(collection_dir: Path, version: str) -> tuple[Path, Path]:
    rel = collection_dir / "releases" / f"{version}.yaml"
    return rel, rel.with_suffix(".sources.json")


# --- commands ---------------------------------------------------------------

def cmd_sources_check(args):
    bucket, _ = src.split_s3(args.uri)
    status = src.versioning(bucket)
    listed = src.list_prefix(args.uri)
    print(f"source   {args.uri}")
    print(f"read     ok      listed {len(listed):,} objects, {_fmt_bytes(sum(i.size for i in listed))}")
    if status == "Enabled":
        print("version  ON      each file is pinned to its version id")
    else:
        print(f"version  {status.upper():7s} files can be watched for change, not pinned. To pin them:")
        print(f"                 aws s3api put-bucket-versioning --bucket {bucket} "
              "--versioning-configuration Status=Enabled")


def _sample(location: str, max_bytes: int) -> tuple[bytes, bool, int]:
    """(first bytes of an object, whether that is less than all of it, its size). A source that is not
    s3:// is read as a local directory."""
    if not location.startswith("s3://"):
        path = Path(location.removeprefix("file://"))
        with open(path, "rb") as fh:
            data = fh.read(max_bytes)
        size = path.stat().st_size
        return data, size > len(data), size
    bucket, key = src.split_s3(location)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "sample"
        head = json.loads(src._aws("s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json"))
        size = int(head["ContentLength"])
        if size == 0:
            return b"", False, 0
        src._aws("s3api", "get-object", "--bucket", bucket, "--key", key, "--range", f"bytes=0-{max_bytes - 1}",
                 str(dest))
        data = dest.read_bytes()
    return data, size > len(data), size


def cmd_collection_init(args):
    out_dir = Path(args.collection)
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        sys.exit(f"{out_dir} is not empty (--force to write into it)")
    location = args.source if args.source.endswith("/") else args.source + "/"
    dim_columns = dict(item.split("=", 1) for item in args.dimension or [])
    samples = {}
    for item in args.sample:
        role, _, rel = item.partition("=")
        if not rel:
            sys.exit(f"--sample {item!r}: write it as role=path/relative/to/the/source")
        data, truncated, size = _sample(location + rel, args.sample_bytes)
        fmt = scaffold.file_format(rel)
        rows, notes = scaffold.read_sample(data, fmt, truncated)
        if not rows:
            sys.exit(f"{rel}: no rows could be read")
        samples[role] = {"rel": rel, "fmt": fmt, "rows": rows, "notes": notes, "size": size,
                         "truncated": truncated, "cols": scaffold.profile(rows)}
        print(f"{rel:72s} {fmt:6s} {len(rows):>8,} rows sampled of {_fmt_bytes(size)}   "
              f"{len(samples[role]['cols'])} columns")
        if notes["python_none"]:
            print(f"  ! {notes['python_none']:,} lines use a bare Python None; read as null")

    # Dimensions: named columns, else columns constant in every sample whose value is a path segment.
    if not dim_columns:
        for role, smp in samples.items():
            segments = smp["rel"].split("/")
            for col, c in smp["cols"].items():
                if c["constant"] is not None and str(c["constant"]) in segments and col not in dim_columns.values():
                    dim_columns[re.sub(r"[^a-z0-9_]", "_", col.lower())] = col
    # The path pattern: the file's stem is {kind}; a segment equal to a dimension column's value is {dimension}.
    patterns = set()
    roles = {}
    for role, smp in samples.items():
        segs = smp["rel"].split("/")
        stem = segs[-1].rsplit(".", 1)[0]
        roles[role] = stem
        out = []
        for seg in segs:
            if seg == stem:
                out.append("{kind}")
            elif seg.rsplit(".", 1)[0] == stem:
                out.append("{kind}." + seg.rsplit(".", 1)[1])
            else:
                dim = next((d for d, col in dim_columns.items()
                            if str(smp["cols"].get(col, {}).get("constant")) == seg), None)
                out.append(f"{{{dim}}}" if dim else seg)
        patterns.add("/".join(out))
    if len(patterns) != 1:
        sys.exit(f"the samples' paths do not share one pattern: {sorted(patterns)}. Name the dimension columns "
                 "with --dimension name=column")
    path_pattern = patterns.pop()
    dims = [d for d in src.placeholders(path_pattern) if d != "kind"]
    trait_dim = next((d for d in dims if d == "trait" or "trait" in dim_columns.get(d, "")
                      or "phenotype" in dim_columns.get(d, "")), None)

    # Row kinds and edge kinds.
    subject_of, edges = {}, {}
    for role, smp in samples.items():
        cols = {c: v for c, v in smp["cols"].items() if c not in dim_columns.values()}
        strings = [c for c, v in cols.items() if v["type"] == "string" and v["present"] == v["rows"]]
        unique = [c for c in strings if cols[c]["unique"]]
        if unique:
            subject_of[role] = unique[0]
            continue
        pairs = [(a, b) for i, a in enumerate(strings) for b in strings[i + 1:]
                 if len({(r.get(a), r.get(b)) for r in smp["rows"]}) == len(smp["rows"])]
        if not pairs:
            sys.exit(f"{smp['rel']}: no column (or pair of columns) is unique per row; cannot tell what a row is")
        edges[role] = pairs[0]
    # An edge end that is another file's subject is that file's entity; a minted set when it is the `to` end.
    row_entity = {role: scaffold.entity_name(col) for role, col in subject_of.items()}
    entities, prefixes = {}, {"biolink": scaffold.KNOWN_PREFIXES["biolink"]}
    library_dim = next((d for d in dims if d != trait_dim), dims[0] if dims else None)
    minted = {}
    for role, (a, b) in edges.items():
        ent_b = scaffold.entity_name(b)
        if ent_b in row_entity.values():
            minted[ent_b] = role
        else:  # the end that is a known subject goes last (`to`)
            ent_a = scaffold.entity_name(a)
            if ent_a in row_entity.values():
                edges[role] = (b, a)
                minted[ent_a] = role
    for col in sorted(set(subject_of.values()) | {c for pair in edges.values() for c in pair}):
        name = scaffold.entity_name(col)
        if name in minted:
            entities[name] = {"mint": "GeneSet", "members_from": _edge_kind(minted[name], edges),
                              **({"library_dimension": library_dim} if library_dim else {})}
        else:
            prefix = scaffold.KNOWN_IDS.get(col.lower())
            if prefix:
                prefixes[prefix] = scaffold.KNOWN_PREFIXES[prefix]
            entities[name] = {"id": f"{prefix or 'TODO'}:{{value}}"}
    object_dim = trait_dim or (dims[0] if dims else None)
    object_entity = scaffold.entity_name(object_dim) if object_dim else "TODO"
    if object_dim:
        entities[object_entity] = {"dimension": object_dim}
    if trait_dim:
        prefixes["KPN.TRAIT"] = scaffold.KNOWN_PREFIXES["KPN.TRAIT"]

    row_kinds, edge_kinds, file_roles, metrics = {}, {}, {}, {}
    reserved = {"name", "value", "text"}
    for role, col in subject_of.items():
        smp = samples[role]
        kind = scaffold.kind_name(scaffold.entity_words(row_entity[role]).replace(" ", "_"), object_entity)
        numeric = [c for c, v in smp["cols"].items() if v["type"] == "number" and v["present"] == v["rows"]
                   and c not in dim_columns.values() and c not in reserved and re.fullmatch(r"[a-z][a-z0-9_]*", c)]
        keep = [c for c, v in smp["cols"].items() if v["type"] == "string" and v["present"] == v["rows"]
                and c != col and c not in dim_columns.values() and re.fullmatch(r"[a-z][a-z0-9_]*", c)][:3]
        if not numeric:
            sys.exit(f"{smp['rel']}: no numeric column present on every row to use as a metric")
        row_kinds[kind] = {"subject": {"entity": row_entity[role], "column": col}, "object": {"entity": object_entity},
                           "metrics": numeric, **({"keep": keep} if keep else {})}
        for m in numeric:
            metrics.setdefault(m, {"score_kind": "SCORE", "scale": f"{scaffold.TODO}: e.g. log odds", "better": "higher",
                                   "description": f"{scaffold.TODO}: what {m} measures, in one sentence.",
                                   "interpretation": f"{scaffold.TODO}: how to read {m}, as you would write it in a methods "
                                                     "section. Copied into every score this collection mints."})
        file_roles[role] = _role(smp, kind, dim_columns)
    for role, (a, b) in edges.items():
        kind = _edge_kind(role, edges)
        edge_kinds[kind] = {"from": {"entity": scaffold.entity_name(a), "column": a},
                            "to": {"entity": scaffold.entity_name(b), "column": b}}
        file_roles[role] = _role(samples[role], kind, dim_columns)
    method = args.method or args.handle.upper()
    doc = {
        "collection": args.handle,
        "title": args.title or f"{method} results",
        "description": f"{scaffold.TODO}: one paragraph on what this collection holds.",
        "owners": [f"{scaffold.TODO}-owner"],
        "methods": [method],
        "dapper_profile": "0.2.1-a1",
        "attribution": {"agent": f"urn:{scaffold.TODO}:agent-the-claims-are-attributed-to"},
        "prefixes": prefixes,
        "relations": {"associated_with": "biolink:associated_with",
                      **({"has_members": "urn:kpn:relation:has_members"} if edge_kinds else {})},
        "dataset_dimensions": [{"name": d} for d in dims],
        **({"mappings": {trait_dim: f"mappings/{trait_dim}.tsv"}, "xrefs": {trait_dim: f"mappings/{trait_dim}.sssom.tsv"},
            "unmapped": {trait_dim: "keep-raw"}} if trait_dim else {}),
        "entities": entities,
        "row_kinds": row_kinds,
        **({"edge_kinds": edge_kinds} if edge_kinds else {}),
        "file_roles": file_roles,
        "metrics": metrics,
        "assumptions": [f"{scaffold.TODO}: state what a reader must assume to use these numbers, or remove this list."],
        "templates": [],
        "queries": [],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("templates", "queries", "releases", "provenance"):
        (out_dir / sub).mkdir(exist_ok=True)
    if trait_dim:  # the collection's own trait vocabulary, started from the sampled keys
        column = dim_columns.get(trait_dim)
        keys = sorted({str(smp["cols"][column]["constant"]) for smp in samples.values()
                       if column and smp["cols"].get(column, {}).get("constant") is not None})
        release = vocab.load_release(args.traits)
        vocab.write_vocabulary(out_dir / doc["mappings"][trait_dim], out_dir / doc["xrefs"][trait_dim], keys, release,
                               vocab.Suggester(release) if release else None)
    (out_dir / "collection.yaml").write_text(scaffold.dump(doc, {
        "collection": "Drafted by `claims collection init`. Finish every TODO, then `claims collection validate`.\n"
                      "The handle is the collection's identity: every claim id depends on it.",
        "metrics": "One entry per score column. Remove columns that are not results (counts, parameters).",
        "entities": "How each column's values become identifiers. A TODO prefix must be replaced.",
        "file_roles": "What each kind of file means. Part of what claim ids depend on.",
        "mappings": "The collection's own vocabulary: how each key becomes an id, and what the id means. The\n"
                    "service reads nothing else. Fill it for every key: claims vocab check <dir> --write-template.",
    }))
    templates = scaffold.generate_templates(doc)
    for name, t in templates.items():
        (out_dir / "templates" / name).write_text(scaffold.dump(t))
    scaffold.set_templates_list(out_dir / "collection.yaml", sorted(templates))
    q = scaffold.default_query(doc)
    if q:
        (out_dir / "queries" / f"{q[0]}.yaml").write_text(scaffold.dump(q[1]))
        text = (out_dir / "collection.yaml").read_text()
        (out_dir / "collection.yaml").write_text(text.replace("queries: []", f"queries:\n  - queries/{q[0]}.yaml"))
    source = {"location": location, "path": path_pattern,
              "roles": {role: {"kind": roles[role], **({"requires": next(r for r, c in subject_of.items()
                                                                          if scaffold.entity_name(c) == scaffold.entity_name(edges[role][1]))}
                                                       if role in edges else {})} for role in samples},
              "run": f"{scaffold.TODO}: one sentence on the run that wrote these files.",
              "provenance": []}
    (out_dir / "source.yaml").write_text(scaffold.dump(source, {
        "location": "Where the result files live and how they are named. Moving them changes no claim id.",
        "provenance": "DAPPER provenance for the run: `- path: provenance/run.dapper.yaml` (stored with each\n"
                      "release) or `- url: https://...` (referenced; resolved where it is hosted).",
    }))
    _write_guide(out_dir, args, samples, doc, source, dim_columns, templates, q)
    print(f"pattern      {location}{path_pattern}")
    print(f"dimensions   " + ", ".join(f"{d} (from column {dim_columns.get(d, '?')})" for d in dims))
    for kind, rk in row_kinds.items():
        print(f"row kind     {kind}: {rk['subject']['entity']} ({rk['subject']['column']}) -> {rk['object']['entity']}; "
              f"metrics {', '.join(rk['metrics'])}")
    for kind, ek in edge_kinds.items():
        print(f"edge kind    {kind}: {ek['from']['entity']} -> {ek['to']['entity']}")
    print(f"wrote        {out_dir}/ (collection.yaml, source.yaml, {len(templates)} templates, "
          f"{'1 query' if q else 'no query'}, GUIDE.md)")
    print(f"next         read {out_dir}/GUIDE.md, finish the TODOs, then: claims collection validate {out_dir}")


def _edge_kind(role: str, edges: dict) -> str:
    """`member_of` for the only edge file; `<role>_member_of` when there are several."""
    return "member_of" if len(edges) == 1 else scaffold.kind_name(role, "member_of")


def _role(smp: dict, kind: str, dim_columns: dict) -> dict:
    present = {d: c for d, c in dim_columns.items() if c in smp["cols"]}
    return {"format": "ndjson", "media_type": "application/x-ndjson", "yields": kind,
            "description": f"{scaffold.TODO}: what this file is, in one sentence.",
            **({"dimension_columns": present} if present else {})}


def _write_guide(out_dir: Path, args, samples, doc, source, dim_columns, templates, query) -> None:
    lines = [f"# Finishing the `{doc['collection']}` collection", "",
             "`claims collection init` drafted this directory from sample files. It could read the files' shape,",
             "but not what the numbers mean or who stands behind them. Work through the checklist, then publish.", "",
             "## Checklist", "",
             "1. **Metrics** (`collection.yaml` → `metrics`). Keep only columns that are results. For each, write",
             "   `scale`, `description` and `interpretation`. The interpretation is copied into every score.",
             "2. **Entities** (`collection.yaml` → `entities`). Replace every `TODO:{value}` with the identifier",
             "   prefix its values belong to, and add the prefix's URL under `prefixes`.",
             "3. **About** (`collection.yaml`). `description`, `owners`, `attribution.agent`, `assumptions`.",
             "4. **Run** (`source.yaml` → `run`), and provenance if you have it (`source.yaml` → `provenance`):",
             "   a DAPPER provenance document under `provenance/` (stored with each release), or a `url:` to one",
             "   hosted elsewhere, such as the provenance indexer (referenced, never copied).",
             "5. **File descriptions** (`collection.yaml` → `file_roles.*.description`).",
             "6. **Templates** (`templates/`). Generated defaults: one measurement claim per row kind"
             + (", one roster claim per set membership file" if any("roster" in t for t in templates) else "") + ".",
             "   Edit the wording if you like. To add evidence tiers, add a rule under `tier_rules` and a claim",
             "   with `focus:` and `classify:` (see the PIGEAN collection's `templates/gene.yaml`).",
             "7. **Query** (`queries/`). Replace the example's TODO values with a real dataset.", "",
             "Then:", "", "```bash", f"claims collection validate {out_dir}",
             f"claims release plan {out_dir} --version 0.1.0 --only <one dimension value>",
             f"claims release publish {out_dir} 0.1.0", "```", "",
             "## What the samples showed", ""]
    for role, smp in samples.items():
        lines.append(f"- `{role}`: `{smp['rel']}`, {smp['fmt']}, {len(smp['rows']):,} rows sampled"
                     + (" (first part only)" if smp["truncated"] else "") + f", {len(smp['cols'])} columns.")
        if smp["notes"]["python_none"]:
            lines.append(f"  {smp['notes']['python_none']:,} lines use a bare Python `None`; the publisher reads it "
                         "as null in edge files only.")
        if smp["fmt"] != "ndjson":
            lines.append("  The publisher reads NDJSON only today; this format is not publishable yet.")
        partial = [c for c, v in smp["cols"].items() if v["present"] < v["rows"]]
        if partial:
            lines.append(f"  Columns missing on some rows (not used as metrics): {', '.join(partial)}.")
    lines += ["", f"Dimensions come from these columns, which must agree with the path: "
              + ", ".join(f"`{d}` ← `{c}`" for d, c in dim_columns.items()) + "."]
    (out_dir / "GUIDE.md").write_text("\n".join(lines) + "\n")


def cmd_templates_generate(args):
    collection_dir = Path(args.collection)
    manifest = collection_dir / "collection.yaml"
    doc = yaml.safe_load(manifest.read_text())
    templates = scaffold.generate_templates(doc)
    (collection_dir / "templates").mkdir(exist_ok=True)
    written = []
    for name, t in sorted(templates.items()):
        path = collection_dir / "templates" / name
        if path.exists() and not args.force:
            print(f"kept         templates/{name} (exists; --force to overwrite)")
            continue
        path.write_text(scaffold.dump(t))
        written.append(name)
        print(f"wrote        templates/{name}   for {t['for']}   claims: {', '.join(c['key'] for c in t['claims'])}")
    listed = [Path(p).name for p in doc.get("templates") or []]
    missing = [n for n in sorted(templates) if n not in listed]
    if missing:
        scaffold.set_templates_list(manifest, listed + missing)
        print(f"listed       {', '.join(missing)} in collection.yaml")


def cmd_collection_validate(args):
    sys.path.insert(0, str(REPO))
    from service.collections.manifest import ManifestError, load_collection
    from service.collections.publish import _git_info

    try:
        col = load_collection(args.collection)
    except ManifestError as exc:
        sys.exit(f"invalid: {exc}")
    d = col.doc
    if d.get("kind") == "objects":  # built from deposits: object kinds, no metrics or claim templates
        print(f"manifest     ok   {col.key} (objects): {len(d.get('object_kinds') or {})} object kind(s): "
              + ", ".join(d.get("object_kinds") or {}))
    else:
        print(f"manifest     ok   {col.key}: {len(d['metrics'])} metrics, {len(d.get('tier_rules') or {})} tier rules, "
              f"{len(d.get('relations') or {})} relations")
        print("templates    ok   " + ", ".join(f"{t.doc['template']} ({len(t.doc['claims'])} claims)"
                                               for t in col.templates) + "; every placeholder resolves")
    print("queries      ok   " + ", ".join(col.queries) + ": no step orders or filters on another kind's metric")
    commit, dirty, _ = _git_info(col.manifest.path)
    git = (f"{commit[:7]}{', uncommitted changes' if dirty else ', clean'}" if commit
           else "not in git (a release records it as uncommitted)")
    print(f"git          {'ok  ' if commit and not dirty else 'warn'} {git}")
    if (Path(args.collection) / "source.yaml").exists():
        s = _load_source(Path(args.collection))
        print(f"source       ok   {s['location']}{s['path']}")
    left = scaffold.todos(Path(args.collection))
    if left:
        print(f"draft        {len(left)} TODO(s) left; claims minted now would carry them:")
        for t in left[:25]:
            print(f"               {t}")
        if len(left) > 25:
            print(f"               ... and {len(left) - 25} more")


def cmd_release_plan(args):
    db, index = _deployment(args)
    from service.collections.manifest import load_collection

    collection_dir = Path(args.collection).resolve()
    col = load_collection(collection_dir)
    source = _load_source(collection_dir)
    dims = [d["name"] for d in col.doc["dataset_dimensions"]]
    pattern_dims = [p for p in src.placeholders(source["path"]) if p != "kind"]
    if sorted(pattern_dims) != sorted(dims):
        sys.exit(f"source.yaml path has {pattern_dims}; the manifest's dataset dimensions are {dims}")
    unknown_roles = sorted(set(source["roles"]) - set(col.doc["file_roles"]))
    if unknown_roles:
        sys.exit(f"source.yaml names roles the manifest does not have: {unknown_roles}")
    bucket, _ = src.split_s3(source["location"])
    versioned = src.versioning(bucket) == "Enabled"
    found, _ = _discover(source, dims, versioned)
    only = set(args.only.split(",")) if args.only else None
    pins = src.Pins(index)
    datasets, skipped, empty, sidecar = [], [], 0, {}
    for key in sorted(found):
        values = dict(zip(dims, key))
        if only and not (set(values.values()) & only):
            continue
        roles = dict(found[key])
        for role, spec in source["roles"].items():
            if role in roles and roles[role].size == 0 and spec.get("optional"):
                del roles[role]
                empty += 1
        for role, spec in source["roles"].items():
            if spec.get("requires") and spec["requires"] not in roles:
                roles.pop(role, None)
        missing = [r for r, spec in source["roles"].items()
                   if r not in roles and not spec.get("optional") and not spec.get("requires")]
        if missing:
            skipped.append((values, missing))
            continue
        files = {}
        for role, item in sorted(roles.items()):
            entry = {"location": item.location, "size": item.size}
            sha = pins.sha256(item.location, item.etag)
            if sha:
                entry["sha256"] = sha
            files[role] = entry
            sidecar[item.location] = {"etag": item.etag, "size": item.size, "version_id": item.version_id,
                                      "last_modified": item.last_modified}
        ds = {"dimensions": values, "files": files}
        if source.get("run"):
            ds["run"] = source["run"]
        datasets.append(ds)
    if not datasets:
        sys.exit("no datasets match")
    datasets, vocab_lines = _apply_unmapped(col, collection_dir, datasets)
    where = _parse_where(args.where, source.get("where"))
    provenance, prov_report = _plan_provenance(collection_dir, source, datasets)
    spec = {"version": args.version, **({"where": where} if where else {}),
            **({"provenance": provenance} if provenance else {}), "datasets": datasets}
    rel_path, side_path = _release_paths(collection_dir, args.version)
    rel_path.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# Release {args.version} of the {col.key} collection, planned by `claims release plan` from\n"
              f"# {source['location']}{source['path']}. Files are referenced by location; each one's sha256 is\n"
              "# recorded when publishing first reads it. `where` holds only rows above the bounds.\n")
    rel_path.write_text(header + yaml.safe_dump(spec, sort_keys=False, width=200))
    side_path.write_text(json.dumps(sidecar, indent=1, sort_keys=True))

    n_files = sum(len(d["files"]) for d in datasets)
    known = sum(1 for d in datasets for f in d["files"].values() if "sha256" in f)
    total = sum(f["size"] for d in datasets for f in d["files"].values())
    print(f"release      {col.key} {args.version}")
    print(f"datasets     {len(datasets):,} ({' x '.join(dims)})        files {n_files:,}, {_fmt_bytes(total)}")
    print(f"pins         {'version ids' if versioned else 'versioning is OFF: sha256 + ETag only'}; "
          f"{known:,} of {n_files:,} files already read ({n_files - known:,} will be read when publishing)")
    if where:
        print("where        " + "; ".join(f"{k}: " + ", ".join(f"{m} > {b['gt']:g}" for m, b in v.items())
                                          for k, v in where.items()))
    for line in vocab_lines:
        print(f"vocabulary   {line}")
    for line in prov_report:
        print(f"provenance   {line}")
    if empty:
        print(f"empty files  {empty:,} optional file(s) are empty: those datasets have no rows of that role")
    for values, missing in skipped[:5]:
        print(f"skipped      {values}: no {', '.join(missing)} file")
    print(f"wrote        {rel_path}")


def _vocab(col, collection_dir: Path, rewriting_xrefs: bool = False):
    """The collection's own vocabulary, from its mapping and cross-reference files (only the mapping
    files when the cross-references are about to be rewritten)."""
    from service.collections.traits import MappingError, collection_vocabulary

    doc = {k: v for k, v in col.doc.items() if k != "xrefs"} if rewriting_xrefs else col.doc
    try:
        return collection_vocabulary(doc, collection_dir)
    except MappingError as exc:
        sys.exit(f"vocabulary: {exc}")


def _resolution(col, vocabulary, values_by_dim: dict[str, set]) -> dict[str, dict[str, list]]:
    """{dimension: {'mapping'|'unresolved': [values]}} for the dimensions the collection maps."""
    out = {}
    for d in col.doc["dataset_dimensions"]:
        if d["name"] not in vocabulary.mappings:
            continue
        groups = {"mapping": [], "unresolved": []}
        for v in sorted(values_by_dim.get(d["name"], ())):
            groups[vocabulary.how(d["name"], v)].append(v)
        out[d["name"]] = groups
    return out


def _apply_unmapped(col, collection_dir: Path, datasets: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop (`skip`) or refuse (`refuse`) datasets whose dimension values resolve nowhere; report the rest."""
    from service.collections.traits import unmapped_policy

    vocabulary = _vocab(col, collection_dir)
    values = {}
    for ds in datasets:
        for k, v in ds["dimensions"].items():
            values.setdefault(k, set()).add(v)
    lines, drop = [], {}
    for dim, groups in _resolution(col, vocabulary, values).items():
        policy = unmapped_policy(col.doc, dim)
        bad = groups["unresolved"]
        lines.append(f"{dim}: {len(groups['mapping']):,} through the collection's mapping, {len(bad):,} unmapped"
                     + (f" ({policy}): {', '.join(bad[:5])}{', ...' if len(bad) > 5 else ''}" if bad else ""))
        if bad and policy == "refuse":
            sys.exit(f"{len(bad)} {dim} value(s) resolve nowhere and `unmapped: refuse` is set: {', '.join(bad[:10])}. "
                     f"Map them: claims vocab check {collection_dir} --write-template")
        if bad and policy == "skip":
            drop[dim] = set(bad)
    if drop:
        kept = [ds for ds in datasets if not any(ds["dimensions"].get(dim) in vals for dim, vals in drop.items())]
        lines.append(f"left out {len(datasets) - len(kept):,} dataset(s) whose values resolve nowhere (`unmapped: skip`)")
        datasets = kept
    return datasets, lines


def cmd_vocab_check(args):  # reads the collection's files and the source; no deployment
    from service.collections.manifest import load_collection, load_release

    collection_dir = Path(args.collection).resolve()
    col = load_collection(collection_dir)
    dims = [d["name"] for d in col.doc["dataset_dimensions"]]
    if args.version:
        spec, _ = load_release(_release_paths(collection_dir, args.version)[0])
        datasets = spec["datasets"]
        where = f"release {args.version}"
    else:
        source = _load_source(collection_dir)
        bucket, _ = src.split_s3(source["location"])
        found, _ = _discover(source, dims, src.versioning(bucket) == "Enabled")
        datasets = [{"dimensions": dict(zip(dims, key)),
                     "files": {r: {"location": i.location, "size": i.size} for r, i in roles.items()}}
                    for key, roles in sorted(found.items())]
        where = f"{source['location']}{source['path']}"
    if args.write_template:  # start any vocabulary file the manifest names but that does not exist yet
        from service.collections.traits import vocabulary_files

        for block, _dim, rel in vocabulary_files(col.doc):
            if not (collection_dir / rel).exists():
                (collection_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                (collection_dir / rel).write_text("\t".join(vocab.MAPPING_COLUMNS[:3] if block == "mappings"
                                                            else vocab.XREF_COLUMNS) + "\n")
    vocabulary = _vocab(col, collection_dir, rewriting_xrefs=args.write_template)
    values = {}
    for ds in datasets:
        for k, v in ds["dimensions"].items():
            values.setdefault(k, set()).add(v)
    print(f"checked      {len(datasets):,} datasets from {where}")
    release = vocab.load_release(args.traits)
    print(f"traits       {f'{release.label} ({release.path})' if release else 'no KPN trait release file: no suggestions (--traits PATH)'}")
    suggester = vocab.Suggester(release) if release else None
    from service.collections.traits import unmapped_policy

    for dim, groups in _resolution(col, vocabulary, values).items():
        total = sum(len(v) for v in groups.values())
        bad = groups["unresolved"]
        print(f"{dim:12s} {total:,} values: {len(groups['mapping']):,} through the collection's mapping, "
              f"{len(bad):,} unmapped (unmapped: {unmapped_policy(col.doc, dim)})")
        for v in groups["mapping"][:3]:
            print(f"             {v} -> {vocabulary.mappings[dim][v][0]}  {vocabulary.mappings[dim][v][1]}")
        if bad:
            best = {v: (suggester.suggest(v) or [None])[0] if suggester else None for v in bad}
            bands = {"exact name": sum(1 for b in best.values() if b and b[0] >= 0.99),
                     "close (>= 0.7)": sum(1 for b in best.values() if b and 0.7 <= b[0] < 0.99),
                     "weak or none": sum(1 for b in best.values() if not b or b[0] < 0.7)}
            print("             closest release trait for each (a hint, never applied): "
                  + ", ".join(f"{n:,} {k}" for k, n in bands.items()))
            for v in sorted(bad, key=lambda v: -(best[v][0] if best[v] else 0))[:args.show]:
                b = best[v]
                print(f"             {v:44s} " + (f"{b[0]:.2f}  {b[1]}  {b[2]}" if b else "no candidate"))
            if len(bad) > args.show:
                print(f"             ... and {len(bad) - args.show:,} more (--show N)")
        if args.write_template:
            rel = (col.doc.get("mappings") or {}).get(dim) or f"mappings/{dim}.tsv"
            xrel = (col.doc.get("xrefs") or {}).get(dim) or f"mappings/{dim}.sssom.tsv"
            n = vocab.write_vocabulary(collection_dir / rel, collection_dir / xrel, sorted(values.get(dim, ())),
                                       release, suggester, fill_at=args.fill_from_suggestions)
            _ensure_vocabulary_entries(collection_dir / "collection.yaml", dim, rel, xrel)
            print(f"             wrote {rel}: {n['kept']:,} row(s) kept, {n['exact']:,} filled from the release, "
                  f"{n['suggested']:,} prefilled from suggestions (review them), {n['blank']:,} left blank; "
                  f"{n['xrefs']:,} cross-reference(s) in {xrel}")
            if n.get("malformed"):
                print(f"             skipped {len(n['malformed']):,} cross-reference(s) whose target in the release is not "
                      "a CURIE, e.g. " + "; ".join(f"{t}: {x!r}" for t, x in n["malformed"][:3]))
    # Entity values, from a few files.
    sample = [ds for ds in datasets if ds["files"]][:args.sample_datasets]
    if sample and args.sample_datasets:
        _check_entities(col, sample, args.sample_bytes)


def _ensure_vocabulary_entries(manifest: Path, dim: str, rel: str, xrel: str) -> None:
    """Add `mappings: {dim: rel}` and `xrefs: {dim: xrel}` to collection.yaml unless they are there."""
    import yaml as _yaml

    for block, path, note in (("mappings", rel, "The collection's own vocabulary: keys -> ids (claims vocab check)."),
                              ("xrefs", xrel, "Cross-references of the mapped ids (claims vocab check).")):
        text = manifest.read_text()
        doc = _yaml.safe_load(text)
        if (doc.get(block) or {}).get(dim) == path:
            continue
        if re.search(rf"(?m)^{block}:[ \t]*$", text):
            text = re.sub(rf"(?m)^{block}:[ \t]*$", f"{block}:\n  {dim}: {path}", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n\n# {note}\n{block}:\n  {dim}: {path}\n"
        manifest.write_text(text)


def _check_entities(col, sample: list[dict], max_bytes: int) -> None:
    doc = col.doc
    role_kind = {role: spec["yields"] for role, spec in doc["file_roles"].items()}
    columns: dict[tuple[str, str], set] = {}  # (entity, column) -> values
    for ds in sample:
        for role, f in ds["files"].items():
            kind = role_kind.get(role)
            data, truncated, _ = _sample(f["location"], max_bytes)
            rows, _ = scaffold.read_sample(data, "ndjson", truncated)
            ends = []
            if kind in doc["row_kinds"]:
                ends.append(doc["row_kinds"][kind]["subject"])
            elif kind in (doc.get("edge_kinds") or {}):
                ends += [doc["edge_kinds"][kind]["from"], doc["edge_kinds"][kind]["to"]]
            for end in ends:
                if "column" in end:
                    columns.setdefault((end["entity"], end["column"]), set()).update(
                        str(r[end["column"]]) for r in rows if r.get(end["column"]) is not None)
    print(f"entities     from {len(sample)} dataset(s), the first {_fmt_bytes(max_bytes)} of each file")
    for (entity, column), values in sorted(columns.items()):
        spec = doc["entities"].get(entity) or {}
        template = spec.get("id")
        issues = vocab.value_issues(values, template)
        how = template or ("minted " + spec["mint"] if "mint" in spec else "dimension")
        example = next(iter(sorted(values)), "")
        print(f"  {entity:10s} {column:12s} {len(values):,} distinct  as {how}   e.g. "
              f"{template.replace('{value}', example) if template else example}")
        for problem, vals in issues.items():
            print(f"             ! {len(vals):,} {problem}: {', '.join(repr(v) for v in vals[:5])}")
        if not issues:
            print("             ok")


def _plan_provenance(collection_dir: Path, source: dict, datasets: list[dict]) -> tuple[list[dict], list[str]]:
    """source.yaml's provenance entries, pinned (sha256) and checked: each document is read or fetched,
    validated, and matched against the release's files. Paths become relative to the release file."""
    from service.collections import provenance as prov_mod

    entries, report = [], []
    for raw in source.get("provenance") or []:
        try:
            doc = prov_mod.load(raw, collection_dir)
        except prov_mod.ProvenanceError as exc:
            sys.exit(f"provenance: {exc}")
        entry = {**raw, "sha256": doc.sha256}
        if "path" in raw:
            entry["path"] = str(Path("..") / raw["path"])
        entries.append(entry)
        index = prov_mod.Index([doc])
        matched = sum(1 for d in datasets if index.run_for({r: (f.get("sha256"), f["location"])
                                                            for r, f in d["files"].items()}))
        kinds = {}
        for cls, _ in doc.nodes.values():
            kinds[cls] = kinds.get(cls, 0) + 1
        report.append(f"{doc.label}: {', '.join(f'{n} {c}' for c, n in sorted(kinds.items()))}; "
                      f"{'stored with the release' if doc.stored else f'referenced, resolved at {doc.resolver}'}; "
                      f"names the run of {matched:,} of {len(datasets):,} datasets")
    return entries, report


def cmd_provenance_draft(args):
    """A starter DAPPER provenance document for the run that wrote a release's files: one Activity, a File per
    result file (sha256 where it is already known), and which Activity generated which File."""
    index = Path(args.index_dir or os.environ.get("CLAIMS_INDEX_DIR") or ".")
    sys.path.insert(0, str(REPO))
    from service.collections import identity as idn
    from service.collections.manifest import load_release

    collection_dir = Path(args.collection).resolve()
    rel_path, side_path = _release_paths(collection_dir, args.version)
    spec, _ = load_release(rel_path)
    listing = json.loads(side_path.read_text()) if side_path.exists() else {}
    pins = src.Pins(index) if index.exists() else None
    activity = {"name": args.name, "description": args.description, "software_name": args.software,
                "software_version": args.software_version, "generated_at_time": args.date,
                "command": args.command, "repo_url": args.repo_url, "code_version": args.code_version}
    run = idn.mint("Activity", {k: v for k, v in activity.items() if v})
    files, edges, with_sha = [], [], 0
    for ds in spec["datasets"]:
        for f in ds["files"].values():
            item = listing.get(f.get("location")) or {}
            sha = f.get("sha256") or (pins.sha256(f["location"], item.get("etag", "")) if pins else None)
            with_sha += bool(sha)
            node = idn.mint("File", {k: v for k, v in {
                "filename": f["location"].rsplit("/", 1)[-1], "location": f["location"],
                "size_in_bytes": f.get("size") or item.get("size"), "sha256": sha}.items() if v is not None})
            files.append(node)
            edges.append({"subject": node["id"], "object": run["id"]})
    out = collection_dir / (args.out or "provenance/run.dapper.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    header = ("# DAPPER provenance for the run that wrote this collection's result files. Drafted by\n"
              "# `claims provenance draft`; ids are content addresses, so re-run the command after any edit.\n")
    out.write_text(header + yaml.safe_dump({"activities": [run], "files": files, "was_generated_by_edges": edges},
                                           sort_keys=False, width=200))
    rel = str(out.relative_to(collection_dir))
    source_path = collection_dir / "source.yaml"
    text = source_path.read_text()
    source = yaml.safe_load(text)
    if not any(e.get("path") == rel for e in source.get("provenance") or []):
        entry = f"  - path: {rel}\n"
        if re.search(r"(?m)^provenance: \[\][ \t]*$", text):
            text = re.sub(r"(?m)^provenance: \[\][ \t]*$", "provenance:\n" + entry.rstrip("\n"), text)
        elif re.search(r"(?m)^provenance:[ \t]*$", text):
            text = re.sub(r"(?m)^provenance:[ \t]*$", "provenance:\n" + entry.rstrip("\n"), text, count=1)
        else:
            text = text.rstrip("\n") + "\nprovenance:\n" + entry
        source_path.write_text(text)
    print(f"wrote        {out}")
    print(f"             1 Activity ({run['id']}), {len(files):,} Files ({with_sha:,} with a sha256; the rest match "
          "by location)")
    print(f"listed       {rel} under provenance in source.yaml; plan a new release to use it")


def _publish_once(args, collection_dir: Path, shard: str | None) -> dict:
    from service.collections.manifest import load_collection, load_release
    from service.collections.publish import PublishError, publish

    col = load_collection(collection_dir)
    rel_path, side_path = _release_paths(collection_dir, args.version)
    if not rel_path.exists():
        sys.exit(f"{rel_path} not found: run `claims release plan` first")
    spec, base = load_release(rel_path)
    listing = json.loads(side_path.read_text()) if side_path.exists() else {}
    pins = src.Pins(Path(os.environ["CLAIMS_INDEX_DIR"]))
    for ds in spec["datasets"]:
        for f in ds["files"].values():
            item = listing.get(f.get("location"))
            if "sha256" not in f and item:
                sha = pins.sha256(f["location"], item["etag"])
                if sha:
                    f["sha256"] = sha

    def fetch(location: str, dest: Path) -> None:
        src.fetch(location, dest, (listing.get(location) or {}).get("version_id"))

    def on_hash(location: str, sha: str, size: int) -> None:
        item = listing.get(location) or {"etag": "", "size": size, "version_id": None, "last_modified": ""}
        pins.record(src.Listed(location, size, item["etag"], item["last_modified"], item.get("version_id")), sha)

    started = time.time()

    def progress(phase: str) -> None:
        if not args.quiet and phase.startswith(("Ingesting", "Rebuilding", "Recording")):
            print(f"[{time.time() - started:7.0f}s]{f' shard {shard}' if shard else ''} {phase}", flush=True)

    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="claims-publish-"))
    target = con = None
    try:
        if args.api:
            from . import push

            target = push.RemoteTarget(_publish_api(args), _publisher_token(args), col, args.version)
        else:
            con = _connect()
        result = publish(con, col, spec, base, allow_dirty=True, progress=progress, fetch=fetch, workdir=workdir,
                         max_ingest=args.first, on_hash=on_hash,
                         shard=tuple(int(x) for x in shard.split("/")) if shard else None, target=target)
    except PublishError as exc:
        sys.exit(f"error: {exc}")
    finally:
        if con is not None:
            con.close()
    return {"result": result, "seconds": time.time() - started}


def _publish_api(args) -> str:
    """The deployed service `--api` names (`qa` for QA): publishing to one is never a default."""
    return auth.api_base(auth.QA if args.api == "qa" else args.api)


def _publisher_token(args) -> str:
    api = _publish_api(args)
    token = auth.token_for(api)
    if not token:
        sys.exit(f"claims: not signed in to {api}. Run `claims login --api {api}` first.")
    return token


def cmd_release_publish(args):
    db, index = _deployment(args)
    collection_dir = Path(args.collection).resolve()
    left = scaffold.todos(collection_dir)
    if left and not args.allow_draft:
        sys.exit(f"{len(left)} TODO(s) left in {collection_dir} (`claims collection validate` lists them); claims "
                 "published now would carry them. Finish them, or pass --allow-draft for a test deployment.")
    if args.api and not args.shard:
        api = _publish_api(args)
        _publisher_token(args)
        print(f"publishing to {api} (the files are read here; the sha256 cache is {index})", flush=True)
    if args.shard:  # a worker started by --workers
        out = _publish_once(args, collection_dir, args.shard)
        print(json.dumps({"stats": out["result"].stats, "complete": out["result"].complete}))
        return
    if args.workers and args.workers > 1:
        print(f"publishing with {args.workers} workers", flush=True)
        procs = []
        for i in range(args.workers):
            cmd = [sys.executable, "-m", "claims_cli.main", "--db", str(db), "--index-dir", str(index),
                   "release", "publish", str(collection_dir), args.version, "--shard", f"{i}/{args.workers}",
                   "--quiet", *(["--allow-draft"] if args.allow_draft else []),
                   *(["--api", args.api] if args.api else [])]
            if args.first:
                cmd += ["--first", str(-(-args.first // args.workers))]
            procs.append(subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, text=True))
        failed = 0
        for i, p in enumerate(procs):
            out, _ = p.communicate()
            failed += p.returncode != 0
            print(f"worker {i}/{args.workers}: {'ok' if p.returncode == 0 else 'FAILED'} {out.strip()[-200:]}")
        if failed:
            sys.exit(f"{failed} worker(s) failed; run the same command again to continue")
        if args.first:
            print("first batch stored; the release is not recorded. Check it, then run again without --first.")
            return
    out = _publish_once(args, collection_dir, None)
    r = out["result"]
    s = r.stats
    if not r.complete:
        print(f"not served yet: {s['datasets_ingested']} dataset(s) ingested in this run "
              f"({out['seconds']:.0f}s); {r.warnings[-1]}")
        return
    print(f"recorded     {s['datasets']} datasets as {r.release_id} ({out['seconds']:.0f}s)"
          + (f" at {_publish_api(args)}" if args.api else ""))
    for k in ("rows", "edges", "mint_contexts_recorded", "index_bytes"):
        if k in s:
            print(f"  {k}: {_fmt_bytes(s[k]) if k == 'index_bytes' else f'{s[k]:,}'}")
    for w in r.warnings:
        if args.api or "uncommitted" not in w:
            print(f"warning      {w}")


def cmd_release_status(args):
    db, index = _deployment(args)
    from service.collections.manifest import load_collection, load_release
    from service.collections.store import get_store

    collection_dir = Path(args.collection).resolve()
    col = load_collection(collection_dir)
    spec, _ = load_release(_release_paths(collection_dir, args.version)[0])
    con = _connect()
    try:
        vocabulary = _vocab(col, collection_dir)
        recorded = con.execute("SELECT release_id FROM collection_releases WHERE collection_key = ? AND version = ?",
                               (col.key, args.version)).fetchone()
        done, rows, edges = 0, 0, 0
        store = get_store(con)
        for ds in spec["datasets"]:
            params, sql = [], "SELECT f.dataset_id FROM dataset_files f"
            for i, (dim, raw) in enumerate(sorted(ds["dimensions"].items())):
                d = next(x for x in col.doc["dataset_dimensions"] if x["name"] == dim)
                value = vocabulary.mappings.get(dim, {}).get(raw, (raw,))[0]
                sql += f" JOIN dataset_dimensions d{i} ON d{i}.dataset_id = f.dataset_id AND d{i}.dimension = ? AND d{i}.value = ?"
                params += [dim, value]
            hit = con.execute(sql + " LIMIT 1", params).fetchone()
            if hit:
                done += 1
                c = store.counts(hit["dataset_id"])
                rows += sum(v for k, v in c.items() if k in col.doc["row_kinds"])
                edges += sum(v for k, v in c.items() if k not in col.doc["row_kinds"])
    finally:
        con.close()
    state = "published" if recorded else ("ingesting" if done else "planned")
    print(f"{col.key} {args.version}   {state:10s} {done:,} / {len(spec['datasets']):,} datasets   "
          f"{rows:,} rows, {edges:,} edges")
    if not recorded:
        print("             not served yet: the release is recorded when every dataset is in")
    else:
        print(f"             {recorded['release_id']}")


def cmd_sources_verify(args):
    index = Path(args.index_dir or os.environ.get("CLAIMS_INDEX_DIR") or "")
    if not str(index) or not index.exists():
        sys.exit("name the index: --index-dir")
    collection_dir = Path(args.collection).resolve()
    rel_path, side_path = _release_paths(collection_dir, args.version)
    sys.path.insert(0, str(REPO))
    from service.collections.manifest import load_release

    spec, _ = load_release(rel_path)
    pins = src.Pins(index)
    source = _load_source(collection_dir)
    bucket, _ = src.split_s3(source["location"])
    versioned = src.versioning(bucket) == "Enabled"
    now = {}
    for role, rspec in source["roles"].items():
        literal = source["path"].replace("{kind}", rspec["kind"]).split("{", 1)[0]
        now.update({i.location: i for i in src.list_prefix(source["location"] + literal, versioned)})
    unchanged = changed = missing = unread = 0
    examples = []
    for ds in spec["datasets"]:
        for f in ds["files"].values():
            loc = f["location"]
            pinned = [p for (l, _), p in pins.by_key.items() if l == loc and p.get("sha256")]
            if not pinned:
                unread += 1
                continue
            cur = now.get(loc)
            if cur is None:
                missing += 1
                examples.append(f"gone     {loc}")
            elif any(p["etag"] == cur.etag and p["size"] == cur.size for p in pinned):
                unchanged += 1
            else:
                changed += 1
                examples.append(f"changed  {loc}")
    print(f"{args.version}   {unchanged + changed + missing + unread:,} files")
    print(f"  {unchanged:,} unchanged   (size + ETag{'; version ids pinned' if versioned else ''})")
    print(f"  {changed:,} changed")
    print(f"  {missing:,} missing")
    if unread:
        print(f"  {unread:,} not read yet (no pin)")
    for e in examples[:10]:
        print(f"  {e}")


def cmd_query_run(args):
    variables = {}
    for item in args.var or []:
        k, _, v = item.partition("=")
        try:
            variables[k] = json.loads(v)
        except json.JSONDecodeError:
            variables[k] = v
    body = {"variables": variables}
    if args.no_claims:
        body["claims"] = []
    path = f"/v1/collections/{args.collection}/queries/{args.query}:run"
    started = time.time()
    if args.api:
        import urllib.request

        req = urllib.request.Request(args.api.rstrip("/") + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=300).read())
    else:
        _deployment(args)
        from fastapi.testclient import TestClient
        from service.app import app

        resp = TestClient(app).post(path, json=body)
        if resp.status_code != 200:
            sys.exit(f"{resp.status_code}: {resp.text[:500]}")
        out = resp.json()
    took = time.time() - started
    if args.json:
        print(json.dumps(out, indent=1))
        return
    keys, n_sets = set(), 0
    for row in out["rows"]:
        keys |= set(row.get("claims") or {})
        sets = []
        for step in [k for k, v in row.items() if isinstance(v, list)]:
            for item in row[step]:
                n_sets += 1
                sets.append(item["row"]["subject"]["label"])
                keys |= set(item.get("edge", {}).get("claims") or {}) | set(item["row"].get("claims") or {})
        metrics = "  ".join(f"{m} {v:g}" for m, v in row["metrics"].items())
        print(f"{row['subject']['label']:12s} {metrics}   sets: {', '.join(sets[:3])}{', ...' if len(sets) > 3 else ''}")
    print(f"{len(out['rows'])} rows, {n_sets} joined rows; claims: {', '.join(sorted(keys)) or 'none served'}"
          f"   ({took * 1000:.0f} ms)")
    for w in out.get("warnings") or []:
        print(f"warning: {w}")


def cmd_serve(args):
    db, index = _deployment(args)
    print(f"serving the claims repository on http://127.0.0.1:{args.port}  (control {db}, index {index})")
    os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "service.app:app", "--port", str(args.port)])


def main(argv=None):
    # Starlette asks for httpx2 behind fastapi.testclient; httpx works and the warning is noise here.
    import warnings
    warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")
    warnings.filterwarnings("ignore", message=".*starlette.testclient.*httpx2.*")
    parser = argparse.ArgumentParser(prog="claims", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="control-plane SQLite file (or CLAIMS_DB)")
    parser.add_argument("--index-dir", help="Parquet index directory (or CLAIMS_INDEX_DIR)")
    sub = parser.add_subparsers(dest="group", required=True)

    sources = sub.add_parser("sources", help="check and verify where result files live").add_subparsers(
        dest="cmd", required=True)
    p = sources.add_parser("check", help="read access, object count and versioning for a prefix")
    p.add_argument("uri")
    p.set_defaults(func=cmd_sources_check)
    p = sources.add_parser("verify", help="compare a release's files with what was pinned when they were read")
    p.add_argument("collection")
    p.add_argument("version")
    p.set_defaults(func=cmd_sources_verify)

    collection = sub.add_parser("collection", help="check a collection definition").add_subparsers(
        dest="cmd", required=True)
    p = collection.add_parser("init", help="draft a collection from sample files")
    p.add_argument("collection", help="the directory to write")
    p.add_argument("--handle", required=True, help="the collection's handle, e.g. pigean")
    p.add_argument("--title")
    p.add_argument("--method", help="the method's name, e.g. PIGEAN (default: the handle in capitals)")
    p.add_argument("--source", required=True, help="where the files live, e.g. s3://bucket/out/")
    p.add_argument("--sample", action="append", required=True,
                   help="role=path relative to the source, one per kind of file (repeatable)")
    p.add_argument("--dimension", action="append", help="name=column: a dataset dimension and the column holding it")
    p.add_argument("--sample-bytes", type=int, default=8_000_000, help="read at most this much of each sample")
    p.add_argument("--force", action="store_true")
    p.add_argument("--traits", help="KPN trait release file the trait vocabulary is started from "
                                    f"(default: $CLAIMS_TRAIT_RELEASE or {vocab.DEFAULT_TRAIT_RELEASE})")
    p.set_defaults(func=cmd_collection_init)
    p = collection.add_parser("validate", help="manifest, templates and queries; reads no data")
    p.add_argument("collection")
    p.set_defaults(func=cmd_collection_validate)

    templates = sub.add_parser("templates", help="claim templates").add_subparsers(dest="cmd", required=True)
    p = templates.add_parser("generate", help="default claim templates from the manifest")
    p.add_argument("collection")
    p.add_argument("--force", action="store_true", help="overwrite existing template files")
    p.set_defaults(func=cmd_templates_generate)

    release = sub.add_parser("release", help="plan, publish and track releases").add_subparsers(
        dest="cmd", required=True)
    p = release.add_parser("plan", help="list the files, group them into datasets, and write the release file")
    p.add_argument("collection")
    p.add_argument("--version", required=True)
    p.add_argument("--where", action="append", help="'row_kind: metric > value' (repeatable)")
    p.add_argument("--only", help="comma-separated dimension values to include, e.g. IBD,HEIGHT")
    p.set_defaults(func=cmd_release_plan)
    p = release.add_parser("publish", help="read the files and build the index; resumable")
    p.add_argument("collection")
    p.add_argument("version")
    p.add_argument("--first", type=int, help="ingest at most this many datasets, then stop without recording")
    p.add_argument("--workers", type=int, help="ingest with this many parallel workers")
    p.add_argument("--shard", help=argparse.SUPPRESS)
    p.add_argument("--workdir", help="where files are downloaded while their dataset is read (default: a temp dir)")
    p.add_argument("--allow-draft", action="store_true", help="publish even though TODOs are left")
    p.add_argument("--api", help="publish into this deployed service instead (`qa`, or its base URL); needs "
                   "`claims login` (docs/publishing-to-a-deployment.md)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_release_publish)
    p = release.add_parser("status", help="how far a release has been published")
    p.add_argument("collection")
    p.add_argument("version")
    p.set_defaults(func=cmd_release_status)

    query = sub.add_parser("query", help="run a collection's query").add_subparsers(dest="cmd", required=True)
    p = query.add_parser("run")
    p.add_argument("collection")
    p.add_argument("query")
    p.add_argument("--var", action="append", help="name=value (repeatable; values parse as JSON when they can)")
    p.add_argument("--api", help="a deployed service's base URL instead of the local deployment")
    p.add_argument("--no-claims", action="store_true", help="return rows only; serve (and so record) no claims")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query_run)

    vocab_p = sub.add_parser("vocab", help="how a collection's values resolve to identifiers").add_subparsers(
        dest="cmd", required=True)
    p = vocab_p.add_parser("check", help="dimension values against the collection's mappings; entity values in samples")
    p.add_argument("collection")
    p.add_argument("--version", help="check a planned release's datasets (default: list the source)")
    p.add_argument("--write-template", action="store_true",
                   help="write every value into the dimension's mapping file (exact release matches filled in, "
                        "the rest with suggestions) and the mapped ids' cross-references")
    p.add_argument("--traits", help=f"KPN trait release file (default: $CLAIMS_TRAIT_RELEASE or {vocab.DEFAULT_TRAIT_RELEASE})")
    p.add_argument("--show", type=int, default=15, help="how many unresolved values to list")
    p.add_argument("--fill-from-suggestions", type=float, metavar="SCORE",
                   help="with --write-template, put suggestions scoring at least SCORE in the id column, for review")
    p.add_argument("--sample-datasets", type=int, default=2, help="datasets whose files are sampled for entity values")
    p.add_argument("--sample-bytes", type=int, default=2_000_000)
    p.set_defaults(func=cmd_vocab_check)

    provenance = sub.add_parser("provenance", help="provenance for a collection's runs").add_subparsers(
        dest="cmd", required=True)
    p = provenance.add_parser("draft", help="write a starter DAPPER provenance document for a planned release")
    p.add_argument("collection")
    p.add_argument("version", help="a planned release whose files the run wrote")
    p.add_argument("--name", required=True, help="the run, e.g. 'PIGEAN run over mouse_msigdb'")
    p.add_argument("--description")
    p.add_argument("--software", required=True)
    p.add_argument("--software-version")
    p.add_argument("--date", help="when the run finished, e.g. 2026-10-14T00:00:00Z")
    p.add_argument("--command")
    p.add_argument("--repo-url")
    p.add_argument("--code-version")
    p.add_argument("--out", help="path inside the collection (default provenance/run.dapper.yaml)")
    p.set_defaults(func=cmd_provenance_draft)

    auth.add_commands(sub)

    p = sub.add_parser("serve", help="run the service over the local deployment")
    p.add_argument("--port", type=int, default=8100)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
