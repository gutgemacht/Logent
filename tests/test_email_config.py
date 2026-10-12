import os
import tempfile
import unittest
from unittest.mock import patch

import config
from core.email_config import load_smtp, save_smtp, smtp_path, contacts, save_contacts, DEFAULT_TEMPLATE
from core.problem_actions import save_groups, groups, send_notification
from core.reporting import SessionReport
from core.history import HistoryStore
from core.watcher import ErrorEvent
from tests.test_history import TinyEmbedding


class EmailConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch('config.RUNTIME_PATH', self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = HistoryStore(self.tmp.name + '/db', TinyEmbedding())

    def settings(self, password='demo-secret'):
        return dict(host='smtp.example.com', port=587, sender='logent@example.com', security='starttls', username='logent', password=password)

    def test_smtp_roundtrip_permissions_password_preservation_and_clear(self):
        save_smtp(self.settings())
        self.assertEqual(load_smtp()['host'], 'smtp.example.com')
        self.assertEqual(smtp_path().stat().st_mode & 0o777, 0o600)
        save_smtp(self.settings(password=''))
        self.assertEqual(load_smtp()['password'], 'demo-secret')
        save_smtp(self.settings(password=''), clear_password=True)
        self.assertEqual(load_smtp()['password'], '')
        with self.assertRaises(ValueError):
            save_smtp(dict(self.settings(), sender='bad\nBcc: test@example.com'))
        self.assertEqual(load_smtp()['sender'], 'logent@example.com')

    def test_contacts_templates_and_alias_group_resolution(self):
        save_contacts(self.store, [{'Alias': 'Ops', 'Email address': 'ops@example.com', 'Template': 'Hello {alias}: {error} ({case_number})'}])
        save_groups(self.store, [{'Group': 'Operators', 'Email addresses': 'Ops'}])
        self.assertEqual(groups(self.store)['Operators'], ['ops@example.com'])
        save_contacts(self.store, [{'Alias': 'Ops', 'Email address': 'ops@example.com', 'Template': ''}])
        self.assertEqual(contacts(self.store)[0]['Template'], DEFAULT_TEMPLATE)
        with self.assertRaises(ValueError):
            save_contacts(self.store, [{'Alias': 'Ops', 'Email address': 'ops@example.com', 'Template': '{error.__class__}'}])

    def test_sender_uses_saved_smtp_settings_and_personal_template(self):
        save_smtp(self.settings())
        event = ErrorEvent('/tmp/log', 'ERROR demo', 'now')
        report = SessionReport(event, self.store.context)
        with patch('core.problem_actions.smtplib.SMTP') as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            send_notification(['ops@example.com'], event, report, templates={'ops@example.com': {'Alias': 'Ops', 'Template': 'Dear {alias}: {error}'}})
            smtp.assert_called_once_with('smtp.example.com', 587, timeout=20)
            client.login.assert_called_once_with('logent', 'demo-secret')
            self.assertIn('Dear Ops: ERROR demo', client.send_message.call_args.args[0].get_content())

    def test_dashboard_saves_settings_and_contacts_without_activating_demo(self):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path
        root = Path(self.tmp.name)
        (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
        self.store = HistoryStore(self.tmp.name + '/ui-db')
        with patch('config.CHROMA_PATH', self.tmp.name + '/ui-db'):
            app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error, [item.value for item in app.error])
            self.assertEqual(contacts(self.store), [])
            demo = next(item for item in app.dataframe if 'Alias' in item.value.columns and item.value.iloc[0]['Alias'] == 'Operations')
            self.assertTrue(demo.proto.disabled)
            next(item for item in app.text_input if item.label == 'SMTP server').set_value('smtp.example.com')
            next(item for item in app.text_input if item.label == 'Sender email address').set_value('logent@example.com')
            next(item for item in app.text_input if item.label == 'SMTP password').set_value('demo-secret')
            next(button for button in app.button if button.label == 'Save SMTP settings').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(load_smtp()['password'], 'demo-secret')
            self.assertEqual(next(item for item in app.text_input if item.label == 'SMTP password').value, '')
            app.session_state['email-addresses-editor'] = {'edited_rows': {0: {'Alias': 'Ops', 'Email address': 'ops@example.com', 'Template': 'Hello {alias}: {error}'}}, 'added_rows': [], 'deleted_rows': []}
            next(button for button in app.button if button.label == 'Save email addresses').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(contacts(self.store)[0]['Alias'], 'Ops')
