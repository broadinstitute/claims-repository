# Publishing a new PIGEAN run as a collection

A self-contained guide for whoever (person or agent) runs PIGEAN on a server and turns the results into a
new claims collection with the `claims` CLI. It covers four things:

1. Set up the CLI on the server.
2. Lay out the run's result files on S3 so the CLI can read them.
3. Create the collection, starting from a copy of the published PIGEAN collection.
4. Plan, publish, query and check it.

Every command here was run end to end on 2026-11-24 against the existing PIGEAN run
(`s3://dig-pigean-paper-regression-2/out/falcon/`). [creating-a-collection.md](creating-a-collection.md) is the
full reference.

**What works today.** The CLI publishes into a *local deployment*: a SQLite control database plus a
Parquet index on the server's disk, which `claims serve` serves over HTTP. Publishing into the shared QA
service is the same command with `--api qa --index-to s3://...`, after a `claims login` (device-code sign-in, so it works on
a remote machine; see [cli-login.md](cli-login.md) and
[publishing-to-a-deployment.md](publishing-to-a-deployment.md)). Everything you prepare here (the S3
layout and the collection directory) carries over unchanged, because both paths read the same
`collection.yaml` and `source.yaml`.

---

## 1. Set up the CLI

You need Python 3.12, git, and the aws CLI with credentials that can list and read the result bucket.
Clone both repositories side by side:

```bash
mkdir -p ~/src && cd ~/src
git clone git@github.com:broadinstitute/claims-repository.git
git clone git@github.com:broadinstitute/kpn-dapper-toolkit.git     # holds collections/pigean
cd claims-repository
python3 -m venv .venv && .venv/bin/pip install -r requirements-cli.txt
aws sts get-caller-identity
```

Name the local deployment once per shell. Both paths are created on first use. Do not point `CLAIMS_DB`
at the repository's own `claims.db`; the CLI refuses it.

```bash
export CLAIMS_DB=$HOME/claims-local/claims.db
export CLAIMS_INDEX_DIR=$HOME/claims-local/index
unset CLAIMS_MYSQL_HOST            # the CLI refuses to run while it is set
```

Trait suggestions read a KPN trait release file. They are optional. Without one, every command still
runs and only the suggestions are missing. To get them, clone
[kpn-data-models](https://github.com/broadinstitute/kpn-data-models) and set
`CLAIMS_TRAIT_RELEASE=<clone>/versions/trait/v0.0.2/kpn_trait_collection.yaml`.

Check the install (this reads no data):

```bash
./bin/claims collection validate ../kpn-dapper-toolkit/collections/pigean
```

If a package is missing, the CLI prints the `pip install` command to run.

---

## 2. Lay out the results on S3

### Recommended layout

Give each run its own prefix, with one folder per (library, trait) holding that dataset's three files:

```text
s3://<bucket>/pigean-<run-name>/
  <library>/<trait>/combined_gene_stats.json        # one row per gene
  <library>/<trait>/combined_gene_set_stats.json    # one row per gene set (may be empty)
  <library>/<trait>/gene_gene_set_stats.json        # one row per (gene, gene set) membership
```

The collection's `source.yaml` then says:

```yaml
location: s3://<bucket>/pigean-<run-name>/
path: "{library}/{trait}/{kind}.json"
```

`{kind}` is the file's name, and `{library}` and `{trait}` become each dataset's dimension values. The
existing run uses `{kind}/{library}/portal/{trait}/{kind}.json`. Any order works, but grouping a dataset's
files together is easier to inspect and to clean up.

### Hard requirements (publishing refuses files that break them)

1. **Uncompressed NDJSON**: one JSON object per line, UTF-8, no gzip. Every claim's evidence cites an
   exact byte range of the original file, so the bytes on S3 must be the bytes that were read. NDJSON is
   also the only format the CLI reads.
2. **Dimension columns on every row of every file**, including the membership file. `phenotype` must equal
   the `{trait}` folder and `gene_set_size` must equal the `{library}` folder. (`gene_set_size` holds the
   library name, such as `mouse_msigdb`.) A file whose rows disagree with its path is refused.
3. **One row per subject**: each `gene` once per gene file, and each `gene_set` once per gene-set file.
4. **Metrics are finite numbers.** No `NaN`, `Infinity`, `null` or Python `None` in `combined`, `log_bf`,
   `prior`, `beta` or `beta_uncorrected`. Write with `json.dumps(row, allow_nan=False)`. The existing
   membership files contain bare Python `None`; the CLI tolerates that only in membership files, so fix it
   at the source.
5. **Trait folder names are trait keys, exactly as written**: no `/`, and no leading or trailing spaces.
   The key is part of the dataset's id. Reuse the keys of the existing run (`IBD`, `HEIGHT`,
   `Migraine`, ...) so the copied trait vocabulary maps them. 1,150 of 1,444 keys map today.

