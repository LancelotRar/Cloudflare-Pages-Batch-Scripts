"""Tests for cf_pages_batch_scripts.workflows"""

import io
import zipfile
from pathlib import Path
from unittest.mock import Mock, call, patch

import httpx

from cf_pages_batch_scripts.models import Account, Config, DnsConfig, EnvVar, FilesToRedeploy, PagesConfig
from cf_pages_batch_scripts.workflows import (
    deploy_project,
    prepare_source,
    set_project_config,
    sync_dns_record,
    sync_project_domain,
)


class TestPrepareSource:
    """下载/解压先进入临时目录，失败时不破坏既有源码。"""

    def make_zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("proj/index.js", "x" * 300)
        return buf.getvalue()

    def test_success_unwraps_single_top_dir(self, tmp_path: Path):
        deploy_dir = tmp_path / "deploy"
        cfg = Config(
            files_to_redeploy=FilesToRedeploy(dir=str(deploy_dir), download_url="https://example.com/src.zip")
        )

        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content

            def raise_for_status(self) -> None:
                return None

        with patch(
            "cf_pages_batch_scripts.workflows.httpx.get",
            return_value=FakeResponse(self.make_zip_bytes()),
        ):
            src = prepare_source(cfg)

        assert src is not None
        assert src.name == "proj"
        assert (src / "index.js").exists()
        assert not (deploy_dir.parent / ".deploy.download.tmp").exists()

    def test_failure_keeps_previous_source(self, tmp_path: Path):
        deploy_dir = tmp_path / "deploy"
        prev = deploy_dir / "extracted" / "proj"
        prev.mkdir(parents=True)
        marker = prev / "keep.txt"
        marker.write_text("old", encoding="utf-8")
        cfg = Config(
            files_to_redeploy=FilesToRedeploy(dir=str(deploy_dir), download_url="https://example.com/src.zip")
        )

        with patch(
            "cf_pages_batch_scripts.workflows.httpx.get",
            side_effect=httpx.ConnectError("boom"),
        ):
            assert prepare_source(cfg) is None

        assert marker.exists()
        assert not (deploy_dir.parent / ".deploy.download.tmp").exists()


class TestSetProjectConfig:
    def test_env_whitelist_deletes_extra_variables(self):
        api = Mock()
        api.get_project.return_value = {
            "deployment_configs": {"production": {"env_vars": {"KEEP": {}, "OLD": {}}}}
        }
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="new")],
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        api.patch_project_config.assert_called_once_with("p", {
            "production": {
                "env_vars": {
                    "OLD": None,
                    "KEEP": {"value": "new", "type": "plain_text"},
                }
            }
        })

    def test_kv_false_removes_all_bindings(self):
        api = Mock()
        api.get_project.return_value = {
            "deployment_configs": {"production": {"kv_namespaces": {"OLD": {"namespace_id": "old"}}}}
        }
        api.patch_project_config.return_value = True
        pages = PagesConfig(project_name="p", kv_binding=False, kv_configured=True)
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        api.patch_project_config.assert_called_once_with("p", {
            "production": {"kv_namespaces": {"OLD": None}}
        })

    def test_empty_managed_fields_skip_project_query(self):
        api = Mock()
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=PagesConfig(project_name="p"))

        assert set_project_config(api, account) is True
        api.get_project.assert_not_called()


class TestSyncProjectDomain:
    def test_replaces_old_domains(self):
        api = Mock()
        api.list_domains.return_value = [
            {"name": "old-1.example.com"},
            {"name": "new.example.com"},
            {"name": "old-2.example.com"},
        ]
        api.delete_domain.return_value = {"success": True}

        assert sync_project_domain(api, "project", "new.example.com") is True
        assert api.delete_domain.call_args_list == [
            call("project", "old-1.example.com"),
            call("project", "old-2.example.com"),
        ]
        api.add_domain.assert_not_called()

    def test_adds_target_after_deleting_old_domain(self):
        api = Mock()
        api.list_domains.return_value = [{"name": "old.example.com"}]
        api.delete_domain.return_value = {"success": True}
        api.add_domain.return_value = {"success": True}

        assert sync_project_domain(api, "project", "new.example.com") is True
        assert api.method_calls == [
            call.list_domains("project"),
            call.delete_domain("project", "old.example.com"),
            call.add_domain("project", "new.example.com"),
        ]
        api.delete_domain.assert_called_once_with("project", "old.example.com")
        api.add_domain.assert_called_once_with("project", "new.example.com")

    def test_stops_when_domain_query_fails(self):
        api = Mock()
        api.list_domains.return_value = None

        assert sync_project_domain(api, "project", "new.example.com") is False
        api.delete_domain.assert_not_called()
        api.add_domain.assert_not_called()

    def test_stops_when_old_domain_delete_fails(self):
        api = Mock()
        api.list_domains.return_value = [{"name": "old.example.com"}]
        api.delete_domain.return_value = {"success": False}

        assert sync_project_domain(api, "project", "new.example.com") is False
        api.add_domain.assert_not_called()

    def test_stops_when_target_domain_add_fails(self):
        api = Mock()
        api.list_domains.return_value = []
        api.add_domain.return_value = {"success": False}

        assert sync_project_domain(api, "project", "new.example.com") is False


