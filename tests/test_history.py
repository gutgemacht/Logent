import tempfile
import unittest
from unittest.mock import Mock, patch

from chromadb.api.types import EmbeddingFunction
from core.history import HistoryStore, normalize_error, error_key
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent


class TinyEmbedding(EmbeddingFunction):
    def __init__(self):
        pass

    def __call__(self, input):
        return [[float('permission' in text.lower()), float('disk' in text.lower()), 1.0] for text in input]

    @staticmethod
    def name():
        return 'logent-test-embedding'

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return TinyEmbedding()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = HistoryStore(self.tmp.name, TinyEmbedding())
        self.event = ErrorEvent('/tmp/log', 'ERROR permission denied /etc/resolv.conf pid=42', 'now')

    def test_normalization_preserves_distinct_paths_and_codes(self):
        self.assertEqual(error_key('[2026-10-04 11:00:00 +0200] ERROR pid=1 /tmp/a'), error_key('ERROR pid=999 /tmp/a'))
        self.assertNotEqual(error_key('ERROR /tmp/a code 403'), error_key('ERROR /tmp/b code 404'))
        self.assertEqual(normalize_error('service[123]: ERROR'), 'service[<PID>]: ERROR')

    def test_occurrence_count_and_persistence(self):
        key, first = self.history.record_occurrence(self.event)
        _, second = self.history.record_occurrence(self.event)
        self.assertNotEqual(first, second)
        reopened = HistoryStore(self.tmp.name, TinyEmbedding())
        self.assertEqual(reopened.summary(key)['seen'], 2)
        self.assertEqual(reopened.summary(key)['solved'], 0)

    def test_ranking_deduplication_and_unknown_outcomes(self):
        key, occurrence = self.history.record_occurrence(self.event)
        sid = self.history.save_solution(key, [{'command': 'chmod 644 /tmp/file'}], 'fix permissions', 'user')
        self.assertEqual(sid, self.history.save_solution(key, [{'command': 'chmod 644 /tmp/file'}], 'fix permissions', 'user'))
        untested = self.history.save_solution(key, [], 'check service settings', 'user')
        for outcome in ['yes', 'yes', 'no', 'unsure']:
            self.history.record_attempt(key, occurrence, sid, outcome)
        result = self.history.summary(key)
        self.assertEqual(result['solved'], 1)  # unique occurrences, not attempts
        self.assertEqual(result['solutions'][0]['id'], sid)
        self.assertEqual(result['solutions'][0]['helped'], 2)
        self.assertAlmostEqual(result['solutions'][0]['success_rate'], 2 / 3)
        self.assertIsNone(result['solutions'][1]['success_rate'])

    def test_skipping_solution_still_records_outcome_without_commands(self):
        key, occurrence = self.history.record_occurrence(self.event)
        self.history.record_attempt(key, occurrence, None, 'yes', [{'success': True}])
        result = self.history.summary(key)
        self.assertEqual(result['solved'], 1)
        self.assertEqual(result['solutions'], [])

    def test_related_errors_do_not_merge_statistics(self):
        key, occurrence = self.history.record_occurrence(self.event)
        self.history.record_occurrence(ErrorEvent('/tmp/log', 'ERROR permission denied /tmp/other', 'now'))
        related = self.history.related(self.event.line, key)
        self.assertEqual(len(related), 1)
        self.assertNotEqual(related[0]['key'], key)
        self.assertEqual(self.history.summary(key)['seen'], 1)


class HistoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)
        self.o._history = Mock()
        self.o._history.record_occurrence.return_value = ('key', 'occurrence')
        self.o._history.summary.return_value = {'seen': 1, 'solved': 0, 'solutions': []}
        self.o._history.related.return_value = []
        self.o._ui = Mock()
        self.o._ui.show_error_panel.return_value = True
        self.o._ui.show_history.return_value = []
        self.o._diagnostic = Mock()
        self.o._solver = Mock()
        self.event = ErrorEvent('/tmp/log', 'ERROR denied', 'now')

    def test_initial_ignore_still_counts(self):
        self.o._ui.show_error_panel.return_value = False
        self.o._handle_error(self.event)
        self.o._history.record_occurrence.assert_called_once_with(self.event)
        self.o._diagnostic.diagnose.assert_not_called()

    @patch('core.orchestrator.run_command', return_value=(True, ''))
    def test_reuse_requires_approval_and_user_confirmation(self, execute):
        selected = {'id': 'saved', 'error_key': 'key', 'commands': [{'command': 'chmod 644 /tmp/file'}], 'notes': 'fix'}
        self.o._ui.choose_history_action.return_value = ('reuse', selected)
        self.o._ui.show_solution_panel.return_value = True
        self.o._ui.ask_outcome.return_value = 'no'
        self.o._ui.ask_save_solution.return_value = 'skip'
        self.o._handle_error(self.event)
        self.o._diagnostic.diagnose.assert_not_called()
        self.o._ui.show_solution_panel.assert_called_once()
        self.assertEqual(self.o._history.record_attempt.call_args.args[3], 'no')
        self.assertEqual(self.o._history.record_attempt.call_args.args[2], 'saved')

    @patch('core.orchestrator.run_command')
    def test_custom_paste_is_not_executed_and_can_be_untested(self, execute):
        self.o._ui.choose_history_action.return_value = ('paste', None)
        self.o._ui.read_custom_solution.return_value = ([{'command': 'chmod 644 /tmp/file'}], 'my fix')
        self.o._ui.ask_outcome.return_value = 'unsure'
        self.o._handle_error(self.event)
        execute.assert_not_called()
        self.o._history.save_solution.assert_called_once()
        self.assertEqual(self.o._history.record_attempt.call_args.args[3], 'unsure')

    @patch('core.orchestrator.run_command')
    def test_reuse_denied_is_not_executed(self, execute):
        self.o._ui.choose_history_action.return_value = ('reuse', {'commands': [{'command': 'chmod 644 /tmp/file'}]})
        self.o._ui.show_solution_panel.return_value = False
        self.o._handle_error(self.event)
        execute.assert_not_called()
        self.o._history.record_attempt.assert_not_called()

class HistoryUiTests(unittest.TestCase):
    def test_paste_accepts_multiline_notes_and_commands(self):
        import io
        from ui.display import Display, TimestampConsole
        ui = Display()
        ui._c = TimestampConsole(file=io.StringIO())
        with patch('ui.display.Prompt.ask', side_effect=['My fix', 'Second note', '.done', 'chmod 644 /tmp/file', '.done']):
            commands, notes = ui.read_custom_solution()
        self.assertEqual(notes, 'My fix\nSecond note')
        self.assertEqual(commands[0]['command'], 'chmod 644 /tmp/file')

    def test_history_shows_untested_solution_and_counts(self):
        import io
        from ui.display import Display, TimestampConsole
        stream = io.StringIO()
        ui = Display()
        ui._c = TimestampConsole(file=stream, width=100)
        solution = {'id': 'id', 'commands': [], 'notes': 'Manual fix', 'helped': 0, 'failed': 0, 'unsure': 1, 'success_rate': None}
        candidates = ui.show_history({'seen': 4, 'solved': 2, 'solutions': [solution]}, [])
        self.assertEqual(candidates, [solution])
        self.assertIn('Confirmed solved: 2', stream.getvalue())
        self.assertIn('untested', stream.getvalue())
