import io
import tempfile
import unittest

from core.history import HistoryStore
from core.watcher import ErrorEvent
from test_history import TinyEmbedding
from ui.display import Display, TimestampConsole


class HistoryStatisticsTests(unittest.TestCase):
    def test_occurrence_outcomes_are_disjoint_and_unsaved_success_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(directory, TinyEmbedding())
            key, solved = store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR permission denied', 'now'))
            ids = [store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR permission denied', 'now'))[1] for _ in range(3)]
            store.record_attempt(key, solved, None, 'yes')
            store.record_attempt(key, solved, None, 'no')
            store.record_attempt(key, ids[0], None, 'no')
            store.record_attempt(key, ids[1], None, 'unsure')
            summary = store.summary(key)
            self.assertEqual([summary[name] for name in ['seen', 'solved', 'failed', 'unsure', 'unreported', 'solved_without_saved_solution']], [4, 1, 1, 1, 1, 1])

    def test_similar_history_shows_commands_ratings_and_cases_without_solution(self):
        ui = Display()
        output = io.StringIO()
        ui._c = TimestampConsole(file=output, width=160)
        empty = {'seen': 3, 'solved': 1, 'failed': 0, 'unsure': 0, 'unreported': 2,
                 'solved_without_saved_solution': 1, 'solutions': []}
        solution = {'commands': [{'command': 'chmod 644 /tmp/example', 'sudo': True}], 'notes': 'Agent proposed fix',
                    'helped': 1, 'failed': 0, 'unsure': 1, 'success_rate': 1.0}
        with_solution = dict(empty, solutions=[solution])
        candidates = ui.show_history(empty, [
            {'error': 'ERROR permission denied /tmp/example', 'summary': with_solution},
            {'error': 'ERROR permission denied /tmp/other', 'summary': empty}])
        self.assertEqual(candidates, [])
        text = output.getvalue()
        for expected in ['ChromaDB', '$ sudo chmod 644 /tmp/example', 'Worked: 1', 'Cannot verify: 1',
                         'No feedback: 2', 'without a saved solution: 1',
                         'No saved solution is available for this case', 'not available for direct reuse']:
            self.assertIn(expected, text)


if __name__ == '__main__':
    unittest.main()
