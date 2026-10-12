import io
import os
import unittest
from unittest.mock import Mock, patch

from core.watcher import ErrorEvent
from rich.console import Console
from ui.display import Display
from ui.investigation import pinned_error


class TerminalStream(io.StringIO):
    def fileno(self):
        return 1


class PinnedErrorTests(unittest.TestCase):
    def setUp(self):
        self.event = ErrorEvent('/tmp/log', 'ERROR: missing /tmp/file', 'now')
        self.stream = TerminalStream()
        self.console = Console(file=self.stream, width=80, height=24, force_terminal=True)

    @patch('ui.investigation.supported', return_value=True)
    @patch('ui.investigation.os.get_terminal_size', return_value=os.terminal_size((80, 24)))
    def test_header_reserves_rows_and_releases_after_exception(self, size, supported):
        with self.assertRaises(KeyboardInterrupt):
            with pinned_error(self.console, self.event) as active:
                self.assertTrue(active)
                self.assertLess(self.console.height, 24)
                for i in range(100):
                    self.console.print(f'Diagnostic evidence {i}')
                raise KeyboardInterrupt
        output = self.stream.getvalue()
        self.assertIn(self.event.line, output)
        self.assertEqual(output.count(self.event.line), 1)
        self.assertRegex(output, r'\x1b\[\d+;24r')
        self.assertTrue(output.endswith('\x1b7\x1b[r\x1b8'))
        self.assertEqual(self.console.height, 24)

    @patch('ui.investigation.supported', return_value=False)
    def test_redirected_output_has_no_terminal_control(self, supported):
        with pinned_error(self.console, self.event) as active:
            self.assertFalse(active)
        self.assertEqual(self.stream.getvalue(), '')

    @patch('ui.investigation.supported', return_value=True)
    @patch('ui.investigation.os.get_terminal_size', return_value=os.terminal_size((80, 6)))
    def test_tiny_terminal_uses_plain_output(self, size, supported):
        with pinned_error(self.console, self.event) as active:
            self.assertFalse(active)
        self.assertEqual(self.console.height, 24)

    def test_error_panel_not_repeated_when_pinned(self):
        ui = Display()
        ui._c = Mock()
        ui._error_pinned = True
        with patch('ui.display._ask', return_value=True):
            self.assertTrue(ui.show_error_panel(self.event))
        ui._c.print.assert_not_called()


if __name__ == '__main__':
    unittest.main()
