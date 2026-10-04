import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from agents.diagnostic import DiagnosticAgent
from core.error_context import compatible_errors, problem_kind, reported_paths, validate_solution_context
from core.history import HistoryStore
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent
from ui.display import Display, TimestampConsole
from test_history import TinyEmbedding

MISSING = 'ERROR: File /etc/resolv.conf is not presented in the file system.'
PERMISSIONS = 'ERROR: Wrong permissions for the file /etc/resolv.conf 777. Fit it now.'


class ErrorContextTests(unittest.TestCase):
    def test_missing_file_and_permissions_are_different(self):
        self.assertEqual(problem_kind(MISSING), 'missing_path')
        self.assertEqual(problem_kind(PERMISSIONS), 'permissions')
        self.assertFalse(compatible_errors(MISSING, PERMISSIONS))
        self.assertTrue(compatible_errors(PERMISSIONS, 'ERROR permission denied /etc/resolv.conf'))
        self.assertEqual(reported_paths('File /etc/resolv.conf.'), ['/etc/resolv.conf'])

    def test_chmod_is_rejected_for_missing_file(self):
        with self.assertRaisesRegex(ValueError, 'cannot fix a missing file'):
            validate_solution_context(MISSING, [{'command': 'sudo chmod 644 /etc/resolv.conf'}])
        validate_solution_context(PERMISSIONS, [{'command': 'chmod 644 /etc/resolv.conf'}])

    def test_real_chroma_filters_shared_path_with_conflicting_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(directory, TinyEmbedding())
            missing_key, _ = store.record_occurrence(ErrorEvent('/tmp/log', MISSING, 'now'))
            store.record_occurrence(ErrorEvent('/tmp/log', PERMISSIONS, 'now'))
            store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR file /etc/resolv.conf does not exist', 'now'))
            matches = store.related(MISSING, missing_key)
            self.assertTrue(matches)
            self.assertNotIn(PERMISSIONS, [m['error'] for m in matches])

    def test_related_solution_never_becomes_direct_reuse_candidate(self):
        ui = Display()
        stream = io.StringIO()
        ui._c = TimestampConsole(file=stream, width=100)
        solution = {'commands': [{'command': 'chmod 644 /etc/resolv.conf'}], 'notes': 'permission repair', 'helped': 2, 'failed': 0, 'unsure': 0, 'success_rate': 1.0}
        candidates = ui.show_history({'seen': 1, 'solved': 0, 'solutions': []}, [{'error': PERMISSIONS, 'summary': {'seen': 2, 'solved': 2, 'solutions': [solution]}}])
        self.assertEqual(candidates, [])
        self.assertIn('not available for direct reuse', stream.getvalue())

    def test_diagnostics_check_current_target_existence(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps({'hypothesis': 'missing file', 'commands': [{'command': 'id'}]}))
        plan = agent.diagnose(MISSING)
        self.assertEqual(plan['commands'][0]['command'], 'stat -L -- /etc/resolv.conf')

    def test_model_cannot_offer_chmod_for_confirmed_missing_file(self):
        o = Orchestrator.__new__(Orchestrator)
        o._history = None
        o._ui = Mock()
        o._ui.show_error_panel.return_value = True
        o._diagnose_error = Mock(return_value="stat: No such file or directory; confirmed missing")
        o._solver = Mock()
        o._solver.solve.return_value = [{'command': 'sudo chmod 644 /etc/resolv.conf'}]
        o._apply_fixes = Mock()
        o._handle_error(ErrorEvent('/tmp/log', MISSING, 'now'))
        o._apply_fixes.assert_not_called()
        o._ui.show_solution_panel.assert_not_called()
        self.assertIn('not applicable', o._ui.error.call_args.args[0])

    def test_monitoring_message_follows_processed_error(self):
        o = Orchestrator.__new__(Orchestrator)
        o._watcher = Mock()
        o._watcher.error_queue.get.side_effect = [Mock(), KeyboardInterrupt]
        o._paths = []
        o._ui = Mock()
        o._handle_error = Mock()
        o.run()
        o._handle_error.assert_called_once()
        o._ui.info.assert_called_once_with('Continuing monitoring for new errors…')

    def test_choice_labels_render_bold(self):
        ui = Display()
        ui._c = TimestampConsole(file=io.StringIO(), force_terminal=True, color_system='standard')
        with patch('ui.display.Prompt.ask', return_value='3'):
            ui.ask_outcome()
        self.assertIn('\x1b[1m', ui._c.file.getvalue())
