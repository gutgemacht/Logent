import io
import json
import unittest
from unittest.mock import Mock, patch

from agents.diagnostic import DiagnosticAgent
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent
from ui.display import TimestampConsole
from rich.panel import Panel


class DiagnosticFlowTests(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)
        self.o._ui = Mock()
        self.o._diagnostic = Mock()
        self.o._solver = Mock()
        self.o._diagnostic.diagnose.return_value = {
            'hypothesis': 'missing file', 'commands': [{'command': 'stat /tmp/example'}]
        }
        self.event = ErrorEvent('/tmp/log', 'ERROR missing file', 'now')
        self.o._ui.show_error_panel.return_value = True
        self.o._ui.show_diagnostic_panel.return_value = True

    @patch('core.orchestrator.run_commands_collect', return_value='Status: success\nexists')
    def test_disproved_ignore_never_calls_solver(self, execute):
        self.o._diagnostic.verify.return_value = {'status': 'disproved', 'explanation': 'File exists'}
        self.o._ui.ask_research_further.return_value = False
        self.o._handle_error(self.event)
        self.o._solver.solve.assert_not_called()
        self.assertIn('exists', self.o._diagnostic.verify.call_args.args[2])

    @patch('core.orchestrator.run_commands_collect', side_effect=['first output', 'second output'])
    def test_research_retains_evidence_then_confirms(self, execute):
        self.o._diagnostic.verify.side_effect = [
            {'status': 'disproved', 'explanation': 'First hypothesis wrong'},
            {'status': 'confirmed', 'explanation': 'Alternative supported'},
        ]
        self.o._ui.ask_research_further.return_value = True
        result = self.o._diagnose_error(self.event)
        self.assertIn('first output', self.o._diagnostic.diagnose.call_args.args[1])
        self.assertIn('second output', result)
        self.assertEqual(execute.call_count, 2)

    @patch('core.orchestrator.run_commands_collect')
    def test_denied_commands_are_not_executed(self, execute):
        self.o._ui.show_diagnostic_panel.return_value = False
        self.assertIsNone(self.o._diagnose_error(self.event))
        execute.assert_not_called()
        self.o._diagnostic.verify.assert_not_called()

    @patch('core.orchestrator.run_commands_collect', return_value='Status: failed\ntool missing')
    def test_inconclusive_does_not_call_solver(self, execute):
        self.o._diagnostic.verify.return_value = {'status': 'inconclusive', 'explanation': 'Tool unavailable'}
        self.o._ui.ask_research_further.return_value = False
        self.o._handle_error(self.event)
        self.o._solver.solve.assert_not_called()

    @patch('core.orchestrator.run_commands_collect', return_value='Status: success\nevidence')
    def test_confirmed_sends_verdict_to_solver(self, execute):
        self.o._diagnostic.verify.return_value = {'status': 'confirmed', 'explanation': 'Supported by evidence'}
        self.o._solver.solve.return_value = []
        self.o._handle_error(self.event)
        self.assertIn('confirmed', self.o._solver.solve.call_args.args[1])
        self.o._ui.ask_research_further.assert_not_called()


class AgentAndOutputTests(unittest.TestCase):
    def test_verification_payload_and_invalid_verdict(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps({'status': 'disproved', 'explanation': 'exists'}))
        self.assertEqual(agent.verify('error', 'missing file', 'stat output')['status'], 'disproved')
        self.assertIn('stat output', agent.call.call_args.args[0])
        agent.call.return_value = '{"status":"maybe","explanation":"unknown"}'
        with self.assertRaises(ValueError):
            agent.verify('error', 'hypothesis', 'output')

    def test_timestamps_for_text_panels_and_prompt(self):
        stream = io.StringIO()
        console = TimestampConsole(file=stream, width=100, color_system=None)
        console.print('Starting error analysis')
        console.print(Panel('Diagnosis confirmed'))
        console.print('Choose 1 or 2', end='')
        lines = [line for line in stream.getvalue().splitlines() if line.startswith('[')]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\]')


if __name__ == '__main__':
    unittest.main()
