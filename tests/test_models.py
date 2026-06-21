"""Tests for cf_wrangler.models"""

from dataclasses import FrozenInstanceError

import pytest

from cf_wrangler.models import Account, Config, EnvVar, FilesToRedeploy, PagesConfig


class TestFilesToRedeploy:
    """Verify FilesToRedeploy defaults and construction."""

    def test_defaults(self):
        fr = FilesToRedeploy()
        assert fr.dir == "files-to-redeploy"
        assert fr.download_url == ""

    def test_custom_values(self):
        fr = FilesToRedeploy(dir="custom-dir", download_url="https://example.com/z.zip")
        assert fr.dir == "custom-dir"
        assert fr.download_url == "https://example.com/z.zip"

    def test_frozen(self):
        fr = FilesToRedeploy()
        with pytest.raises(FrozenInstanceError):
            fr.dir = "changed"


class TestPagesConfig:
    """Verify PagesConfig defaults and construction."""

    def test_defaults(self):
        pc = PagesConfig(project_name="test")
        assert pc.project_name == "test"
        assert pc.domain == ""
        assert pc.kv_create is False
        assert pc.kv_binding is False
        assert pc.kv_binding_env == "KV"
        assert pc.project_type == "production"

    def test_full_config(self):
        pc = PagesConfig(
            project_name="my-proj",
            domain="example.com",
            kv_create=True,
            kv_namespace="my-kv",
            kv_binding=True,
            kv_binding_env="KV",
            project_type="preview",
        )
        assert pc.project_name == "my-proj"
        assert pc.domain == "example.com"
        assert pc.kv_create is True
        assert pc.kv_binding is True
        assert pc.kv_binding_env == "KV"
        assert pc.project_type == "preview"

    def test_frozen(self):
        pc = PagesConfig(project_name="p")
        with pytest.raises(FrozenInstanceError):
            pc.domain = "new"

    def test_equal(self):
        a = PagesConfig(project_name="p", domain="d")
        b = PagesConfig(project_name="p", domain="d")
        assert a == b


class TestEnvVar:
    """Verify EnvVar construction."""

    def test_minimal(self):
        ev = EnvVar(name="UUID", var_type="plain_text", value="abc")
        assert ev.name == "UUID"
        assert ev.var_type == "plain_text"
        assert ev.value == "abc"

    def test_frozen(self):
        ev = EnvVar(name="K", var_type="secret_text", value="v")
        with pytest.raises(FrozenInstanceError):
            ev.value = "changed"


class TestAccount:
    """Verify Account construction."""

    def test_minimal(self):
        pc = PagesConfig(project_name="p")
        acct = Account(name="a", enabled=True, token="secret123", account_id="aid123", pages=pc)
        assert acct.name == "a"
        assert acct.enabled is True
        assert acct.token == "secret123"
        assert acct.account_id == "aid123"
        assert acct.pages == pc
        assert acct.env == []

    def test_with_env(self):
        pc = PagesConfig(project_name="p")
        ev = EnvVar(name="UUID", var_type="plain_text", value="abc")
        acct = Account(name="a", enabled=True, token="t", account_id="aid", pages=pc, env=[ev])
        assert len(acct.env) == 1
        assert acct.env[0].name == "UUID"

    def test_token_repr_hidden(self):
        pc = PagesConfig(project_name="p")
        acct = Account(name="a", enabled=True, token="mysecret", account_id="aid", pages=pc)
        assert "mysecret" not in repr(acct)

    def test_frozen(self):
        pc = PagesConfig(project_name="p")
        acct = Account(name="a", enabled=True, token="t", account_id="aid", pages=pc)
        with pytest.raises(FrozenInstanceError):
            acct.name = "new"


class TestConfig:
    """Verify Config construction."""

    def test_empty(self):
        cfg = Config()
        assert cfg.files_to_redeploy.dir == "files-to-redeploy"
        assert cfg.accounts == []

    def test_with_accounts(self):
        pc = PagesConfig(project_name="p")
        acct = Account(name="a", enabled=True, token="t", account_id="aid", pages=pc)
        cfg = Config(accounts=[acct])
        assert len(cfg.accounts) == 1
        assert cfg.accounts[0].name == "a"

    def test_frozen(self):
        cfg = Config()
        with pytest.raises(FrozenInstanceError):
            cfg.accounts = []
