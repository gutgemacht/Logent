import json
import unittest
from unittest.mock import Mock, patch

from agents.solver import SolverAgent
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent


class SolverResponseTests(unittest.TestCase):
    def setUp(self):
        self.agent = SolverAgent('test', 'http://localhost')
        self.agent.call = Mock()

    def test_wrapped_commands_not_silently_discarded(self):
        self.agent.call.return_value = json.dumps({'commands': [{'command': 'chmod 644 /tmp/file'}]})
        self.assertEqual(self.agent.solve('error', 'evidence')[0]['command'], 'chmod 644 /tmp/file')

    def test_explicit_no_fix_preserves_reason(self):
        self.agent.call.return_value = '{"no_applicable_fix":true,"reason":"No verified backup"}'
        self.assertEqual(self.agent.solve('error', 'evidence')['reason'], 'No verified backup')

    def test_invalid_objects_and_commands_are_errors(self):
        for response in [{'unexpected': 'format'}, [{'description': 'missing command'}], {'no_applicable_fix': True}]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.agent.call.return_value = json.dumps(response)
                self.agent.solve('error', 'evidence')


class UnresolvedFlowTests(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)
        self.o._enhanced = True
        self.o._ui = Mock()
        self.o._solver = Mock()
        self.o._diagnostic = Mock()
        self.o._diagnostic.verify.return_value = {'status':'confirmed', 'explanation':'Fresh evidence confirms the diagnosis'}
        self.o._hypothesis = 'Suspected missing file'
        self.o._checks = []
        self.o._llm = lambda operation, label: operation()
        self.o._note = Mock()
        self.event = ErrorEvent('/tmp/log', 'ERROR missing file', 'now')

    def test_no_fix_prompts_before_finishing_without_applying(self):
        self.o._solver.solve.return_value = []
        self.o._ui.choose_unresolved_action.return_value = 'continue'
        self.assertIsNone(self.o._propose_fixes(self.event, 'evidence'))
        self.o._ui.choose_unresolved_action.assert_called_once()
        self.o._ui.show_solution_panel.assert_not_called()
        self.o._ui.ask_outcome.assert_not_called()

    def test_retry_can_produce_fix(self):
        fixes = [{'command': 'touch /tmp/file'}]
        self.o._solver.solve.side_effect = [[], fixes]
        self.o._ui.choose_unresolved_action.return_value = 'retry'
        self.assertEqual(self.o._propose_fixes(self.event, 'evidence'), fixes)
        self.assertEqual(self.o._solver.solve.call_count, 2)

    def test_research_retains_evidence_and_solver_reason(self):
        self.o._solver.solve.side_effect = [{'no_applicable_fix': True, 'reason': 'Need owner'}, [{'command': 'fix'}]]
        self.o._ui.choose_unresolved_action.return_value = 'research'
        self.o._diagnose_error = Mock(return_value='new confirmed evidence')
        self.o._propose_fixes(self.event, 'original evidence')
        context = self.o._diagnose_error.call_args.args[1]
        self.assertIn('original evidence', context)
        self.assertIn('Need owner', context)
        self.assertEqual(self.o._solver.solve.call_args.args[1], 'new confirmed evidence')

    @patch('core.orchestrator.run_commands_collect', return_value='extra evidence')
    def test_second_request_for_diagnostics_is_not_treated_as_fix(self, execute):
        self.o._solver.solve.return_value = {'need_more_diagnostics': True, 'reason': 'Need metadata', 'commands': [{'command': 'stat /tmp/file'}]}
        self.o._ui.show_diagnostic_panel.return_value = True
        self.o._ui.choose_unresolved_action.return_value = 'continue'
        self.assertIsNone(self.o._propose_fixes(self.event, 'evidence'))
        self.assertEqual(execute.call_count, 1)
        self.assertIn('still needs more evidence', self.o._ui.choose_unresolved_action.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
