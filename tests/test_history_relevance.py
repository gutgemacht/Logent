import io
import tempfile
import unittest
from unittest.mock import Mock, patch
from rich.console import Console, Group
from rich.text import Text
from core.error_context import compatible_errors, problem_kind
from core.history import HistoryStore
from core.watcher import ErrorEvent
from ui.terminal_menu import scroll_panel, read_key
from test_history import TinyEmbedding
import test_pipeline_experience as fixtures


class RelevanceTests(unittest.TestCase):
    def test_memory_wordings_never_match_missing_programs(self):
        for memory in ['ERROR: no RAM', 'ERROR: No free RAM!', 'kernel: Out of RAM memory: Kill process 123', 'ERROR: less ram', 'ERROR: High memory consumption!']:
            self.assertEqual(problem_kind(memory), 'memory_exhaustion')
            for program in ['ERROR: mc is not installed', 'bash: mc: command not found', 'ERROR: mc is not found']:
                self.assertEqual(problem_kind(program), 'missing_executable')
                self.assertFalse(compatible_errors(memory, program))

    def test_unknown_categories_need_specific_shared_symptoms(self):
        self.assertFalse(compatible_errors('ERROR something strange', 'ERROR unrelated problem'))
        self.assertFalse(compatible_errors('ERROR', 'ERROR'))
        self.assertTrue(compatible_errors('ERROR invalid widget schema', 'WARN invalid widget schema'))

    def test_chroma_rejects_irrelevant_results_even_when_vectors_are_identical(self):
        with tempfile.TemporaryDirectory() as root:
            store = HistoryStore(root, TinyEmbedding())
            key, _ = store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR mc is not installed', 'now'))
            store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR No free RAM', 'now'))
            store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR something strange', 'now'))
            self.assertEqual(store.related('ERROR mc is not installed', key), [])
            store.record_occurrence(ErrorEvent('/tmp/log', 'bash: mc: command not found', 'now'))
            matches = store.related('ERROR mc is not installed', key)
            self.assertEqual(len(matches), 1)
            self.assertIn('mc', matches[0]['error'])

    def test_old_unknown_scenario_is_reclassified_without_migration(self):
        with tempfile.TemporaryDirectory() as root:
            store = HistoryStore(root, TinyEmbedding())
            id_ = store.save_playbook('ERROR mc is not installed', {'hypothesis':'Unavailable executable', 'commands':[{'command':'which -- mc'}]}, {}, source='user')
            store.playbooks.update(ids=[id_], metadatas=[{'category':'unknown'}])
            scenarios = store.diagnostic_candidates('bash: mc: command not found')
            self.assertEqual(len(scenarios), 1)
            self.assertEqual(scenarios[0]['category'], 'missing_executable')
            self.assertEqual(store.diagnostic_candidates('ERROR no RAM'), [])
            again = store.save_playbook('ERROR mc is not installed', {'hypothesis':'Unavailable executable', 'commands':[{'command':'which -- mc'}]}, {}, source='user')
            self.assertEqual(again, id_)
            self.assertEqual(store.playbooks.count(), 1)

    def test_viewport_is_bounded_and_reaches_last_history_row(self):
        console = Console(file=io.StringIO(), width=80, height=25)
        content = Text('\n'.join(f'case row {i}' for i in range(100)))
        panel, offset, maximum = scroll_panel(console, content, 0, 6)
        self.assertEqual((offset, maximum), (0, 94))
        self.assertLessEqual(len(console.render_lines(panel)), 8)
        panel, offset, maximum = scroll_panel(console, content, 999, 6)
        console.print(panel)
        self.assertEqual(offset, 94)
        self.assertIn('case row 99', console.file.getvalue())
        self.assertIn('█', console.file.getvalue())

    def test_page_keys_and_wheel_sequences_are_read_in_full(self):
        for code in [b'\x1b[6~', b'\x1b[A', b'\x1b[<65;30;12M']:
            with patch('ui.terminal_menu.os.read', side_effect=[bytes([x]) for x in code]), patch('ui.terminal_menu.select.select', return_value=([1], [], [])):
                self.assertEqual(read_key(1), code)


class DiagnosisFirstTests(unittest.TestCase):
    setUp = fixtures.PipelineExperienceTests.setUp
    run_case = fixtures.PipelineExperienceTests.run_case

    def test_saved_repair_is_not_sent_to_diagnostic_agent_or_shown_before_evidence(self):
        key, _ = self.history.record_occurrence(self.event)
        self.history.save_solution(key, [{'command':f'chmod 644 {self.target}'}], 'saved repair', 'user')
        order = Mock()
        order.attach_mock(self.o._diagnostic.diagnose, 'plan')
        order.attach_mock(self.o._diagnostic.verify, 'verify')
        order.attach_mock(self.o._ui.show_history, 'history')
        self.run_case()
        methods = [call[0] for call in order.mock_calls]
        self.assertLess(methods.index('verify'), methods.index('history'))
        self.assertNotIn('chmod 644', self.o._diagnostic.diagnose.call_args.args[1])
        self.assertIn('chmod 644', self.o._solver.solve.call_args.args[1])
