# Offline storage split migration

The one-time migration command converts a quiescent legacy `state.db` into a
new, independently staged directory containing `state.db`, `audit.db`,
`research.db`, and `migration-manifest.json`.

It never starts Bongus, connects to an exchange, changes configuration, or
replaces, renames, deletes, checkpoints, or opens the source database for
writing. Publication is a no-overwrite directory rename: either the complete
verified output is visible or the requested output path is absent.

## Preconditions

1. Stop the trader, Rust execution engine, watchdog, dashboard, and every other
   process that can access the database.
2. Confirm neither `state.db-wal` nor `state.db-shm` exists. Do not delete a
   sidecar to make the check pass; its presence means the database is not a
   proven quiescent image.
3. Create and retain a verified backup with
   `bongus.engine.database_backup.create_verified_backup`. The migration
   independently verifies that backup and then compares every source table's
   schema, row count, and deterministic content hash against it.
4. Choose a new, nonexistent output directory on a volume with enough space
   for one source-sized rebuild plus the required recovery headroom.

The source must have the current `StateWriter` `application_id` and
`user_version`. Unknown tables, views, foreign-key violations, corruption,
backup mismatch, sidecars, changing source bytes, insufficient space, or an
existing output path all fail closed.

## Dry run

From the release root:

```powershell
python -m bongus.engine.offline_storage_migration dry-run `
  --source C:\BongusData\state.db `
  --backup-manifest C:\BongusBackups\state.20260809T120000Z.db.manifest.json `
  --output C:\BongusData\migration-20260809
```

Dry run performs the full read-only source/backup integrity, canonical-content,
schema-routing, and peak-space preflight. It does not create the output path.

## Execute

After reviewing the dry-run JSON:

```powershell
python -m bongus.engine.offline_storage_migration execute `
  --source C:\BongusData\state.db `
  --backup-manifest C:\BongusBackups\state.20260809T120000Z.db.manifest.json `
  --output C:\BongusData\migration-20260809
```

By default, legacy Tier-C rows (candidate, score, feature, shadow, execution
quality, and market samples) remain only in the verified authoritative backup.
Their source count and content hash, omission reason, and backup SHA-256 are
recorded in the migration manifest. Pass `--retain-research` only when there is
enough peak space and those legacy rows are explicitly required in the new
`research.db`.

The output routing is:

- `state.db`: mutable positions, risk latches, reservations, pending intents,
  outbox, cursors, telemetry receipts, cooldowns, and governance state.
- `audit.db`: immutable order/fill/economic/statement/lifecycle evidence and
  bounded reconciliation/health records.
- `research.db`: reproducible Tier-C evidence schemas, with rows retained only
  when requested.

All three outputs use the source application/schema versions and
`auto_vacuum=INCREMENTAL`. Before publication, the command checks exact retained
row counts and content hashes, schema objects, `quick_check`, `integrity_check`,
and `foreign_key_check`, fsyncs every file, and writes a hash-bound manifest.

## Stopped activation procedure

Successful migration creates a verified, inactive artifact. Keep the legacy
database and independently verified backup unchanged, and do not move files out
of the published directory. The three databases and
`migration-manifest.json` are one activation unit.

With every Bongus process still stopped:

1. Review the manifest, preserve enough rollback headroom, and set
   `BONGUS_DATA_ROOT` to the absolute published migration directory. Do not set
   it to the legacy database's directory.
2. Start a non-live maintenance/preflight invocation that constructs the
   production split writer. Before its first writable database connection, the
   runtime verifies the manifest hash, fixed filenames, exact role schemas,
   recorded file sizes and SHA-256 hashes, SQLite integrity evidence, and the
   absence of sidecars. It then durably records a manifest-bound activation
   marker in `state.db`.
3. Stop again and independently inspect the activation marker and all three
   database paths. A reader refuses an unactivated migration, and later starts
   refuse a missing, malformed, replaced, or identity-mismatched manifest and
   any role-schema drift.
4. Start only paper mode, complete signed account/exchange reconciliation, and
   exercise backup/restore and rollback procedures before considering any
   promotion gate.

Never replace or delete the source as part of activation, never point
`BONGUS_DATA_ROOT` at a partial trio, and never enable live mode as part of
migration or activation. A newly initialized empty trio is supported for a new
installation; an existing legacy monolith or an unmarked trio without its
manifest fails closed.
