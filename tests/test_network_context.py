import json
import tempfile
import unittest
from unittest.mock import Mock

from agents.diagnostic import DiagnosticAgent
from core.diagnostic_commands import diagnostic_argv
from core.error_context import compatible_errors, problem_kind, reported_paths, validate_solution_context
from core.history import HistoryStore
from core.orchestrator import Orchestrator
from core.watcher import ErrorEvent
from test_history import TinyEmbedding

TIMEOUT = 'ERROR: Connection to 127.0.0.1:8080 timed out after 30 seconds.'
PERMISSIONS = 'ERROR: Wrong permissions for /etc/resolv.conf 777.'


class NetworkContextTests(unittest.TestCase):
    def test_distinct_network_and_filesystem_categories(self):
        cases = [(TIMEOUT, 'connection_timeout'), ('ERROR Connection refused', 'connection_refused'),
                 ('ERROR Temporary failure in name resolution', 'dns_resolution'),
                 ('ERROR Address already in use', 'address_in_use'), ('ERROR No space left on device', 'disk_full'),
                 ('ERROR Unit logent-test.service not found', 'missing_service')]
        for error, expected in cases:
            self.assertEqual(problem_kind(error), expected)
            self.assertFalse(compatible_errors(error, PERMISSIONS))
        self.assertFalse(compatible_errors(TIMEOUT, 'ERROR unknown issue'))

    def test_urls_do_not_become_filesystem_paths(self):
        self.assertEqual(reported_paths('ERROR connection timeout https://api.example.com/path'), [])

    def test_permission_changes_rejected_for_network_errors(self):
        for command in ['sudo chmod 644 /etc/resolv.conf', 'chown root /tmp/file', 'setfacl -m u:test:r /tmp/file', 'chgrp root /tmp/file']:
            with self.assertRaisesRegex(ValueError, 'connection timeout'):
                validate_solution_context(TIMEOUT, [{'command': command}])

    def test_read_only_network_commands_allowed(self):
        for command in ['ss -ltnp', 'ss -tn state syn-sent', 'ip route get 127.0.0.1', 'ip address show', 'ip link show', 'getent hosts localhost', 'netstat -ltn']:
            self.assertTrue(diagnostic_argv(command))

    def test_mutating_network_commands_rejected(self):
        for command in ['ss -K', 'ss -tK', 'ss --kill', 'ss --kil', 'ss -D /tmp/dump', 'ss --diag=/tmp/dump',
                        'ip link set lo down', 'ip route add default via 1.2.3.4', 'ip addr flush dev lo']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                diagnostic_argv(command)

    def test_network_plan_retries_filesystem_only_inspection(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=[json.dumps({'hypothesis': 'file permissions', 'commands': [{'command': 'stat /etc/resolv.conf'}]}),
                                       json.dumps({'hypothesis': 'connection timeout', 'commands': [{'command': 'ss -tn'}]})])
        self.assertEqual(agent.diagnose(TIMEOUT)['commands'][0]['command'], 'ss -tn')
        self.assertEqual(agent.call.call_count, 2)

    def test_chroma_does_not_return_permissions_for_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(directory, TinyEmbedding())
            key, _ = store.record_occurrence(ErrorEvent('/tmp/log', TIMEOUT, 'now'))
            store.record_occurrence(ErrorEvent('/tmp/log', PERMISSIONS, 'now'))
            store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR connection timeout to 127.0.0.1:9000', 'now'))
            related = store.related(TIMEOUT, key)
            self.assertEqual(len(related), 1)
            self.assertNotEqual(related[0]['error'], PERMISSIONS)
            sid = store.save_solution(key, [{'command': 'chmod 644 /etc/resolv.conf'}], 'old bad fix', 'agent')
            self.assertFalse(store.summary(key)['solutions'][0]['context_valid'])
            self.assertEqual(store.get_solution(sid)['commands'][0]['command'], 'chmod 644 /etc/resolv.conf')

    def test_solver_retries_unrelated_fix_before_showing_solution(self):
        o = Orchestrator.__new__(Orchestrator)
        o._enhanced = True
        o._ui = Mock()
        o._llm = lambda operation, label: operation()
        o._note = Mock()
        o._solver = Mock()
        correct = [{'command': 'systemctl start logent-test.service'}]
        o._solver.solve.side_effect = [[{'command': 'chmod 644 /etc/resolv.conf'}], correct]
        self.assertEqual(o._propose_fixes(ErrorEvent('/tmp/log', TIMEOUT, 'now'), 'network evidence'), correct)
        self.assertIn('Rejected previous fix', o._solver.solve.call_args.args[1])
        o._ui.show_solution_panel.assert_not_called()


if __name__ == '__main__':
    unittest.main()
