"""Versioned Linux x64 reference knowledge. Patterns select checks, never prove a cause."""
from __future__ import annotations

import copy
import re
import shlex
import shutil
from pathlib import Path

from core.diagnostic_commands import validate_diagnostic_commands
from core.error_context import reported_paths, reported_programs
from core.system_context import reported_services

VERSION = 1
ERRNO = 'https://man7.org/linux/man-pages/man3/errno.3.html'
SYSTEMD = 'https://www.freedesktop.org/software/systemd/man/latest/systemctl.html'
MEMORY = 'https://www.kernel.org/doc/html/latest/admin-guide/mm/concepts.html'
COREUTILS = 'https://www.gnu.org/software/coreutils/manual/html_node/'


def entry(name, category, title, pattern, example, commands, guidance, *, target='', rule='', sources=None):
    return dict(id='linux-x64-v1-' + name, version=VERSION, category='default-linux-' + category,
                title=title, patterns=[pattern], symptom=example, commands=[{'command': command,
                'description': description, 'question': description, 'expected_result':
                'Inspect actual output; a log match alone does not confirm the problem.'}
                for command, description in commands], guidance=guidance, target=target,
                rule=rule, source='bundled-linux-reference', architecture=['x86_64', 'amd64'],
                sources=sources or [ERRNO], active=True)


