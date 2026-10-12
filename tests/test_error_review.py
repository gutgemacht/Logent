import io
import unittest
from unittest.mock import Mock, patch

from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent
from ui.display import Display, TimestampConsole


class ErrorReviewTests(unittest.TestCase):
    def setUp(self):
        self.ui = Display()
        self.output = io.StringIO()
        self.ui._c = TimestampConsole(file=self.output, width=100)
        self.event = ErrorEvent('/tmp/log', 'ERROR: Wrong permissions for the file /etc/resolv.conf 777. Fit it now.', 'now')

    def test_blank_enter_does_not_ignore_new_error(self):
        with patch('sys.stdin', io.StringIO('\n1\n')):
            self.assertTrue(self.ui.show_error_panel(self.event))
        self.assertNotIn('Enter = Ignore', self.output.getvalue())

    def test_ignore_requires_explicit_two(self):
        with patch('sys.stdin', io.StringIO('2\n')):
            self.assertFalse(self.ui.show_error_panel(self.event))

    def test_closed_input_is_not_an_ignore_decision(self):
        with patch('sys.stdin', io.StringIO('')):
            with self.assertRaises(EOFError):
                self.ui.show_error_panel(self.event)

    def test_ignored_error_does_not_suppress_similar_new_error(self):
        o = Orchestrator.__new__(Orchestrator)
        o._history = Mock()
        o._history.record_occurrence.side_effect = [('old', 'first'), ('new', 'second')]
        o._history.summary.return_value = {'seen': 1, 'solved': 0, 'solutions': []}
        o._history.related.return_value = [{'key': 'old', 'error': 'ERROR permission denied /etc/resolv.conf', 'summary': {'seen': 1, 'solved': 0, 'solutions': []}}]
        o._history.diagnostic_candidates.return_value = []
        o._ui = Mock()
        o._ui.show_error_panel.side_effect = [False, True]
        o._ui.show_history.return_value = []
        o._ui.choose_history_action.return_value = ('diagnose', None)
        o._diagnose_error = Mock(return_value=None)
        o._handle_error(ErrorEvent('/tmp/log', 'ERROR permission denied /etc/resolv.conf', 'before'))
        o._handle_error(self.event)
        self.assertEqual(o._ui.show_error_panel.call_count, 2)
        self.assertEqual(o._history.record_occurrence.call_count, 2)
        self.assertEqual(o._diagnose_error.call_args.args[0], self.event)

    def test_input_closed_stops_watcher_cleanly(self):
        o = Orchestrator.__new__(Orchestrator)
        o._watcher = Mock()
        o._paths = []
        o._ui = Mock()
        o._handle_error = Mock(side_effect=EOFError)
        o.run()
        o._watcher.stop.assert_called_once()
        self.assertIn('without an ignore decision', o._ui.info.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
