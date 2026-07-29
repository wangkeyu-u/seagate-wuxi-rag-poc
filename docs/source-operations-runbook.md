# Governed source synchronization and health runbook

Status: implemented offline operational boundary for the PoC. It does not connect to a live SeaTrack or enterprise DMS endpoint and does not start a background daemon.

## Operating model

An enterprise scheduler invokes one bounded process with one or more files that have already arrived through an approved transfer channel:

```text
approved transfer -> signed manifest + export -> scheduler -> one-shot locked job
                  -> pinned-key signature / digest binding -> schema validation
                  -> master-data reconciliation -> source ledger / quarantine -> health signal
                  -> explicit service restart when the retrieval catalog changed
```

The job never polls an upstream system, deletes or moves input files, follows a symlink, calls a URL from file content, or opens an HTTP upload route. Transfer authentication, landing-zone retention, malware/DLP checks, scheduler credentials, and restart orchestration remain deployment responsibilities.

## Signed delivery manifest

The controlled deployment mode requires one exact-schema manifest per export and a pinned local public JWKS. The manifest binds:

- schema version and unique manifest ID;
- approved `source_system`;
- creation and expiry timestamps with timezone, with a maximum 24-hour validity;
- artifact basename, exact byte count, and lowercase SHA-256;
- fixed `RS256` algorithm and approved signing-key `kid`.

The signature covers canonical UTF-8 JSON with sorted keys and compact separators. It includes `signing.alg` and `signing.kid` but omits only `signing.signature`. The exporter owns the private key and signature operation. This repository reads only a public JWKS, rejects private RSA parameters, duplicate key IDs, weak keys and non-RS256 keys, and never follows a key URL supplied by a manifest.

The export is read once after signature verification. That exact byte buffer is checked for size and digest and then parsed as JSON, avoiding a verify/reopen replacement window. The signed manifest `source_system` must equal the validated export envelope source.

Verified manifest IDs are registered in the runtime database. Reusing the same ID with the same source, artifact digest and signing key is treated as an observable idempotent retry. Rebinding an existing ID to a different source, digest or signing key is rejected as a critical `MANIFEST_REPLAY_CONFLICT` quarantine event. Corrected deliveries therefore need a new signed manifest ID.

[source_manifest_v1.template.json](../examples/source_manifest_v1.template.json) shows the shape and current example artifact hash. Its signature and key ID are placeholders and are intentionally not valid.

## Scheduled command

The preferred automation entry point is strict by default:

```bash
python3 scripts/run_source_sync_job.py \
  --export /approved/landing/seatrack-export.json \
  --manifest /approved/landing/seatrack-export.manifest.json \
  --export /approved/landing/dms-export.json \
  --manifest /approved/landing/dms-export.manifest.json \
  --trust-jwks /approved/config/source-exporters.jwks.json \
  --require-signed-manifests \
  --database /approved/runtime/rag_mvp.sqlite3
```

Files are processed in the supplied order, up to 32 per invocation. Paths must be unique. If a file for a source fails, later files for that same source are skipped during the invocation; an independent source may continue. The JSON result contains a unique job ID, per-file status, ledger run IDs, counts, `restart_required`, and a redacted health snapshot.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Every file completed cleanly |
| `2` | Validation, reconciliation, or synchronization did not complete cleanly |
| `3` | Another non-expired source synchronization job owns the lease |

`--allow-partial` is an explicit exception: accepted records are ledgered, the run becomes `PARTIAL`, its source cursor does not advance, and the process still exits non-zero. Do not configure a scheduler to treat exit `2` as success.

Unsigned invocation remains available only for local compatibility and migration tests. A controlled deployment should always set `--require-signed-manifests`; missing manifests or a missing trust JWKS then fail before acquiring the job lease or writing the runtime database.

## Job lease and retry behavior

The runtime database contains a single `source-sync` lease. Acquisition and expiry reclamation are transactional. The default lease is 900 seconds; `--lock-ttl-seconds` accepts 60–3,600 seconds. The job releases its lease in a `finally` boundary, while a crashed process becomes retryable after expiry.

The ledger provides record-level idempotency using source ID, source timestamp, state, and canonical SHA-256. Retrying the same clean file is safe and yields unchanged records. A scheduler should use bounded exponential retry only for transient process or storage failures. It should not automatically retry schema conflicts, master-data conflicts, or partial runs until an operator has reviewed the source file.

