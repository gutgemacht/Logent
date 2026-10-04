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

from core.diagnostic_commands import validate_diagnostic_commands
from ui.terminal_menu import supported, select_option
from ui.progress import activity
import config

from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.control import Control
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

class TimestampConsole(Console):
    """Prefix each non-empty console message, including panels and prompts."""

    def print(self, *objects, **kwargs):
        if objects and all(isinstance(obj, Control) for obj in objects):
            return super().print(*objects, **kwargs)
        if not objects or all(isinstance(obj, str) and not obj.strip() for obj in objects):
            return super().print(*objects, **kwargs)
        objects = tuple(obj.lstrip("\n") if isinstance(obj, str) else obj for obj in objects)
        stamp = Text(datetime.now().astimezone().strftime("[%Y-%m-%d %H:%M:%S %z] "), style="dim")
        if all(isinstance(obj, (str, Text)) for obj in objects):
            return super().print(stamp, *objects, **kwargs)
        return super().print(Group(stamp, *objects), **kwargs)


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
    default: str | None = "2",
) -> bool:
    """
    Display two labelled options and read a single-key choice.

    Returns True if the user chose *key_a*, False for *key_b*.
    Pressing Enter without input uses *default*, or asks again if it is None.
    """
    console.print()
    hint = f"Enter = {option_b if default == key_b else option_a}" if default is not None else "Choose 1 or 2 explicitly"
    # Show the two options with colour-coded keys
    console.print(
        f"  [bold]{label}:[/bold]  "
        f"[[bold green]{key_a}[/bold green]] [bold]{option_a}[/bold]"
        f"    "
        f"[[bold {'yellow' if default == key_b else 'red'}]{key_b}[/bold {'yellow' if default == key_b else 'red'}]] [bold]{option_b}[/bold]"
        f"  [dim]({hint})[/dim]"
    )
    if config.ARROW_MENUS and supported(console):
        chosen = select_option(console, label, [(key_a, option_a), (key_b, option_b)], "Waiting for your decision", default)
        return chosen == key_a
    chosen = Prompt.ask(
        "  [bold dim]›[/bold dim]",
        choices=[key_a, key_b],
        **({"default": default} if default is not None else {}),
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
        self._c = TimestampConsole(highlight=False)
        self._status = "Monitoring"

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
            if Path(p).exists():
                body.append(f"  • {p}\n", style="cyan")
            else:
                body.append(f"  • {p} — unavailable, skipped\n", style="yellow")
        body.append("\nWaiting for new error lines in the available logs.\n", style="bold")
        body.append("Use ↑/↓ and Enter to choose (or numbered choices in a basic terminal).\n", style="dim")
        body.append("Press Ctrl+C to stop.", style="dim")

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
            default=None,
        )

    # ── Block 2: Diagnostics ──────────────────────────────────────────

    def show_diagnostic_panel(
        self, commands: list[dict[str, str]], title: str = "Diagnostics"
    ) -> bool:
        """
        Display proposed diagnostic commands and ask: Allow or Deny.

        Returns True (Allow) or False (Deny).
        """
        validate_diagnostic_commands(commands)
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
        table.add_column("Purpose / expected result", style="white")

        for idx, cmd in enumerate(commands, start=1):
            table.add_row(
                str(idx),
                cmd.get("command", ""),
                cmd.get("description", "") + ("\nExpected: " + cmd["expected_result"] if cmd.get("expected_result") else ""),
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

    def show_diagnostic_verdict(self, verdict: dict) -> None:
        body = Text(verdict["explanation"])
        for item in verdict.get('observations', []):
            body.append(f"\nEvidence: $ {item['command']}\nObserved: {item['result']}\nSupports: {item['supports']}")
        if verdict.get('uncertainties'):
            body.append('\nUnknown: ' + '; '.join(verdict['uncertainties']), style='yellow')
        self._c.print(Panel(
            body,
            title=f"Diagnosis: {verdict['status']}",
            border_style="green" if verdict["status"] == "confirmed" else "yellow",
        ))

    def ask_research_further(self) -> bool:
        return _ask(self._c, "Next step", "Research further", "Ignore", default="2")

    def show_command_output(self, output: str) -> None:
        """Show exactly the selected command evidence transmitted to the LLM."""
        self._c.print()
        self._c.print(
            Panel(
                Text(output, style="dim"),
                title="[dim]Diagnostic evidence sent to LLM (omissions marked)[/dim]",
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
                Text(cmd.get("description", "") + ("\nExpected: " + cmd["expected_result"] if cmd.get("expected_result") else ""), style="dim"),
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

    def show_history(self, summary: dict, related: list[dict]) -> list[dict]:
        self._c.print(Panel(
            Text(f"Seen: {summary['seen']} times | Confirmed solved: {summary['solved']} times"),
            title='Error history', border_style='blue',
        ))
        candidates = []
        groups = [('This error', summary['solutions'])]
        for match in related:
            self._c.print(Text(f"Related error (separate statistics): {match['error']} — seen {match['summary']['seen']}, solved {match['summary']['solved']}"))
            for solution in match['summary']['solutions']:
                self._c.print(Panel(
                    Text(f"Historical context only — this fix is not verified for the current error.\n"
                         f"Helped: {solution['helped']} | Failed: {solution['failed']}\n{solution['notes']}"),
                    title='Related history (not available for direct reuse)', border_style='dim',
                ))
        for label, solutions in groups:
            for solution in solutions:
                eligible = not solution.get('obsolete') and solution.get('applicable', True)
                if eligible:
                    candidates.append(solution)
                rate = f"{solution['success_rate']:.0%}" if solution['success_rate'] is not None else 'untested'
                content = Text(f"{label}\nHelped: {solution['helped']} | Failed: {solution['failed']} | Unsure: {solution['unsure']} | Success: {rate}\n")
                content.append(solution['notes'] + '\n')
                content.append(f"Last worked: {solution.get('last_worked') or 'never'}\n")
                if not eligible:
                    content.append('Not reusable: ' + ('obsolete' if solution.get('obsolete') else solution.get('applicability_reason', 'conditions not verified')) + '\n', style='yellow')
                for command in solution['commands']:
                    content.append(f"$ {command['command']}\n")
                self._c.print(Panel(content, title=f"Saved solution {len(candidates)}" if eligible else "Historical solution (diagnose again)"))
        return candidates

    def choose_history_action(self, candidates: list[dict]) -> tuple[str, dict | None]:
        self._c.print(Text('Next: [1] Use saved solution  [2] Diagnose  [3] Ignore  [4] Paste my solution  [5] Manage history', style='bold'))
        choices = ['1', '2', '3', '4', '5'] if candidates else ['2', '3', '4', '5']
        action = self.choose('Action', {key: {'1': 'Use saved solution', '2': 'Diagnose', '3': 'Ignore', '4': 'Paste my solution', '5': 'Manage history'}[key] for key in choices}, default='2')
        if action == '1':
            number = self.choose('Solution number', {str(i): item['notes'][:80] for i, item in enumerate(candidates, 1)})
            return 'reuse', candidates[int(number) - 1]
        return {'2': 'diagnose', '3': 'ignore', '4': 'paste', '5': 'manage'}[action], None

    def ask_outcome(self) -> str:
        self._c.print(Text('Did this solution solve the problem? [1] Yes  [2] No  [3] Unsure', style='bold'))
        answer = self.choose('Outcome', {'1': 'Yes', '2': 'No', '3': 'Unsure'}, default='3')
        return {'1': 'yes', '2': 'no', '3': 'unsure'}[answer]

    def ask_save_solution(self) -> str:
        self._c.print(Text('Solution history: [1] Save this solution  [2] Skip saving  [3] Paste a different solution', style='bold'))
        answer = self.choose('Save', {'1': 'Save this solution', '2': 'Skip saving', '3': 'Paste a different solution'}, default='2')
        return {'1': 'save', '2': 'skip', '3': 'paste'}[answer]

    def read_custom_solution(self) -> tuple[list[dict], str]:
        self._c.print('Paste solution notes, one line at a time; finish with .done. This only saves the entry.')
        notes = self._read_lines()
        self._c.print('Optional commands, one per line; finish with .done. Commands are saved, not executed now.')
        commands = [{'command': line, 'description': 'User supplied command', 'criticality': 'warning', 'sudo': False} for line in self._read_lines().splitlines() if line.strip()]
        if not notes.strip() and not commands:
            self.info('Empty solution skipped.')
        return commands, notes

    def _read_lines(self) -> str:
        lines = []
        while True:
            line = Prompt.ask('›', console=self._c, show_default=False)
            if line.strip() == '.done':
                return '\n'.join(lines)
            lines.append(line)

    def choose(self, title: str, options: dict[str, str], default=None) -> str:
        self._status = "Waiting for your decision"
        if config.ARROW_MENUS and supported(self._c):
            return select_option(self._c, title, list(options.items()), self._status, default)
        return Prompt.ask(title, choices=list(options), **({"default": default} if default is not None else {}), console=self._c)

    def set_status(self, status: str) -> None:
        self._status = status

    def activity(self, message: str):
        self.set_status(message)
        return activity(self._c, message)

    def ask_retry(self) -> bool:
        self._c.print(Text('Request interrupted or failed: [1] Retry  [2] Cancel this error', style='bold'))
        return self.choose('Request', {'1': 'Retry', '2': 'Cancel this error'}, default='2') == '1'

    def show_structured_diagnosis(self, error: str, plan: dict, facts: dict) -> None:
        import json
        self._c.print(Panel(Text(
            'Reported symptom: ' + error + '\nSuspected cause: ' + plan['hypothesis']
            + '\nCurrent filesystem facts:\n' + json.dumps(facts, indent=2)
            + '\nConclusion: pending diagnostic evidence'
        ), title='Structured diagnosis', border_style='cyan'))

    def show_verification(self, verdict: dict) -> None:
        self._c.print(Panel(Text(verdict['explanation']), title='After-fix verification: ' + verdict['status'], border_style='green' if verdict['status'] == 'disproved' else 'yellow'))

    def manage_history(self, history) -> None:
        while True:
            entries = history.list_solutions()
            for i, entry in enumerate(entries, 1):
                self._c.print(Text(f"[{i}] {entry['notes']} | {'obsolete' if entry.get('obsolete') else 'active'} | helped: {entry.get('helped', 0)} | last worked: {entry.get('last_worked') or 'never'} | machine: {entry.get('machine_id', 'legacy/unscoped')}", style='bold'))
            if not entries:
                self.info('No saved solutions.')
                return
            action = self.choose('History', {'1': 'Edit / add notes', '2': 'Mark obsolete', '3': 'Delete', '4': 'Back', '5': 'View details'}, default='4')
            if action == '4':
                return
            number = self.choose('Entry', {str(i): entry['notes'][:80] for i, entry in enumerate(entries, 1)})
            entry = entries[int(number) - 1]
            if action == '5':
                import json
                self._c.print(Panel(Text(json.dumps(entry, indent=2)), title='Saved solution details'))
            elif action == '1':
                self.info('Editing creates a new untested version; the original history is preserved. Leave commands empty to keep existing commands.')
                commands, notes = self.read_custom_solution()
                if not commands and not notes.strip():
                    continue
                if not commands:
                    commands = entry['commands']
                history.edit_solution(entry['id'], commands, notes or entry['notes'])
            elif action == '2':
                history.set_obsolete(entry['id'])
            elif self.choose('Delete this saved solution?', {'1': 'Delete', '2': 'Keep'}, default='2') == '1':
                history.delete_solution(entry['id'])

    def ask_export(self) -> bool:
        self._c.print(Text('Troubleshooting report: [1] Export Markdown and JSON  [2] Skip', style='bold'))
        return self.choose('Report', {'1': 'Export', '2': 'Skip'}, default='2') == '1'

    def monitoring(self, source):
        from rich.live import Live
        return Live(get_renderable=lambda: Text(
            f"{datetime.now().astimezone():%H:%M:%S} | Monitoring | {source.qsize()} queued errors | Ctrl+C stops",
            style='bold blue'), console=self._c, refresh_per_second=2, transient=True)
