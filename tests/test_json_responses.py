import json
import unittest
from unittest.mock import Mock

from agents.base import Agent
from agents.diagnostic import DiagnosticAgent


class JsonResponseTests(unittest.TestCase):
    def test_reported_diagnostic_response_with_explanation(self):
        raw = '''{
"hypothesis": "suspected file permissions issue",
"commands": [
{"command": "stat -c %A /etc/resolv.conf", "description": "verify file permissions"},
{"command": "ls -ld /etc/resolv.conf", "description": "verify directory permissions"}
]
}

Explanation: The error message indicates that the file permissions may be wrong.'''
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value=raw)
        plan = agent.diagnose('permission denied')
        self.assertEqual(plan['hypothesis'], 'suspected file permissions issue')
        self.assertEqual(len(plan['commands']), 2)
        self.assertEqual(plan['commands'][0]['command'], 'stat -c %A /etc/resolv.conf')

    def test_fenced_verdict_followed_by_explanation(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value='```json\n{"status":"disproved","explanation":"File exists"}\n```\nExplanation: file found.')
        self.assertEqual(agent.verify('error', 'missing file', 'exists')['status'], 'disproved')

    def test_nested_json_and_brackets_inside_strings(self):
        value = {'commands': [{'command': 'printf "[{}]"'}]}
        raw = json.dumps(value) + '\nExplanation: {braces} and [brackets] are prose.'
        self.assertEqual(Agent.parse_json(raw), value)

    def test_array_followed_by_prose(self):
        self.assertEqual(Agent.parse_json('[{"command":"id"}]\nExplanation: identity check.'), [{'command': 'id'}])

    def test_malformed_json_is_not_recovered_as_nested_object(self):
        for raw in ['{"commands": [{"command":"id"}]', '{"commands": [invalid]}', 'No JSON here']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                Agent.parse_json(raw)

    def test_trailing_prose_does_not_bypass_schema_validation(self):
        agent = DiagnosticAgent('test', 'http://localhost')
        agent.call = Mock(return_value='{"hypothesis":"test","commands":[]}\nExplanation: no commands.')
        with self.assertRaises(ValueError):
            agent.diagnose('error')


if __name__ == '__main__':
    unittest.main()