ENTRIES = [
    entry('missing-path', 'filesystem', 'Required path is missing', r'no such file(?: or directory)?|(?:file|directory|configuration).*?(?:does not exist|not present(?:ed)?|not found)',
          'ERROR: Configuration file /tmp/example.conf does not exist.',
          [('stat -L --format=%a -- {target_path}', 'Read target existence and octal mode'), ('namei -l -- {target_path}', 'Inspect each parent path component')],
          ['Confirm the required path in the application configuration and the originating container or service.',
           'Restore the required file from its application package, documented template or a trusted backup. Inspect its contents before restarting the application; creating an empty file may not fix it.',
           'Rerun the existence check and reproduce the original operation.'], target='path', rule='missing_path'),
    entry('wrong-mode', 'permissions', 'Explicit file mode requirement is not met', r'(?:wrong|incorrect|invalid) permissions|(?:permissions|mode).*?(?:expected|required|must be|should be)',
          'ERROR: Wrong permissions for /tmp/example.conf: mode 0777, expected 0644.',
          [('stat -L --format=%a -- {target_path}', 'Read actual octal file mode'), ('namei -l -- {target_path}', 'Inspect ownership and access on the whole path')],
          ['Check the explicitly stated required mode against the application documentation; never assume one mode suits every file.',
           'After approval, change only the reported target to its documented required mode. Then rerun the checks and original operation.'], target='path_mode', rule='file_mode', sources=[COREUTILS + 'stat-invocation.html']),
    entry('access-denied', 'permissions', 'Access to a path is denied', r'permission denied|access denied|\bEACCES\b|\bEPERM\b',
          'ERROR: Permission denied while reading /tmp/example.conf.',
          [('stat -L -- {target_path}', 'Read target mode and owner'), ('namei -l -- {target_path}', 'Inspect parent directory traversal permissions'), ('id', 'Identify the diagnostic user and groups')],
          ['Identify the user actually running the failing application. Inspect owner, group, parent traversal permissions, ACLs and SELinux/AppArmor denials.',
           'Grant only the access required by that application policy. Do not apply chmod 777 or change unrelated paths.', 'Retry under the original application user.'], target='path'),
    entry('blocks-full', 'filesystem', 'Filesystem space or inodes exhausted', r'no space left on device|disk full|\bENOSPC\b',
          'ERROR: Failed to write /tmp/output.log: No space left on device.',
          [('df -Pk -- {filesystem_path}', 'Read free blocks on the target filesystem'), ('df -Pi -- {filesystem_path}', 'Read free inodes on the target filesystem')],
          ['Identify the target mount and distinguish block exhaustion from inode exhaustion or quotas.',
           'Review retention policies and backups before removing expendable files, rotating logs or expanding the volume. No deletion is proposed automatically.',
           'Confirm free capacity and repeat the original write operation.'], target='filesystem', rule='disk_full', sources=[COREUTILS + 'df-invocation.html']),
    entry('quota', 'filesystem', 'User or project quota exceeded', r'quota exceeded|\bEDQUOT\b',
          'ERROR: Disk quota exceeded while writing /home/user/output.log.',
          [('df -Pk -- {filesystem_path}', 'Inspect mount capacity; free space does not rule out a quota')],
          ['Inspect the failing user/project quota with the filesystem administrator. Determine whether block or inode limits were reached.', 'Reduce approved usage or adjust the documented quota, then retry as the original user.'], target='filesystem'),
    entry('read-only-mount', 'filesystem', 'Target filesystem is read-only', r'read-only file system|\bEROFS\b',
          'ERROR: Write to /var/tmp/output.log failed: Read-only file system.',
          [('findmnt -n -o OPTIONS --target {filesystem_path}', 'Inspect mount options on the target filesystem'), ('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect recent kernel storage errors')],
          ['Determine whether the read-only mount is intentional or caused by storage/filesystem errors.', 'Back up affected data and investigate the storage device before considering a remount or offline filesystem repair. Do not run fsck on a mounted filesystem.', 'Recheck mount options and the original write operation.'], target='filesystem', rule='read_only'),
    entry('io-errors', 'filesystem', 'Storage I/O error', r'I/O error|medium error|blk_update_request|Buffer I/O',
          'kernel: I/O error, dev sda, sector 123456.',
          [('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect recent kernel storage evidence'), ('lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS', 'Identify devices and filesystems')],
          ['Identify the affected device, controller and mount. Preserve recent kernel evidence and back up accessible data.', 'Check storage health and cabling with the administrator. Repair or replace failing storage before restoring service.']),
    entry('filesystem-corruption', 'filesystem', 'Filesystem corruption or forced shutdown', r'filesystem corruption|(?:EXT[234]-fs|XFS|BTRFS).*?(?:error|corrupt|shutdown|inconsisten)',
          'kernel: EXT4-fs error (device sda1): filesystem corruption detected.',
          [('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect recent filesystem errors'), ('lsblk -f', 'Identify affected filesystem and mount')],
          ['Preserve evidence and back up accessible data. Identify the exact filesystem and any underlying device fault.', 'Use the filesystem-specific offline repair procedure only after arranging unmounting and recovery. Never apply a generic repair to a live mount.']),
    entry('oom', 'memory', 'Memory exhaustion or OOM kill', r'out[- ]of[- ]memory|oom[-_ ](?:kill|reaper)|cannot allocate memory|\bENOMEM\b|(?:no free|insufficient|not enough) (?:ram|memory)',
          'kernel: Out of memory: Kill process 18423 (python3).',
          [('free -m', 'Inspect current available RAM and swap'), ('ps -eo pid,comm,rss --sort=-rss', 'Inspect current process resident memory'), ('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect recent OOM events')],
          ['Distinguish host RAM exhaustion from a container/cgroup memory limit and a past OOM event.', 'Find the responsible workload and its limits. Reduce concurrency, fix a leak or size memory/swap to the workload; do not kill arbitrary processes.', 'Verify under representative load; free RAM after an OOM kill does not disprove the earlier event.'], sources=[MEMORY]),
    entry('cgroup-oom', 'memory', 'Container or cgroup memory limit reached', r'memory cgroup.*(?:oom|limit)|cgroup.*out of memory',
          'kernel: Memory cgroup out of memory: Killed process 18423 (python3).',
          [('free -m', 'Inspect host memory; this is not the container limit'), ('journalctl -k -b --since=-10min -n 200 --no-pager', 'Locate cgroup OOM evidence')],
          ['Inspect memory.current, memory.max and memory.events in the identified workload cgroup.', 'Fix workload growth or adjust that workload limit based on capacity. Host free memory alone does not rule out cgroup exhaustion.'], sources=[MEMORY]),
    entry('kernel-panic', 'kernel', 'Kernel panic or Oops', r'kernel panic|\bOops\b|kernel.*(?:NULL pointer|general protection|BUG:)',
          'kernel: Kernel panic - not syncing: Fatal exception.',
          [('uname -a', 'Record the running kernel'), ('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect accessible current-boot kernel evidence')],
          ['Preserve the full call trace, kernel version, loaded driver details and crash dump. A previous-boot panic requires previous-boot records or pstore; current logs may be insufficient.', 'Investigate recent kernel/driver/hardware changes and test a supported kernel through a planned maintenance procedure. A panic has no universal repair command.']),
    entry('lockup', 'kernel', 'Kernel watchdog lockup', r'(?:soft|hard)[ -]?lockup|watchdog.*(?:stuck|lockup)',
          'kernel: watchdog: BUG: soft lockup - CPU#2 stuck for 26s!',
          [('uname -a', 'Record kernel version'), ('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect lockup trace and timing'), ('uptime', 'Inspect current load')],
          ['Preserve the watchdog stack trace and correlate it with load, drivers and virtualization.', 'Investigate kernel/driver regressions or stalled hardware. Do not hide the fault by disabling the watchdog.']),
    entry('hung-task', 'kernel', 'Hung task or RCU stall', r'blocked for more than.*seconds|rcu.*stall',
          'kernel: INFO: task postgres:18423 blocked for more than 120 seconds.',
          [('journalctl -k -b --since=-10min -n 200 --no-pager', 'Inspect blocked-task stack and recent I/O'), ('ps -eo pid,stat,comm,wchan', 'Inspect waiting processes')],
          ['Use task stack and wait channel to identify blocked I/O, locking or driver issues.', 'Address the underlying dependency and verify recovery rather than only increasing the warning timeout.']),
    entry('connection-refused', 'network', 'TCP connection refused', r'connection refused|\bECONNREFUSED\b',
          'ERROR: Connection refused while connecting to 127.0.0.1:8080.',
          [('ss -H -ltn', 'Inspect local TCP listeners'), ('ip route show', 'Inspect current routing')],
          ['Check the exact destination, port, protocol and originating network namespace. A local listener list cannot prove the state of a remote server.', 'Check whether the expected service listens on the correct interface and whether a firewall rejects the connection.', 'Repair the identified service/configuration and retry the original request.']),
    entry('connection-timeout', 'network', 'Connection timeout', r'(?:connection|connect|request).*?(?:timed out|timeout)|\bETIMEDOUT\b',
          'ERROR: Connection to 127.0.0.1:8080 timed out after 30 seconds.',
          [('ip route show', 'Inspect routes'), ('ss -H -tan', 'Inspect current TCP connection states')],
          ['Identify the destination and inspect routing, firewall policy, remote health and request timing from the originating environment.', 'A timeout does not establish a file-permissions problem. Change only the component demonstrated to be failing.']),
    entry('dns', 'network', 'Hostname resolution failed', r'failed to resolve|could not resolve|name resolution|\bNXDOMAIN\b|\bEAI_AGAIN\b',
          'ERROR: Failed to resolve hostname api.example.invalid: Temporary failure in name resolution.',
          [('getent ahosts {hostname}', 'Resolve the reported name using this environment resolver'), ('cat -- /etc/resolv.conf', 'Inspect resolver configuration')],
          ['Check the spelling, expected DNS record, resolver reachability and the originating container/service DNS configuration.', 'Distinguish temporary resolver failure from an intentionally nonexistent name. Do not chmod resolver files as a DNS repair.'], target='hostname'),
    entry('address-used', 'network', 'TCP bind address is already in use', r'address already in use|\bEADDRINUSE\b',
          'ERROR: Failed to bind to 127.0.0.1:8080: Address already in use.',
          [('ss -H -ltn', 'Inspect TCP listening addresses'), ('ss -H -ltnp', 'Inspect visible listener processes')],
          ['Identify the expected owner of the conflicting listening socket before stopping anything.', 'Resolve duplicate service instances or choose a documented free port and update dependent configuration.', 'Retry the original bind.'], target='optional_endpoint', rule='address_used'),
    entry('network-unreachable', 'network', 'No route to destination', r'network is unreachable|no route to host|\bENETUNREACH\b|\bEHOSTUNREACH\b',
          'ERROR: Network is unreachable while connecting to 192.0.2.10.',
          [('ip address show', 'Inspect addresses and link state'), ('ip route show', 'Inspect routes')],
          ['Verify interface state, destination route, gateway and originating network namespace.', 'Correct the demonstrated route or interface configuration; absence of a route does not justify changing file permissions.']),
    entry('missing-service', 'services', 'Systemd unit not found', r'\bunit\b.*not found|\.service.*(?:not found|does not exist)',
          'ERROR: Failed to start example.service: Unit example.service not found.',
          [('systemctl show --no-pager --property=LoadState,ActiveState,SubState -- {service}', 'Inspect exact system unit load and runtime state')],
          ['Verify the service name and whether it belongs to the system or user manager.', 'Restore the documented unit from its package or configuration. Reload the correct manager only after restoring the unit, then start it with approval.'], target='service', rule='missing_service', sources=[SYSTEMD]),
    entry('failed-service', 'services', 'Systemd service failed', r'main process exited|failed with result|start request repeated|dependency failed',
          'example.service: Main process exited, code=exited, status=1/FAILURE.',
          [('systemctl show --no-pager --property=LoadState,ActiveState,SubState -- {service}', 'Read unit state'), ('journalctl -b -u {service} --since=-10min -n 100 --no-pager', 'Inspect recent service errors')],
          ['Find the first service error and its failed dependency, user, configuration or exit code.', 'Correct the demonstrated cause before resetting failure state or restarting. A restart alone may hide recurring errors.'], target='service', sources=[SYSTEMD]),
    entry('missing-command', 'packages', 'Executable unavailable in PATH', r'not installed|command not found|(?:program|executable|binary).*not found',
          'ERROR: screen is not installed.', [('which -- {program}', 'Look up the exact reported program in the current PATH')],
          ['Verify the executable name and original runtime PATH. A missing executable does not establish its package name.', 'For known Debian mc/screen packages, inspect dpkg-query and apt-cache before proposing installation. For other tools identify the correct package or application environment.'], target='program', rule='missing_executable'),
    entry('open-files', 'resources', 'File descriptor limit exhausted', r'too many open files|\bEMFILE\b|\bENFILE\b',
          'ERROR: Too many open files while accepting a new connection.',
          [('cat -- /proc/sys/fs/file-nr', 'Read system allocated file handles'), ('cat -- /proc/sys/fs/file-max', 'Read system file handle ceiling')],
          ['Identify the failing process, its open descriptors and its effective limit. Host file counters do not prove a process limit was reached.', 'Fix leaked descriptors or size the documented process/service limit for the workload. Do not raise every system limit blindly.']),
    entry('fork-failed', 'resources', 'Process or task creation failed', r'cannot fork|fork.*resource temporarily unavailable|pthread_create.*failed',
          'ERROR: fork: Resource temporarily unavailable.',
          [('ps -eo user,pid,ppid,comm', 'Inspect current process population'), ('free -m', 'Inspect host memory'), ('cat -- /proc/sys/kernel/pid_max', 'Read PID ceiling')],
          ['Inspect the failing user/process task limits and cgroup pids.max as well as host memory.', 'Correct runaway task creation or an undersized documented limit; do not terminate arbitrary processes.']),
    entry('hardware', 'hardware', 'Hardware or thermal fault', r'machine check|hardware error|uncorrectable|thermal trip|overheat',
          'kernel: mce: Hardware Error: Machine check events logged.',
          [('journalctl -k -b --since=-10min -n 200 --no-pager', 'Preserve recent hardware evidence'), ('uname -a', 'Record kernel and platform')],
          ['Preserve exact machine-check/EDAC/thermal records and inspect component health and cooling.', 'Arrange hardware diagnosis or maintenance for the identified component. Do not treat a hardware fault as a generic software permissions issue.']),
    entry('application-exception', 'application', 'Application exception or crash', r'Traceback|ModuleNotFoundError|segmentation fault|core dumped',
          "Traceback: ModuleNotFoundError: No module named 'example_plugin'.",
          [('uname -a', 'Record platform context'), ('ps -eo pid,comm,args', 'Inspect current application processes')],
          ['Preserve the full exception/stack trace, exact application version and its runtime environment.', 'For missing Python modules verify the interpreter and virtual environment before installing into the intended environment. A program name alone is not enough to choose a dependency.', 'Reproduce using the original interpreter and configuration.']),
]
BY_ID = {item['id']: item for item in ENTRIES}


