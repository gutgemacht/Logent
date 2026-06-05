"""
ui/display.py
=============
Rich-based terminal UI with verbose output control.  
Shows File + Timestamp on same line, Error Line on single line.
Only shows current step description (e.g., "Connection to LLM", "Waiting for LLM").
"""  
from __future__ import annotations  
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich.prompt import Prompt
from rich.live import Live
import logging

_logger = logging.getLogger("log_monitor.ui")

class Display:
    """Rich terminal UI for log monitoring."""
    
    def __init__(self) -> None:
        self._c = Console(highlight=False, width=100)
        _logger.debug("Display UI initialized")
    
    def show_error_panel(self, event: dict) -> bool:
        """
        Display detected error with File + Timestamp on same line.
        Error Line on single line. Returns True (Confirm) or False (Ignore).
        """
        self._c.print()
        self._c.print(Rule("[bold red]⚠   Error Detected[/bold red]"))
        
        body = Text()
        # File and Timestamp on same line
        body.append("File:       ", style="bold dim")
        body.append(f"{event['filepath']} | Timestamp:  ", style="bold")
        body.append(f"{event['timestamp']}\n", style="cyan")
        
        # Error Line label and content on same line
        body.append("Error Line: ", style="bold")
        body.append(f"  {event['line']}", style="bold red")
        
        self._c.print(
            Panel(
                body,
                title="[bold red]Found Error[/bold red]",
                border_style="red",
                padding=(1, 2),
            )
        )
        
        return self._ask(
            self._c,
            label="Action",
            option_a="Confirm (start analysis)",
            option_b="Ignore",
            default="1",
        )
    
    def show_diagnostics_panel(self, commands: list[dict]) -> None:
        """Display diagnostic commands waiting for user approval."""
        self._c.print()
        self._c.print(Rule("[bold cyan]🔍   Diagnostics[/bold cyan]"))
        
        body = Text()
        body.append("# │  Command               │ Description                   │\n", style="dim")
        for i, cmd in enumerate(commands, 1):
            body.append(f"{i:2} │  {cmd['command'][:22]:<22} │  {cmd['description'][:25]:<25} │\n")
        
        self._c.print(
            Panel(
                body,
                title="[bold cyan]Diagnostics[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )
        
        return self._ask(
            self._c,
            label="Permission",
            option_a="Allow execution",
            option_b="Deny",
            default="2",
        )
    
    def show_command_output(self, output: str) -> None:
        """Show combined output of diagnostic commands."""
        self._c.print()
        self._c.print(
            Panel(
                Text(output, style="dim"),
                title="[dim]Diagnostic Output[/dim]",
                border_style="dim",
                padding=(0, 1),
            )
        )
    
    def show_solution_panel(self, commands: list[dict]) -> bool:
        """Display fix commands and ask user to apply or skip."""
        self._c.print()
        self._c.print(Rule("[bold yellow]✓   Solution Ready[/bold yellow]"))
        
        body = Text()
        body.append("# │  Command               │ Description                   │ Criticality │\n", style="dim")
        for i, cmd in enumerate(commands, 1):
            style = {"safe": "dim", "warning": "yellow", "danger": "red"}
            crit_style = style.get(cmd.get("criticality", "warning"), "red")
            body.append(f"{i:2} │  {cmd['command'][:22]:<22} │  {cmd['description'][:25]:<25} │  {cmd.get('criticality', 'warning'):<9} │\n", style=crit_style)
        
        self._c.print(
            Panel(
                body,
                title="[bold yellow]Proposed Fixes[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        
        return self._ask(
            self._c,
            label="Action",
            option_a="Apply fixes",
            option_b="Skip",
            default="2",
        )
    
    def show_fix_result(self, command: str, success: bool, output: str) -> None:
        """Show result of executing a single fix command."""
        self._c.print()
        
        title_text = Text("✓  SUCCESS" if success else "✗  FAILED", 
                         style="bold " + ("bright_green" if success else "bright_red"))
        border = "green" if success else "red"
        
        body = Text()
        body.append(f"$ {command}\n", style="cyan bold")
        if output:
            body.append(output, style="dim")
        
        self._c.print(
            Panel(
                body,
                title=title_text,
                border_style=border,
                padding=(0, 1),
            )
        )
    
    def info(self, message: str) -> None:
        """Dim informational line - minimal output"""
        self._c.print(f"  [dim]{message}[/dim]")
    
    def thinking(self, message: str) -> None:
        """Single-line agent working indicator - concise"""
        self._c.print(f"  [cyan]⟳  {message}[/cyan]")
    
    def step_status(self, message: str) -> None:
        """Single-line step status - current step only"""
        self._c.print(f"[bold blue]→[/bold blue] {message}")
    
    def error(self, message: str) -> None:
        """Red error message - minimal output"""
        self._c.print(f"  [bold red]ERROR:[/bold red] {message}")
    
    def _ask(self, console: Console, label: str, option_a: str, option_b: str, default: str = "1") -> bool:
        """Ask user for confirmation or rejection."""
        _logger.debug(f"User prompt: {label}")
        choice = console.input(f"[dim]Press [1] for {option_a} or [2] for {option_b}[/dim]\n› ")
        return choice == default
