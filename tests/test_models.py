"""Tests for cf_wrangler.models"""

from cf_wrangler.models import Config, Account, PagesConfig, EnvVar, FilesToRedeploy


class TestModels:
    """Verify data class construction and defaults."""

    def test_files_to_redeploy_defaults(self):
        fr = FilesToRedeploy()
        assert fr.dir == "files-to-redeploy"
        assert fr.download_url == ""

    def test_pages_config_defaults(self):
        pc = PagesConfig(project_name="test")
        assert pc.project_name == "test"
        assert pc.domain == ""
        assert pc.kv_create is False
        assert pc.kv_binding is False
        assert pc.kv_binding_env == "KV"
        assert pc.project_type == "production"

    def test_account_minimal(self):
        pc = PagesConfig(project_name="p")
        acct = Account(name="a", enabled=True, token="tok", account_id="aid", pages=pc)
        assert acct.name == "a"
        assert acct.enabled is True
        assert acct.token == "tok"
        assert acct.account_id == "aid"
        assert acct.env == []

    def test_env_var(self):
        ev = EnvVar(name="UUID", var_type="plain_text", value="abc")
        assert ev.name == "UUID"
        assert ev.var_type == "plain_text"
        assert ev.value == "abc"

    def test_config_empty(self):
        cfg = Config()
        assert cfg.files_to_redeploy.dir == "files-to-redeploy"
        assert cfg.accounts == []