## Master-data reconciliation

After exact-schema validation and before any ledger write, every UPSERT is reconciled against `data/master_data.json`:

- observations require known product, line, station, equipment, Failure Code, and software references;
- a station must belong to the supplied line and its assigned equipment must match;
- material lots and firmware/test-program versions must apply to the supplied product;
- firmware and test-program references must have the expected software type;
- documents require a known owner team, Failure Codes, products or product families, lines, and stations;
- a document station cannot contradict its declared line scope.

DELETE tombstones do not contain business fields, so their reference relationships are not re-evaluated. Current lifecycle status is not used to reject historical observations because the demo master catalog is not effective-dated at every relationship boundary.

## Read-only health API

`GET /api/admin/source-health` and `GET /api/admin/source-quarantine` require an authenticated identity with the explicit `sources:monitor` permission. An application role alone, including a quality role, does not grant it. OIDC deployments must add that permission to `RAG_OIDC_PERMISSION_ALLOWLIST` and arrange for the approved entitlement claim to carry it.

The response contains only source-level counts, cursor/freshness state, latest run summary, current lease state, and sanitized alerts. It does not expose record content, rejection details, lock owner, raw tokens, or upstream credentials.

Default successful-sync freshness thresholds are:

| Source | Threshold |
| --- | --- |
| `SEATRACK_EXPORT` | 7,200 seconds |
| `APPROVED_DMS_EXPORT` | 86,400 seconds |

Configure both together at service startup:

```bash
RAG_SOURCE_STALE_SECONDS='{"SEATRACK_EXPORT":3600,"APPROVED_DMS_EXPORT":43200}' \
python3 server.py --host 127.0.0.1 --port 8787
```

Values must be integers from 60 through 2,678,400 seconds. Invalid or incomplete configuration prevents startup.

Alert codes:

| Code | Severity | Operator response |
| --- | --- | --- |
| `NEVER_SYNCED` | Critical | Verify scheduler deployment and initial approved export |
| `STALE_SOURCE` | Critical | Check upstream export, transfer, scheduler, and recent clean cursor |
| `LAST_RUN_PARTIAL` | Warning | Review the latest run and rejected source records |
| `REJECTIONS_DETECTED` | Warning | Correct isolated recent quality or reconciliation failures |
| `HIGH_REJECTION_RATE` | Critical | Stop automated progression and investigate source/schema drift |
| `QUESTIONABLE_RECORDS` | Warning | Keep the quality flag visible and require human evidence review |
| `OPEN_QUARANTINE` | Warning or critical | Review the delivery failure and record an explicit disposition |

`HEALTHY` means both approved sources have fresh clean cursors and no active warnings. `DEGRADED` means warnings exist without a critical alert. `CRITICAL` means at least one critical source condition exists. An actively held, non-expired lease is reported as state and is not itself an alert.

## Quarantine and disposition

Manifest verification and source-binding failures create critical quarantine events. Schema, master-data reconciliation and partial-ledger failures create warning events. The ledger stores only job/source identifiers, manifest ID, artifact basename and SHA-256 when trusted, stage, sanitized reason code/summary, severity and rejected count. It does not store export content, record-level error payloads, private keys, raw tokens or absolute landing-zone paths.

Listing requires `sources:monitor`. Resolution requires the independent `sources:quarantine:manage` permission:

```http
POST /api/admin/source-quarantine/QUAR-.../resolution
Content-Type: application/json

{"resolution":"RETRY","notes":"Approved exporter regenerated and re-signed the delivery."}
```

`resolution` is `RETRY` or `REJECT`. It records the authenticated operator and notes but does not import, delete, move or modify the source artifact. An event can be resolved once; a retry is a new scheduler invocation and is still subject to the complete verification chain.

## Recovery

Use `scripts/import_seatrack_export.py --list-runs` to inspect the ledger. Only the latest active run for a source is rollback-eligible, and rollback must proceed in reverse order. After any result with `"restart_required": true`, restart the service through the approved deployment controller so its in-memory retrieval catalog is rebuilt. Do not kill or replace the database file as a recovery mechanism.

Before a live pilot, place the pinned JWKS under approved configuration management, define exporter key rotation and revocation, connect these signals to the enterprise monitoring platform, define on-call ownership and retention, validate scheduler failover, and test source-to-index withdrawal propagation under the approved change process.
