from .config import load_config
from .ui import main_menu, print_error, wait_enter
from .workflows import deploy_workflow, delete_workflow


def main():
    """入口函数：加载配置，循环显示菜单直到用户退出。"""
    try:
        cfg = load_config()
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
