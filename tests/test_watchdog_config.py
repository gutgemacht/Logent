import json
from pathlib import Path
import queue
import tempfile
import unittest

from core.watchdog_config import load_rules, save_rules, validate_rules
from core.watcher import LogEventHandler


class WatchdogConfigurationTests(unittest.TestCase):
    def test_saved_edits_reload_without_restarting_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / 'rules.json'
            log = root / 'test.log'
            log.write_text('')
            save_rules([dict(enabled=True, kind='Keyword', pattern='warn')], rules)
            events = queue.Queue()
            handler = LogEventHandler(events, [], frozenset([str(log)]), frozenset(), str(rules))
            with log.open('a') as stream:
                stream.write('WARN: something happened\nprewarning ignored\n')
            handler._scan_new_lines(str(log))
            self.assertEqual(events.get_nowait().line, 'WARN: something happened')
            self.assertTrue(events.empty())
            save_rules([dict(enabled=True, kind='Keyword', pattern='severe')], rules)
            with log.open('a') as stream:
                stream.write('WARN: disabled now\nSevere failure\n')
            handler._scan_new_lines(str(log))
            self.assertEqual(events.get_nowait().line, 'Severe failure')
            self.assertTrue(events.empty())
            save_rules([], rules)
            with log.open('a') as stream:
                stream.write('Severe failure\n')
            handler._scan_new_lines(str(log))
            self.assertTrue(events.empty())

    def test_invalid_regex_does_not_overwrite_saved_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rules.json'
            rules = [dict(enabled=True, kind='Keyword', pattern='kernel panic')]
            save_rules(rules, path)
            with self.assertRaisesRegex(ValueError, 'invalid regex'):
                save_rules([dict(enabled=True, kind='Regex', pattern='[')], path)
            self.assertEqual(load_rules(path), rules)
            self.assertEqual(json.loads(path.read_text())['version'], 1)

    def test_disabled_rules_and_empty_lists_are_preserved(self):
        self.assertEqual(validate_rules([]), [])
        self.assertFalse(validate_rules([dict(enabled=False, kind='Keyword', pattern='err')])[0]['enabled'])
