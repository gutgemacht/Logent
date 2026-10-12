import unittest
from unittest.mock import patch

from core.diagnostic_evidence import relevant_output
from core.executor import run_command, run_commands_collect
from ui.display import Display
from rich.console import Console
from io import StringIO


class DiagnosticEvidenceTests(unittest.TestCase):
    def test_short_output_unchanged(self):
        text = '  File: /etc/resolv.conf\nAccess: (0644)'
        self.assertEqual(relevant_output(text, 100), text)

    def test_error_at_end_survives_large_output(self):
        text = '\n'.join(f'unrelated record {i}' for i in range(500)) + '\n[stderr] Permission denied: /etc/resolv.conf'
        result = relevant_output(text, 200, 'permissions /etc/resolv.conf')
        self.assertIn('Permission denied: /etc/resolv.conf', result)
        self.assertIn('omitted', result)
        self.assertLessEqual(len(result), 200)

    def test_relevant_middle_line_kept_in_original_order(self):
        text = '\n'.join(['noise ' * 20] * 60 + ['resolv.conf mode 0777', 'resolv.conf owner root'] + ['noise ' * 20] * 60)
        result = relevant_output(text, 180, 'resolv.conf permissions')
        self.assertIn('resolv.conf mode 0777', result)
        self.assertLess(result.index('mode 0777'), result.index('owner root'))

    def test_long_line_preserves_relevant_excerpt(self):
        text = 'x' * 5000 + 'Permission denied: resolv.conf' + 'x' * 5000
        result = relevant_output(text, 200, 'resolv.conf')
        self.assertIn('Permission denied', result)
        self.assertLessEqual(len(result), 200)

    @patch('core.executor.run_command', return_value=(False, 'noise\n' * 1000 + '[stderr] permission denied'))
    def test_batch_budget_includes_headers_and_status(self, execute):
        result = run_commands_collect([{'command': 'stat /tmp/a'}, {'command': 'ls /tmp/a'}],
                                      max_output_chars=300, max_total_chars=400, context='permissions')
        self.assertLessEqual(len(result), 400)
        self.assertEqual(result.count('Status: failed'), 2)
        self.assertEqual(result.count('permission denied'), 2)
        self.assertEqual(execute.call_count, 2)

    @patch('core.executor.run_command')
    def test_unrepresentable_batch_not_executed(self, execute):
        result = run_commands_collect([{'command': 'stat ' + '/tmp/' + 'x' * 300}], max_total_chars=128)
        self.assertIn('not executed', result)
        execute.assert_not_called()

    @patch('core.executor.subprocess.run')
    def test_successful_stderr_is_retained(self, execute):
        execute.return_value.returncode = 0
        execute.return_value.stdout = 'metadata'
        execute.return_value.stderr = 'warning: partial information'
        success, output = run_command(['stat', '/tmp/a'], shell=False)
        self.assertTrue(success)
        self.assertIn('[stderr] warning: partial information', output)

    def test_display_shows_literal_filtered_evidence(self):
        stream = StringIO()
        ui = Display()
        ui._c = Console(file=stream, width=120, color_system=None)
        evidence = '$ stat /tmp/a\nStatus: failed\n[… omitted …]\n[stderr] permission denied'
        ui.show_command_output(evidence)
        for line in evidence.splitlines():
            self.assertIn(line, stream.getvalue())
        self.assertIn('evidence for assessment', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
