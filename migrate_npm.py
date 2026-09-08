"""CLI for migrating NPM proxy hosts from a source to a destination server."""

from __future__ import annotations

import argparse
import json
import os
import sys

from npm_migration import (
    NpmApiError,
    NpmClient,
    configure_logging,
    migrate_proxy_hosts,
    write_audit_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default=os.getenv("NPM_SOURCE_URL"))
    parser.add_argument("--source-user", default=os.getenv("NPM_SOURCE_USER"))
    parser.add_argument("--source-password", default=os.getenv("NPM_SOURCE_PASSWORD"))
    parser.add_argument("--destination-url", default=os.getenv("NPM_DESTINATION_URL"))
    parser.add_argument("--destination-user", default=os.getenv("NPM_DESTINATION_USER"))
    parser.add_argument("--destination-password", default=os.getenv("NPM_DESTINATION_PASSWORD"))
    parser.add_argument(
        "--mode",
        choices=("skip", "update", "overwrite"),
        default=os.getenv("NPM_MIGRATION_MODE", "skip"),
        help="Cara menangani Proxy Host yang sudah ada di tujuan.",
    )
    parser.add_argument("--backup-dir", default=os.getenv("NPM_BACKUP_DIR", "backups"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-resources",
        action="store_true",
        help="Coba migrasikan Access Lists dan Certificates sebelum Proxy Hosts.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--insecure", action="store_true", help="Matikan verifikasi TLS.")
    parser.add_argument("--log-file", default=os.getenv("NPM_LOG_FILE"))
    parser.add_argument("--report-file", default=os.getenv("NPM_REPORT_FILE"))
    parser.add_argument("--report-dir", default=os.getenv("NPM_REPORT_DIR", "reports"))
    parser.add_argument("--verbose", action="store_true")
    return parser


def require_config(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    names = (
        ("--source-url", args.source_url),
        ("--source-user", args.source_user),
        ("--source-password", args.source_password),
        ("--destination-url", args.destination_url),
        ("--destination-user", args.destination_user),
        ("--destination-password", args.destination_password),
    )
    missing = [name for name, value in names if not value]
    if missing:
        parser.error("Konfigurasi wajib belum diisi: " + ", ".join(missing))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    require_config(parser, args)
    configure_logging(args.log_file, args.verbose)
    source = NpmClient(
        args.source_url,
        args.source_user,
        args.source_password,
        timeout=args.timeout,
        retries=args.retries,
        verify=not args.insecure,
    )
    destination = NpmClient(
        args.destination_url,
        args.destination_user,
        args.destination_password,
        timeout=args.timeout,
        retries=args.retries,
        verify=not args.insecure,
    )
    try:
        source.login()
        destination.login()
        summary = migrate_proxy_hosts(
            source,
            destination,
            mode=args.mode,
            dry_run=args.dry_run,
            backup_dir=args.backup_dir,
            include_resources=args.include_resources,
        )
    except NpmApiError as exc:
        summary = {"failed": 1, "errors": [str(exc)], "items": []}
        report_path = write_audit_report(
            "migration",
            summary,
            context={"mode": args.mode, "dry_run": args.dry_run},
            report_file=args.report_file,
            report_dir=args.report_dir,
        )
        print(json.dumps({"error": str(exc), "report": str(report_path)}, ensure_ascii=True), file=sys.stderr)
        return 1
    report_path = write_audit_report(
        "migration",
        summary,
        context={
            "mode": args.mode,
            "dry_run": args.dry_run,
            "include_resources": args.include_resources,
            "source_url": args.source_url,
            "destination_url": args.destination_url,
        },
        report_file=args.report_file,
        report_dir=args.report_dir,
    )
    summary["report"] = str(report_path)
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 1 if summary["failed"] or summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())