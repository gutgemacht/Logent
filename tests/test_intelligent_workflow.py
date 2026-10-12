import io
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from agents.diagnostic import DiagnosticAgent
from core.event_grouping import EventInbox
from core.history import HistoryStore
from core.orchestrator import Orchestrator, RequestCancelled
from core.reporting import SessionReport
from core.system_context import check_conditions, file_facts, solution_conditions, system_context
from core.watcher import ErrorEvent
from test_history import TinyEmbedding
from ui.display import Display, TimestampConsole


class ConditionsTests(unittest.TestCase):
    def test_current_file_conditions_and_machine_are_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'file'
            file.write_text('test')
            error = f'ERROR wrong permissions {file}'
            context = system_context()
            conditions = solution_conditions(error, file_facts(error), context)
            self.assertTrue(check_conditions(conditions, error, file_facts(error), context)[0])
            file.chmod(0o777)
            self.assertFalse(check_conditions(conditions, error, file_facts(error), context)[0])
            self.assertFalse(check_conditions(conditions, error, file_facts(error), dict(context, machine_id='other'))[0])

    def test_missing_symlink_target_and_legacy_conditions(self):
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / 'link'
            link.symlink_to(Path(folder) / 'absent')
            facts = file_facts(f'ERROR file {link} missing')
            self.assertFalse(facts[str(link)]['exists'])
            self.assertTrue(facts[str(link)]['symlink'])
            self.assertFalse(check_conditions({}, 'error', {}, system_context())[0])


class GroupAndReportTests(unittest.TestCase):
    def test_grouping_retains_order_and_different_errors(self):
        source = queue.Queue()
        events = [ErrorEvent('/tmp/log', message, 'now') for message in ['ERROR pid=1 missing', 'ERROR pid=2 missing', 'ERROR permissions', 'ERROR missing']]
        for event in events:
            source.put(event)
        inbox = EventInbox(source)
        self.assertEqual(inbox.next_batch(), events[:2])
        self.assertEqual(inbox.next_batch(), [events[2]])
        self.assertEqual(inbox.next_batch(), [events[3]])

    def test_report_exports_evidence_and_unverified_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            report = SessionReport(ErrorEvent('/tmp/log', 'ERROR test', 'now'), system_context())
            report.add('diagnostic_output', '$ stat /tmp/file\nNo such file')
            md = report.export(folder)
            self.assertIn('not_reported', md.read_text())
            data = json.loads(md.with_suffix('.json').read_text())
            self.assertEqual(data['steps'][0]['stage'], 'diagnostic_output')


class ManagementTests(unittest.TestCase):
    def test_scopes_do_not_mix_environments(self):
        with tempfile.TemporaryDirectory() as folder:
            context = system_context()
            a = HistoryStore(folder, TinyEmbedding(), context)
            b = HistoryStore(folder, TinyEmbedding(), dict(context, machine_id='other'))
            event = ErrorEvent('/tmp/log', 'ERROR denied', 'now')
            key_a, _ = a.record_occurrence(event)
            key_b, _ = b.record_occurrence(event)
            self.assertNotEqual(key_a, key_b)
            self.assertEqual(a.summary(key_a)['seen'], 1)
            self.assertEqual(a.related(event.line, key_a), [])

    def test_edited_solution_does_not_inherit_old_successes(self):
        with tempfile.TemporaryDirectory() as folder:
            history = HistoryStore(folder, TinyEmbedding())
            key, event = history.record_occurrence(ErrorEvent('/tmp/log', 'ERROR denied', 'now'))
            old = history.save_solution(key, [{'command': 'old'}], 'old fix', 'user')
            history.record_attempt(key, event, old, 'yes')
            new = history.edit_solution(old, [{'command': 'new'}], 'new fix')
            self.assertTrue(history.get_solution(old)['obsolete'])
            result = {row['id']: row for row in history.summary(key)['solutions']}
            self.assertEqual(result[new]['helped'], 0)
            self.assertIsNotNone(result[old]['last_worked'])
            history.delete_solution(new)
            self.assertEqual(len(history.list_solutions()), 1)