def compatible_platform(context):
    return (context.get('architecture', '').lower() in {'x86_64', 'amd64'}
            and bool(context.get('os_id')) and context.get('os_id', '').lower() not in {'windows', 'darwin', 'freebsd'})


def match_entry(rows, error, context):
    """Require one specific trusted pattern; conflicting signatures go to the LLM."""
    if not compatible_platform(context):
        return None
    matches = []
    for row in rows:
        builtin = BY_ID.get(row['id'])
        if not builtin or not row.get('active') or row.get('version') != builtin['version']:
            continue
        # Editing reference data must not silently create executable local rules.
        if any(row.get(key) != builtin[key] for key in ('patterns', 'commands', 'rule', 'target', 'title',
                'category', 'source', 'architecture', 'sources', 'guidance')):
            continue
        if any(re.search(pattern, error, re.I) for pattern in builtin['patterns']):
            matches.append(row)
    # Specific subtypes override their broad parent only; other ambiguity falls back.
    for specific, broad in [('cgroup-oom', 'oom'), ('wrong-mode', 'access-denied'), ('lockup', 'kernel-panic')]:
        if any(row['id'] == 'linux-x64-v1-' + specific for row in matches):
            matches = [row for row in matches if row['id'] != 'linux-x64-v1-' + broad]
    return copy.deepcopy(matches[0]) if len(matches) == 1 else None