### Columns each file needs

| File | Required columns | Optional, carried into query results |
| --- | --- | --- |
| `combined_gene_stats.json` | `gene`, `combined`, `log_bf`, `prior`, `phenotype`, `gene_set_size` | `trait_group` |
| `combined_gene_set_stats.json` | `gene_set`, `beta`, `beta_uncorrected`, `phenotype`, `gene_set_size` | `trait_group`, `source` |
| `gene_gene_set_stats.json` | `gene`, `gene_set`, `phenotype`, `gene_set_size` | none; other columns are ignored |

Genes are HGNC symbols (`HGNC.SYMBOL:{value}`). Example rows from the existing run:

```json
{"gene": "IFNG", "prior": 4.44, "combined": 9.64, "log_bf": 5.2, "trait_group": "portal", "phenotype": "IBD", "gene_set_size": "mouse_msigdb"}
{"gene_set": "mp_enlarged_lymph_nodes", "source": "mouse_2024", "beta": 0.861, "beta_uncorrected": 1.36, "trait_group": "portal", "phenotype": "IBD", "gene_set_size": "mouse_msigdb"}
{"gene": "IFNG", "gene_set": "mp_enlarged_lymph_nodes", "phenotype": "IBD", "gene_set_size": "mouse_msigdb"}
```

### Strong recommendations

- **Use a prefix of its own.** Planning lists every object under `location`. The existing
  `out/falcon/` prefix holds 265,253 objects (106 GB), of which the collection uses 4,300.
- **Turn on bucket versioning before writing**, so each file's version is pinned when it is published:
  `aws s3api put-bucket-versioning --bucket <bucket> --versioning-configuration Status=Enabled`.
- **Never overwrite a published file.** A rerun goes to a new prefix and becomes a new release.
  Overwriting breaks the evidence of claims already served from the old bytes.
- **Slim the membership file** to the four required columns. Only the (gene, gene set) pairs are used.
  The existing file repeats eight metrics on every line, so IBD's membership file is 74.5 MB against
  3.5 MB for its gene file.
- **Write all three files for every trait**, even an empty gene-set file. An empty or missing gene-set
  file is allowed, and that trait then has no gene-set rows or rosters.
- **Upload a trait only when it is complete** (write locally, then `aws s3 cp`), so a plan never sees a
  half-written file.
- **Record the run**: PIGEAN version or commit, command line, parameters, gene-set library and its version,
  and finish date. `claims provenance draft` turns these into the collection's provenance document
  (section 3).

---

## 3. Create the collection

Start from a copy of the published PIGEAN collection. Its templates, queries, tier rules, metric meanings
and trait vocabulary carry over.

```bash
cd ~/src/kpn-dapper-toolkit
git checkout -b pigean-<library>
cp -R collections/pigean collections/pigean-<library>
rm -rf collections/pigean-<library>/releases/*
```

Edit the copy:

| File | Change |
| --- | --- |
| `collection.yaml` | `collection:` to a new handle, such as `pigean-<library>`. Claim ids depend on the handle, so choose it once; keeping `pigean` would collide with the published collection. Also change `title:` (and `description:` if it names the library). |
| `source.yaml` | `location:` and `path:` from section 2, and `run:` to describe the new run. Keep the `roles` if you kept the file names. Keep the `where` bounds (gene rows with `combined > 0.5`, gene-set rows with `beta_uncorrected > 0.01`) or change them. |
| `queries/*.yaml` | All three queries default `library` to `mouse_msigdb`. Change the default to the new library name. |
| `mappings/trait.tsv` | Nothing yet. Check it in step 4. |

Then, from `~/src/claims-repository`:

```bash
C=../kpn-dapper-toolkit/collections/pigean-<library>
./bin/claims collection validate $C                 # manifest, templates, queries; reads no data
./bin/claims sources check s3://<bucket>/pigean-<run-name>/
./bin/claims vocab check $C                         # trait keys against the mapping; samples gene values
```

`vocab check` lists trait keys the mapping does not cover. They are kept as given (`unmapped: keep-raw`).
To map them, run `vocab check $C --write-template`, fill the `id` column of `mappings/trait.tsv`, and run it
again. A wrong mapping makes every claim of that dataset name the wrong trait, so only map what you are
sure of.

