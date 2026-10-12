import tempfile
import unittest
from unittest.mock import patch

from core.history import HistoryStore
from core.problem_actions import groups, save_groups, save_policy, policy, process, send_notification
from core.reporting import SessionReport
from core.watcher import ErrorEvent
from tests.test_history import TinyEmbedding


class ProblemActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HistoryStore(self.tmp.name, TinyEmbedding())
        self.event = ErrorEvent('/tmp/log', 'ERROR service example failed', 'now')
        self.key, _ = self.store.record_occurrence(self.event)
        self.sid = self.store.save_solution(self.key, [{'command': 'echo repair'}, {'command': 'echo done'}], 'Repair', 'user')

    def report(self):
        return SessionReport(self.event, self.store.context)

    def test_settings_and_groups_persist(self):
        save_groups(self.store, [{'Group': 'Operators', 'Email addresses': 'one@example.com, two@example.com'}])
        save_policy(self.store, self.key, True, 'Operators', self.sid)
        reopened = HistoryStore(self.tmp.name, TinyEmbedding())
        self.assertTrue(policy(reopened, self.key)['automate'])
        self.assertEqual(groups(reopened)['Operators'], ['one@example.com', 'two@example.com'])
        with self.assertRaises(ValueError):
            save_groups(self.store, [{'Group': 'Bad', 'Email addresses': 'bad\r\nBcc: another@example.com'}])

    def test_exact_match_execution_and_trace_without_false_success_rating(self):
        save_policy(self.store, self.key, True, 'None', self.sid)
        report = self.report()
        with patch('core.problem_actions.run_command', return_value=(True, 'captured output')) as run:
            self.assertTrue(process(self.store, self.event, report))
            self.assertEqual(run.call_count, 2)
            different = ErrorEvent('/tmp/log', 'ERROR different service failed', 'now')
            self.assertFalse(process(self.store, different, SessionReport(different, self.store.context)))
            self.assertEqual(run.call_count, 2)
        report.finish()
        self.store.save_investigation(report.data)
        trace = self.store.case_records()[0]
        self.assertIn('captured output', str(trace['steps']))
        self.assertEqual(trace['status'], 'unresolved')
        self.assertEqual(self.store.summary(self.key)['solutions'][0]['helped'], 0)

    def test_failure_stops_remaining_commands_and_notification_is_logged(self):
        save_groups(self.store, [{'Group': 'Ops', 'Email addresses': 'ops@example.com'}])
        save_policy(self.store, self.key, True, 'Ops', self.sid)
        report = self.report()
        with patch('core.problem_actions.run_command', return_value=(False, 'repair failed')) as run, patch('core.problem_actions.send_notification', side_effect=OSError('SMTP unavailable')):
            self.assertTrue(process(self.store, self.event, report))
            run.assert_called_once()
        self.assertEqual(report.data['steps'][-1]['data']['status'], 'failed')
        self.assertIn('SMTP unavailable', str(report.data['steps']))

    def test_obsolete_solution_is_never_run(self):
        save_policy(self.store, self.key, True, 'None', self.sid)
        self.store.set_obsolete(self.sid)
        report = self.report()
        with patch('core.problem_actions.run_command') as run:
            self.assertTrue(process(self.store, self.event, report))
            run.assert_not_called()
        self.assertEqual(report.data['steps'][-1]['stage'], 'automation_failed')

    def test_inform_only_preserves_manual_flow(self):
        save_groups(self.store, [{'Group': 'Ops', 'Email addresses': 'ops@example.com'}])
        save_policy(self.store, self.key, False, 'Ops')
        report = self.report()
        with patch('core.problem_actions.send_notification') as send, patch('core.problem_actions.run_command') as run:
            self.assertFalse(process(self.store, self.event, report))
            send.assert_called_once()
            run.assert_not_called()
        self.assertEqual(report.data['steps'][-1]['data']['status'], 'sent')

    def test_orchestrator_bypasses_terminal_prompts_and_persists_trace(self):
        from unittest.mock import Mock
        from core.orchestrator import Orchestrator
        save_policy(self.store, self.key, True, 'None', self.sid)
        coordinator = Orchestrator.__new__(Orchestrator)
        coordinator._history = self.store
        coordinator._system = self.store.context
        coordinator._session = self.report()
        coordinator._ui = Mock()
        coordinator._stage = Mock()
        coordinator._process_error = Mock()
        coordinator._retain_repeats = Mock()
        with patch('core.problem_actions.run_command', return_value=(True, 'output')):
            coordinator._review_error(self.event)
        coordinator._process_error.assert_not_called()
        coordinator._ui.ask_export.assert_not_called()
        self.assertTrue(self.store.case_records()[0]['automated'])

    def test_deleting_problem_removes_automation_settings(self):
        save_policy(self.store, self.key, True, 'None', self.sid)
        self.store.delete_problem(self.key)
        self.assertFalse(policy(self.store, self.key)['automate'])

    def test_smtp_tls_and_recipient_failure(self):
        with patch.dict('os.environ', {'LOGENT_SMTP_HOST': 'smtp.example.com', 'LOGENT_SMTP_FROM': 'logent@example.com'}, clear=True), patch('core.problem_actions.smtplib.SMTP') as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            send_notification(['ops@example.com'], self.event, self.report())
            client.starttls.assert_called_once()
            self.assertEqual(client.send_message.call_args.args[0]['To'], 'ops@example.com')
            client.send_message.return_value = {'ops@example.com': (550, b'rejected')}
            with self.assertRaises(RuntimeError):
                send_notification(['ops@example.com'], self.event, self.report())
