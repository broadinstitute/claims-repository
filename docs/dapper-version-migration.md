# DAPPER versions and id migration

Status: design, with a read-only prototype (`scripts/rescan_identity.py`). Written 2026-11-27 on branch
`chase/dapper-0.3`, which vendors DAPPER 0.3.0-a1 (dapper commit `d515697`).

DAPPER will be released many times. Each release can change which slots exist and which are hashed, so it can
change the id an object's content mints to. The claims repository has to keep working across that:

1. know which DAPPER version each object was minted under;
2. recompute any object's id under the current version;
3. say which hashed slots made an id change;
4. keep old ids resolvable and searchable, marked as replaced by the new id, without marking the object itself
   deprecated;
5. run the rescan and the re-mint from the admin console.

## What is stored today

- `dapper_objects` holds each object once: `id`, `class_name`, `identity_profile` (`DAPPER-ID-1`), `payload`
  (the document as deposited, id included), timestamps and `times_seen`. **No DAPPER version is recorded.**
- `object_refs` indexes every `dapper:` reference in a payload (`from_id`, `to_id`). `deposit_edges` keeps
  deposited edges with their payloads. `deposits` and `deposit_objects` record who sent what and when.
- Object collections: `object_releases` (state, definition, `release_id` once closed) and `release_objects`
  (`collection_key`, `version`, `object_id`). A closed release is immutable, and its ResultCollection lists its
  members by id.
- The manifest's `dapper_profile` (e.g. `0.2.1-a1`) is a label only. Nothing checks it.
- Deposits are checked with one vendored DAPPER (`vendor/dapper`, `service/dapper_validation.py`): every key must
  be a slot, required slots present, and the id must be what `dapper_identity.compute_id` mints.

## How an id depends on the schema

DAPPER-ID-1 hashes:
- the class name;
- for each slot the class marks `hashable`, the slot's **name** and value.

Slot URIs, ranges and descriptions don't enter the id. So two versions mint the same id for a payload exactly
when:
- the class still exists;
- every key is still a slot;
- the same keys are hashable.

That makes the diff exact. `changed_slots` lists:
- `+slot`: newly hashed;
- `-slot`: no longer hashed;
- `?slot`: no longer a slot of the class.

What was checked on 2026-11-27:

- **0.2.1-a1 → 0.3.0-a1 changes no hashability for any slot present in both.** Only three classes went
  (`CausalStep`, `DismechLinked`, `HasCausalStep`). Everything else that changed is shape: slots removed or
  renamed (`question` and `hypothesis` → `framing`, `mechanistic_model` → `explanatory_model`, `trait` and
  `ancestry` → `contexts`, `dismech_*` → edges). So between these two versions an id never changes silently: an
  object either mints the same, or no longer validates and needs converting.
- CFDE producer documents from six libraries (GTEx, HuBMAP, IDG, IGVF, KPMP, RummaGEO) and the
  `cfde_sources` document verify unchanged under 0.3: no id changes for the 96,175 `cfde-genesets` objects'
  kinds.
- 0.2.1-shaped ScientificAccounts fail validation under 0.3 (`ScientificAccount has no slot 'question'`).

## Several DAPPER versions side by side

`vendor/dapper/` is the current version. Deposits are minted and checked against it.
`vendor/dapper-archive/<version>/` keeps each version vendored before it, with its own `dapper_identity.py` and
schema. `scripts/vendor_dapper.py` now copies the outgoing version there before replacing it.
`service/collections/identity_versions.py` loads any of them in one process (each identity module under its own
module name) and exposes `mint_id`, `hashed`, `hashable` and `unknown_slots`.

Proposed next step: let a deposit be checked under an archived version.
- **Why.** REVEAL's hosted agent pins 0.2.1-a1. Moving the service to 0.3 alone would refuse every REVEAL
  account deposit, because they use `question` and `mechanistic_model`.
- **Rule.** A deposit names its version (`dapper_version` in the deposit body; default: current). The service
  checks it against that vendored version and records the version with each object. A collection's manifest
  `dapper_profile` becomes binding: deposits into a release must use that version.
- **Retiring a version.** Once nothing deposits under it, drop it from the accepted list. Keep it in the archive
  for rescans.

This also fixes most of the 16 test failures on this branch:
- the suite's fixtures are 0.2.1-shaped documents (accounts with `question`, the 0.2.1 test vectors);
- with per-deposit versions they would deposit as 0.2.1, unchanged.

