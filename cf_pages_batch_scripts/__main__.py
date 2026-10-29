import argparse
import contextlib
import sys
import traceback
from pathlib import Path

import yaml

from .config import load_config
from .ui import main_menu, print_error, wait_enter
from .workflows import delete_workflow, deploy_workflow


def _ensure_console_encoding() -> None:
    """让 stdout/stderr 用替换字符处理无法编码的字符。

    Windows 控制台默认 GBK 代码页，而 wrangler 输出含 emoji（如 ⛅）时，
    打印会抛 UnicodeEncodeError，进而把成功的部署误判为失败。
    这里保留原编码，只把无法表示的字符替换掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="replace")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloudflare Pages 批量部署/删除工具")
    parser.add_argument("-c", "--config", type=Path, help="指定 YAML 配置文件路径")
    return parser.parse_args()


def main() -> None:
    """入口函数：加载配置，循环显示菜单直到用户退出。"""
    _ensure_console_encoding()
    args = _parse_args()
    try:
        cfg = load_config(path=args.config)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print_error(str(e))
        wait_enter()
        return

    while True:
        try:
            choice = main_menu()
            if choice == 0:
                break
            elif choice == 1:
                deploy_workflow(cfg)
            elif choice == 2:
                delete_workflow(cfg)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print_error(f"发生错误：{e}")
            traceback.print_exc()
            wait_enter()

    print("\n退出。")


if __name__ == "__main__":
    main()
