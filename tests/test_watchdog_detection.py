import queue
import tempfile
import unittest
from pathlib import Path

import config
from core.watcher import LogEventHandler


class WatchdogDetectionTests(unittest.TestCase):
    def scan(self, lines):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'system.log'
            file.write_text('kernel: normal startup\n')
            events = queue.Queue()
            handler = LogEventHandler(events, config.ERROR_PATTERNS, frozenset({str(file)}), frozenset())
            with file.open('a') as out:
                out.write('\n'.join(lines) + '\n')
            handler._scan_new_lines(str(file))
            return [events.get_nowait().line for _ in range(events.qsize())]

    def test_kernel_memory_and_system_failures_without_error_prefix(self):
        lines = [
            'worker: ZeroDivisionError: division by zero',
            'worker: TypeError: invalid argument',
            'kernel: warning: suspicious scheduler state',
            'kernel: MCE: CPU0: Machine Check',
            'kernel: Out of memory: kill process 1234 (worker) score 900',
            'kernel: Out of memory: Killed process 1234 (worker) total-vm:10000kB',
            'kernel: worker invoked oom-killer: gfp_mask=0x0',
            'kernel: oom-kill:constraint=CONSTRAINT_MEMCG',
            'kernel: Memory cgroup out of memory: Killed process 1234',
            'kernel: page allocation failure: order:4, mode:0x0',
            'worker: Cannot allocate memory',
            'kernel: Kernel panic - not syncing: fatal fault',
            'kernel: Oops: 0000 [#1] SMP',
            'kernel: BUG: unable to handle kernel NULL pointer dereference',
            'kernel: general protection fault: 0000 [#1]',
            'kernel: worker[123]: segfault at 0 ip 1234',
            'kernel: watchdog: BUG: soft lockup - CPU#1 stuck for 23s!',
            'kernel: NMI watchdog: Watchdog detected hard LOCKUP on cpu 1',
            'kernel: INFO: task worker:123 blocked for more than 120 seconds.',
            'kernel: rcu: INFO: rcu_sched detected stalls on CPUs/tasks',
            'kernel: WARNING: CPU: 1 PID: 123 at drivers/test.c:42',
            'kernel: mce: [Hardware Error]: Machine check events logged',
            'kernel: EDAC MC0: 1 UE on DIMM0',
            'kernel: CPU1: Core temperature above threshold, cpu clock throttled',
            'kernel: EXT4-fs warning (device sda1): mounting fs with errors',
            'kernel: XFS (sda1): metadata corruption detected',
            'kernel: BTRFS warning (device sda1): forced readonly',
            'kernel: nvme nvme0: I/O 1 timeout, reset controller',
            'kernel: ata1: device offline',
            'worker: No space left on device',
            'worker: Read-only file system',
            'worker: Too many open files',
            'worker: Network is unreachable',
            'worker: Connection reset by peer',
            'kernel: NETDEV WATCHDOG: eth0: transmit queue 0 timed out',
            'systemd: app.service: Main process exited, code=exited, status=1/FAILURE',
            'systemd: app.service: Start request repeated too quickly.',
            'worker: Aborted (core dumped)',
            '<27>kernel: serious problem with no severity word',
            '<0>kernel: urgent condition',
        ]
        self.assertEqual(self.scan(lines), lines)

    def test_normal_system_messages_not_flagged(self):
        lines = ['kernel: Memory: 32000K/64000K available', 'kernel: process 1234 started',
                 'kernel: CPU0 online', 'kernel: EXT4-fs (sda1): mounted filesystem',
                 'systemd: Started app.service.', 'worker: connection established',
                 'kernel: vm.oom_dump_tasks = 1', '<30>kernel: system ready']
        self.assertEqual(self.scan(lines), [])


if __name__ == '__main__':
    unittest.main()
