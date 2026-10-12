import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agents.diagnostic import DiagnosticAgent
from agents.solver import SolverAgent
from core.history import HistoryStore
from core.knowledge import render_commands, redact, local_check_results
from core.watcher import ErrorEvent
from test_history import TinyEmbedding
import test_pipeline_experience as fixtures


def verdict(symptom='confirmed', cause='confirmed', result='mode 0777', command='stat /tmp/test'):
    return {'symptom': {'status': symptom, 'explanation': 'Measured current symptom'},
            'cause': {'status': cause, 'description': 'Incorrect file mode', 'explanation': 'Current diagnostic evidence'},
            'observations': [{'command': command, 'result': result, 'supports': 'Measured mode'}], 'uncertainties': []}


class StructuredAgentTests(unittest.TestCase):
    def test_available_mc_does_not_confirm_historical_missing_program_report(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=AssertionError('Measured availability does not require a model verdict'))
        captures = [{'command':cmd, 'success':True, 'selected_output':'/usr/bin/mc'} for cmd in ['which -- mc', 'which mc']]
        for after_fix in [False, True]:
            result = agent.verify('ERROR: mc is not installed', 'mc is not installed', '', separate=True,
                after_fix=after_fix, captures=captures, executed_commands=[x['command'] for x in captures])
            self.assertEqual(result['symptom']['status'], 'not_reproduced')
            self.assertEqual(result['cause']['status'], 'rejected')
            self.assertEqual(result['status'], 'disproved')
            self.assertIn('current diagnostic PATH', result['explanation'])
        agent.call.assert_not_called()

    def test_lookup_for_another_program_does_not_disprove_mc_report(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        data = verdict(symptom='inconclusive', cause='inconclusive')
        data['observations'] = []
        agent.call = Mock(return_value=json.dumps(data))
        result = agent.verify('ERROR: mc is not installed', 'mc is not installed', '', separate=True,
            captures=[{'command':'which curl', 'success':True, 'selected_output':'/usr/bin/curl'}], executed_commands=['which curl'])
        self.assertEqual(result['status'], 'inconclusive')
        agent.call.assert_called_once()

    def test_successful_lookup_requires_the_reported_path_not_a_status_alone(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        data = verdict(symptom='inconclusive', cause='inconclusive')
        data['observations'] = []
        agent.call = Mock(return_value=json.dumps(data))
        result = agent.verify('ERROR: mc is not installed', 'mc is not installed', '', separate=True,
            captures=[{'command':'which mc', 'success':True, 'selected_output':'(no output)'}], executed_commands=['which mc'])
        self.assertEqual(result['status'], 'inconclusive')
        agent.call.assert_called_once()

    def test_symptom_can_be_confirmed_while_cause_is_rejected(self):
        data = DiagnosticAgent._parse_verdict(verdict(cause='rejected'))
        self.assertEqual(data['symptom']['status'], 'confirmed')
        self.assertEqual(data['cause']['status'], 'rejected')
        self.assertNotEqual(data['status'], 'confirmed')

    def test_fabricated_output_and_legacy_single_verdict_are_rejected(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps(verdict(result='invented mode 0644')))
        result = agent.verify('error', 'cause', '$ stat /tmp/test\nmode 0777', separate=True,
            executed_commands=['stat /tmp/test'], captures=[{'command':'stat /tmp/test', 'selected_output':'mode 0777'}])
        self.assertEqual(result['status'], 'inconclusive')
        self.assertIn('actual selected output', result['verification_issue'])
        self.assertEqual(result['observations'][0]['result'], 'mode 0777')
        agent.call = Mock(return_value=json.dumps({'status':'confirmed', 'explanation':'guess'}))
        result = agent.verify('error', 'cause', 'evidence', separate=True)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertIn('separate symptom', result['verification_issue'])

    def test_evidence_id_binds_verdict_to_actual_capture(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        data = verdict()
        data['observations'] = [{'evidence_id':'E1', 'supports':'File metadata reports this mode'}]
        agent.call = Mock(return_value=json.dumps(data))
        result = agent.verify('error', 'mode mismatch', '$ stat /tmp/test\nStatus: success\nmode 0777',
            executed_commands=['stat /tmp/test'], separate=True,
            captures=[{'command':'stat /tmp/test', 'selected_output':'mode 0777', 'success':True}])
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['observations'][0]['result'], 'mode 0777')
        self.assertEqual(agent.call.call_count, 1)
        self.assertIn('captured_evidence', agent.call.call_args.args[0])

    def test_unknown_evidence_id_and_rewritten_output_remain_inconclusive(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        for observation in [{'evidence_id':'E99', 'supports':'invented'},
                            {'evidence_id':'E1', 'result':'mode 0644', 'supports':'invented'},
                            {'evidence_id':'E1', 'command':'id', 'supports':'wrong command'}]:
            data = verdict()
            data['observations'] = [observation]
            agent.call = Mock(return_value=json.dumps(data))
            result = agent.verify('error', 'cause', '$ stat /tmp/test\nmode 0777', separate=True,
                executed_commands=['stat /tmp/test'], captures=[{'command':'stat /tmp/test', 'selected_output':'mode 0777'}])
            self.assertEqual(result['status'], 'inconclusive')

    def test_literal_quotes_tolerate_whitespace_without_changing_facts(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps(verdict(result='mode   0777')))
        result = agent.verify('error', 'cause', '$ stat /tmp/test\nmode 0777', separate=True,
            executed_commands=['stat /tmp/test'], captures=[{'command':'stat /tmp/test', 'selected_output':'mode\t0777'}])
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['observations'][0]['result'], 'mode\t0777')

    def test_manual_remedy_is_a_solution(self):
        agent = SolverAgent('test', 'http://localhost')
        agent.call = Mock(return_value=json.dumps({'manual_steps':['Correct the endpoint in the application settings'],
                                                  'description':'Use the configured endpoint', 'commands':[]}))
        solution = agent.solve('error', 'confirmed diagnosis')
        self.assertEqual(len(solution['manual_steps']), 1)
        self.assertEqual(solution['commands'], [])

    def test_template_target_is_quoted_and_repair_rejected(self):
        rendered = render_commands([{'command':'stat -- {target_path}'}], '/tmp/file with spaces')
        self.assertIn("'/tmp/file with spaces'", rendered[0]['command'])
        with self.assertRaises(ValueError):
            render_commands([{'command':'stat -- {target_path}'}], '/tmp/file; touch /tmp/unwanted')
        with self.assertRaises(ValueError):
            render_commands([{'command':'chmod 644 {target_path}'}], '/tmp/file')
        with self.assertRaises(ValueError):
            render_commands([{'command':'stat {unknown}'}], '/tmp/file')

    def test_redaction_and_local_criteria_preserve_unknown_states(self):
        text = redact('Authorization: Bearer secret-token\npassword=secret\nhttps://user:pass@example.org')
        self.assertNotIn('secret-token', text)
        self.assertNotIn('password=secret', text)
        self.assertNotIn('user:pass', text)
        checks = [{'kind':'file_mode', 'target':'/tmp/file', 'expected':'0644', 'basis':'explicit requirement'}]
        self.assertEqual(local_check_results(checks, {'/tmp/file':{'exists':False}})[0]['state'], 'unknown')
        self.assertEqual(local_check_results(checks, {'/tmp/file':{'exists':True, 'mode':'0777'}})[0]['state'], 'unexpected')


class StructuredHistoryTests(unittest.TestCase):
    def test_one_attempt_can_cover_many_events_and_feedback_can_be_corrected(self):
        with tempfile.TemporaryDirectory() as root:
            history = HistoryStore(root, TinyEmbedding())
            event = ErrorEvent('/tmp/log', 'ERROR wrong permissions /tmp/test', 'now')
            occurrences = [history.record_occurrence(event)[1] for _ in range(12)]
            key = history.errors.get()['ids'][0]
            sid = history.save_solution(key, [{'command':'chmod 644 /tmp/test'}], 'Correct mode', 'user')
            history.record_attempt(key, occurrences[0], sid, 'yes', attempt_id='application-1', occurrence_ids=occurrences)
            self.assertEqual(history.summary(key)['solutions'][0]['helped'], 1)
            self.assertEqual(history.summary(key)['solved'], 12)
            history.update_feedback('application-1', 'no')
            summary = history.summary(key)
            self.assertEqual((summary['solutions'][0]['helped'], summary['solutions'][0]['failed']), (0, 1))
            self.assertEqual(summary['failed'], 12)
            row = history.problem_details(key)['attempts'][0]
            self.assertEqual([item['outcome'] for item in row['feedback_history']], ['yes', 'no'])
            self.assertEqual(history.attempts.count(), 1)

    def test_playbook_reuse_and_cross_environment_reference(self):
        with tempfile.TemporaryDirectory() as root:
            history = HistoryStore(root, TinyEmbedding())
            error = 'ERROR wrong permissions /tmp/example'
            id_ = history.save_playbook(error, {'hypothesis':'Mode mismatch', 'commands':[{'command':'stat /tmp/example'}]}, verdict(), case_id='case1')
            saved = history.list_playbooks()[0]
            self.assertEqual(saved['commands'][0]['command'], 'stat {target_path}')
            self.assertEqual(history.diagnostic_candidates('ERROR wrong permissions /tmp/other')[0]['id'], id_)
            history.set_playbook_active(id_, False)
            self.assertEqual(history.diagnostic_candidates(error), [])
            a, _ = history.record_occurrence(ErrorEvent('/tmp/log', error, 'now'))
            other = HistoryStore(root, TinyEmbedding(), dict(history.context, machine_id='other'))
            other.record_occurrence(ErrorEvent('/tmp/log', error, 'now'))
            self.assertEqual(history.related(error, a), [])
            self.assertEqual(len(history.related(error, a, include_other_environments=True)), 1)


class StructuredFlowTests(unittest.TestCase):
    setUp = fixtures.PipelineExperienceTests.setUp
    run_case = fixtures.PipelineExperienceTests.run_case
    # Reuse the real temporary database/command fixtures without duplicating the
    # original inherited tests in this additional suite.
    def test_reused_solution_always_diagnoses_before_repair(self):
        key, _ = self.history.record_occurrence(self.event)
        sid = self.history.save_solution(key, [{'command':f'chmod 644 {self.target}'}], 'Old fix', 'user')
        self.o._ui.choose_history_action.return_value = ('reuse', self.history.get_solution(sid))
        self.run_case()
        self.o._diagnostic.diagnose.assert_called_once()
        self.o._solver.solve.assert_called_once()
        self.assertEqual(self.o._diagnostic.verify.call_count, 2)

    def test_rejected_cause_leaves_confirmed_symptom_open(self):
        self.o._diagnostic.verify.side_effect = None
        self.o._diagnostic.verify.return_value = DiagnosticAgent._parse_verdict(verdict(cause='rejected'))
        self.o._ui.choose_diagnostic_next.return_value = 'watch'
        case = self.run_case()
        self.o._solver.solve.assert_not_called()
        self.assertEqual(case['symptom_assessment']['status'], 'confirmed')
        self.assertEqual(case['cause_assessment']['status'], 'rejected')
        self.assertEqual(case['status'], 'watching')

    def test_unverifiable_model_verdict_offers_next_steps_without_a_fix(self):
        command = f'stat -L -- {self.target}'
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=[
            json.dumps({'hypothesis':'Incorrect file mode', 'commands':[{'command':command}]}),
            json.dumps(verdict(command=command, result='invented mode 0644')),
            json.dumps(verdict(command=command, result='invented mode 0644'))])
        self.o._diagnostic = agent
        self.o._ui.choose_diagnostic_next.return_value = 'watch'
        case = self.run_case()
        self.assertEqual(case['status'], 'watching')
        self.assertEqual(case['symptom_assessment']['status'], 'inconclusive')
        self.o._ui.choose_diagnostic_next.assert_called_once()
        self.o._solver.solve.assert_not_called()
        self.assertEqual(self.history.attempts.count(), 0)

    def test_grouped_events_have_one_vote_per_application(self):
        self.o._batch = [self.event] * 8
        case = self.run_case()
        summary = self.history.summary(case['error_key'])
        self.assertEqual(summary['seen'], 8)
        self.assertEqual(summary['solved'], 8)
        self.assertEqual(summary['attempts'], 1)
        self.assertEqual(summary['solutions'][0]['helped'], 1)

    def test_corrected_feedback_updates_case_without_an_extra_vote(self):
        case = self.run_case()
        self.history.update_feedback(case['attempt_id'], 'no')
        revised = self.history.case_records()[0]
        self.assertEqual(revised['status'], 'needs_review')
        self.assertEqual(revised['outcome'], 'no')
        self.assertEqual(self.history.attempts.count(), 1)
        solution = self.history.summary(case['error_key'])['solutions'][0]
        self.assertEqual((solution['helped'], solution['failed']), (0, 1))

    def test_real_agent_schema_plan_evidence_and_after_fix_verification(self):
        command = f'stat -L -- {self.target}'
        self.event = ErrorEvent('/tmp/log', f'ERROR wrong permissions {self.target}: mode 0777, expected 0644', self.event.timestamp)
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=[json.dumps({'hypothesis':'Incorrect file mode', 'commands':[{'command':command}],
            'verification_checks':[{'kind':'file_mode','target':str(self.target),'expected':'0644','basis':'expected 0644 in the error'}]}),
            json.dumps(verdict(command=command)), json.dumps(verdict(symptom='not_reproduced', result='mode 0644', command=command))])
        self.o._diagnostic = agent
        # This test covers the LLM schema path even when a bundled local plan
        # could now handle the explicit mode requirement.
        with patch.object(self.history, 'linux_diagnostic_plan', return_value=None):
            case = self.run_case()
        self.assertEqual(case['status'], 'resolved')
        self.assertEqual(case['symptom_assessment']['status'], 'confirmed')
        self.assertEqual(case['verification']['symptom']['status'], 'not_reproduced')
        self.assertEqual(self.history.playbooks.count(), 1)
        self.assertEqual(agent.call.call_count, 3)
