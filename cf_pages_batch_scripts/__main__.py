import argparse
from pathlib import Path

from .config import load_config
from .ui import main_menu, print_error, wait_enter
from .workflows import deploy_workflow, delete_workflow


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloudflare Pages 批量部署/删除工具")
    parser.add_argument("-c", "--config", type=Path, help="指定 YAML 配置文件路径")
    return parser.parse_args()


def main():
    """入口函数：加载配置，循环显示菜单直到用户退出。"""
    args = _parse_args()
    try:
        cfg = load_config(path=args.config)
    except FileNotFoundError as e:
        print_error(str(e))
        wait_enter()
        return

    while True:
        try:
            choice = main_menu()
            if choice == 0:
                break
            elif choice == 1:
                delete_workflow(cfg)
            elif choice == 2:
                deploy_workflow(cfg)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print_error(f"发生错误：{e}")
            import traceback
            traceback.print_exc()
            wait_enter()

    print("\n退出。")


if __name__ == "__main__":
    main()