They are left failing on purpose until the rule above is agreed.

## Shape conversion versus re-minting

- **Re-mint only**: the payload is valid under the new version but hashes different slots (`different`). The new
  id is `compute_id` of the same payload. No content changes.
- **Convert, then re-mint**: the payload is not valid under the new version (`invalid_current`). It needs a
  conversion first. DAPPER has no 0.2→0.3 converter: `dapper/converter/` converts dig.geneset provenance, and
  the 0.3 release notes describe the 0.2→0.3 steps in prose ("Migration from 0.2.0"). Proposed: DAPPER ships
  one converter per release step (`converter/migrate_0_2_1_to_0_3_0.py`), as a pure function from a document
  under the old version to one under the new. Some steps are mechanical:
  - rename `mechanistic_model`;
  - wrap `question` and `hypothesis` into `framing`;
  - move `trait` and `ancestry` into `contexts`.

  One isn't: a CausalStep needs its two variables named. So the converter must be able to refuse an object and
  say why; such objects stay on their old id. The claims repository vendors the converters with the schema, so
  rescans and re-mints use the same code DAPPER tests.
- **References move with it.** Converting a document changes the ids of its records and of every record that
  references them. A re-mint must run over whole documents (the deposit's objects together), in dependency order,
  as `dapper_identity.assign_ids` already does, never object by object.

## Old ids stay resolvable: the alias table

```sql
CREATE TABLE IF NOT EXISTS object_id_aliases (
  old_id        VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  new_id        VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  from_version  VARCHAR(32)  CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  to_version    VARCHAR(32)  CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  reason        VARCHAR(16)  CHARACTER SET ascii COLLATE ascii_bin NOT NULL,  -- remint | convert
  changed_slots VARCHAR(1024) NULL,                                           -- "+scope -trait ?trait"
  migration_id  VARCHAR(40)  CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at    DATETIME NOT NULL,
  PRIMARY KEY (old_id, to_version),
  KEY idx_alias_new (new_id)
);
```

How the old id behaves:

- **The old object is never deleted or edited.** Its row stays in `dapper_objects`, so every closed release that
  lists it still resolves exactly as published. The new object is a new row. The alias says one replaces the other
  as an *identifier*; the object itself isn't deprecated.
- **`GET /v1/objects/<old id>`** serves the stored old document, as today, with two added fields:
  `deprecated_by: <new id>` and `id_status: superseded`. It doesn't redirect, because a client that pinned the
  old id must still get the bytes it pinned. The HTML landing page shows a banner linking to the new id. A
  `?follow=1` parameter returns a 307 to the new id for clients that want the current form.
- **`GET /v1/objects/<new id>`** carries `supersedes: [<old id>, ...]`.
- **Search** (`/v1/objects/search`, the admin object list) matches either id. Results show the current id, with
  the old one under `also_known_as`.
- **References.** An old document's references are left as written: they're part of what was published. New
  documents written by a re-mint reference new ids. `object_refs` gains rows for the new documents. A reference
  query that starts from an old id follows the alias to the new id as well.
- **Edges.** `deposit_edges` rows are deposit records and stay as deposited. A migration writes the re-minted
  edges as a new deposit (a system deposit by the migration's key), so an edge's provenance shows it came from a
  migration.

## Releases stay immutable

A closed release lists ids, and those ids keep resolving to the same bytes. A migration never edits a release.
For an object collection whose members changed id:

- the migration opens a **new release** of the collection (for example `2026.11.0` → `2026.11.0+dapper-0.3.0`,
  or the next version the owner chooses);
- it deposits the re-minted objects plus every unchanged member, then closes it;
- the new release's ResultCollection names the old one in `was_revision_of`, so the chain is in DAPPER itself.

When nothing changed (the `cfde-genesets` case under 0.3), no release is needed. The rescan records that the
release verified under the new version (`object_release_versions`, below).

```sql
CREATE TABLE IF NOT EXISTS object_versions (       -- one row per object per version it was checked under
  object_id   VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dapper_version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dapper_commit  VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NULL,
  status      VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,  -- minted | same | different | invalid
  hashed_digest CHAR(64) CHARACTER SET ascii NULL,  -- sha256 of the canonical hashed slot->value map
  checked_at  DATETIME NOT NULL,
  PRIMARY KEY (object_id, dapper_version)
);
CREATE TABLE IF NOT EXISTS object_release_versions (  -- a release verified, or re-released, under a version
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version        VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dapper_version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  outcome        VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,  -- unchanged | migrated | blocked
  successor_version VARCHAR(64) NULL,
  migration_id   VARCHAR(40) NULL,
  PRIMARY KEY (collection_key, version, dapper_version)
);
```

**What a deposit records from now on.** One `object_versions` row with `status = minted`, holding:
- the version and vendored commit it was checked under;
- `hashed_digest`, a sha256 of the hashed slot→value map.

The digest makes a later rescan cheap: an object whose hashed map is unchanged under a new version is `same`
without re-canonicalising RDF. It also shows what changed when the map differs. The identity profile stays in
`dapper_objects.identity_profile`.

**Back-filling.** For objects stored before this, the rescan derives `minted under` by trying each vendored
version in turn (oldest first). An id no vendored version reproduces is `unknown`.

## The admin flow

A new admin console page, "DAPPER versions", lists the vendored versions (current and archived) and, per
collection and for the objects deposited outside any collection, the last rescan.

1. **Scan.** Start a rescan job (the console's job runner, as for collection registration) for everything, one
   collection or one class. It runs `rescan_identity` against the database and writes `object_versions` rows. It
   is read-only towards objects.
2. **Report.** The page shows:
   - counts by class and status (`same`, `different`, `invalid_current`, `other_profile`);
   - the most common `changed_slots`;
   - for each collection, whether every member is `same`;
   - samples with a side-by-side view of the hashed maps.

   The TSV downloads.
3. **Approve.** For `different` (re-mint) and `invalid_current` with a converter, an admin approves a migration
   for a scope (a collection, or the uncollected objects of a class). Objects the converter refuses are listed
   and stay as they are.
4. **Apply.** The migration job:
   - groups objects by their original deposit, so documents are re-minted whole;
   - converts if needed and re-mints with `assign_ids`;
   - deposits the new objects as a system deposit;
   - writes `object_id_aliases`, opens and closes successor releases for affected collections, and writes
     `object_release_versions`.

   It is resumable: it records progress per deposit, and resending changes nothing. Every step is audited in
   `admin_auth_events`.

QA and prod share one database, so apply runs once, against that database, after the report is reviewed. The scan
can run anywhere.

## MySQL migration

`schema/migrations/013_dapper_versions.sql` (to write):
- creates `object_versions`, `object_id_aliases` and `object_release_versions` as above;
- adds `deposits.dapper_version VARCHAR(32) NULL`, the version a deposit was checked under.

All of it is additive. It needs the SQLite mirror in `service/db.py` and a backfill step in `scripts/migrate.py`
that runs the rescan in `minted`-derivation mode.

## Prototype results on the local database

`scripts/rescan_identity.py --db ~/src/research/claims-repository/claims.db` (opened read-only; the local copy
dates from 2026-11-11 and has no object collections) with 0.3.0-a1 current and 0.2.1-a1 archived:

| | Objects |
|---|---|
| Stored objects scanned | 470,372 |
| `DAPPER-ID-1` objects | 297,671 |
| … mint the same id under 0.2.1-a1 and 0.3.0-a1 (`same`) | 297,669 |
| … mint a different id under 0.3 (`different`) | 0 |
| … no longer valid under 0.3 (`invalid_current`) | 2 |
| … minted under no vendored version (`unknown`) | 0 |
| Older `local-dev-sha256-1` profile, not recomputed (`other_profile`) | 172,701 |

The two invalid objects are one Activity and one Dataset that carry `trait`
(`changed_slots: -trait ?trait`). 0.3 moved `trait` into `contexts`, so they need the 0.2→0.3 conversion, and
their new ids would cascade to whatever references them. Everything else, including the 5,000 GeneSets, the
129,926 EvidenceItems and the 87,135 Propositions, is unchanged. The run takes about five minutes on a laptop.
The full report is the TSV and JSON the script writes.

## Open questions

- Should a deposit name its DAPPER version explicitly, or should the service try the current version and then the
  archived ones? Explicit is safer: a payload valid under two versions with different meanings shouldn't be
  guessed at.
- How long should the service accept 0.2.1-a1 deposits after 0.3 is current? That is until REVEAL's hosted agent
  moves to 0.3.
- Successor release naming for migrated object collections: a new owner-chosen version, or a derived label such
  as `2026.11.0+dapper-0.3.0`?
- Should DAPPER own the per-release converters (proposed), or the claims repository?
