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


class TestConsoleEncoding:
    """Windows GBK 代码页下打印 wrangler 的 emoji 输出不应崩溃。"""

    def test_reconfigures_stdout_and_stderr(self, monkeypatch):
        calls: list[dict] = []

        class Fake:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(sys, "stdout", Fake())
        monkeypatch.setattr(sys, "stderr", Fake())

        main_mod._ensure_console_encoding()

        assert calls == [{"errors": "replace"}, {"errors": "replace"}]

    def test_tolerates_streams_without_reconfigure(self, monkeypatch):
        class Fake:
            pass

        monkeypatch.setattr(sys, "stdout", Fake())
        monkeypatch.setattr(sys, "stderr", Fake())

        main_mod._ensure_console_encoding()

    def test_swallows_reconfigure_failure(self, monkeypatch):
        class Fake:
            def reconfigure(self, **kwargs):
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(sys, "stdout", Fake())
        monkeypatch.setattr(sys, "stderr", Fake())

        main_mod._ensure_console_encoding()
