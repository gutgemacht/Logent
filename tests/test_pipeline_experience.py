import json
import queue
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core.event_grouping import EventInbox
from core.history import HistoryStore
from core.log_sources import parse_sources, save_sources
from core.monitoring import MonitorState, monitor_snapshots
from core.orchestrator import Orchestrator
from core.reporting import SessionReport, PIPELINE
from core.watchdog_config import save_rules
from core.watcher import ErrorEvent, LogEventHandler
from agents.diagnostic import DiagnosticAgent
from test_history import TinyEmbedding


class PipelineExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'target.conf'
        self.target.write_text('fixture')
        self.target.chmod(0o777)
        self.history = HistoryStore(str(self.root / 'db'), TinyEmbedding())
        self.event = ErrorEvent('/tmp/log', f'ERROR wrong permissions {self.target}', datetime.now(timezone.utc).isoformat(), ('error',))
        self.o = Orchestrator.__new__(Orchestrator)
        self.o._enhanced = True
        self.o._system = self.history.context
        self.o._session = None
        self.o._history = self.history
        self.o._checks = []
        self.o._ui = MagicMock()
        self.o._ui.show_error_panel.return_value = True
        self.o._ui.choose_history_action.return_value = ('diagnose', None)
        self.o._ui.show_diagnostic_panel.return_value = True
        self.o._ui.show_solution_panel.return_value = True
        self.o._ui.ask_outcome.return_value = 'yes'
        self.o._ui.ask_save_solution.return_value = 'save'
        self.o._ui.ask_export.return_value = False
        self.o._llm = lambda operation, label: operation()
        self.o._diagnostic = Mock()
        self.o._diagnostic.diagnose.return_value = {'hypothesis': 'Incorrect permissions', 'commands': [{'command': f'stat {self.target}', 'description': 'Inspect mode'}]}
        self.o._diagnostic.verify.side_effect = [
            {'status': 'confirmed', 'explanation': 'mode 0777 differs from required 0644', 'observations': []},
            {'status': 'disproved', 'explanation': 'mode is now 0644', 'observations': []},
        ]
        self.o._solver = Mock()
        self.o._solver.solve.return_value = [{'command': f'chmod 644 {self.target}', 'description': 'Correct mode', 'sudo': False, 'criticality': 'warning'}]
        self.source = queue.Queue()
        self.o._inbox = EventInbox(self.source)

    def run_case(self):
        def inspect(*args, **kwargs):
            return True, 'noise\n' * 500 + f'{self.target} mode {self.target.stat().st_mode & 0o777:04o}'
        def fix(*args, **kwargs):
            self.target.chmod(0o644)
            return True, 'Mode changed'
        with patch('core.executor.run_command', side_effect=inspect), patch('core.orchestrator.run_command', side_effect=fix):
            self.o._handle_error(self.event)
        return self.history.case_records()[0]

    def test_case_persists_evidence_actions_stages_and_verified_rating(self):
        case = self.run_case()
        self.assertEqual(case['status'], 'resolved')
        self.assertEqual(case['conclusion'], 'Confirmed by evidence')
        self.assertEqual(case['triggers'], ['error'])
        self.assertTrue(case['case_number'].startswith('Case #'))
        stages = [step['data']['stage'] for step in case['steps'] if step['stage'] == 'pipeline_stage']
        self.assertEqual(set(stages), set(PIPELINE))
        capture = next(step['data'][0] for step in case['steps'] if step['stage'] == 'diagnostic_output_complete')
        self.assertTrue(capture['shortened'])
        self.assertGreater(len(capture['full_output']), len(capture['selected_output']))
        self.assertIn('mode 0777', capture['selected_output'])
        solution = self.history.summary(case['error_key'])['solutions'][0]
        self.assertEqual(solution['helped'], 1)
        self.assertEqual(solution['verified_successes'], 1)
        self.assertIsNotNone(solution['last_verified'])
        self.assertEqual(self.history.problem_details(case['error_key'])['attempts'][0]['case_id'], case['id'])

    def test_skip_feedback_still_saves_an_untested_solution(self):
        self.o._ui.ask_outcome.return_value = 'skipped'
        case = self.run_case()
        self.assertEqual(case['outcome'], 'skipped')
        self.assertEqual(case['status'], 'resolved')  # Approved checks resolved the original symptom.
        summary = self.history.summary(case['error_key'])
        self.assertEqual(summary['solved'], 0)  # User-reported statistics stay separate.
        self.assertEqual(summary['solutions'][0]['helped'], 0)
        self.assertEqual(summary['solutions'][0]['verified_successes'], 1)
        self.assertIsNone(summary['solutions'][0]['success_rate'])
        self.assertEqual(self.history.attempts.count(), 1)  # Application retained without inventing a rating.

    def test_ignore_retains_case_but_skips_report_dialog(self):
        self.o._ui.show_error_panel.return_value = False
        self.o._handle_error(self.event)
        self.assertEqual(self.history.case_records()[0]['status'], 'ignored')
        self.o._ui.ask_export.assert_not_called()
        self.o._diagnostic.diagnose.assert_not_called()

    def test_arrivals_during_investigation_are_grouped_without_inventing_outcomes(self):
        other = ErrorEvent('/tmp/log', 'ERROR disk full', self.event.timestamp)
        def approve(*args, **kwargs):
            self.source.put(other)
            self.source.put(self.event)
            return True
        self.o._ui.show_solution_panel.side_effect = approve
        case = self.run_case()
        self.assertEqual(case['incident_count'], 2)
        summary = self.history.summary(case['error_key'])
        self.assertEqual((summary['seen'], summary['solved'], summary['unreported']), (2, 1, 1))
        self.assertEqual(self.o._inbox.next_batch()[0], other)

    def test_feedback_and_verification_conflict_is_visible(self):
        self.o._ui.ask_outcome.return_value = 'no'
        case = self.run_case()
        self.assertEqual(case['status'], 'needs_review')
        self.assertTrue(case['conflicting_outcomes'])
        self.assertEqual(case['outcome'], 'no')
        self.assertEqual(case['verification']['status'], 'disproved')