class EnhancedWorkflowTests(unittest.TestCase):
    def make_orchestrator(self):
        o = Orchestrator.__new__(Orchestrator)
        o._enhanced = True
        o._system = system_context()
        o._ui = MagicMock()
        o._ui.ask_export.return_value = False
        o._history = Mock()
        o._history.record_occurrence.return_value = ('key', 'event')
        o._history.summary.return_value = {'seen': 1, 'solved': 0, 'solutions': []}
        o._history.related.return_value = []
        o._history.diagnostic_candidates.return_value = []
        o._history.case_records.return_value = []
        o._ui.show_history.return_value = []
        o._ui.choose_history_action.return_value = ('diagnose', None)
        o._diagnostic = Mock()
        o._diagnostic.diagnose.return_value = {'hypothesis': 'permissions', 'commands': [{'command': 'id'}]}
        o._diagnostic.verify.side_effect = [
            {'status': 'confirmed', 'explanation': 'Bad permissions'},
            {'status': 'disproved', 'explanation': 'Original problem resolved'},
        ]
        o._solver = Mock()
        o._solver.solve.return_value = [{'command': 'echo fixed', 'description': 'fix', 'criticality': 'safe'}]
        o._ui.ask_outcome.return_value = 'yes'
        o._ui.ask_save_solution.return_value = 'save'
        return o

    @patch('core.orchestrator.run_command', return_value=(True, 'fixed'))
    @patch('core.orchestrator.run_commands_collect', side_effect=['before evidence', 'after evidence'])
    def test_full_flow_reruns_checks_and_saves_conditions(self, inspect, execute):
        o = self.make_orchestrator()
        o._handle_error(ErrorEvent('/tmp/log', 'ERROR permissions /tmp/file', 'now'))
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(o._diagnostic.verify.call_count, 2)
        self.assertIn('AFTER FIX', o._diagnostic.verify.call_args.args[2])
        kwargs = o._history.save_solution.call_args.kwargs
        self.assertIn('machine_id', kwargs['conditions'])
        self.assertEqual(kwargs['verification_commands'], [{'command': 'id'}])
        self.assertEqual(o._session.data['outcome'], 'yes')
        self.assertTrue(any(step['stage'] == 'after_fix_conclusion' for step in o._session.data['steps']))

    def test_llm_interrupt_can_retry_or_cancel(self):
        o = self.make_orchestrator()
        o._session = None
        operation = Mock(side_effect=[KeyboardInterrupt, 'ok'])
        o._ui.ask_retry.return_value = True
        self.assertEqual(o._llm(operation, 'Working'), 'ok')
        o._ui.ask_retry.return_value = False
        with self.assertRaises(RequestCancelled):
            o._llm(Mock(side_effect=RuntimeError('offline')), 'Working')

    def test_legacy_solution_is_not_offered_for_reuse(self):
        o = self.make_orchestrator()
        legacy = {'conditions': {}, 'commands': [{'command': 'chmod 644 /tmp/file'}], 'notes': 'old'}
        o._history.summary.return_value = {'seen': 1, 'solved': 0, 'solutions': [legacy]}
        o._ui.choose_history_action.return_value = ('ignore', None)
        o._handle_error(ErrorEvent('/tmp/log', 'ERROR permissions /tmp/file', 'now'))
        self.assertFalse(legacy['applicable'])

    @patch('core.orchestrator.run_command', return_value=(False, 'failed'))
    def test_fix_sequence_stops_after_failure(self, execute):
        o = self.make_orchestrator()
        o._session = None
        o._apply_fixes([{'command': 'first'}, {'command': 'second'}])
        self.assertEqual(execute.call_count, 1)

    def test_grouped_errors_are_all_recorded_when_ignored(self):
        o = self.make_orchestrator()
        o._ui.show_error_panel.return_value = False
        event = ErrorEvent('/tmp/log', 'ERROR denied', 'now')
        o._batch = [event, event, event]
        o._handle_error(event)
        self.assertEqual(o._history.record_occurrence.call_count, 3)


class EvidenceTests(unittest.TestCase):
    def test_verdict_cannot_cite_unexecuted_command(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps({'status': 'confirmed', 'explanation': 'result', 'observations': [{'command': 'stat /tmp/file', 'result': 'exists', 'supports': 'file found'}]}))
        with self.assertRaisesRegex(ValueError, 'not executed'):
            agent.verify('error', 'hypothesis', '$ id\nStatus: success\nuid=1000')

    def test_verdict_links_executed_command_to_conclusion(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        observation = {'command': 'id', 'result': 'uid=1000', 'supports': 'ordinary user'}
        agent.call = Mock(return_value=json.dumps({'status': 'confirmed', 'explanation': 'result', 'observations': [observation], 'uncertainties': []}))
        result = agent.verify('error', 'hypothesis', '$ id\nStatus: success\nuid=1000')
        self.assertEqual(result['observations'], [observation])
