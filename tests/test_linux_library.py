import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.diagnostic_commands import diagnostic_argv
from core.history import HistoryStore
from core.linux_library import ENTRIES, match_entry, build_plan, evaluate
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent
from test_history import TinyEmbedding
import test_pipeline_experience as fixtures

CONTEXT = {'machine_id': 'library-test', 'os': 'Debian GNU/Linux 13', 'os_id': 'debian', 'architecture': 'x86_64', 'uid': 1000}


def plan_for(error):
    row = match_entry(ENTRIES, error, CONTEXT)
    return build_plan(row, error, CONTEXT) if row else None


class LinuxLibraryTests(unittest.TestCase):
    def test_import_is_idempotent_and_does_not_invent_occurrences_or_ratings(self):
        with tempfile.TemporaryDirectory() as root:
            store = HistoryStore(root, TinyEmbedding(), context=CONTEXT)
            self.assertEqual(len(store.list_linux_library()), 25)
            self.assertEqual((store.errors.count(), store.occurrences.count(), store.solutions.count(), store.attempts.count()), (0, 0, 0, 0))
            key, occurrence = store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR custom problem', 'now'))
            sid = store.save_solution(key, [], 'Manual fix', 'user')
            store.record_attempt(key, occurrence, sid, 'yes')
            id_ = ENTRIES[0]['id']
            store.set_linux_reference_active(id_, False)
            reopened = HistoryStore(root, TinyEmbedding(), context=CONTEXT)
            self.assertEqual(reopened.seed_linux_library(), 0)
            self.assertEqual(reopened.linux_library.count(), 25)
            self.assertFalse(next(row for row in reopened.list_linux_library() if row['id'] == id_)['active'])
            self.assertEqual(reopened.summary(key)['solutions'][0]['helped'], 1)
            self.assertEqual(reopened.summary(key)['seen'], 1)

    def test_category_matching_keeps_unrelated_problems_separate(self):
        for error, category in [('kernel: Out of memory: Kill process 42 (python3)', 'memory'),
                                ('kernel: watchdog: BUG: soft lockup - CPU#2 stuck for 26s!', 'kernel'),
                                ('ERROR: screen is not installed.', 'packages'),
                                ('ERROR: Permission denied reading /tmp/test.conf.', 'permissions'),
                                ('ERROR: Connection refused while connecting to 127.0.0.1:8080.', 'network')]:
            self.assertEqual(plan_for(error)['library_category'], 'default-linux-' + category)
        self.assertIsNone(plan_for('ERROR: unknown frobnicator failure'))
        self.assertIsNone(plan_for('ERROR: Connection refused and permission denied reading /tmp/test.conf'))
        self.assertIsNone(plan_for('ERROR: Wrong permissions for /tmp/test.conf'))  # No policy supplied.
        self.assertIsNone(plan_for('ERROR: File /tmp/a does not exist and file /tmp/b does not exist'))
        self.assertIsNone(plan_for('ERROR: Permission denied with no target specified'))

    def test_specific_cgroup_rule_overrides_host_oom_without_cross_category_match(self):
        plan = plan_for('kernel: Memory cgroup out of memory: Killed process 42 (python3)')
        self.assertEqual(plan['library_id'], 'linux-x64-v1-cgroup-oom')

    def test_untrusted_mutation_disabled_entry_and_other_architecture_do_not_execute(self):
        rows = copy.deepcopy(ENTRIES)
        row = next(item for item in rows if item['id'].endswith('-missing-command'))
        row['active'] = False
        self.assertIsNone(match_entry(rows, 'ERROR screen is not installed', CONTEXT))
        row['active'] = True
        row['commands'][0]['command'] = 'chmod 777 /etc/resolv.conf'
        self.assertIsNone(match_entry(rows, 'ERROR screen is not installed', CONTEXT))
        self.assertIsNone(match_entry(ENTRIES, 'ERROR screen is not installed', dict(CONTEXT, architecture='aarch64')))
        self.assertIsNone(match_entry(ENTRIES, 'ERROR screen is not installed', dict(CONTEXT, os_id='')))

    def test_every_bundled_plan_has_only_allowed_read_only_commands(self):
        with patch('core.linux_library.shutil.which', return_value='/usr/bin/tool'):
            for row in ENTRIES:
                plan = build_plan(row, row['symptom'], CONTEXT)
                self.assertIsNotNone(plan, row['id'])
                for command in plan['commands']:
                    diagnostic_argv(command['command'])
        for command in ['journalctl --vacuum-time=1s', 'journalctl --rotate', 'systemctl restart example.service',
                        'systemctl show --property=ExecStart example.service', 'findmnt --poll', 'findmnt --output-all']:
            with self.assertRaises(ValueError, msg=command):
                diagnostic_argv(command)

    def test_mode_and_missing_path_require_fresh_exact_target_evidence(self):
        plan = plan_for('ERROR: Wrong permissions for /tmp/test.conf: mode 0777, expected 0644.')
        command = plan['commands'][0]['command']
        self.assertEqual(evaluate(plan, [{'command':command, 'success':True, 'full_output':'777', 'selected_output':'777'}], '')['status'], 'confirmed')
        self.assertEqual(evaluate(plan, [{'command':command, 'success':True, 'full_output':'644', 'selected_output':'644'}], '', after_fix=True)['status'], 'disproved')
        self.assertIsNone(evaluate(plan, [{'command':command, 'success':False, 'full_output':'Permission denied'}], ''))
        self.assertIsNone(evaluate(plan, [{'command':'stat -L --format=%a -- /tmp/other', 'success':True, 'full_output':'777'}], ''))
        plan = plan_for('ERROR: File /tmp/test.conf does not exist.')
        command = plan['commands'][0]['command']
        self.assertEqual(evaluate(plan, [{'command':command, 'success':False, 'full_output':'stat: No such file or directory'}], '')['status'], 'confirmed')
        self.assertIsNone(evaluate(plan, [{'command':command, 'success':False, 'full_output':'stat: Permission denied'}], ''))
        self.assertEqual(evaluate(plan, [{'command':command, 'success':True, 'full_output':'644'}], '')['status'], 'disproved')

    def test_current_free_memory_does_not_disprove_a_historical_oom(self):
        plan = plan_for('kernel: Out of memory: Kill process 42 (python3)')
        self.assertIsNone(evaluate(plan, [{'command':'free -m', 'success':True, 'full_output':'Mem: 16000 1000 15000'}], ''))

    def test_tcp_listener_does_not_confirm_a_udp_or_unspecified_bind_conflict(self):
        for protocol in ['UDP ', '']:
            error = 'ERROR: ' + protocol + 'bind 127.0.0.1:8080 failed: Address already in use.'
            plan = plan_for(error)
            captures = [{'command':'ss -H -ltn', 'success':True, 'full_output':'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*'}]
            self.assertIsNone(evaluate(plan, captures, error))
        error = 'ERROR: TCP bind 127.0.0.1:8080 failed: Address already in use.'
        self.assertEqual(evaluate(plan_for(error), captures, error)['status'], 'confirmed')

    def test_dns_template_uses_reported_hostname_and_unambiguous_target(self):
        plan = plan_for('ERROR: Failed to resolve hostname api.example.invalid: Temporary failure in name resolution.')
        self.assertEqual(plan['commands'][0]['command'], 'getent ahosts api.example.invalid')
        self.assertIsNone(plan_for('ERROR: Temporary failure in name resolution.'))

    def test_exact_saved_plan_reuse_needs_same_environment_and_target(self):
        with tempfile.TemporaryDirectory() as root:
            store = HistoryStore(root, TinyEmbedding(), context=CONTEXT)
            error = 'ERROR: Invalid widget schema in /tmp/test.conf'
            store.save_playbook(error, {'hypothesis':'Inspect widget schema', 'commands':[{'command':'stat -- /tmp/test.conf'}]}, {}, source='user')
            plan = store.linux_diagnostic_plan(error)
            self.assertEqual(plan['library_category'], 'saved-diagnostic-scenario')
            self.assertEqual(plan['commands'][0]['command'], 'stat -- /tmp/test.conf')
            self.assertIsNone(store.linux_diagnostic_plan('ERROR: Invalid widget schema in /tmp/other.conf'))
            store.context = dict(CONTEXT, uid=0)
            self.assertIsNone(store.linux_diagnostic_plan(error))


