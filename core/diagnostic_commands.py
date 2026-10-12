"""Conservative command policy for the read-only diagnostic phase."""
import shlex
import re

READ_ONLY_COMMANDS = frozenset({
    'stat', 'ls', 'id', 'whoami', 'groups', 'namei', 'getfacl', 'readlink',
    'pwd', 'cat', 'head', 'tail', 'grep', 'wc', 'df', 'du', 'ps', 'uname',
    'free', 'uptime', 'lsblk', 'ss', 'netstat', 'ip', 'getent', 'which', 'dpkg-query', 'apt-cache',
    'journalctl', 'systemctl', 'findmnt',
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
    if argv[0] == 'which':
        targets = argv[2:] if argv[1:2] == ['--'] else argv[1:]
        if not targets or any(target.startswith('-') for target in targets):
            raise ValueError('which requires executable names or paths, without options')
    if argv[0] in {'dpkg-query', 'apt-cache'}:
        modes = {'dpkg-query': {'-s', '--status'}, 'apt-cache': {'policy'}}
        if len(argv) != 3 or argv[1] not in modes[argv[0]] or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', argv[2]):
            raise ValueError('Only dpkg-query --status PACKAGE and apt-cache policy PACKAGE are allowed')
    if argv[0] == 'journalctl':
        kernel = argv == ['journalctl', '-k', '-b', '--since=-10min', '-n', '200', '--no-pager']
        service = (len(argv) == 8 and argv[1:3] == ['-b', '-u']
                   and re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', argv[3])
                   and not argv[3].startswith('-')
                   and argv[4:] == ['--since=-10min', '-n', '100', '--no-pager'])
        # journalctl also has destructive operations (vacuum, rotate, flush).
        if not kernel and not service:
            raise ValueError('Only bounded current-boot kernel/service journal reads are allowed')
    if argv[0] == 'systemctl':
        if not (len(argv) == 6 and argv[1:5] == ['show', '--no-pager', '--property=LoadState,ActiveState,SubState', '--']
                and re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', argv[5]) and not argv[5].startswith('-')):
            raise ValueError('Only systemctl show of an exact service load/runtime state is allowed')
    if argv[0] == 'findmnt':
        if not (len(argv) == 6 and argv[1:5] == ['-n', '-o', 'OPTIONS', '--target'] and argv[5].startswith('/')):
            raise ValueError('Only findmnt read of target mount options is allowed')
    if argv[0] == 'ss' and any((arg.startswith('--') and any(option.startswith(arg.split('=')[0]) for option in ('--kill', '--diag'))) or (arg.startswith('-') and not arg.startswith('--') and any(flag in arg for flag in 'KD')) for arg in argv[1:]):
        raise ValueError('ss --kill/-K and --diag/-D modify connections or files and are not diagnostic')
    if argv[0] == 'ip':
        allowed = (len(argv) >= 3 and argv[1] in {'address', 'addr', 'link', 'route', 'neigh', 'neighbor'}
                   and argv[2] in {'show', 'list'}) or (len(argv) >= 4 and argv[1:3] == ['route', 'get'])
        if not allowed:
            raise ValueError('Only ip address/link/route/neigh show/list and ip route get are allowed')
    if argv[0] == 'getent' and (len(argv) < 2 or argv[1] not in {'hosts', 'ahosts', 'ahostsv4', 'ahostsv6', 'services', 'passwd', 'group'}):
        raise ValueError('getent requires an allowed lookup database')
    return argv


def validate_diagnostic_commands(commands: list[dict]) -> None:
    for index, item in enumerate(commands, 1):
        if not isinstance(item, dict):
            raise ValueError('Invalid diagnostic command entry')
        try:
            diagnostic_argv(item.get('command'))
        except ValueError as exc:
            raise ValueError(f'Command {index} {item.get("command")!r}: {exc}') from exc