class TestSyncDnsRecord:
    def make_account(self, dns: DnsConfig) -> Account:
        return Account(name="a", enabled=True, token="t", account_id="aid", pages=PagesConfig(project_name="p"), dns=dns)

    def test_creates_missing_record(self):
        api = Mock()
        api.list_dns_records.return_value = []
        api.create_dns_record.return_value = {"success": True}
        dns = DnsConfig(zone_id="zone", name="app.example.com", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        api.create_dns_record.assert_called_once_with("zone", {
            "type": "CNAME", "name": "app.example.com", "content": "target.pages.dev", "proxied": False, "ttl": 1,
        })

    def test_skips_matching_record(self):
        api = Mock()
        api.list_dns_records.return_value = [{
            "id": "r1", "type": "CNAME", "name": "app.example.com", "content": "target.pages.dev", "proxied": False, "ttl": 1,
        }]
        dns = DnsConfig(zone_id="zone", name="app.example.com", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        api.update_dns_record.assert_not_called()

    def test_updates_record_name_found_by_content(self):
        api = Mock()
        api.list_dns_records.return_value = [{
            "id": "r1", "type": "CNAME", "name": "old.example.com", "content": "target.pages.dev", "proxied": False, "ttl": 1,
        }]
        api.update_dns_record.return_value = {"success": True}
        dns = DnsConfig(zone_id="zone", name="app.example.com", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        api.update_dns_record.assert_called_once_with("zone", "r1", {
            "type": "CNAME", "name": "app.example.com", "content": "target.pages.dev", "proxied": False, "ttl": 1,
        })

    def test_updates_record_type_found_by_content(self):
        api = Mock()
        api.list_dns_records.return_value = [{
            "id": "old", "type": "A", "name": "old.example.com", "content": "target.pages.dev",
        }]
        api.update_dns_record.return_value = {"success": True}
        dns = DnsConfig(zone_id="zone", name="app.example.com", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        assert api.method_calls == [
            call.list_dns_records("zone", "target.pages.dev"),
            call.update_dns_record("zone", "old", {
                "type": "CNAME", "name": "app.example.com", "content": "target.pages.dev", "proxied": False, "ttl": 1,
            }),
        ]


class TestDeployProjectDnsResult:
    def make_account(self) -> Account:
        dns = DnsConfig(token="dns-token", zone_id="zone", name="app.example.com", content="target.pages.dev")
        return Account(name="a", enabled=True, token="t", account_id="aid", pages=PagesConfig(project_name="p"), dns=dns)

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_dns_failure_fails_account_after_deployment(self, run_wrangler: Mock):
        api = Mock()
        api.create_project.return_value = {"success": True}
        api.list_dns_records.return_value = None

        assert deploy_project(api, self.make_account(), Path("source")) is False
        assert run_wrangler.call_count == 2

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", side_effect=RuntimeError("dns error"))
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_dns_exception_fails_account_after_deployment(self, run_wrangler: Mock, sync_dns: Mock):
        api = Mock()
        api.create_project.return_value = {"success": True}

        assert deploy_project(api, self.make_account(), Path("source")) is False
        assert run_wrangler.call_count == 2
        sync_dns.assert_called_once()


class TestDeployProjectKv:
    """KV 命名空间解析：查询优先、缺失时创建，不再盲目 POST 创建。"""

    def make_account(
        self,
        kv_create: bool = False,
        kv_namespace: str = "",
        kv_binding: bool = False,
        kv_binding_env: str = "",
        kv_configured: bool = False,
    ) -> Account:
        pages = PagesConfig(
            project_name="p",
            kv_create=kv_create,
            kv_namespace=kv_namespace,
            kv_binding=kv_binding,
            kv_binding_env=kv_binding_env,
            kv_configured=kv_configured,
            env=[],
        )
        return Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

    def make_api(self) -> Mock:
        api = Mock()
        api.create_project.return_value = {"success": True}
        api.get_project.return_value = {}
        api.patch_project_config.return_value = True
        api.last_error = None
        return api

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_kv_create_uses_ensure_and_does_not_post_create(self, run_wrangler: Mock):
        api = self.make_api()
        api.ensure_kv_namespace.return_value = "ns-1"
        account = self.make_account(
            kv_create=True, kv_namespace="ns", kv_binding=True, kv_binding_env="KV", kv_configured=True
        )

        assert deploy_project(api, account, Path("source")) is True
        api.ensure_kv_namespace.assert_called_once_with("ns")
        api.create_kv_namespace.assert_not_called()

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_binding_only_lookups_without_creating(self, run_wrangler: Mock):
        api = self.make_api()
        api.list_kv_namespaces.return_value = [{"id": "ns-2", "title": "ns"}]
        account = self.make_account(
            kv_create=False, kv_namespace="ns", kv_binding=True, kv_binding_env="KV", kv_configured=True
        )

        assert deploy_project(api, account, Path("source")) is True
        api.ensure_kv_namespace.assert_not_called()
        api.list_kv_namespaces.assert_called_once()
        api.create_kv_namespace.assert_not_called()

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_missing_namespace_id_fails_before_redeploy(self, run_wrangler: Mock):
        api = self.make_api()
        api.ensure_kv_namespace.return_value = None
        account = self.make_account(
            kv_create=True, kv_namespace="ns", kv_binding=True, kv_binding_env="KV", kv_configured=True
        )

        assert deploy_project(api, account, Path("source")) is False
        # 第三步配置失败即止：只发生过首次上传部署，没有重新部署
        assert run_wrangler.call_count == 1
