import unittest
from unittest.mock import Mock, patch
from agents.diagnostic import DiagnosticAgent
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent


class MissingPackageTests(unittest.TestCase):
    def verify(self, extra=(), after_fix=False):
        agent=DiagnosticAgent('test','http://localhost')
        agent.call=Mock(side_effect=AssertionError('Measured package state should not require an LLM'))
        captures=[{'command':'which -- screen','success':False,'selected_output':'(no output)'}] + list(extra)
        with patch('agents.diagnostic.system_context', return_value={'os_id':'debian'}), patch('agents.diagnostic.shutil.which', return_value='/usr/bin/apt-get'):
            result=agent.verify('ERROR: screen is not installed','screen is not installed','', separate=True, after_fix=after_fix,
                captures=captures, executed_commands=[x['command'] for x in captures])
        agent.call.assert_not_called()
        return result

    def test_failed_lookup_confirms_unavailable_executable_not_rejected_cause(self):
        result=self.verify()
        self.assertEqual(result['symptom']['status'],'confirmed')
        self.assertEqual(result['cause']['status'],'inconclusive')
        self.assertNotIn('repair_hint',result)

    def test_installed_package_with_missing_path_is_not_an_installation_remedy(self):
        result=self.verify([{'command':'dpkg-query --status screen','success':True,'selected_output':'Package: screen\nStatus: install ok installed'}])
        self.assertEqual(result['cause']['status'],'inconclusive')
        self.assertNotIn('repair_hint',result)

    def test_absent_package_and_repository_candidate_propose_installation(self):
        result=self.verify([{'command':'dpkg-query --status screen','success':False,'selected_output':"[stderr] dpkg-query: package 'screen' is not installed and no information is available"},
            {'command':'apt-cache policy screen','success':True,'selected_output':'screen:\n  Installed: (none)\n  Candidate: 4.9.1-1'}])
        self.assertEqual(result['cause']['status'],'confirmed')
        self.assertEqual(result['repair_hint']['command'],'apt-get install -y screen')
        o=Orchestrator.__new__(Orchestrator)
        o._last_verdict=result
        o._solver=Mock()
        o._ui=Mock()
        o._stage=Mock()
        o._note=Mock()
        fixes=o._propose_fixes(ErrorEvent('/tmp/log','ERROR: screen is not installed','now'),'measured evidence')
        self.assertEqual(fixes[0]['command'],'apt-get install -y screen')
        o._solver.solve.assert_not_called()
        o._ui.show_solution_panel.assert_not_called()  # Proposal is not an execution approval.

    def test_missing_repository_candidate_does_not_produce_install_hint(self):
        result=self.verify([{'command':'dpkg-query --status screen','success':False,'selected_output':"dpkg-query: package 'screen' is not installed and no information is available"},
            {'command':'apt-cache policy screen','success':True,'selected_output':'screen:\n  Installed: (none)\n  Candidate: (none)'}])
        self.assertEqual(result['cause']['status'],'confirmed')
        self.assertNotIn('repair_hint',result)

    def test_post_repair_missing_lookup_means_original_problem_still_present(self):
        self.assertEqual(self.verify(after_fix=True)['status'],'confirmed')

    def test_unknown_executable_to_package_mapping_is_not_assumed(self):
        agent=DiagnosticAgent('test','http://localhost')
        captures=[{'command':'which -- python','success':False,'selected_output':'(no output)'},
                  {'command':'dpkg-query --status python','success':False,'selected_output':"dpkg-query: package 'python' is not installed and no information is available"}]
        result=agent.verify('ERROR: python is not installed','python is not installed','',separate=True,
            captures=captures,executed_commands=[x['command'] for x in captures])
        self.assertEqual(result['cause']['status'],'inconclusive')
        self.assertNotIn('repair_hint',result)
