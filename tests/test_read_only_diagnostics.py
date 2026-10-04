import json
import unittest
from unittest.mock import Mock, patch

from agents.diagnostic import DiagnosticAgent
from core.diagnostic_commands import diagnostic_argv
from core.executor import run_commands_collect
from ui.display import Display


class ReadOnlyDiagnosticsTests(unittest.TestCase):
    def test_permission_inspection_is_allowed(self):
        for command in ['stat -c %A /etc/resolv.conf', 'ls -ld /etc/resolv.conf', 'namei -l /etc/resolv.conf', 'getfacl /etc/resolv.conf', 'id']:
            self.assertTrue(diagnostic_argv(command))

    def test_modifications_and_shell_bypasses_are_rejected(self):
        for command in ['chmod 644 /tmp/file', 'chown user /tmp/file', 'sudo stat /tmp/file', 'sh -c "chmod 644 /tmp/file"', 'ls; chmod 644 /tmp/file', 'cat /tmp/file > /tmp/out', 'ls $(touch /tmp/file)', 'ls | tee /tmp/out', 'find /tmp -exec chmod 644 {} +', 'sed -i s/a/b/ /tmp/file', 'file -C', '/tmp/stat file']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                diagnostic_argv(command)

    def test_agent_retries_modifying_plan_with_inspection(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=[
            json.dumps({'hypothesis': 'permissions', 'commands': [{'command': 'chmod 644 /tmp/file'}]}),
            json.dumps({'hypothesis': 'permissions', 'commands': [{'command': 'stat /tmp/file'}]}),
        ])
        self.assertEqual(agent.diagnose('denied')['commands'][0]['command'], 'stat /tmp/file')
        self.assertEqual(agent.call.call_count, 2)
        self.assertIn('No repairs', agent.call.call_args.args[0])

    def test_repeated_modifying_plan_fails(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps({'hypothesis': 'permissions', 'commands': [{'command': 'chmod 644 /tmp/file'}]}))
        with self.assertRaisesRegex(ValueError, 'Read-only diagnostic plan rejected'):
            agent.diagnose('denied')
        self.assertEqual(agent.call.call_count, 2)

    @patch('core.executor.run_command')
    def test_entire_batch_is_checked_before_execution(self, execute):
        with self.assertRaises(ValueError):
            run_commands_collect([{'command': 'id'}, {'command': 'chmod 644 /tmp/file'}])
        execute.assert_not_called()

    @patch('core.executor.run_command', return_value=(True, 'metadata'))
    def test_inspection_executes_without_shell(self, execute):
        run_commands_collect([{'command': 'stat "/tmp/file name"'}])
        execute.assert_called_once_with(['stat', '/tmp/file name'], timeout=30, shell=False)

    def test_modifying_command_is_never_displayed_for_approval(self):
        ui = Display()
        ui._c = Mock()
        with self.assertRaises(ValueError):
            ui.show_diagnostic_panel([{'command': 'chmod 644 /tmp/file'}])
        ui._c.print.assert_not_called()


if __name__ == '__main__':
    unittest.main()
