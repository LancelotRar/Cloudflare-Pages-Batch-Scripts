"""Interactive console UI built on Rich, plus selection input parsing."""

import contextlib

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from .models import Account

console = Console()


def print_header(text: str) -> None:
    """Display a centered panel header."""
    console.print(Panel.fit(text, border_style="cyan"))


def print_info(text: str) -> None:
    """Cyan info message."""
    console.print(f"[cyan][INFO][/]  {text}")


def print_ok(text: str) -> None:
    """Green success message."""
    console.print(f"[green][OK][/]    {text}")


def print_warn(text: str) -> None:
    """Yellow warning message."""
    console.print(f"[yellow][WARN][/]  {text}")


def print_error(text: str) -> None:
    """Red error message."""
    console.print(f"[red][ERROR][/] {text}")


def wait_enter() -> None:
    """Wait for user to press Enter."""
    console.print("\n[dim]按 Enter 返回菜单 ...[/]", end="")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input()


def main_menu() -> int:
    """显示主菜单。返回：0=退出, 1=批量部署, 2=批量删除"""
    console.clear()
    menu = Panel.fit(
        "[bold cyan]Cloudflare Pages Batch Scripts[/]\n\n"
        "  [bold]1.[/]  批量部署\n"
        "  [bold]2.[/]  批量删除\n"
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


def parse_selection(sel: str, items: list[dict]) -> list[dict]:
    """解析用户选择字符串，返回去重后的条目列表。

    支持单个序号（"1"）、逗号列表（"1,3,5"）、区间（"2-4"，可反写 "4-2"）
    以及 "a"/"A" 全选。无效或越界的条目会被静默跳过。
    """
    selected: list[dict] = []
    sel_lower = sel.strip().lower()

    if sel_lower == "a":
        return list(items)

    parts = [p.strip() for p in sel.split(",")]
    for part in parts:
        if not part:
            continue
        if "-" in part:
            try:
                start_str, end_str = part.split("-", 1)
                start, end = int(start_str.strip()), int(end_str.strip())
                lo, hi = (start, end) if start <= end else (end, start)
                selected.extend(
                    [item for item in items if lo <= item["index"] <= hi]
                )
            except (ValueError, IndexError):
                continue
        else:
            try:
                n = int(part)
                selected.extend([item for item in items if item["index"] == n])
            except ValueError:
                continue

    seen: set[int] = set()
    unique: list[dict] = []
    for item in selected:
        if item["index"] not in seen:
            seen.add(item["index"])
            unique.append(item)
    return unique


def select_accounts(accounts: list[Account]) -> list[Account]:
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
    console.print("[yellow]#[/] 输入序号（如 [bold]1[/]、[bold]1,3,5[/] 或 [bold]1-3[/]）选择单个或多个账号")
    console.print("[yellow][Q]uit[/] 退出\n")

    sel = Prompt.ask("请选择", default="q")
    if sel.strip().lower() == "q":
        return []

    items: list[dict] = [{"index": i, "account": acct} for i, acct in enumerate(accounts, 1)]
    selected = parse_selection(sel, items)
    if not selected:
        print_error("未选择有效账号")
        return []
    if len(selected) < len([p for p in sel.split(",") if p.strip()]):
        print_warn("部分序号无效或重复，已自动跳过")
    return [item["account"] for item in selected]
