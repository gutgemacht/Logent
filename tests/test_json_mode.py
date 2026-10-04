import json
import unittest
from unittest.mock import Mock, patch

from agents.diagnostic import DiagnosticAgent

PLAN = {'hypothesis': 'permissions', 'commands': [{'command': 'stat /etc/resolv.conf', 'description': 'Inspect permissions'}]}
PROSE = "Hypothesis: suspected problem with permissions for the file /etc/resolv.conf.\nCommands:\n1. `stat /etc/resolv.conf`: Checks permissions."


class JsonModeTests(unittest.TestCase):
    def test_plain_text_plan_is_retried_as_json(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=[PROSE, json.dumps(PLAN)])
        self.assertEqual(agent.diagnose('denied'), PLAN)
        self.assertEqual(agent.call.call_count, 2)
        self.assertIn('Return ONLY a JSON object', agent.call.call_args.args[0])

    def test_plain_text_verdict_is_retried(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(side_effect=['Problem confirmed by stat.', '{"status":"confirmed","explanation":"stat confirms permissions"}'])
        self.assertEqual(agent.verify('error', 'permissions', 'stat output')['status'], 'confirmed')
        self.assertEqual(agent.call.call_count, 2)

    def test_repeated_prose_fails_after_bounded_retry(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=PROSE)
        with self.assertRaisesRegex(ValueError, 'after two attempts'):
            agent.diagnose('error')
        self.assertEqual(agent.call.call_count, 2)

    @patch('agents.base.urllib.request.urlopen')
    def test_actual_http_requests_enable_json_mode_for_both_phases(self, urlopen):
        responses = []
        for content in [PLAN, {'status': 'confirmed', 'explanation': 'Evidence confirms hypothesis'}]:
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps({'message': {'content': json.dumps(content)}}).encode()
            responses.append(response)
        urlopen.side_effect = responses
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.diagnose('denied')
        agent.verify('denied', 'permissions', 'stat result')
        for call in urlopen.call_args_list:
            payload = json.loads(call.args[0].data)
            self.assertEqual(payload['format'], 'json')
            self.assertFalse(payload['stream'])
            self.assertEqual(payload['options']['temperature'], 0)


if __name__ == '__main__':
    unittest.main()
