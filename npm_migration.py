"""Shared API and migration helpers for Nginx Proxy Manager."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger(__name__)
RETRY_STATUSES = (429, 500, 502, 503, 504)
EXCLUDED_RESOURCE_FIELDS = {"id", "created_on", "modified_on"}
PROXY_HOSTS_PATH = "/api/nginx/proxy-hosts"


class NpmApiError(RuntimeError):
    """Raised when NPM returns an unsuccessful API response."""


def normalize_base_url(base_url: str) -> str:
    """Return a base URL without trailing slashes."""
    return base_url.strip().rstrip("/")


def create_session(
    retries: int = 3,
    verify: bool = True,
) -> requests.Session:
    """Create a requests session with bounded retries for transient errors."""
    retry_policy = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.5,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST", "PUT", "DELETE"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_policy)
    session = requests.Session()
    for scheme in ("http" + "://", "https://"):
        session.mount(scheme, adapter)
    session.verify = verify
    return session


class NpmClient:
    """Small authenticated client for the NPM endpoints used by this project."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        verify: bool = True,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = session or create_session(retries=retries, verify=verify)
        self._token: str | None = None

    def login(self) -> None:
        response = self.session.post(
            f"{self.base_url}/api/tokens",
            json={"identity": self.username, "secret": self.password},
            timeout=self.timeout,
        )
        data = self._json_or_text(response)
        if not isinstance(data, dict) or response.status_code != 200 or not data.get("token"):
            raise NpmApiError(
                f"Login gagal ke {self.base_url}: "
                f"HTTP {response.status_code} {response_message(data)}"
            )
        self._token = data["token"]

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: Iterable[int] = (200,),
        **kwargs: Any,
    ) -> Any:
        if not self._token:
            raise NpmApiError("Client belum login")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._token}"
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code not in expected:
            data = self._json_or_text(response)
            raise NpmApiError(
                f"{method} {path} gagal: HTTP {response.status_code} "
                f"{response_message(data)}"
            )
        return self._json_or_text(response)

    def collection(self, path: str) -> list[dict[str, Any]]:
        result = self.request("GET", path)
        if not isinstance(result, list):
            raise NpmApiError(f"Response {path} bukan list")
        return result

    def create(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", path, expected=(200, 201), json=payload)
        return result if isinstance(result, dict) else {}

    def update(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.request("PUT", path, json=payload)
        return result if isinstance(result, dict) else {}

    def delete(self, path: str) -> None:
        self.request("DELETE", path, expected=(200, 204))

    @staticmethod
    def _json_or_text(response: requests.Response) -> dict[str, Any] | list[Any]:
        try:
            data = response.json()
            if isinstance(data, (dict, list)):
                return data
        except ValueError:
            pass
        return {"body": response.text}


def normalize_domain(domain: str) -> str:
    """Normalize a DNS name for comparison without changing the API value."""
    return domain.strip().lower().rstrip(".")


def response_message(data: dict[str, Any] | list[Any]) -> str:
    if isinstance(data, dict):
        return str(data.get("message", data.get("body", "")))
    return str(data)


def domain_matches(domain: str, target: str, include_subdomains: bool = True) -> bool:
    """Match only the target domain or its real subdomains."""
    normalized_domain = normalize_domain(domain)
    normalized_target = normalize_domain(target)
    if not normalized_domain or not normalized_target:
        return False
    return normalized_domain == normalized_target or (
        include_subdomains and normalized_domain.endswith(f".{normalized_target}")
    )


def host_matches(host: dict[str, Any], target: str, include_subdomains: bool = True) -> bool:
    return any(
        domain_matches(domain, target, include_subdomains)
        for domain in host.get("domain_names", [])
    )


def domain_key(domains: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_domain(domain) for domain in domains if domain)


def proxy_payload(
    host: dict[str, Any],
    *,
    access_list_id: int = 0,
    certificate_id: int = 0,
    preserve_security: bool = False,
) -> dict[str, Any]:
    """Build an API payload without copying the source host ID."""
    return {
        "domain_names": host.get("domain_names", []),
        "forward_scheme": host.get("forward_scheme", "http"),
        "forward_host": host.get("forward_host"),
        "forward_port": host.get("forward_port"),
        "access_list_id": access_list_id,
        "certificate_id": certificate_id,
        "ssl_forced": host.get("ssl_forced", False) if preserve_security else False,
        "caching_enabled": host.get("caching_enabled", False),
        "block_exploits": host.get("block_exploits", False),
        "advanced_config": host.get("advanced_config", ""),
        "meta": host.get("meta", {}),
        "allow_websocket_upgrade": host.get("allow_websocket_upgrade", False),
        "http2_support": host.get("http2_support", False),
        "hsts_enabled": host.get("hsts_enabled", False) if preserve_security else False,
        "hsts_subdomains": host.get("hsts_subdomains", False) if preserve_security else False,
    }


def save_backup(payload: Any, backup_dir: str | Path, prefix: str = "server-a") -> Path:
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{prefix}-{timestamp}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def write_audit_report(
    operation: str,
    summary: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    report_file: str | Path | None = None,
    report_dir: str | Path = "reports",
) -> Path:
    """Write an audit-safe JSON report for one CLI execution."""
    started_at = summary.pop("started_at", None) or datetime.now(timezone.utc).isoformat()
    finished_at = datetime.now(timezone.utc).isoformat()
    status = "failed" if summary.get("failed") or summary.get("errors") else "success"
    if summary.get("dry_run"):
        status = f"dry-run-{status}"
    if report_file:
        path = Path(report_file)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = Path(report_dir) / f"{operation}-{timestamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "context": context or {},
        "summary": summary,
    }
    path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def _resource_identity(resource: dict[str, Any]) -> tuple[Any, ...]:
    nice_name = resource.get("nice_name")
    if nice_name:
        return ("nice_name", str(nice_name).strip().lower())
    domains = resource.get("domain_names", [])
    if domains:
        return ("domains", domain_key(domains))
    return ("id", resource.get("id"))


def resource_payload(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in resource.items()
        if key not in EXCLUDED_RESOURCE_FIELDS
    }


def _migrate_one_resource(
    source_item: dict[str, Any],
    existing: dict[str, Any] | None,
    destination: NpmClient,
    path: str,
    mode: str,
    dry_run: bool,
) -> int | None:
    if existing:
        existing_id = existing.get("id")
        if not isinstance(existing_id, int):
            raise NpmApiError("resource tujuan tidak memiliki integer id")
        if mode == "skip" or dry_run:
            return existing_id
        if mode == "update":
            destination.update(f"{path}/{existing_id}", resource_payload(source_item))
            return existing_id
        destination.delete(f"{path}/{existing_id}")
    if dry_run:
        return None
    created = destination.create(path, resource_payload(source_item))
    created_id = created.get("id")
    return created_id if isinstance(created_id, int) else None


def migrate_resources(
    source: NpmClient,
    destination: NpmClient,
    *,
    collection_name: str,
    mode: str,
    dry_run: bool,
    logger: logging.Logger = LOGGER,
) -> tuple[dict[int, int], list[str]]:
    """Best-effort migration of API resources and return old-to-new ID mapping."""
    path = f"/api/nginx/{collection_name}"
    source_items = source.collection(path)
    destination_items = destination.collection(path)
    destination_by_identity = {
        _resource_identity(item): item for item in destination_items
    }
    id_map: dict[int, int] = {}
    errors: list[str] = []

    for item in source_items:
        source_id = item.get("id")
        if not isinstance(source_id, int):
            errors.append(f"{collection_name}: resource tanpa integer id")
            continue
        existing = destination_by_identity.get(_resource_identity(item))
        try:
            destination_id = _migrate_one_resource(
                item, existing, destination, path, mode, dry_run
            )
            if destination_id is not None:
                id_map[source_id] = destination_id
        except NpmApiError as exc:
            message = f"{collection_name} id={source_id}: {exc}"
            errors.append(message)
            logger.error(message)
    return id_map, errors


def migrate_proxy_hosts(
    source: NpmClient,
    destination: NpmClient,
    *,
    mode: str = "skip",
    dry_run: bool = False,
    backup_dir: str | Path = "backups",
    include_resources: bool = False,
    logger: logging.Logger = LOGGER,
) -> dict[str, Any]:
    """Migrate proxy hosts with backup, idempotency, and an error summary."""
    source_hosts = source.collection(PROXY_HOSTS_PATH)
    backup_path = save_backup(source_hosts, backup_dir)
    destination_hosts = destination.collection(PROXY_HOSTS_PATH)
    existing_by_domains = {
        domain_key(host.get("domain_names", [])): host for host in destination_hosts
    }
    access_map: dict[int, int] = {}
    certificate_map: dict[int, int] = {}
    errors: list[str] = []

    if include_resources:
        access_map, resource_errors = migrate_resources(
            source,
            destination,
            collection_name="access-lists",
            mode=mode,
            dry_run=dry_run,
            logger=logger,
        )
        errors.extend(resource_errors)
        certificate_map, resource_errors = migrate_resources(
            source,
            destination,
            collection_name="certificates",
            mode=mode,
            dry_run=dry_run,
            logger=logger,
        )
        errors.extend(resource_errors)

    summary = {
        "total": len(source_hosts),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "dry_run": dry_run,
        "backup": str(backup_path),
        "errors": errors,
        "items": [],
    }

    for host in source_hosts:
        label = ", ".join(host.get("domain_names", [])) or "<without-domain>"

        try:
            action = _migrate_one_proxy_host(
                host,
                existing_by_domains,
                destination,
                access_map,
                certificate_map,
                mode,
                dry_run,
            )
            summary[action] += 1
            summary["items"].append(
                {"domains": host.get("domain_names", []), "action": action}
            )
            logger.info("Proxy Host berhasil diproses: %s", label)
        except NpmApiError as exc:
            summary["failed"] += 1
            message = f"Proxy Host {label}: {exc}"
            summary["errors"].append(message)
            summary["items"].append(
                {"domains": host.get("domain_names", []), "action": "failed", "error": str(exc)}
            )
            logger.error(message)
    return summary


def _migrate_one_proxy_host(
    host: dict[str, Any],
    existing_by_domains: dict[frozenset[str], dict[str, Any]],
    destination: NpmClient,
    access_map: dict[int, int],
    certificate_map: dict[int, int],
    mode: str,
    dry_run: bool,
) -> str:
    key = domain_key(host.get("domain_names", []))
    existing = existing_by_domains.get(key)
    if existing and mode == "skip":
        return "skipped"
    if dry_run:
        return "updated" if existing else "created"
    payload = proxy_payload(
        host,
        access_list_id=access_map.get(host.get("access_list_id", 0), 0),
        certificate_id=certificate_map.get(host.get("certificate_id", 0), 0),
        preserve_security=bool(certificate_map.get(host.get("certificate_id", 0), 0)),
    )
    if existing and mode == "update":
        destination.update(f"{PROXY_HOSTS_PATH}/{existing['id']}", payload)
        return "updated"
    if existing:
        destination.delete(f"{PROXY_HOSTS_PATH}/{existing['id']}")
    created = destination.create(PROXY_HOSTS_PATH, payload)
    existing_by_domains[key] = created
    return "updated" if existing else "created"


def ssl_payload(
    host: dict[str, Any],
    certificate_id: int,
    *,
    enable_http2: bool = True,
    force_ssl: bool = True,
) -> dict[str, Any]:
    payload = proxy_payload(
        host,
        access_list_id=host.get("access_list_id", 0),
        certificate_id=certificate_id,
        preserve_security=True,
    )
    payload["ssl_forced"] = force_ssl
    payload["http2_support"] = enable_http2
    return payload


def update_ssl_hosts(
    client: NpmClient,
    *,
    target_domain: str,
    certificate_id: int,
    dry_run: bool = False,
    include_subdomains: bool = True,
    enable_http2: bool = True,
    force_ssl: bool = True,
    logger: logging.Logger = LOGGER,
) -> dict[str, Any]:
    hosts = client.collection(PROXY_HOSTS_PATH)
    summary = {
        "total": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "items": [],
    }
    for host in hosts:
        if not host_matches(host, target_domain, include_subdomains):
            summary["skipped"] += 1
            summary["items"].append(
                {"id": host.get("id"), "domains": host.get("domain_names", []), "action": "skipped"}
            )
            continue
        summary["total"] += 1
        label = ", ".join(host.get("domain_names", []))
        try:
            if not dry_run:
                client.update(
                    f"{PROXY_HOSTS_PATH}/{host['id']}",
                    ssl_payload(
                        host,
                        certificate_id,
                        enable_http2=enable_http2,
                        force_ssl=force_ssl,
                    ),
                )
            summary["updated"] += 1
            summary["items"].append(
                {"id": host.get("id"), "domains": host.get("domain_names", []), "action": "updated"}
            )
            logger.info("SSL berhasil diproses: %s", label)
        except NpmApiError as exc:
            summary["failed"] += 1
            message = f"SSL {label}: {exc}"
            summary["errors"].append(message)
            summary["items"].append(
                {"id": host.get("id"), "domains": host.get("domain_names", []), "action": "failed", "error": str(exc)}
            )
            logger.error(message)
    return summary


class JsonFormatter(logging.Formatter):
    """Compact JSON formatter for file and CI-friendly logs."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "message": record.getMessage(),
            },
            ensure_ascii=True,
        )


def configure_logging(log_file: str | None = None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=handlers,
        force=True,
    )