import os
import select
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(os.name == 'posix', 'POSIX terminal controls')
class TerminalMenuTests(unittest.TestCase):
    def run_menu(self, keys, options="[('1','First'),('2','Second')]", default="'1'"):
        import pty
        master, slave = pty.openpty()
        code = f'''
import sys, termios
from ui.display import TimestampConsole
from ui.terminal_menu import select_option
before = termios.tcgetattr(sys.stdin.fileno())
try:
    value = select_option(TimestampConsole(), 'Test menu ready', {options}, 'Testing', {default})
    print('MENU_RESULT=' + value)
except KeyboardInterrupt:
    print('MENU_CANCELLED')
print('TERMINAL_RESTORED=' + str(before == termios.tcgetattr(sys.stdin.fileno())))
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', code], stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, TERM='xterm-256color'))
        os.close(slave)
        data = b''
        sent = False
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data += chunk
                    if not sent and b'Test menu ready' in data:
                        os.write(master, keys)
                        sent = True
                if child.poll() is not None:
                    break
            child.wait(timeout=1)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            os.close(master)
        self.assertEqual(child.returncode, 0, data.decode(errors='replace'))
        self.assertIn(b'TERMINAL_RESTORED=True', data)
        return data

    def test_arrow_down_selects_second_choice(self):
        self.assertIn(b'MENU_RESULT=2', self.run_menu(b'\x1b[B\r'))

    def test_number_selection_supports_two_digit_entries(self):
        options = repr([(str(i), f'Entry {i}') for i in range(1, 12)])
        self.assertIn(b'MENU_RESULT=10', self.run_menu(b'10\r', options))

    def test_ctrl_c_restores_terminal(self):
        self.assertIn(b'MENU_CANCELLED', self.run_menu(b'\x03'))
