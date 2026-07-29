# SeaTrack / approved DMS offline export contract v1

Status: implemented adapter contract for this PoC. This is not evidence of a live SeaTrack or enterprise DMS connection.

All example identifiers and records in this repository are fictional. A real deployment must have the schema, source ownership, classification rules, and transport approved by Manufacturing IT, Quality, Product Engineering, and Security.

## Purpose and trust boundary

The adapter provides a production-shaped bridge between approved upstream exports and the local read-only retrieval catalog:

```text
approved exporter -> signed manifest + controlled JSON file -> pinned-key verification
                  -> strict validator -> master-data reconciliation
                  -> locked one-shot job -> SQLite source ledger or quarantine
                  -> service restart -> repository overlay -> ACL filter -> retrieval
```

It deliberately does not expose an HTTP upload endpoint, fetch arbitrary URLs, follow file paths from document metadata, or execute content from an export. Document text must be inline JSON and is treated as untrusted evidence content.

The command accepts regular, non-symlink UTF-8 JSON files up to 10 MiB. JSON objects with duplicate keys are rejected. The database directory and file retain the runtime store's owner-only permission controls.

The scheduler entry point can require a paired `rag-source-manifest/v1` signed with `RS256`. The exact artifact basename, size and SHA-256, source, validity window, algorithm and signing key ID are covered by the signature. Trust comes only from a pinned local public JWKS; the manifest cannot redirect key lookup. See [Governed source synchronization and health runbook](./source-operations-runbook.md) for canonicalization, rotation and failure handling.

Verified manifest IDs are replay-tracked. Exact repeats remain safe scheduler retries; the same ID bound to a different source, digest or signing key is quarantined and never reaches the source ledger.

## Envelope

Every file is one exact-schema object. Unknown fields are rejected.

```json
{
  "schema_version": "seatrack-export/v1",
  "source_system": "SEATRACK_EXPORT",
  "exported_at": "2026-07-30T09:05:00+08:00",
  "cursor": "opaque-upstream-cursor",
  "records": []
}
```

Rules:

- `schema_version` is exactly `seatrack-export/v1`.
- `source_system` is allowlisted. `SEATRACK_EXPORT` may submit only `TEST_OBSERVATION`; `APPROVED_DMS_EXPORT` may submit only `DOCUMENT_VERSION`.
- `exported_at` and every source timestamp are ISO-8601 values with a timezone.
- `cursor` is an opaque non-empty string, at most 512 characters.
- One file contains at most 5,000 records.
- The pair `(entity_type, source_record_id)` is unique within a file.

Each record has:

```json
{
  "entity_type": "TEST_OBSERVATION",
  "source_record_id": "OBS-DEMO-EXT-0001",
  "source_updated_at": "2026-07-30T09:01:00+08:00",
  "operation": "UPSERT",
  "data": {}
}
```

`operation` is `UPSERT` or `DELETE`. `UPSERT` requires `data`; `DELETE` must omit it or set it to `null`. A delete creates a timestamped tombstone so an older export cannot silently recreate withdrawn data.

## TEST_OBSERVATION

