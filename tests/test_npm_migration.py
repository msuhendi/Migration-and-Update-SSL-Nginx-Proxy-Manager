import json
import logging
import tempfile
import unittest
from pathlib import Path

from npm_migration import (
    domain_matches,
    migrate_proxy_hosts,
    proxy_payload,
    ssl_payload,
    update_ssl_hosts,
    write_audit_report,
)


class FakeClient:
    def __init__(self, collections=None):
        self.collections = collections or {}
        self.created = []
        self.updated = []
        self.deleted = []
        self._next_id = 100

    def collection(self, path):
        return [dict(item) for item in self.collections.get(path, [])]

    def create(self, path, payload):
        self._next_id += 1
        item = dict(payload, id=self._next_id)
        self.created.append((path, item))
        return item

    def update(self, path, payload):
        self.updated.append((path, payload))
        return dict(payload, id=int(path.rsplit("/", 1)[-1]))

    def delete(self, path):
        self.deleted.append(path)


class MigrationTests(unittest.TestCase):
    def test_domain_matching_is_exact_or_real_subdomain(self):
        self.assertTrue(domain_matches("www.Example.com.", "example.com"))
        self.assertTrue(domain_matches("example.com", "example.com"))
        self.assertFalse(domain_matches("notexample.com", "example.com"))
        self.assertFalse(domain_matches("www.example.com", "example.com", False))

    def test_payload_resets_security_by_default(self):
        payload = proxy_payload(
            {
                "domain_names": ["example.com"],
                "certificate_id": 7,
                "ssl_forced": True,
                "hsts_enabled": True,
            }
        )
        self.assertEqual(payload["certificate_id"], 0)
        self.assertFalse(payload["ssl_forced"])
        self.assertFalse(payload["hsts_enabled"])

    def test_ssl_payload_allows_https_and_http2_to_be_disabled(self):
        payload = ssl_payload({"domain_names": ["example.com"]}, 4)
        self.assertEqual(payload["certificate_id"], 4)
        self.assertTrue(payload["ssl_forced"])
        self.assertTrue(payload["http2_support"])
        disabled = ssl_payload(
            {"domain_names": ["example.com"]},
            4,
            enable_http2=False,
            force_ssl=False,
        )
        self.assertFalse(disabled["ssl_forced"])
        self.assertFalse(disabled["http2_support"])

    def test_dry_run_creates_backup_without_writing_destination(self):
        source = FakeClient({"/api/nginx/proxy-hosts": [{"id": 1, "domain_names": ["example.com"]}]})
        destination = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            summary = migrate_proxy_hosts(
                source,
                destination,
                dry_run=True,
                backup_dir=directory,
                logger=logging.getLogger("test"),
            )
            backup = Path(summary["backup"])
            self.assertTrue(backup.exists())
            self.assertEqual(json.loads(backup.read_text())[0]["id"], 1)
        self.assertEqual(destination.created, [])
        self.assertEqual(summary["created"], 1)

    def test_skip_and_update_existing_host(self):
        host = {"id": 1, "domain_names": ["example.com"], "forward_port": 8080}
        source = FakeClient({"/api/nginx/proxy-hosts": [host]})
        destination = FakeClient({"/api/nginx/proxy-hosts": [{"id": 8, "domain_names": ["example.com"]}]})
        with tempfile.TemporaryDirectory() as directory:
            skipped = migrate_proxy_hosts(source, destination, backup_dir=directory)
            updated = migrate_proxy_hosts(
                source,
                destination,
                mode="update",
                backup_dir=directory,
            )
        self.assertEqual(skipped["skipped"], 1)
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(destination.updated[0][0], "/api/nginx/proxy-hosts/8")

    def test_ssl_update_only_targets_exact_domain_tree(self):
        hosts = [
            {"id": 1, "domain_names": ["app.example.com"]},
            {"id": 2, "domain_names": ["notexample.com"]},
        ]
        client = FakeClient({"/api/nginx/proxy-hosts": hosts})
        summary = update_ssl_hosts(
            client,
            target_domain="example.com",
            certificate_id=9,
            dry_run=False,
        )
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(client.updated[0][0], "/api/nginx/proxy-hosts/1")

    def test_audit_report_contains_status_context_and_items(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = write_audit_report(
                "ssl-update",
                {"updated": 1, "failed": 0, "errors": [], "items": [{"id": 1}]},
                context={"target_domain": "example.com", "force_ssl": False},
                report_dir=directory,
            )
            report = json.loads(report_path.read_text())
        self.assertEqual(report["operation"], "ssl-update")
        self.assertEqual(report["status"], "success")
        self.assertFalse(report["context"]["force_ssl"])
        self.assertEqual(report["summary"]["items"][0]["id"], 1)
        self.assertIn("started_at", report)
        self.assertIn("finished_at", report)


if __name__ == "__main__":
    unittest.main()