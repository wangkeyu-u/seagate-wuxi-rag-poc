#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_app.ingestion import DEFAULT_ALLOWED_SOURCES  # noqa: E402
from rag_app.source_operations import MAX_JOB_EXPORTS, run_source_sync_job  # noqa: E402
from rag_app.storage import SourceJobLockedError  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one locked, governed synchronization job over approved offline exports"
    )
    parser.add_argument(
        "--export",
        action="append",
        type=Path,
        required=True,
        dest="exports",
        help=f"export path in processing order; repeat up to {MAX_JOB_EXPORTS} times",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "runtime" / "rag_mvp.sqlite3",
        help="runtime SQLite database",
    )
    parser.add_argument(
        "--manifest",
        action="append",
        type=Path,
        dest="manifests",
        help="signed manifest paired by position with each --export; repeat for every export",
    )
    parser.add_argument(
        "--trust-jwks",
        type=Path,
        help="pinned local public JWKS used only for source manifest verification",
    )
    parser.add_argument(
        "--require-signed-manifests",
        action="store_true",
        help="fail configuration unless every export has a manifest and pinned trust JWKS",
    )
    parser.add_argument(
        "--master-data",
        type=Path,
        default=ROOT / "data" / "master_data.json",
        help="approved local master-data JSON catalog",
    )
    parser.add_argument(
        "--allow-source",
        action="append",
        dest="allowed_sources",
        help="restrict the job to this source_system; may be repeated",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="write accepted records from a partially rejected export without advancing its cursor",
    )
    parser.add_argument(
        "--lock-ttl-seconds",
        type=int,
        default=900,
        help="job lease duration between 60 and 3600 seconds",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        result = run_source_sync_job(
            export_paths=args.exports,
            database_path=args.database,
            master_data_path=args.master_data,
            allowed_sources=args.allowed_sources or sorted(DEFAULT_ALLOWED_SOURCES),
            allow_partial=args.allow_partial,
            lock_ttl_seconds=args.lock_ttl_seconds,
            manifest_paths=args.manifests,
            trust_jwks_path=args.trust_jwks,
            require_signed_manifests=args.require_signed_manifests,
        )
    except SourceJobLockedError as exc:
        print(json.dumps({"status": "LOCKED", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(3) from exc
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "COMPLETED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
