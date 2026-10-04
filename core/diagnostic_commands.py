"""Conservative command policy for the read-only diagnostic phase."""
import shlex

READ_ONLY_COMMANDS = frozenset({
    'stat', 'ls', 'id', 'whoami', 'groups', 'namei', 'getfacl', 'readlink',
    'pwd', 'cat', 'head', 'tail', 'grep', 'wc', 'df', 'du', 'ps', 'uname',
    'free', 'uptime', 'lsblk',
})


def diagnostic_argv(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError('Diagnostic command must be a non-empty string')
    # No shell wrappers, chaining, redirection, or expansion. Arguments are
    # passed directly to the executable, never evaluated by a shell.
    if any(char in command for char in ';|&<>`$\n\r'):
        raise ValueError('Diagnostics require one read-only command without shell operators')
    argv = shlex.split(command)
    if not argv or argv[0] not in READ_ONLY_COMMANDS:
        raise ValueError(f'Diagnostic executable is not allowed: {argv[0] if argv else command}')
    return argv


def validate_diagnostic_commands(commands: list[dict]) -> None:
    for item in commands:
        if not isinstance(item, dict):
            raise ValueError('Invalid diagnostic command entry')
        diagnostic_argv(item.get('command'))