class LocalWorkflowTests(unittest.TestCase):
    setUp = fixtures.PipelineExperienceTests.setUp

    def test_confirmed_mode_guidance_and_fresh_verification_use_no_llm_or_automatic_fix(self):
        self.event = ErrorEvent('/tmp/log', f'ERROR: Wrong permissions for {self.target}: mode 0777, expected 0644.', 'now')
        self.o._diagnostic.diagnose = Mock(side_effect=AssertionError('Local plan should bypass the LLM'))
        self.o._diagnostic.verify = Mock(side_effect=AssertionError('Measured modes should bypass the LLM'))
        self.o._solver.solve = Mock(side_effect=AssertionError('Use reference guidance'))
        def completed_manual_action(*args):
            self.target.chmod(0o644)  # Simulate the user performing the displayed guidance.
            return True
        self.o._ui.show_manual_solution.side_effect = completed_manual_action
        with patch('core.orchestrator.run_command') as repairs:
            self.o._handle_error(self.event)
            repairs.assert_not_called()
        case = self.history.case_records()[0]
        self.assertEqual(case['status'], 'resolved')
        self.assertEqual(case['verification']['assessment_source'], 'local_linux_library')
        self.o._diagnostic.diagnose.assert_not_called()
        self.o._diagnostic.verify.assert_not_called()
        self.o._solver.solve.assert_not_called()
        solution = self.history.summary(case['error_key'])['solutions'][0]
        self.assertEqual(solution['source'], 'bundled-linux-reference')
        self.assertEqual(solution['commands'], [])
        self.assertEqual(solution['helped'], 1)
        self.assertTrue(solution['manual_steps'])

    def test_read_only_command_denial_prevents_local_assessment_and_repairs(self):
        self.event = ErrorEvent('/tmp/log', f'ERROR: File {self.root / "absent.conf"} does not exist.', 'now')
        self.o._ui.show_diagnostic_panel.return_value = False
        with patch('core.executor.run_command') as commands:
            self.o._handle_error(self.event)
            commands.assert_not_called()
        self.o._diagnostic.diagnose.assert_not_called()
        self.o._solver.solve.assert_not_called()
        self.assertEqual(self.history.attempts.count(), 0)

    def test_complex_known_case_uses_local_plan_but_falls_back_to_llm_for_evidence(self):
        self.event = ErrorEvent('/tmp/log', 'kernel: Out of memory: Kill process 42 (python3)', 'now')
        self.o._diagnostic.verify.side_effect = None
        self.o._diagnostic.verify.return_value = {'status':'inconclusive', 'explanation':'Current output does not establish a root cause', 'observations':[]}
        self.o._ui.choose_diagnostic_next.return_value = 'watch'
        with patch('core.executor.run_command', return_value=(True, 'Current evidence')):
            self.o._handle_error(self.event)
        self.o._diagnostic.diagnose.assert_not_called()
        self.o._diagnostic.verify.assert_called_once()
        self.o._solver.solve.assert_not_called()
        self.assertEqual(self.history.case_records()[0]['status'], 'watching')