def build_plan(row, error, context):
    paths, programs, services = reported_paths(error), reported_programs(error), reported_services(error)
    bindings = {}
    target = row['target']
    if target in {'path', 'path_mode', 'filesystem'}:
        if len(paths) != 1:
            return None
        bindings['target_path'] = paths[0]
        if target == 'path_mode':
            modes = re.findall(r'(?:expected|required|should be|must be)\s*[:=]?\s*(0?[0-7]{3,4})\b', error, re.I)
            if len(set(modes)) != 1:
                return None
            bindings['required_mode'] = modes[0]
        if target == 'filesystem':
            probe = Path(paths[0])
            # A not-yet-created output file is checked on its nearest existing parent.
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            bindings['filesystem_path'] = str(probe)
    if target == 'program':
        if len(programs) != 1:
            return None
        bindings['program'] = programs[0]
    if target == 'service':
        if len(services) != 1 or re.search(r'--user|user (?:unit|service|manager)', error, re.I):
            return None
        bindings['service'] = services[0]
    if target == 'hostname':
        hosts = re.findall(r'(?:hostname|resolve(?:\s+hostname)?)\s+[\'\"]?([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9.-]+)', error, re.I)
        if len(set(hosts)) != 1:
            return None
        bindings['hostname'] = hosts[0]
    if target == 'optional_endpoint':
        endpoints = re.findall(r'\b(127\.0\.0\.1|0\.0\.0\.0):(\d{1,5})\b', error)
        if len(endpoints) == 1 and 0 < int(endpoints[0][1]) <= 65535 and re.search(r'\bTCP\b', error, re.I):
            bindings['address'], bindings['port'] = endpoints[0]
    commands = []
    for item in row['commands']:
        argv = shlex.split(item['command'])
        for index, arg in enumerate(argv):
            if arg.startswith('{') and arg.endswith('}'):
                value = bindings.get(arg[1:-1])
                if not value or value.startswith('-') or '\n' in value:
                    return None
                argv[index] = value
        if shutil.which(argv[0]):
            commands.append(dict(item, command=shlex.join(argv)))
    if row['rule'] == 'missing_executable':
        from agents.diagnostic import DiagnosticAgent
        commands = [item for item in DiagnosticAgent._baseline_checks(error) if shutil.which(shlex.split(item['command'])[0])]
    if not commands:
        return None
    validate_diagnostic_commands(commands)
    checks = []
    if row['rule'] == 'missing_path':
        checks = [dict(kind='file_exists', target=bindings['target_path'], expected=True, basis='The current error explicitly requires this path to exist')]
    if row['rule'] == 'file_mode':
        checks = [dict(kind='file_mode', target=bindings['target_path'], expected=bindings['required_mode'], basis='Required mode explicitly stated in the current error')]
    return dict(hypothesis=row['title'], commands=commands, verification_checks=checks,
                library_id=row['id'], library_category=row['category'], library_bindings=bindings,
                library_rule=row['rule'], source=row['source'], sources=row['sources'], guidance=row.get('guidance', []))