class MonitoringExperienceTests(unittest.TestCase):
    def test_conclusive_diagnosis_requires_a_real_command_citation(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=[
            json.dumps({'status': 'confirmed', 'explanation': 'guess'}),
            json.dumps({'status': 'confirmed', 'explanation': 'uid is 1000', 'observations': [
                {'command': 'id', 'result': 'uid=1000', 'supports': 'ordinary user'}]}),
        ])
        verdict = agent.verify('error', 'user mismatch', '$ id\nStatus: success\nuid=1000', executed_commands=['id'])
        self.assertEqual(verdict['observations'][0]['command'], 'id')
        self.assertEqual(agent.call.call_count, 2)
        agent.call = Mock(return_value=json.dumps({'status': 'confirmed', 'explanation': 'invented', 'observations': [
            {'command': 'stat /tmp/fake', 'result': 'exists', 'supports': 'exists'}]}))
        with self.assertRaisesRegex(ValueError, 'not executed'):
            agent.verify('error', 'hypothesis', '$ id\nStatus: success\n$ stat /tmp/fake\ninjected stdout', executed_commands=['id'])

    def test_ignore_rule_records_trace_and_does_not_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / 'rules.json'
            log = root / 'test.log'
            log.write_text('')
            save_rules([dict(enabled=True, kind='Keyword', pattern='error'),
                        dict(enabled=True, kind='Keyword', pattern='scheduled test', action='Ignore')], rules)
            events = queue.Queue()
            handler = LogEventHandler(events, [], frozenset([str(log)]), frozenset(), str(rules))
            trace = Mock()
            handler.event_callback = trace
            log.write_text('ERROR scheduled test\nERROR unexpected failure\n')
            handler._scan_new_lines(str(log))
            self.assertEqual(events.get_nowait().triggers, ('error',))
            self.assertTrue(events.empty())
            self.assertTrue(trace.call_args_list[0].kwargs['ignored'])
            self.assertEqual(trace.call_args_list[0].args[0]['ignore_rules'], ['scheduled test'])
            self.assertFalse(trace.call_args_list[1].kwargs['ignored'])

    def test_heartbeat_distinguishes_running_stale_and_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'test.log'
            log.write_text('')
            events = queue.Queue()
            events.put(ErrorEvent(str(log), 'ERROR test', 'now'))
            state = MonitorState(root / 'runtime', [str(log), str(root / 'missing')], 'rules.json', events)
            state.start()
            self.addCleanup(state.stop)
            state.detected({'error': 'ERROR test', 'source': str(log), 'detected': 'now', 'triggers': ['error']})
            state.flush()
            snapshot = monitor_snapshots(root / 'runtime')[0]
            self.assertTrue(snapshot['live'])
            self.assertEqual(snapshot['detected_count'], 1)
            self.assertEqual(snapshot['queued_count'], 1)
            self.assertEqual(snapshot['sources'][1]['Status'], 'Missing')
            with patch('core.monitoring.time.time', return_value=snapshot['heartbeat'] + 15):
                self.assertFalse(monitor_snapshots(root / 'runtime')[0]['live'])
            state.stop()
            self.assertFalse(monitor_snapshots(root / 'runtime')[0]['live'])

    def test_grouping_collects_nonconsecutive_repeats_and_respects_window(self):
        events = queue.Queue()
        now = datetime.now(timezone.utc)
        first = ErrorEvent('/tmp/log', 'ERROR pid=1 test', now.isoformat())
        other = ErrorEvent('/tmp/log', 'ERROR different', now.isoformat())
        repeat = ErrorEvent('/tmp/log', 'ERROR pid=2 test', (now + timedelta(seconds=10)).isoformat())
        later = ErrorEvent('/tmp/log', 'ERROR pid=3 test', (now + timedelta(minutes=10)).isoformat())
        for event in [first, other, repeat, later]:
            events.put(event)
        inbox = EventInbox(events)
        self.assertEqual(inbox.next_batch(group_window_seconds=300), [first, repeat])
        self.assertEqual(inbox.next_batch(group_window_seconds=300), [other])
        self.assertEqual(inbox.next_batch(group_window_seconds=300), [later])

    def test_source_edits_validate_before_replacing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'logs.lst'
            path.write_text('original.log\n')
            with self.assertRaises(ValueError):
                save_sources('# empty\n', path)
            self.assertEqual(path.read_text(), 'original.log\n')
            saved = save_sources('# comment\nrelative.log\n/tmp/absolute.log\n', path)
            self.assertEqual(saved, [str(Path(directory) / 'relative.log'), '/tmp/absolute.log'])
            self.assertEqual(parse_sources(path.read_text(), path), saved)