The complete example is [seatrack_observation_export_v1.json](../examples/seatrack_observation_export_v1.json). The data object uses the fields in [data-spec.md](./data-spec.md#51-test_observation测试聚合观测), except `source_system` is derived from the authenticated export envelope and must not be supplied inside `data`.

The validator enforces:

- safe bounded identifiers and finite numeric values;
- positive `units_tested` and non-negative counts;
- `units_passed + units_failed = units_tested`;
- `failure_count <= units_failed`;
- FPY and failure rate agree with their underlying counts within `0.00001`;
- `window_end > window_start`;
- `quality_status` is `RAW`, `VALIDATED`, or `QUESTIONABLE`.

The adapter preserves the upstream quality flag. `QUESTIONABLE` data remains visible as provenance-bearing data and must not become sole deterministic evidence in a future analytics or LLM layer.

Before a write, the controlled import path also requires known product, line, station, equipment and Failure Code references, verifies station/line/equipment ownership, and checks the product applicability and software type of material and software references. These are current-catalog relationship checks, not a substitute for an effective-dated enterprise master-data service.

## DOCUMENT_VERSION

The complete example is [dms_document_export_v1.json](../examples/dms_document_export_v1.json). An imported version contains identity, status, effective dates, owner, approver, classification, canonical locator, applicability, inline content, and ACL scope.

Additional rules:

- an `EFFECTIVE` document requires `approved_by`;
- `confidentiality` is `PUBLIC`, `INTERNAL`, or `RESTRICTED`;
- `allowed_roles` contains only supported server roles;
- `line_ids` and `station_ids` are enforced before retrieval and again for direct document access;
- `canonical_uri` uses `https`, `approved-dms`, or `seatrack`;
- inline content is limited to 200,000 characters;
- obvious instruction-like prompt-injection patterns are quarantined as rejected records;
- superseded, draft, and withdrawn versions are not returned by document retrieval.

The prompt-injection check is defense in depth, not a complete content-security product. A production ingestion service still needs approved malware scanning, DLP/classification, content disarm where applicable, human quarantine review, and model-gateway defenses.

Document imports are reconciled against known owner teams, Failure Codes, products or product families, lines and stations. When both line and station scopes are present, a station outside the declared line scope is rejected.

## Incremental semantics

Source records are keyed by `(source_system, entity_type, source_record_id)` and contain a canonical SHA-256 hash.

- Same timestamp, hash, and state: idempotent `unchanged`.
- Older timestamp: rejected.
- Same timestamp with different content or state: rejected as a source conflict.
- Newer upsert: inserted or updated after a history snapshot is stored.
- Newer delete: withdrawn after a history snapshot is stored.
- Any rejected record makes the run `PARTIAL`; valid records are retained, but the source cursor does not advance.
- A clean run is `COMPLETED` and advances the source cursor atomically.

Every run records counts, timestamps, requested and previous cursor, sanitized rejection reasons, and the run ID that last changed each source record. Imported records expose source system, source ID, update time, ingestion time, content hash, and synchronization run as provenance.

Rollback is intentionally constrained: only the latest non-rolled-back run for a given source may be rolled back. Runs must be unwound in reverse order, preventing a rollback from overwriting a later source change.

## Operator commands

Validate without writing:

```bash
python3 scripts/import_seatrack_export.py \
  examples/seatrack_observation_export_v1.json \
  --dry-run --strict
```

Import into the runtime source ledger:

```bash
python3 scripts/import_seatrack_export.py examples/seatrack_observation_export_v1.json
python3 scripts/import_seatrack_export.py examples/dms_document_export_v1.json
```

For an enterprise scheduler, use the strict, mutually exclusive one-shot entry point:

```bash
python3 scripts/run_source_sync_job.py \
  --export examples/seatrack_observation_export_v1.json \
  --export examples/dms_document_export_v1.json
```

Controlled deployments must pair every export with `--manifest`, configure `--trust-jwks`, and set `--require-signed-manifests`. The repository contains no exporter private key and does not provide a signing operation.

Inspect and roll back:

```bash
python3 scripts/import_seatrack_export.py --list-runs
python3 scripts/import_seatrack_export.py --rollback SYNC-REPLACE_WITH_RUN_ID
```

The running service builds its retrieval index at startup. Restart it after an import or rollback when the command returns `"restart_required": true`.

`--strict` aborts before writing if any record fails validation. Without it, valid records are ledgered, the run is marked `PARTIAL`, the cursor is held, and the command exits non-zero so an operator or scheduler cannot mistake partial acceptance for success.

The scheduled job is strict by default, uses an expiring SQLite lease to prevent concurrent execution, skips later files for a source after that source fails, and reports redacted source health. Its operating contract is [Governed source synchronization and health runbook](./source-operations-runbook.md).

## What remains for a live connector

This contract is the stable seam for the next production step. A live integration still requires:

- approved service identity and network path to each upstream system;
- approved transfer-channel service identity, mutual authentication and landing-zone controls around the implemented signed manifest boundary;
- exporter key issuance, rotation, revocation and pinned-JWKS configuration management;
- enterprise scheduler ownership, retry/failover policy, alert routing, retention, and runbook validation;
- authoritative effective-dated master-data synchronization beyond the implemented local catalog reconciliation;
- production malware/DLP/classification services and content quarantine; the implemented metadata-only failure quarantine does not retain or scan artifacts;
- measured ingestion SLOs and source-to-index withdrawal propagation tests.
