#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_app.ingestion import (  # noqa: E402
    DEFAULT_ALLOWED_SOURCES,
    ExportValidationError,
    load_export_file,
    validate_export,
)
from rag_app.reconciliation import MasterDataCatalog, MasterDataError, reconcile_export  # noqa: E402
from rag_app.storage import RuntimeStorage  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and import an approved SeaTrack-style JSON export")
    parser.add_argument("path", nargs="?", type=Path, help="path to a v1 JSON export")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "runtime" / "rag_mvp.sqlite3",
        help="runtime SQLite database",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate without writing the source ledger")
    parser.add_argument("--strict", action="store_true", help="abort the whole import when any record is rejected")
    parser.add_argument(
        "--allow-source",
        action="append",
        dest="allowed_sources",
        help="restrict import to this source_system; may be repeated",
    )
    parser.add_argument("--rollback", metavar="RUN_ID", help="roll back the latest active run for its source")
    parser.add_argument("--list-runs", action="store_true", help="list recent source synchronization runs")
    parser.add_argument("--limit", type=int, default=20, help="number of synchronization runs to list")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    requested_modes = sum(bool(value) for value in (args.path, args.rollback, args.list_runs))
    if requested_modes != 1:
        raise SystemExit("provide exactly one export path, --rollback RUN_ID, or --list-runs")
    if not args.path and (args.dry_run or args.strict or args.allowed_sources):
        raise SystemExit("--dry-run, --strict, and --allow-source require an export path")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.list_runs:
        storage = RuntimeStorage(args.database)
        print(json.dumps({"items": storage.list_sync_runs(args.limit)}, ensure_ascii=False, indent=2))
        return
    if args.rollback:
        storage = RuntimeStorage(args.database)
        result = storage.rollback_sync_run(args.rollback, created_at=created_at)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    try:
        raw = load_export_file(args.path)
        allowed_sources = args.allowed_sources or sorted(DEFAULT_ALLOWED_SOURCES)
        export = validate_export(raw, allowed_sources=allowed_sources)
        catalog = MasterDataCatalog.from_file(ROOT / "data" / "master_data.json")
        export = reconcile_export(export, catalog)
    except (OSError, ValueError, ExportValidationError, MasterDataError) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc

    validation_summary = {
        "status": "VALID" if not export.rejected else "PARTIAL",
        "source_system": export.source_system,
        "cursor": export.cursor,
        "total_count": export.total_count,
        "accepted_count": len(export.records),
        "rejected_count": len(export.rejected),
        "errors": list(export.rejected),
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        print(json.dumps(validation_summary, ensure_ascii=False, indent=2))
        if args.strict and export.rejected:
            raise SystemExit(2)
        return
    if args.strict and export.rejected:
        validation_summary["status"] = "REJECTED"
        print(json.dumps(validation_summary, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    storage = RuntimeStorage(args.database)
    result = storage.sync_source_records(export.as_dict(), created_at=created_at)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "PARTIAL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
