"""CLI for applying a certificate to matching NPM Proxy Hosts."""

from __future__ import annotations

import argparse
import json
import os
import sys

from npm_migration import (
    NpmApiError,
    NpmClient,
    configure_logging,
    update_ssl_hosts,
    write_audit_report,
)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Nilai {name} harus boolean: true/false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.getenv("NPM_SERVER_URL"))
    parser.add_argument("--user", default=os.getenv("NPM_USER"))
    parser.add_argument("--password", default=os.getenv("NPM_PASSWORD"))
    parser.add_argument(
        "--certificate-id",
        type=int,
        default=int(os.getenv("NPM_SSL_CERTIFICATE_ID", "0")),
    )
    parser.add_argument("--target-domain", default=os.getenv("NPM_TARGET_DOMAIN"))
    parser.add_argument("--exact-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    http2_group = parser.add_mutually_exclusive_group()
    http2_group.add_argument(
        "--http2",
        dest="enable_http2",
        action="store_true",
        help="Aktifkan HTTP/2 pada host target.",
    )
    http2_group.add_argument(
        "--no-http2",
        dest="enable_http2",
        action="store_false",
        help="Jangan aktifkan HTTP/2 pada host target.",
    )
    ssl_group = parser.add_mutually_exclusive_group()
    ssl_group.add_argument(
        "--ssl-forced",
        dest="force_ssl",
        action="store_true",
        help="Aktifkan redirect HTTP ke HTTPS pada host target.",
    )
    ssl_group.add_argument(
        "--no-ssl-forced",
        dest="force_ssl",
        action="store_false",
        help="Jangan paksa redirect HTTP ke HTTPS.",
    )
    parser.set_defaults(
        enable_http2=env_bool("NPM_ENABLE_HTTP2", True),
        force_ssl=env_bool("NPM_SSL_FORCED", True),
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
        ("--server-url", args.server_url),
        ("--user", args.user),
        ("--password", args.password),
        ("--target-domain", args.target_domain),
    )
    missing = [name for name, value in names if not value]
    if missing:
        parser.error("Konfigurasi wajib belum diisi: " + ", ".join(missing))
    if args.certificate_id <= 0:
        parser.error("--certificate-id harus berupa angka positif")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    require_config(parser, args)
    configure_logging(args.log_file, args.verbose)
    client = NpmClient(
        args.server_url,
        args.user,
        args.password,
        timeout=args.timeout,
        retries=args.retries,
        verify=not args.insecure,
    )
    try:
        client.login()
        summary = update_ssl_hosts(
            client,
            target_domain=args.target_domain,
            certificate_id=args.certificate_id,
            dry_run=args.dry_run,
            include_subdomains=not args.exact_only,
            enable_http2=args.enable_http2,
            force_ssl=args.force_ssl,
        )
    except NpmApiError as exc:
        summary = {"failed": 1, "errors": [str(exc)], "items": []}
        report_path = write_audit_report(
            "ssl-update",
            summary,
            context={
                "target_domain": args.target_domain,
                "certificate_id": args.certificate_id,
                "enable_http2": args.enable_http2,
                "force_ssl": args.force_ssl,
            },
            report_file=args.report_file,
            report_dir=args.report_dir,
        )
        print(json.dumps({"error": str(exc), "report": str(report_path)}, ensure_ascii=True), file=sys.stderr)
        return 1
    report_path = write_audit_report(
        "ssl-update",
        summary,
        context={
            "server_url": args.server_url,
            "target_domain": args.target_domain,
            "certificate_id": args.certificate_id,
            "exact_only": args.exact_only,
            "dry_run": args.dry_run,
            "enable_http2": args.enable_http2,
            "force_ssl": args.force_ssl,
        },
        report_file=args.report_file,
        report_dir=args.report_dir,
    )
    summary["report"] = str(report_path)
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 1 if summary["failed"] or summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())