def _verdict(plan, captures, symptom, explanation, *, cause='confirmed', cause_description=None):
    observations = [dict(command=item['command'], result=item.get('selected_output') or '(no output)',
                         status='success' if item['success'] else 'failed', supports='Fresh approved diagnostic evidence') for item in captures]
    return dict(status='disproved' if symptom == 'not_reproduced' else 'confirmed' if symptom == cause == 'confirmed' else 'inconclusive',
                explanation=explanation, symptom=dict(status=symptom, explanation=explanation),
                cause=dict(status=cause, description=cause_description or plan['hypothesis'], explanation=explanation),
                observations=observations, uncertainties=['Scope: current diagnostic user and environment. Reproduce in the original context.'],
                assessment_source='local_linux_library', library_id=plan['library_id'])


def evaluate(plan, captures, error, *, after_fix=False):
    """Only narrow measured rules bypass reasoning. All other evidence goes to the LLM."""
    if not plan:
        return None
    rule, bindings = plan.get('library_rule'), plan.get('library_bindings', {})
    expected_commands = {item['command'] for item in plan['commands']}
    captures = [item for item in captures if item['command'] in expected_commands]
    for item in captures:
        argv = shlex.split(item['command'])
        output = item.get('full_output', item.get('selected_output', '')).strip()
        if rule in {'missing_path', 'file_mode'} and argv == ['stat', '-L', '--format=%a', '--', bindings['target_path']]:
            if rule == 'missing_path':
                if item['success'] and re.fullmatch(r'[0-7]{3,4}', output):
                    return _verdict(plan, captures, 'not_reproduced', 'The required target exists in the current environment.', cause='rejected')
                if not item['success'] and 'No such file or directory' in output:
                    return _verdict(plan, captures, 'confirmed', 'The target cannot be resolved: it or a parent component is missing (including a possible broken symlink).')
            elif item['success'] and re.fullmatch(r'[0-7]{3,4}', output):
                different = int(output, 8) != int(bindings['required_mode'], 8)
                return _verdict(plan, captures, 'confirmed' if different else 'not_reproduced',
                    f"Measured mode {output}; explicit required mode {bindings['required_mode']}.", cause='confirmed' if different else 'rejected')
        if rule == 'read_only' and argv == ['findmnt', '-n', '-o', 'OPTIONS', '--target', bindings['filesystem_path']] and item['success']:
            if 'ro' in output.split(','):
                return _verdict(plan, captures, 'confirmed', 'The target filesystem has the read-only mount option. Why it became read-only still requires storage investigation.', cause='inconclusive')
        if rule == 'disk_full' and argv[:3] in (['df', '-Pk', '--'], ['df', '-Pi', '--']) and item['success']:
            lines = output.splitlines()
            if len(lines) == 2:
                parts = lines[1].split()
                if len(parts) >= 6 and parts[3].isdigit() and int(parts[3]) == 0:
                    return _verdict(plan, captures, 'confirmed', 'The target filesystem reports zero available ' + ('inodes.' if argv[1] == '-Pi' else 'blocks.'))
        if rule == 'missing_service' and argv[0] == 'systemctl' and item['success'] and 'LoadState=not-found' in output.splitlines():
            return _verdict(plan, captures, 'confirmed', 'The named unit is not found in the system service manager.')
        if rule == 'address_used' and bindings.get('port') and argv == ['ss', '-H', '-ltn'] and item['success']:
            for line in output.splitlines():
                fields = line.split()
                if len(fields) >= 5 and fields[0] == 'LISTEN':
                    address = fields[3]
                    matching = {f"{bindings['address']}:{bindings['port']}", f"0.0.0.0:{bindings['port']}"}
                    if address in matching:
                        return _verdict(plan, captures, 'confirmed', f'A local TCP listener occupies {address}. Identify its owner before changing anything.')
    return None


def resolution(plan, verdict):
    """Guidance is proposed only after symptom AND cause confirmation; never auto-executed."""
    if not plan or verdict.get('assessment_source') not in {'local_linux_library', 'local_executable_lookup'}:
        return None
    if verdict['symptom']['status'] != 'confirmed' or verdict['cause']['status'] != 'confirmed':
        return None
    if verdict.get('repair_hint'):
        return None  # Existing confirmed Debian package proposal remains preferred.
    return dict(commands=[], description='Linux reference guidance: ' + plan['hypothesis'],
                manual_steps=plan.get('guidance', []), verification='Rerun the approved diagnostic checks, then retry the original failing operation.')
