"""Tests for cf_pages_batch_scripts.__main__"""

import sys

import pytest
import yaml

from cf_pages_batch_scripts import __main__ as main_mod


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("no config"),
        ValueError("bad config"),
        yaml.YAMLError("bad yaml"),
    ],
    ids=["file-not-found", "value-error", "yaml-error"],
)
def test_config_load_error_reported_cleanly(error: Exception, capsys: pytest.CaptureFixture, monkeypatch):
    """配置加载失败应打印可读错误并退出，而不是抛裸 traceback。"""

    def _boom(path=None):
        raise error

    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.setattr(main_mod, "load_config", _boom)
    monkeypatch.setattr(main_mod, "wait_enter", lambda: None)

    main_mod.main()

    assert str(error) in capsys.readouterr().out
