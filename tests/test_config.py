"""Tests for cf_wrangler.config"""

from pathlib import Path

import pytest
import yaml

from cf_wrangler.config import (
    _get_bool,
    _get_str,
    _parse_accounts,
    _parse_env_vars,
    _parse_files_to_redeploy,
    _parse_pages_config,
    get_enabled_accounts,
    load_config,
)
from cf_wrangler.models import Account, Config, EnvVar, PagesConfig


class TestGetStr:
    def test_existing(self):
        assert _get_str({"key": "val"}, "key") == "val"

    def test_default(self):
        assert _get_str({"key": "val"}, "other") == ""

    def test_custom_default(self):
        assert _get_str({}, "key", "fallback") == "fallback"

    def test_none_value(self):
        assert _get_str({"key": None}, "key") == ""

    def test_non_string(self):
        assert _get_str({"key": 42}, "key") == "42"


class TestGetBool:
    def test_true(self):
        assert _get_bool({"k": True}, "k") is True

    def test_false(self):
        assert _get_bool({"k": False}, "k") is False

    def test_default(self):
        assert _get_bool({}, "k") is False

    def test_string_true(self):
        assert _get_bool({"k": "true"}, "k") is True

    def test_none_value(self):
        assert _get_bool({"k": None}, "k") is False


class TestParseFilesToRedeploy:
    def test_empty(self):
        fr = _parse_files_to_redeploy({})
        assert fr.dir == "files-to-redeploy"
        assert fr.download_url == ""

    def test_full(self):
        fr = _parse_files_to_redeploy({
            "files_to_redeploy": {"dir": "my-dir", "download_url": "https://example.com/x.zip"}
        })
        assert fr.dir == "my-dir"
        assert fr.download_url == "https://example.com/x.zip"

    def test_none_raw(self):
        fr = _parse_files_to_redeploy(None)
        assert fr.dir == "files-to-redeploy"


class TestParseEnvVars:
    def test_empty(self):
        assert _parse_env_vars({}) == []

    def test_single(self):
        envs = _parse_env_vars({"env": [{"name": "K1", "type": "plain_text", "value": "v1"}]})
        assert len(envs) == 1
        assert envs[0].name == "K1"
        assert envs[0].var_type == "plain_text"
        assert envs[0].value == "v1"

    def test_multiple(self):
        raw = {"env": [
            {"name": "K1", "type": "plain_text", "value": "v1"},
            {"name": "K2", "type": "secret_text", "value": "s2"},
        ]}
        envs = _parse_env_vars(raw)
        assert len(envs) == 2
        assert envs[0].var_type == "plain_text"
        assert envs[1].var_type == "secret_text"


class TestParsePagesConfig:
    def test_minimal(self):
        pc = _parse_pages_config({"project_name": "p"})
        assert pc.project_name == "p"
        assert pc.project_type == "production"

    def test_full(self):
        pc = _parse_pages_config({
            "project_name": "p", "domain": "d.com",
            "kv_create": True, "kv_namespace": "ns",
            "kv_binding": True, "kv_binding_env": "KV",
            "project_type": "preview",
        })
        assert pc.domain == "d.com"
        assert pc.kv_create is True
        assert pc.project_type == "preview"


class TestParseAccounts:
    def test_no_accounts(self):
        assert _parse_accounts({}) == []

    def test_one_account(self):
        raw = {"accounts": [{
            "name": "acc1", "enabled": True, "token": "cfat_xxx",
            "account_id": "aid1", "pages": {"project_name": "p1"},
        }]}
        accts = _parse_accounts(raw)
        assert len(accts) == 1
        assert accts[0].name == "acc1"
        assert accts[0].token == "cfat_xxx"

    def test_none_raw(self):
        assert _parse_accounts(None) == []


class TestGetEnabledAccounts:
    def test_filters_disabled(self):
        pc = PagesConfig(project_name="p")
        a1 = Account(name="a1", enabled=True, token="t", account_id="aid", pages=pc)
        a2 = Account(name="a2", enabled=False, token="t", account_id="aid", pages=pc)
        cfg = Config(accounts=[a1, a2])
        assert len(get_enabled_accounts(cfg)) == 1

    def test_filters_incomplete(self):
        pc = PagesConfig(project_name="p")
        a1 = Account(name="a1", enabled=True, token="", account_id="aid", pages=pc)
        a2 = Account(name="a2", enabled=True, token="t", account_id="", pages=pc)
        a3 = Account(name="a3", enabled=True, token="t", account_id="aid", pages=PagesConfig(project_name=""))
        cfg = Config(accounts=[a1, a2, a3])
        assert get_enabled_accounts(cfg) == []

    def test_all_valid(self):
        pc = PagesConfig(project_name="p")
        a1 = Account(name="a1", enabled=True, token="t", account_id="aid", pages=pc)
        a2 = Account(name="a2", enabled=True, token="t2", account_id="aid2", pages=pc)
        cfg = Config(accounts=[a1, a2])
        assert len(get_enabled_accounts(cfg)) == 2


class TestLoadConfig:
    def test_load_from_file(self, tmp_path: Path):
        cfg_path = tmp_path / "config.yaml"
        data = {
            "files_to_redeploy": {"dir": "d", "download_url": "https://u"},
            "accounts": [{
                "name": "a", "enabled": True, "token": "tok", "account_id": "aid",
                "pages": {"project_name": "p"},
            }],
        }
        cfg_path.write_text(yaml.dump(data), encoding="utf-8")

        cfg = load_config(cfg_path)
        assert cfg.files_to_redeploy.dir == "d"
        assert len(cfg.accounts) == 1
        assert cfg.accounts[0].name == "a"

    def test_load_empty(self, tmp_path: Path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("", encoding="utf-8")

        cfg = load_config(cfg_path)
        assert cfg.files_to_redeploy.dir == "files-to-redeploy"
        assert cfg.accounts == []
