from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt

console = Console()


def print_header(text: str):
    """Display a centered panel header."""
    console.print(Panel.fit(text, border_style="cyan"))


def print_info(text: str):
    """Cyan info message."""
    console.print(f"[cyan][INFO][/]  {text}")


def print_ok(text: str):
    """Green success message."""
    console.print(f"[green][OK][/]    {text}")


def print_warn(text: str):
    """Yellow warning message."""
    console.print(f"[yellow][WARN][/]  {text}")


def print_error(text: str):
    """Red error message."""
    console.print(f"[red][ERROR][/] {text}")


def wait_enter():
    """Wait for user to press Enter."""
    console.print("\n[dim]按 Enter 返回菜单 ...[/]", end="")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def confirm(prompt_text: str = "确认？") -> bool:
    """Ask for 'yes' confirmation. Returns True only if user types 'yes'."""
    response = Prompt.ask(prompt_text, default="no")
    return response.strip().lower() == "yes"


def main_menu() -> int:
    """显示主菜单。返回：0=退出, 1=批量删除, 2=批量部署"""
    console.clear()
    menu = Panel.fit(
        "[bold cyan]Cloudflare Pages Manager[/]\n\n"
        "  [bold]1.[/]  批量删除    查询 CF → 删除自定义域 + 项目 + KV\n"
        "  [bold]2.[/]  批量部署    创建/更新 Pages 项目并上传源码\n"
        "  [bold]Q.[/]  退出",
        border_style="cyan",
    )
    console.print(menu)

    while True:
        choice = Prompt.ask("请选择", default="q")
        if choice.lower() == "q":
            return 0
        elif choice == "1":
            return 1
        elif choice == "2":
            return 2
        else:
            print_warn("无效选择，请重新输入")


def select_accounts(accounts: list) -> list:
    """交互式多账号选择。返回选中的账号列表（空列表表示退出）。"""
    if not accounts:
        print_error("没有有效的账号")
        return []

    console.clear()
    table = Table(title="账号列表", border_style="yellow")
    table.add_column("#", style="bold")
    table.add_column("名称")
    table.add_column("项目")
    table.add_column("域名")

    for i, acct in enumerate(accounts, 1):
        domain = acct.pages.domain or ""
        table.add_row(str(i), acct.name, acct.pages.project_name, domain)

    console.print(table)
    console.print("\n[yellow][A]ll[/] 全部账号")
    console.print("[yellow][Q]uit[/] 退出\n")

    sel = Prompt.ask("请选择", default="q")
    if sel.lower() == "q":
        return []
    if sel.lower() == "a":
        return list(accounts)

    result = []
    for part in sel.split(","):
        part = part.strip()
        try:
            n = int(part) - 1
            if 0 <= n < len(accounts):
                result.append(accounts[n])
            else:
                print_warn(f"跳过无效序号：{part}")
        except ValueError:
            print_warn(f"跳过无效输入：{part}")

    if not result:
        print_error("未选择有效账号")
        return []
    return result
