"""
ui/display.py
=============
Rich-based terminal UI.

Visual layout (rendered sequentially, not side-by-side):

  ══════════════════════ ⚠  Error Detected ══════════════════════
  ┌─ Found Error ──────────────────────────────────────────────┐
  │  File:       /var/log/syslog                               │
  │  Timestamp:  2025-06-01 14:22:03                           │
  │                                                            │
  │  Error line:                                               │
  │    Cannot execute deploy.sh, permission denied             │
  └────────────────────────────────────────────────────────────┘
  [1] Confirm (start analysis)   [2] Ignore
  ›

  ════════════════════════ 🔍 Diagnostics ════════════════════════
  ┌─ Diagnostics ──────────────────────────────────────────────┐
  │  # │ Command               │ Description                   │
  │  1 │ ls -la /path/to/…     │ Check file permissions        │
  │  2 │ id -u                 │ Check current user UID        │
  └────────────────────────────────────────────────────────────┘
  [1] Allow execution   [2] Deny
  ›

  ═══════════════════════ 🛠  Solution ═══════════════════════════
  ┌─ Fix Commands ─────────────────────────────────────────────┐
  │  # │ Command           │ Description        │ Risk         │
  │  1 │ chmod +x /…       │ Grant execute perm │ ⚠  warning  │
  └────────────────────────────────────────────────────────────┘
  [1] Apply fix   [2] Skip
  ›
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ── Criticality styling ───────────────────────────────────────────────────────
_COLOR: dict[str, str] = {
    "safe": "bright_green",
    "warning": "yellow",
    "danger": "bright_red",
}
_ICON: dict[str, str] = {
    "safe": "✅",
    "warning": "⚠ ",
    "danger": "🔴",
}


# ─────────────────────────────────────────────────────────────────────────────
# Shared prompt helper
# ─────────────────────────────────────────────────────────────────────────────

def _ask(
    console: Console,
    label: str,
    option_a: str,
    option_b: str,
    key_a: str = "1",
    key_b: str = "2",
    default: str = "2",
) -> bool:
    """
    Display two labelled options and read a single-key choice.

    Returns True if the user chose *key_a*, False for *key_b*.
    Pressing Enter without input uses *default*.
    """
    console.print()
    # Show the two options with colour-coded keys
    console.print(
        f"  [bold]{label}:[/bold]  "
        f"[[bold green]{key_a}[/bold green]] {option_a}"
        f"    "
        f"[[bold {'yellow' if default == key_b else 'red'}]{key_b}[/bold {'yellow' if default == key_b else 'red'}]] {option_b}"
        f"  [dim](Enter = {option_b if default == key_b else option_a})[/dim]"
    )
    chosen = Prompt.ask(
        "  [bold dim]›[/bold dim]",
        choices=[key_a, key_b],
        default=default,
        show_choices=False,
        show_default=False,
        console=console,
    )
    return chosen == key_a


# ─────────────────────────────────────────────────────────────────────────────
# Display class
# ─────────────────────────────────────────────────────────────────────────────

class Display:
    """
    Manages all terminal output.  Every public method corresponds to a
    distinct visual step in the Orchestrator pipeline.
    """

    def __init__(self) -> None:
        self._c = Console(highlight=False)

    # ── Generic helpers ───────────────────────────────────────────────

    def info(self, message: str) -> None:
        """Dim informational line."""
        self._c.print(f"  [dim]{message}[/dim]")

    def error(self, message: str) -> None:
        """Bold red error notice."""
        self._c.print(f"\n  [bold red]✗  {message}[/bold red]\n")

    def thinking(self, message: str) -> None:
        """Cyan 'agent is working' indicator."""
        self._c.print(f"\n  [cyan]⟳  {message}[/cyan]\n")

    # ── Lifecycle ─────────────────────────────────────────────────────

    def show_startup(self, paths: list[str]) -> None:
        """Splash panel shown on startup."""
        self._c.print()
        self._c.print(Rule("[bold blue]Multi-Agent Log Monitor[/bold blue]"))

        body = Text()
        body.append("Monitoring the following paths:\n\n", style="bold")
        for p in paths:
            body.append(f"  • {p}\n", style="cyan")
        body.append("\nPress [bold]Ctrl+C[/bold] to stop.", style="dim")

        self._c.print(
            Panel(body, border_style="blue", padding=(1, 2))
        )
        self._c.print()

    def show_goodbye(self) -> None:
        """Exit message."""
        self._c.print()
        self._c.print(Rule("[bold blue]Monitor stopped. Goodbye.[/bold blue]"))
        self._c.print()

    # ── Block 1: Error ────────────────────────────────────────────────

    def show_error_panel(self, event) -> bool:
        """
        Display the detected error and ask: Confirm or Ignore.

        Returns True (Confirm) or False (Ignore).
        """
        self._c.print()
        self._c.print(Rule("[bold red]⚠   Error Detected[/bold red]"))

        body = Text()
        body.append("File:       ", style="bold dim")
        body.append(event.filepath + "\n", style="cyan")
        body.append("Timestamp:  ", style="bold dim")
        body.append(event.timestamp + "\n\n", style="dim")
        body.append("Error line:\n", style="bold")
        body.append(f"  {event.line}", style="bold red")

        self._c.print(
            Panel(
                body,
                title="[bold red]Found Error[/bold red]",
                border_style="red",
                padding=(1, 2),
            )
        )

        return _ask(
            self._c,
            label="Action",
            option_a="Confirm (start analysis)",
            option_b="Ignore",
            default="2",
        )

    # ── Block 2: Diagnostics ──────────────────────────────────────────

    def show_diagnostic_panel(
        self, commands: list[dict[str, str]], title: str = "Diagnostics"
    ) -> bool:
        """
        Display proposed diagnostic commands and ask: Allow or Deny.

        Returns True (Allow) or False (Deny).
        """
        self._c.print()
        self._c.print(Rule(f"[bold yellow]🔍   {title}[/bold yellow]"))

        table = Table(
            show_header=True,
            header_style="bold yellow",
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False,
        )
        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description", style="white")

        for idx, cmd in enumerate(commands, start=1):
            table.add_row(
                str(idx),
                cmd.get("command", ""),
                cmd.get("description", ""),
            )

        self._c.print(
            Panel(
                table,
                title=f"[bold yellow]{title}[/bold yellow]",
                border_style="yellow",
                padding=(0, 1),
            )
        )

        return _ask(
            self._c,
            label="Permission",
            option_a="Allow execution",
            option_b="Deny",
            default="2",
        )

    def show_command_output(self, output: str) -> None:
        """Show the combined stdout of all diagnostic commands."""
        self._c.print()
        self._c.print(
            Panel(
                Text(output, style="dim"),
                title="[dim]Diagnostic Output[/dim]",
                border_style="dim",
                padding=(0, 1),
            )
        )

    # ── Block 3: Solution ─────────────────────────────────────────────

    def show_solution_panel(self, commands: list[dict]) -> bool:
        """
        Display colour-coded fix commands and ask: Apply or Skip.

        Criticality colour map:
          safe    → bright green
          warning → yellow
          danger  → bright red

        Returns True (Apply) or False (Skip).
        """
        self._c.print()
        self._c.print(Rule("[bold green]🛠   Solution[/bold green]"))

        table = Table(
            show_header=True,
            header_style="bold green",
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False,
        )
        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("Command", no_wrap=True)
        table.add_column("Description")
        table.add_column("Risk", width=12, justify="center")

        has_danger = False
        for idx, cmd in enumerate(commands, start=1):
            crit = cmd.get("criticality", "safe").lower()
            color = _COLOR.get(crit, "white")
            icon = _ICON.get(crit, "")
            if crit == "danger":
                has_danger = True

            table.add_row(
                str(idx),
                Text(cmd.get("command", ""), style=color),
                Text(cmd.get("description", ""), style="dim"),
                Text(f"{icon} {crit}", style=f"bold {color}"),
            )

        self._c.print(
            Panel(
                table,
                title="[bold green]Fix Commands[/bold green]",
                border_style="green",
                padding=(0, 1),
            )
        )

        if has_danger:
            self._c.print(
                "\n  [bold red]⚠  WARNING: This fix contains DANGER-level commands "
                "that may affect running services or be hard to reverse.[/bold red]"
            )

        return _ask(
            self._c,
            label="Apply fix?",
            option_a="Apply",
            option_b="Skip",
            default="2",
        )

    # ── Fix execution status ──────────────────────────────────────────

    def show_fix_result(self, command: str, success: bool, output: str) -> None:
        """Show the result of executing a single fix command."""
        self._c.print()

        if success:
            title_text = Text("✓  SUCCESS", style="bold bright_green")
            border = "green"
        else:
            title_text = Text("✗  FAILED", style="bold bright_red")
            border = "red"

        body = Text()
        body.append(f"$ {command}\n", style="cyan bold")
        if output:
            body.append(output, style="dim")

        self._c.print(
            Panel(body, title=title_text, border_style=border, padding=(0, 1))
        )