Commit the collection directory to the toolkit and open a pull request. Review is the governance step.

---

## 4. Plan, publish, query

Plan a small release first. Planning lists the files and writes `releases/<version>.yaml`; it does not read
file contents:

```bash
./bin/claims release plan $C --version 2026.12.0 --only IBD,HEIGHT
./bin/claims release publish $C 2026.12.0 --first 1        # one dataset; not served yet
./bin/claims release status $C 2026.12.0
./bin/claims release publish $C 2026.12.0                  # resumes and finishes
./bin/claims query run pigean-<library> top-genes-with-sets \
  --var trait=IBD --var priorMax=0.5 --var topGenes=5 --var setsPerGene=2
```

Check that:

- `release plan` shows the expected number of datasets and files, and `0 unmapped` or a count you
  expect.
- `release status` reports `published  2 / 2 datasets` with a `dapper:ResultCollection...` id.
- The query returns genes with `combined`, `log_bf` and `prior` values that match the files.

Then plan the whole run under a new version (a release is immutable, so the test version stays as it was)
and publish it with parallel workers. `--workdir` should be on a disk with room for a few datasets' files
at a time:

```bash
./bin/claims release plan $C --version 2026.12.1
./bin/claims release publish $C 2026.12.1 --workers 4 --workdir $HOME/claims-local/work
```

For scale: the existing run of 1,444 datasets took about 3 hours and gives an index of about 0.9 GB.
Publishing is resumable. If it stops, run the same command again. A dataset is stored whole or not at
all, and the release is served only once every dataset is in.

To publish into QA instead, once the release looks right locally, sign in once and add `--api qa` and
`--index-to`, a bucket prefix the service may read. The files are read again on this machine, each
dataset's Parquet index goes to that bucket, and QA records only where it is; the release records who
published it ([publishing-to-a-deployment.md](publishing-to-a-deployment.md)):

```bash
./bin/claims login                                          # approve the code in the admin console
I=s3://cyaka-claims-index-test/pigean-<library>             # QA's test bucket
./bin/claims release publish $C 2026.12.1 --api qa --index-to $I --first 1
./bin/claims release publish $C 2026.12.1 --api qa --index-to $I --workers 4 --workdir $HOME/claims-local/work
```

QA and prod share one database, so a release published into QA is listed by prod as well.

Serve the deployment and use the API:

```bash
./bin/claims serve --port 8110
curl -s localhost:8110/v1/collections/pigean-<library>
curl -s localhost:8110/v1/queries
curl -s -X POST localhost:8110/v1/collections/pigean-<library>/queries/top-genes-with-sets:run \
  -H 'Content-Type: application/json' -d '{"variables": {"trait": "IBD", "topGenes": 5}}'
curl -s 'localhost:8110/v1/objects/search?q=nkx2%20bowel&fields=statement'
```

GraphiQL is at `http://localhost:8110/v1/graphql`. A claim's id resolves at `/v1/objects/<id>` once a
query has served it.

Optionally, attach provenance for the run before the full release:

```bash
./bin/claims provenance draft $C 2026.12.1 --name "PIGEAN run over <library>" --software PIGEAN \
  --software-version "<version>" --date <finish time, e.g. 2026-12-01T00:00:00Z> --command "<command>"
```

Then plan the release again so it includes the document.

---

## Troubleshooting

| Message | Cause | Fix |
| --- | --- | --- |
| `claims: missing Python packages (...)` | Virtualenv without the CLI's requirements | `.venv/bin/pip install -r requirements-cli.txt` |
| `name the deployment: --db ... --index-dir ...` | `CLAIMS_DB` / `CLAIMS_INDEX_DIR` unset | Export them (section 1) |
| `refusing: CLAIMS_MYSQL_HOST is set` | Shell configured for the deployed service | `unset CLAIMS_MYSQL_HOST` |
| `column 'phenotype' (dimension trait) is missing` | A file, often membership, lacks a dimension column | Add `phenotype` and `gene_set_size` to every row |
| `phenotype='X' does not match the dataset's trait 'Y'` | A file under the wrong folder, or a key that differs (such as stray spaces) | Move the file or fix the key |
| `line N: not valid JSON` | `NaN`, Python `None` or a truncated line | Rewrite with `allow_nan=False`; re-upload whole files |
| `versioning OFF` in `sources check` | Files can be watched for change but not pinned | Turn on bucket versioning before writing the run |
| `skipped  {...}: no gene_stats file` in the plan | That trait's gene file is missing; the gene file is required | Upload it; a trait whose paths do not match `source.yaml`'s `path` is not listed at all |
