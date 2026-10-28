"""Tests for cf_pages_batch_scripts.workflows"""

import io
import zipfile
from pathlib import Path
from unittest.mock import Mock, call, patch

import httpx

from cf_pages_batch_scripts.models import Account, Config, DnsConfig, EnvVar, FilesToRedeploy, PagesConfig
from cf_pages_batch_scripts.workflows import (
    DOWNLOAD_ATTEMPTS,
    _run_wrangler,
    _wrangler_error_hint,
    delete_workflow,
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
        cfg = Config(files_to_redeploy=FilesToRedeploy(dir=str(deploy_dir), download_url="https://example.com/src.zip"))

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
        cfg = Config(files_to_redeploy=FilesToRedeploy(dir=str(deploy_dir), download_url="https://example.com/src.zip"))

        with (
            patch(
                "cf_pages_batch_scripts.workflows.httpx.get",
                side_effect=httpx.ConnectError("boom"),
            ),
            patch("cf_pages_batch_scripts.workflows.time.sleep"),
        ):
            assert prepare_source(cfg) is None

        assert marker.exists()
        assert not (deploy_dir.parent / ".deploy.download.tmp").exists()

    def test_retries_transient_tls_error_until_success(self, tmp_path: Path):
        """线路偶发 TLS 握手中断（UNEXPECTED_EOF_WHILE_READING）应自动重试而非直接失败。"""
        cfg = Config(
            files_to_redeploy=FilesToRedeploy(dir=str(tmp_path / "deploy"), download_url="https://example.com/src.zip")
        )
        tls_error = httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")

        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content

            def raise_for_status(self) -> None:
                return None

        with (
            patch(
                "cf_pages_batch_scripts.workflows.httpx.get",
                side_effect=[tls_error, FakeResponse(self.make_zip_bytes())],
            ) as mock_get,
            patch("cf_pages_batch_scripts.workflows.time.sleep") as mock_sleep,
        ):
            src = prepare_source(cfg)

        assert src is not None
        assert (src / "index.js").exists()
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    def test_transient_errors_retry_up_to_limit(self, tmp_path: Path):
        deploy_dir = tmp_path / "deploy"
        prev = deploy_dir / "extracted" / "proj"
        prev.mkdir(parents=True)
        marker = prev / "keep.txt"
        marker.write_text("old", encoding="utf-8")
        cfg = Config(files_to_redeploy=FilesToRedeploy(dir=str(deploy_dir), download_url="https://example.com/src.zip"))

        with (
            patch(
                "cf_pages_batch_scripts.workflows.httpx.get",
                side_effect=httpx.ConnectTimeout("handshake timed out"),
            ) as mock_get,
            patch("cf_pages_batch_scripts.workflows.time.sleep"),
        ):
            assert prepare_source(cfg) is None

        assert mock_get.call_count == DOWNLOAD_ATTEMPTS
        assert marker.exists()
        assert not (deploy_dir.parent / ".deploy.download.tmp").exists()

    def test_http_status_error_is_not_retried(self, tmp_path: Path):
        """4xx/5xx 是确定性失败，重试无意义，立即返回。"""
        cfg = Config(
            files_to_redeploy=FilesToRedeploy(
                dir=str(tmp_path / "deploy"), download_url="https://example.com/missing.zip"
            )
        )
        request = httpx.Request("GET", "https://example.com/missing.zip")
        status_error = httpx.HTTPStatusError(
            "404 Not Found", request=request, response=httpx.Response(404, request=request)
        )

        class FakeResponse:
            def raise_for_status(self) -> None:
                raise status_error

        with (
            patch(
                "cf_pages_batch_scripts.workflows.httpx.get",
                return_value=FakeResponse(),
            ) as mock_get,
            patch("cf_pages_batch_scripts.workflows.time.sleep") as mock_sleep,
        ):
            assert prepare_source(cfg) is None

        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    def test_proxy_from_cfg_is_used_for_download(self, tmp_path: Path):
        """下载使用 cfg.proxy 作为代理。"""
        deploy_dir = tmp_path / "deploy"
        cfg = Config(
            files_to_redeploy=FilesToRedeploy(
                dir=str(deploy_dir),
                download_url="https://github.com/cmliu/edgetunnel/archive/refs/heads/main.zip",
            ),
            proxy="http://127.0.0.1:7890",
        )

        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content

            def raise_for_status(self) -> None:
                return None

        with patch(
            "cf_pages_batch_scripts.workflows.httpx.get",
            return_value=FakeResponse(self.make_zip_bytes()),
        ) as mock_get:
            src = prepare_source(cfg)

        assert src is not None
        assert mock_get.call_args.kwargs.get("proxy") == "http://127.0.0.1:7890"


class TestWranglerErrorHints:
    """wrangler 的英文报错应转成中文处置提示。"""

    def test_blocked_project_prints_chinese_hint(self, capsys):
        stderr = (
            "X [ERROR] A request to the Cloudflare API "
            "(/accounts/x/pages/projects/p/deployments) failed.\n\n"
            "  Your Pages project has been blocked. Contact abusereply@cloudflare.com. [code: 8000119]\n"
        )
        with (
            patch("cf_pages_batch_scripts.workflows._find_wrangler", return_value="wrangler"),
            patch(
                "cf_pages_batch_scripts.workflows.subprocess.run",
                return_value=Mock(returncode=1, stdout="", stderr=stderr),
            ),
        ):
            ok = _run_wrangler(Path("src"), "p", "t", "aid", "上传部署")

        assert ok is False
        out = capsys.readouterr().out
        assert "封禁" in out
        assert "abusereply@cloudflare.com" in out

    def test_command_pins_branch_and_marks_dirty(self):
        with (
            patch("cf_pages_batch_scripts.workflows._find_wrangler", return_value="wrangler"),
            patch(
                "cf_pages_batch_scripts.workflows.subprocess.run",
                return_value=Mock(returncode=0, stdout="", stderr=""),
            ) as run,
        ):
            assert _run_wrangler(Path("src"), "p", "t", "aid", "上传部署") is True

        cmd = run.call_args.args[0]
        assert cmd[:4] == ["wrangler", "pages", "deploy", "src"]
        assert cmd[cmd.index("--project-name") + 1] == "p"
        # 固定分支：同时让 wrangler 不具备自动委派成 Worker 的条件
        assert cmd[cmd.index("--branch") + 1] == "main"
        # 仓库有未提交改动时不打印 dirty 警告
        assert "--commit-dirty=true" in cmd

    def test_auth_error_hint(self):
        assert "认证失败" in (_wrangler_error_hint("Authentication error [code: 10000]") or "")

    def test_invalid_token_hint(self):
        assert "认证失败" in (_wrangler_error_hint("Invalid access token [code: 9109]") or "")

    def test_permission_error_hint(self):
        assert "权限不足" in (_wrangler_error_hint("Insufficient permissions to deploy") or "")

    def test_network_error_hint(self):
        assert "网络" in (_wrangler_error_hint("fetch failed: ECONNRESET") or "")

    def test_unknown_error_has_no_hint(self):
        """未识别的报错保持原样，不臆造处置建议。"""
        assert _wrangler_error_hint("X [ERROR] some unexpected failure") is None


class TestSetProjectConfig:
    def test_env_whitelist_deletes_extra_variables(self):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {"KEEP": {}, "OLD": {}}}}}
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="new")],
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        api.patch_project_config.assert_called_once_with(
            "p",
            {
                "production": {
                    "env_vars": {
                        "OLD": None,
                        "KEEP": {"value": "new", "type": "plain_text"},
                    }
                }
            },
        )

    def test_kv_create_off_does_not_touch_bindings(self):
        """kv_create=false 是总开关：即使配置了 kv_namespace 也完全不生成 kv_namespaces 补丁。"""
        api = Mock()
        api.get_project.return_value = {
            "deployment_configs": {
                "production": {
                    "env_vars": {"KEEP": {}},
                    "kv_namespaces": {"OLD": {"namespace_id": "old"}},
                }
            }
        }
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="v")],
            kv_create=False,
            kv_namespace="ns",
            kv_binding=True,
            kv_binding_env="KV",
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        patched = api.patch_project_config.call_args.args[1]
        assert "kv_namespaces" not in patched["production"]

    def test_kv_binding_converges_to_target(self):
        api = Mock()
        api.get_project.return_value = {
            "deployment_configs": {"production": {"kv_namespaces": {"OLD": {"namespace_id": "old"}}}}
        }
        api.patch_project_config.return_value = True
        pages = PagesConfig(project_name="p", kv_create=True, kv_binding=True, kv_binding_env="KV")
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account, ns_id="ns-9") is True
        api.patch_project_config.assert_called_once_with(
            "p", {"production": {"kv_namespaces": {"OLD": None, "KV": {"namespace_id": "ns-9"}}}}
        )

    def test_null_env_vars_and_kv_namespaces_do_not_crash(self):
        """CF API 对新建项目返回 env_vars/kv_namespaces 为 null 而非 {}，不应触发迭代异常。"""
        api = Mock()
        api.get_project.return_value = {
            "deployment_configs": {
                "production": {
                    "env_vars": None,
                    "kv_namespaces": None,
                }
            }
        }
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="v")],
            kv_create=True,
            kv_namespace="ns",
            kv_binding=True,
            kv_binding_env="KV",
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account, ns_id="nsid") is True
        api.patch_project_config.assert_called_once_with(
            "p",
            {
                "production": {
                    "env_vars": {"KEEP": {"value": "v", "type": "plain_text"}},
                    "kv_namespaces": {"KV": {"namespace_id": "nsid"}},
                }
            },
        )

    def test_kv_create_on_without_binding_env_fails(self):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {}}
        pages = PagesConfig(project_name="p", kv_create=True, kv_namespace="ns")
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account, ns_id="ns-9") is False
        api.patch_project_config.assert_not_called()

    def test_empty_managed_fields_skip_project_query(self):
        api = Mock()
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=PagesConfig(project_name="p"))

        assert set_project_config(api, account) is True
        api.get_project.assert_not_called()

    def test_env_detail_lists_name_value_and_type(self, capsys):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {}}}}
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[
                EnvVar(name="UUID", var_type="plain_text", value="uuid-value"),
                EnvVar(name="ADMIN", var_type="secret_text", value="admin-value"),
            ],
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        out = capsys.readouterr().out
        assert "环境变量（受管理白名单，共 2 个）" in out
        assert "目标环境：production" in out
        assert "UUID = uuid-value [plain_text]" in out
        assert "ADMIN = admin-value [secret_text]" in out
        assert "项目配置已提交：环境变量 2 个" in out

    def test_unmanaged_env_removal_warned(self, capsys):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {"KEEP": {}, "OLD": {}}}}}
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="v")],
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        out = capsys.readouterr().out
        assert "将删除项目上未声明的变量：OLD" in out
        assert "KEEP" in out

    def test_empty_env_value_warned_and_not_listed(self, capsys):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {}}}}
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[
                EnvVar(name="UUID", var_type="plain_text", value="uuid-value"),
                EnvVar(name="EMPTY", var_type="plain_text", value=""),
            ],
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        out = capsys.readouterr().out
        assert "变量 EMPTY 的值为空" in out
        assert "EMPTY = " not in out
        assert "共 1 个" in out

    def test_kv_binding_detail_printed(self, capsys):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"kv_namespaces": {}}}}
        api.patch_project_config.return_value = True
        pages = PagesConfig(project_name="p", kv_create=True, kv_binding=True, kv_binding_env="KV")
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account, ns_id="ns-9") is True
        out = capsys.readouterr().out
        assert "KV 绑定：KV -> ns-9" in out
        assert "KV 绑定 1 个" in out

    def test_kv_off_prints_no_binding_line(self, capsys):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {}}}}
        api.patch_project_config.return_value = True
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="v")],
            kv_create=False,
            kv_namespace="ns",
            kv_binding=True,
            kv_binding_env="KV",
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is True
        assert "KV 绑定" not in capsys.readouterr().out

    def test_patch_failure_reports_api_reason(self, capsys):
        api = Mock()
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {}}}}
        api.patch_project_config.return_value = False
        api.last_error = "[10000]: Authentication error"
        pages = PagesConfig(
            project_name="p",
            env=[EnvVar(name="KEEP", var_type="plain_text", value="v")],
        )
        account = Account(name="a", enabled=True, token="t", account_id="aid", pages=pages)

        assert set_project_config(api, account) is False
        assert "项目配置已提交" not in capsys.readouterr().out


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
    def make_account(self, dns: DnsConfig, domain: str = "app.example.com") -> Account:
        return Account(
            name="a",
            enabled=True,
            token="t",
            account_id="aid",
            pages=PagesConfig(project_name="p", domain=domain),
            dns=dns,
        )

    def test_derives_content_from_project_name(self):
        """省略 dns.content 时按 <project_name>.pages.dev 拼接目标域名。"""
        api = Mock()
        api.list_dns_records.return_value = []
        api.create_dns_record.return_value = {"success": True}

        assert sync_dns_record(api, self.make_account(DnsConfig(zone_id="zone"))) is True
        api.list_dns_records.assert_called_once_with("zone", "p.pages.dev")
        api.create_dns_record.assert_called_once_with(
            "zone",
            {
                "type": "CNAME",
                "name": "app.example.com",
                "content": "p.pages.dev",
                "proxied": False,
                "ttl": 1,
            },
        )

    def test_explicit_content_overrides_derived(self):
        api = Mock()
        api.list_dns_records.return_value = []
        api.create_dns_record.return_value = {"success": True}

        assert sync_dns_record(api, self.make_account(DnsConfig(zone_id="zone", content="custom.pages.dev"))) is True
        api.list_dns_records.assert_called_once_with("zone", "custom.pages.dev")

    def test_creates_missing_record(self):
        api = Mock()
        api.list_dns_records.return_value = []
        api.create_dns_record.return_value = {"success": True}
        dns = DnsConfig(zone_id="zone", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        api.create_dns_record.assert_called_once_with(
            "zone",
            {
                "type": "CNAME",
                "name": "app.example.com",
                "content": "target.pages.dev",
                "proxied": False,
                "ttl": 1,
            },
        )

    def test_skips_matching_record(self):
        api = Mock()
        api.list_dns_records.return_value = [
            {
                "id": "r1",
                "type": "CNAME",
                "name": "app.example.com",
                "content": "target.pages.dev",
                "proxied": False,
                "ttl": 1,
            }
        ]
        dns = DnsConfig(zone_id="zone", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        api.update_dns_record.assert_not_called()

    def test_updates_record_name_found_by_content(self):
        api = Mock()
        api.list_dns_records.return_value = [
            {
                "id": "r1",
                "type": "CNAME",
                "name": "old.example.com",
                "content": "target.pages.dev",
                "proxied": False,
                "ttl": 1,
            }
        ]
        api.update_dns_record.return_value = {"success": True}
        dns = DnsConfig(zone_id="zone", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        api.update_dns_record.assert_called_once_with(
            "zone",
            "r1",
            {
                "type": "CNAME",
                "name": "app.example.com",
                "content": "target.pages.dev",
                "proxied": False,
                "ttl": 1,
            },
        )

    def test_updates_record_type_found_by_content(self):
        api = Mock()
        api.list_dns_records.return_value = [
            {
                "id": "old",
                "type": "A",
                "name": "old.example.com",
                "content": "target.pages.dev",
            }
        ]
        api.update_dns_record.return_value = {"success": True}
        dns = DnsConfig(zone_id="zone", content="target.pages.dev")

        assert sync_dns_record(api, self.make_account(dns)) is True
        assert api.method_calls == [
            call.list_dns_records("zone", "target.pages.dev"),
            call.update_dns_record(
                "zone",
                "old",
                {
                    "type": "CNAME",
                    "name": "app.example.com",
                    "content": "target.pages.dev",
                    "proxied": False,
                    "ttl": 1,
                },
            ),
        ]


class TestDeployProjectDnsResult:
    def make_account(self) -> Account:
        dns = DnsConfig(token="dns-token", zone_id="zone", content="target.pages.dev")
        return Account(
            name="a",
            enabled=True,
            token="t",
            account_id="aid",
            pages=PagesConfig(project_name="p", domain="app.example.com"),
            dns=dns,
        )

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", return_value=True)
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_dns_sync_runs_when_content_omitted(self, run_wrangler: Mock, sync_dns: Mock):
        """只配 zone_id、不写 content 时仍应同步 DNS，目标域名自动拼接。"""
        account = Account(
            name="a",
            enabled=True,
            token="t",
            account_id="aid",
            pages=PagesConfig(project_name="p", domain="app.example.com"),
            dns=DnsConfig(token="dns-token", zone_id="zone"),
        )
        api = Mock()
        api.create_project.return_value = {"success": True}
        api.list_domains.return_value = []
        api.add_domain.return_value = {"success": True}

        assert deploy_project(api, account, Path("source")) is True
        sync_dns.assert_called_once()

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_dns_failure_fails_account_before_deployment(self, run_wrangler: Mock):
        api = Mock()
        api.create_project.return_value = {"success": True}
        api.list_domains.return_value = []
        api.add_domain.return_value = {"success": True}
        api.list_dns_records.return_value = None

        assert deploy_project(api, self.make_account(), Path("source")) is False
        # DNS 未收敛就不消耗那一次部署机会
        run_wrangler.assert_not_called()

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", side_effect=RuntimeError("dns error"))
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_dns_exception_fails_account_before_deployment(self, run_wrangler: Mock, sync_dns: Mock):
        api = Mock()
        api.create_project.return_value = {"success": True}
        api.list_domains.return_value = []
        api.add_domain.return_value = {"success": True}

        assert deploy_project(api, self.make_account(), Path("source")) is False
        run_wrangler.assert_not_called()
        sync_dns.assert_called_once()


class TestDeployProjectSingleDeploy:
    """配置先于部署：一次 wrangler 部署即携带环境变量 / KV 绑定 / 自定义域名。"""

    def make_account(self) -> Account:
        return Account(
            name="a",
            enabled=True,
            token="t",
            account_id="aid",
            pages=PagesConfig(
                project_name="p",
                domain="app.example.com",
                env=[EnvVar(name="ADMIN", var_type="plain_text", value="secret")],
            ),
            dns=DnsConfig(token="dns-token", zone_id="zone", content="target.pages.dev"),
        )

    def make_api(self) -> Mock:
        api = Mock()
        api.create_project.return_value = {"success": True}
        api.get_project.return_value = {"deployment_configs": {"production": {"env_vars": {}}}}
        api.patch_project_config.return_value = True
        api.list_domains.return_value = []
        api.add_domain.return_value = {"success": True}
        api.list_dns_records.return_value = []
        api.create_dns_record.return_value = {"success": True}
        return api

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", return_value=True)
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_deploys_exactly_once(self, run_wrangler: Mock, sync_dns: Mock):
        assert deploy_project(self.make_api(), self.make_account(), Path("source")) is True
        assert run_wrangler.call_count == 1

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", return_value=True)
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_final_summary_lists_carried_config(self, run_wrangler: Mock, sync_dns: Mock, capsys):
        assert deploy_project(self.make_api(), self.make_account(), Path("source")) is True
        out = capsys.readouterr().out
        assert "已一次部署完成" in out
        assert "域名 app.example.com" in out
        assert "环境变量 1 个" in out

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", return_value=True)
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_project_config_applied_before_deploy(self, run_wrangler: Mock, sync_dns: Mock):
        api = self.make_api()
        order: list[str] = []
        api.patch_project_config.side_effect = lambda *a, **k: order.append("config") or True
        run_wrangler.side_effect = lambda *a, **k: order.append("deploy") or True

        assert deploy_project(api, self.make_account(), Path("source")) is True
        assert order == ["config", "deploy"]

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_dns_created_before_deploy(self, run_wrangler: Mock):
        api = self.make_api()
        order: list[str] = []

        def record_dns(*args, **kwargs):
            order.append("dns")
            return True

        run_wrangler.side_effect = lambda *a, **k: order.append("deploy") or True

        with patch("cf_pages_batch_scripts.workflows.sync_dns_record", side_effect=record_dns):
            assert deploy_project(api, self.make_account(), Path("source")) is True
        assert order == ["dns", "deploy"]

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", return_value=True)
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_config_failure_skips_deploy(self, run_wrangler: Mock, sync_dns: Mock):
        api = self.make_api()
        api.patch_project_config.return_value = False

        assert deploy_project(api, self.make_account(), Path("source")) is False
        run_wrangler.assert_not_called()

    @patch("cf_pages_batch_scripts.workflows.sync_dns_record", return_value=True)
    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=False)
    def test_deploy_failure_fails_account(self, run_wrangler: Mock, sync_dns: Mock):
        assert deploy_project(self.make_api(), self.make_account(), Path("source")) is False
        assert run_wrangler.call_count == 1


class TestDeployProjectKv:
    """kv_create 是 KV 总开关：false 完全不操作；true 要求绑定配置完整。"""

    def make_account(
        self,
        kv_create: bool = False,
        kv_namespace: str = "",
        kv_binding: bool = False,
        kv_binding_env: str = "",
    ) -> Account:
        pages = PagesConfig(
            project_name="p",
            kv_create=kv_create,
            kv_namespace=kv_namespace,
            kv_binding=kv_binding,
            kv_binding_env=kv_binding_env,
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
    def test_kv_create_uses_ensure_and_does_not_post_create(self, run_wrangler: Mock, capsys):
        api = self.make_api()
        api.ensure_kv_namespace.return_value = ("ns-1", True)
        account = self.make_account(kv_create=True, kv_namespace="ns", kv_binding=True, kv_binding_env="KV")

        assert deploy_project(api, account, Path("source")) is True
        api.ensure_kv_namespace.assert_called_once_with("ns")
        api.create_kv_namespace.assert_not_called()
        # 成功路径必须有可见提示（曾因下沉 API 层而静默，属回归）
        assert "KV 命名空间 'ns' 已创建" in capsys.readouterr().out

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_kv_create_off_skips_all_kv_calls(self, run_wrangler: Mock, capsys):
        api = self.make_api()
        account = self.make_account(kv_create=False, kv_namespace="ns", kv_binding=True, kv_binding_env="KV")

        assert deploy_project(api, account, Path("source")) is True
        api.ensure_kv_namespace.assert_not_called()
        api.list_kv_namespaces.assert_not_called()
        api.create_kv_namespace.assert_not_called()
        assert "KV 命名空间" not in capsys.readouterr().out

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_kv_create_on_without_binding_config_fails(self, run_wrangler: Mock, capsys):
        api = self.make_api()
        account = self.make_account(kv_create=True, kv_namespace="ns", kv_binding=False, kv_binding_env="")

        assert deploy_project(api, account, Path("source")) is False
        api.ensure_kv_namespace.assert_not_called()
        # 配置不完整在部署前即失败：一次部署都不该发生
        assert run_wrangler.call_count == 0
        assert "未配置 kv_binding / kv_binding_env" in capsys.readouterr().out

    @patch("cf_pages_batch_scripts.workflows._run_wrangler", return_value=True)
    def test_missing_namespace_id_fails_before_redeploy(self, run_wrangler: Mock):
        api = self.make_api()
        api.ensure_kv_namespace.return_value = (None, False)
        account = self.make_account(kv_create=True, kv_namespace="ns", kv_binding=True, kv_binding_env="KV")

        assert deploy_project(api, account, Path("source")) is False
        # 配置失败即止：一次部署都不该发生
        assert run_wrangler.call_count == 0


class _FakeCtx:
    """把 Mock 包装成上下文管理器，模拟 CfApiClient。"""

    def __init__(self, api: Mock) -> None:
        self.api = api

    def __enter__(self) -> Mock:
        return self.api

    def __exit__(self, *exc: object) -> bool:
        return False


class TestDeleteWorkflowLiveQuery:
    """批量删除实时查询账号下全部资源，不依赖配置里的项目名 / 域名 / KV 名。"""

    def make_account(self, zone_id: str = "zone", **pages_kwargs) -> Account:
        return Account(
            name="a",
            enabled=True,
            token="t",
            account_id="aid",
            pages=PagesConfig(project_name="configured-p", **pages_kwargs),
            dns=DnsConfig(token="dns-token", zone_id=zone_id),
        )

    def make_api(
        self,
        projects: list[dict] | None = None,
        kv: list[dict] | None = None,
        dns: list[dict] | None = None,
    ) -> Mock:
        api = Mock()
        api.list_projects.return_value = projects if projects is not None else [{"name": "p1"}, {"name": "p2"}]
        api.list_domains.side_effect = lambda name: {
            "p1": [{"name": "d1.example.com"}],
            "p2": [],
        }.get(name, [])
        api.list_kv_namespaces.return_value = kv if kv is not None else [{"id": "ns1", "title": "ns1"}]
        api.list_zone_dns_records.return_value = (
            dns
            if dns is not None
            else [
                {"id": "r1", "name": "d1.example.com", "type": "CNAME", "content": "p1.pages.dev"},
                {"id": "r2", "name": "d2.example.com", "type": "CNAME", "content": "p2.pages.dev"},
                {"id": "r3", "name": "example.com", "type": "MX", "content": "mx.example.net"},
                {"id": "r4", "name": "www.example.com", "type": "CNAME", "content": "example.net"},
                # 同 Zone 其他账号的项目，必须保留
                {"id": "r5", "name": "o.example.com", "type": "CNAME", "content": "other-account.pages.dev"},
                # 记录值指向 p1，但记录名不是 p1 绑定的自定义域，也必须保留
                {"id": "r6", "name": "d3.example.com", "type": "CNAME", "content": "p1.pages.dev"},
            ]
        )
        api.delete_domain.return_value = {"success": True}
        api.delete_project.return_value = {"success": True}
        api.delete_kv_namespace.return_value = {"success": True}
        api.delete_dns_record.return_value = {"success": True}
        return api

    def run_delete(
        self,
        accounts: list[Account],
        apis: dict[str, Mock],
        dns_apis: dict[str, Mock] | None = None,
        confirm: str = "yes",
    ) -> None:
        def factory(account_id: str, token: str, **kwargs: object) -> _FakeCtx:
            if dns_apis and token in dns_apis:
                return _FakeCtx(dns_apis[token])
            return _FakeCtx(apis[token])

        with (
            patch("cf_pages_batch_scripts.workflows.CfApiClient") as client_cls,
            patch("cf_pages_batch_scripts.workflows.select_accounts", return_value=accounts),
            patch("cf_pages_batch_scripts.workflows.wait_enter"),
            patch("builtins.input", return_value=confirm),
        ):
            client_cls.side_effect = factory
            delete_workflow(Config(accounts=accounts))

    def test_deletes_all_projects_from_live_query(self):
        api = self.make_api()
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        assert [c.args[0] for c in api.delete_project.call_args_list] == ["p1", "p2"]
        assert "configured-p" not in [c.args[0] for c in api.delete_project.call_args_list]

    def test_deletes_all_domains_before_their_project(self):
        api = self.make_api()
        order: list[tuple] = []
        api.delete_domain.side_effect = lambda project, domain: (
            order.append(("domain", project, domain)) or {"success": True}
        )
        api.delete_project.side_effect = lambda project: order.append(("project", project)) or {"success": True}

        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        assert order[:2] == [("domain", "p1", "d1.example.com"), ("project", "p1")]
        assert ("project", "p2") in order

    def test_deletes_all_kv_namespaces_ignoring_kv_create(self):
        api = self.make_api(kv=[{"id": "ns1", "title": "ns1"}, {"id": "ns2", "title": "ns2"}])
        self.run_delete([self.make_account(kv_create=False, kv_namespace="")], {"t": api}, {"dns-token": api})

        assert [c.args[0] for c in api.delete_kv_namespace.call_args_list] == ["ns1", "ns2"]

    def test_deletes_only_dns_records_owned_by_this_account(self):
        api = self.make_api()
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        deleted = [c.args for c in api.delete_dns_record.call_args_list]
        # 只有「记录名 = p1 已绑定域名」且「记录值 = p1.pages.dev」的 CNAME 才删
        assert deleted == [("zone", "r1")]

    def test_other_accounts_pages_dev_records_kept(self, capsys):
        api = self.make_api()
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        assert "已保留" in capsys.readouterr().out

    def test_dns_uses_dns_token_client(self):
        api = self.make_api()
        dns_api = self.make_api()
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": dns_api})

        # 查询与删除都走 dns_token 的客户端
        dns_api.list_zone_dns_records.assert_called_once_with("zone")
        assert dns_api.delete_dns_record.call_count == 1
        api.delete_dns_record.assert_not_called()

    def test_confirmation_rejected_deletes_nothing(self):
        api = self.make_api()
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api}, confirm="no")

        api.delete_project.assert_not_called()
        api.delete_domain.assert_not_called()
        api.delete_kv_namespace.assert_not_called()
        api.delete_dns_record.assert_not_called()

    def test_query_failure_skips_deletion(self, capsys):
        api = self.make_api()
        api.list_projects.return_value = None
        api.last_error = "Cloudflare API 500"
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        api.delete_project.assert_not_called()
        out = capsys.readouterr().out
        assert "失败账号：a" in out

    def test_delete_failure_marks_account_failed(self, capsys):
        api = self.make_api()
        api.delete_project.return_value = {"success": False}
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        assert "失败账号：a" in capsys.readouterr().out

    def test_missing_zone_id_skips_dns_query(self, capsys):
        api = self.make_api()
        self.run_delete([self.make_account(zone_id="")], {"t": api})

        api.list_zone_dns_records.assert_not_called()
        api.delete_dns_record.assert_not_called()
        assert "未配置 dns.zone_id" in capsys.readouterr().out

    def test_scope_warning_and_extra_projects_flagged(self, capsys):
        api = self.make_api()
        self.run_delete([self.make_account()], {"t": api}, {"dns-token": api})

        out = capsys.readouterr().out
        # 明确提示删除范围是账号下全部资源
        assert "账号下全部资源" in out
        assert "不限于配置文件" in out
        # 配置里只有 configured-p，p1 / p2 都不是配置项目
        assert "非配置文件中的项目" in out

    def test_extra_kv_namespaces_flagged(self, capsys):
        api = self.make_api(kv=[{"id": "ns1", "title": "other"}])
        self.run_delete([self.make_account(kv_namespace="mine")], {"t": api}, {"dns-token": api})

        out = capsys.readouterr().out
        assert "非配置文件中的 KV 命名空间" in out


class TestDeleteWorkflowResilience:
    """单个账号异常不应中断其余账号的删除。"""

    def test_account_exception_does_not_abort_remaining(self, capsys):
        acc1 = Account(
            name="a1",
            enabled=True,
            token="t1",
            account_id="aid",
            pages=PagesConfig(project_name="p1"),
        )
        acc2 = Account(
            name="a2",
            enabled=True,
            token="t2",
            account_id="aid",
            pages=PagesConfig(project_name="p2"),
        )

        def make_api(project: str, boom: bool = False) -> Mock:
            api = Mock()
            api.list_projects.return_value = [{"name": project}]
            api.list_domains.return_value = []
            api.list_kv_namespaces.return_value = []
            api.delete_project.return_value = {"success": True}
            if boom:
                api.delete_project.side_effect = RuntimeError("boom")
            return api

        api1 = make_api("p1", boom=True)
        api2 = make_api("p2")

        with (
            patch("cf_pages_batch_scripts.workflows.CfApiClient") as client_cls,
            patch("cf_pages_batch_scripts.workflows.select_accounts", return_value=[acc1, acc2]),
            patch("cf_pages_batch_scripts.workflows.wait_enter"),
            patch("builtins.input", return_value="yes"),
        ):
            client_cls.side_effect = lambda account_id, token, **kw: _FakeCtx({"t1": api1, "t2": api2}[token])
            delete_workflow(Config(accounts=[acc1, acc2]))

        api2.delete_project.assert_called_once_with("p2")
        out = capsys.readouterr().out
        assert "执行异常" in out
        assert "失败账号：a1" in out
