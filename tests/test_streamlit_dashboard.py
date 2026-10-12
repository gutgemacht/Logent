import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from core.history import HistoryStore
from core.watcher import ErrorEvent


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Optional dashboard dependencies not installed')
class DashboardTests(unittest.TestCase):
    def test_diagnostic_scenarios_validate_version_and_deactivate(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            store = HistoryStore(str(root / 'db'))
            with patch('config.CHROMA_PATH', str(root / 'db')):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                for label, value in [('Example error / symptom', 'ERROR wrong permissions /tmp/example'),
                                     ('Read-only command templates, one per line', 'stat -- {target_path}'),
                                     ('Evidence that would confirm or reject the hypothesis', 'Compare observed mode with the explicit policy')]:
                    next(item for item in app.text_area if item.label == label).set_value(value)
                for label, value in [('Hypothesis to test','Incorrect mode'), ('Target path for validation / preview','/tmp/example'),
                                     ('Question answered by these checks','What are the current permissions?')]:
                    next(item for item in app.text_input if item.label == label).set_value(value)
                next(item for item in app.button if item.label == 'Save diagnostic scenario').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(store.playbooks.count(), 1)
                row = store.list_playbooks()[0]
                self.assertIn('{target_path}', row['commands'][0]['command'])
                next(item for item in app.selectbox if item.label == 'Scenario to inspect or edit').select(row['id']).run()
                self.assertFalse(app.exception)
                next(item for item in app.text_input if item.label == 'Hypothesis to test').set_value('Check ownership too')
                next(item for item in app.button if item.label == 'Save diagnostic scenario').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(store.playbooks.count(), 2)
                self.assertFalse(next(item for item in store.list_playbooks() if item['id'] == row['id'])['active'])
                next(item for item in app.text_area if item.label == 'Read-only command templates, one per line').set_value('chmod 644 {target_path}')
                next(item for item in app.button if item.label == 'Validate and preview commands').click().run()
                self.assertTrue(app.error)
                self.assertEqual(store.playbooks.count(), 2)

    def test_filters_case_timeline_health_and_source_editor(self):
        from streamlit.testing.v1 import AppTest
        from core.reporting import SessionReport
        from core.monitoring import MonitorState
        import queue
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            store = HistoryStore(str(root / 'db'))
            log = root / 'app.log'
            log.write_text('')
            sources = root / 'logs.lst'
            sources.write_text(str(log) + '\n')
            event = ErrorEvent(str(log), 'ERROR connection timeout on localhost:8080', '2026-10-05T10:00:00+00:00', ('timeout',))
            key, occurrence = store.record_occurrence(event)
            sid = store.save_solution(key, [{'command': 'ss -ltnp'}], 'Check listener', 'user')
            store.record_attempt(key, occurrence, sid, 'yes')
            store.record_occurrence(ErrorEvent(str(log), 'ERROR wrong permissions /tmp/test', '2026-10-05T11:00:00+00:00'))
            case = SessionReport(event, store.context)
            case.data.update(error_key=key, occurrence_ids=[occurrence], stage='Outcome saved', cause='Listener issue', conclusion='Confirmed by evidence', outcome='yes')
            case.add('diagnostic_plan', {'hypothesis': 'Listener issue', 'commands': [{'command': 'ss -ltnp'}]})
            case.add('diagnostic_output_complete', [{'command': 'ss -ltnp', 'success': True, 'shortened': True,
                                                    'full_output': 'ALL CAPTURED OUTPUT', 'selected_output': 'SELECTED EVIDENCE',
                                                    'original_characters': 19, 'selected_characters': 17}])
            case.finish()
            store.save_investigation(case.data)
            state = MonitorState(root / 'runtime', [str(log)], root / 'rules.json', queue.Queue())
            state.update(state='Running')
            state.flush()
            with patch('config.CHROMA_PATH', str(root / 'db')), patch('config.RUNTIME_PATH', str(root / 'runtime')), \
                 patch('config.LOGS_CONFIG_FILE', str(sources)), patch('config.WATCHDOG_RULES_FILE', str(root / 'rules.json')):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertIn(('Running monitors', '1'), [(item.label, item.value) for item in app.metric])
                next(item for item in app.selectbox if item.label == 'Show problems').select('Unresolved occurrences').run()
                problems = next(table.value for table in app.dataframe if 'Solution rating' in table.value.columns)
                self.assertEqual(len(problems), 1)
                self.assertIn('permissions', problems.iloc[0]['Error'])
                app.selectbox(key='case-' + case.data['id'] + '-step').select(1).run()
                self.assertIn('ALL CAPTURED OUTPUT', [item.value for item in app.code])
                self.assertIn('SELECTED EVIDENCE', [item.value for item in app.code])
                next(item for item in app.text_area if item.label == 'Files and directories to monitor').set_value(str(log) + '\n' + str(root / 'second.log'))
                next(button for button in app.button if button.label == 'Save sources').click().run()
                self.assertFalse(app.exception)
                self.assertIn('second.log', sources.read_text())

    def test_watchdog_tab_previews_and_saves_edited_rules(self):
        from streamlit.testing.v1 import AppTest
        from core.watchdog_config import save_rules, load_rules
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            HistoryStore(str(root / 'db'))
            rules_path = str(root / 'rules.json')
            save_rules([dict(enabled=True, kind='Keyword', pattern='warn')], rules_path)
            with patch('config.CHROMA_PATH', str(root / 'db')), patch('config.WATCHDOG_RULES_FILE', rules_path):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.error)
                self.assertEqual([tab.label for tab in app.tabs[:2]], ['Problems and solutions', 'Watchdog configuration'])
                next(item for item in app.text_area if item.label == 'Test log lines').set_value('WARN: disk warming\nservice ready')
                next(button for button in app.button if button.label == 'Test triggers').click().run()
                preview = next(table.value for table in app.dataframe if 'Detected' in table.value.columns)
                self.assertEqual(preview['Detected'].tolist(), [True, False])
                app.session_state['watchdog-editor'] = {'edited_rows': {0: {'enabled': False}}, 'added_rows': [], 'deleted_rows': []}
                next(button for button in app.button if button.label == 'Save triggers').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(load_rules(rules_path)[0]['enabled'])

    def test_old_chroma_is_rejected_before_opening_database(self):
        from streamlit_access import LogentConnection
        with patch('streamlit_access.version', return_value='0.5.3'), \
             patch('streamlit_access.HistoryStore') as history:
            with self.assertRaisesRegex(ValueError, 'run ./streamlit_access.sh'):
                LogentConnection._connect(None, path='/unused/db')
            history.assert_not_called()

    def test_browse_search_edit_and_record_feedback(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            store = HistoryStore(str(root / 'db'))
            key, occurrence = store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR connection timeout to 127.0.0.1:8080', 'now'))
            sid = store.save_solution(key, [{'command': 'ss -ltnp', 'description': 'Inspect listener', 'criticality': 'safe', 'sudo': False}], 'Inspect server', 'user')
            store.record_attempt(key, occurrence, sid, 'yes')
            with patch('config.CHROMA_PATH', str(root / 'db')):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertIn(('Saved problems', '1'), [(metric.label, metric.value) for metric in app.metric])
                self.assertIn(('Saved solutions', '1'), [(metric.label, metric.value) for metric in app.metric])
                table = app.dataframe[0].value
                self.assertIn('Inspect server', table.iloc[0]['Solution'])
                self.assertIn('Worked: 1', table.iloc[0]['Solution rating'])
                self.assertIn('Current machine', table.iloc[0]['Environment'])
                self.assertIn(('Solved', '1'), [(metric.label, metric.value) for metric in app.metric])
                self.assertIn(('Worked', '1'), [(metric.label, metric.value) for metric in app.metric])
                search = next(item for item in app.text_input if item.label == 'Search problems')
                search.set_value('timeout').run()
                self.assertFalse(app.exception)
                self.assertIn('connection timeout', app.text[0].value)
                note = next(item for item in app.text_area if item.label == 'Solution description / notes')
                note.set_value('Check server listener first')
                next(button for button in app.button if button.label == 'Save new version').click().run()
                self.assertFalse(app.exception)
                entries = store.summary(key)['solutions']
                self.assertEqual(len(entries), 2)
                self.assertTrue(store.get_solution(sid)['obsolete'])
                new = next(item for item in entries if item['id'] != sid)
                self.assertEqual(new['helped'], 0)
                self.assertEqual(new['commands'][0]['description'], 'Inspect listener')
                select = next(item for item in app.selectbox if item.label == 'Solution tested')
                select.select(new['id'])
                next(item for item in app.radio if item.label == 'Report the outcome').set_value('Fixed')
                next(button for button in app.button if button.label == 'Record feedback').click().run()
                self.assertFalse(app.exception)
                rated = next(item for item in store.summary(key)['solutions'] if item['id'] == new['id'])
                self.assertEqual(rated['helped'], 1)
                attempt = next(item for item in store.problem_details(key)['attempts'] if item['solution_id'] == new['id'])
                next(item for item in app.selectbox if item.label == 'Existing application attempt').select(attempt['id']).run()
                next(item for item in app.radio if item.label == 'Report the outcome').set_value('Still failing')
                next(button for button in app.button if button.label == 'Record feedback').click().run()
                self.assertFalse(app.exception)
                revised = next(item for item in store.summary(key)['solutions'] if item['id'] == new['id'])
                self.assertEqual((revised['helped'], revised['failed']), (0, 1))
                self.assertEqual(store.attempts.count(), 2)

    def test_empty_history_and_empty_search_allow_adding_problem_with_solution(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            store = HistoryStore(str(root / 'db'))
            with patch('config.CHROMA_PATH', str(root / 'db')):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertIn(('Saved problems', '0'), [(metric.label, metric.value) for metric in app.metric])
                next(item for item in app.text_input if item.label == 'Search problems').set_value('no-match').run()
                app.text_area(key='new-error').set_value('ERROR connection refused on localhost:8080')
                app.text_area(key='new-notes').set_value('Start the application server')
                app.text_area(key='new-commands').set_value('systemctl start example.service')
                next(button for button in app.button if button.label == 'Save problem and solution').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertEqual(store.errors.count(), 1)
                key = store.errors.get()['ids'][0]
                summary = store.summary(key)
                self.assertEqual(summary['seen'], 1)
                self.assertEqual(summary['solutions'][0]['notes'], 'Start the application server')
                self.assertIsNone(summary['solutions'][0]['success_rate'])
                next(item for item in app.text_input if item.label == 'Search problems').set_value('').run()
                self.assertIn('systemctl start example.service', app.dataframe[0].value.iloc[0]['Solution'])


    def test_linux_library_filters_preview_and_disable_without_fake_history(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            store = HistoryStore(str(root / 'db'))
            with patch('config.CHROMA_PATH', str(root / 'db')):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertIn('Linux knowledge', [tab.label for tab in app.tabs])
                app.selectbox(key='linux-category').select('default-linux-memory').run()
                table = next(item.value for item in app.dataframe if 'Assessment' in item.value.columns)
                self.assertEqual(set(table['Category']), {'default-linux-memory'})
                app.text_input(key='linux-search').set_value('Container or cgroup memory limit reached').run()
                table = next(item.value for item in app.dataframe if 'Assessment' in item.value.columns)
                self.assertEqual(len(table), 1)
                next(item for item in app.button if item.label == 'Disable Linux reference').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(next(item for item in store.list_linux_library() if item['id'].endswith('-cgroup-oom'))['active'])
                next(item for item in app.text_input if item.label == 'Test an error against local knowledge').set_value('ERROR: screen is not installed.')
                with patch('core.executor.run_command') as command:
                    next(item for item in app.button if item.label == 'Preview local diagnostic plan').click().run()
                    command.assert_not_called()
                self.assertFalse(app.exception)
                self.assertIn('which -- screen', [item.value for item in app.code])
                self.assertEqual((store.errors.count(), store.occurrences.count(), store.attempts.count()), (0, 0, 0))

    def test_semantic_filters_and_confirmed_problem_deletion(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.chroma_models').symlink_to(config.BASE_DIR/'.chroma_models',target_is_directory=True)
            store=HistoryStore(str(root/'db'))
            target_key=None
            for error in ['ERROR: Connection to localhost:8080 timed out', 'ERROR: Permission denied /tmp/alpha', 'kernel: Out of memory: Kill process 42']:
                key,_=store.record_occurrence(ErrorEvent('/tmp/log',error,'now'))
                if 'Permission' in error:
                    target_key=key
            with patch('config.CHROMA_PATH',str(root/'db')):
                app=AppTest.from_file(str(config.BASE_DIR/'streamlit_access.py'),default_timeout=30).run()
                next(item for item in app.checkbox if item.label=='Search by meaning').check()
                next(item for item in app.text_input if item.label=='Search problems').set_value('Arbeitsspeicher').run()
                table=next(item.value for item in app.dataframe if 'Solution rating' in item.value.columns)
                self.assertEqual(len(table),1)
                self.assertIn('Out of memory',table.iloc[0]['Error'])
                next(item for item in app.text_input if item.label=='Search problems').set_value('permissions').run()
                next(item for item in app.button if item.label=='Delete problem permanently').click().run()
                self.assertEqual(store.errors.count(),3)
                self.assertTrue(any('confirmation checkbox' in item.value for item in app.error))
                app.checkbox(key='problem-delete-confirm-'+target_key).check()
                next(item for item in app.button if item.label=='Delete problem permanently').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(store.errors.count(),2)
                self.assertEqual(store.errors.get(ids=[target_key])['ids'],[])
                self.assertTrue(any('Deleted:' in item.value for item in app.success))
                self.assertEqual(store.linux_library.count(),25)

    def test_case_deletion_keeps_problem_and_recalculates_statistics(self):
        from streamlit.testing.v1 import AppTest
        from core.reporting import SessionReport
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.chroma_models').symlink_to(config.BASE_DIR/'.chroma_models',target_is_directory=True)
            store=HistoryStore(str(root/'db'))
            event=ErrorEvent('/tmp/log','ERROR: mc is not installed','now')
            key,occurrence=store.record_occurrence(event)
            case=SessionReport(event,store.context)
            case.data.update(error_key=key,occurrence_ids=[occurrence],outcome='ignored')
            case.finish();store.save_investigation(case.data)
            with patch('config.CHROMA_PATH',str(root/'db')):
                app=AppTest.from_file(str(config.BASE_DIR/'streamlit_access.py'),default_timeout=30).run()
                app.checkbox(key='case-delete-confirm-'+case.data['id']).check()
                next(item for item in app.button if item.label=='Delete case permanently').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(store.investigations.count(),0)
                self.assertEqual(store.occurrences.count(),0)
                self.assertEqual(store.errors.count(),1)
                self.assertEqual(store.summary(key)['seen'],0)

    def test_problem_automation_and_email_group_table_controls(self):
        from streamlit.testing.v1 import AppTest
        from core.problem_actions import policy, groups
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.chroma_models').symlink_to(config.BASE_DIR / '.chroma_models', target_is_directory=True)
            store = HistoryStore(str(root / 'db'))
            key, _ = store.record_occurrence(ErrorEvent('/tmp/log', 'ERROR service example failed', 'now'))
            sid = store.save_solution(key, [{'command': 'echo repair'}], 'Repair', 'user')
            with patch('config.CHROMA_PATH', str(root / 'db')):
                app = AppTest.from_file(str(config.BASE_DIR / 'streamlit_access.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                app.session_state['email-groups-editor'] = {'edited_rows': {0: {'Group': 'Ops', 'Email addresses': 'ops@example.com'}}, 'added_rows': [], 'deleted_rows': []}
                next(button for button in app.button if button.label == 'Save email groups').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(groups(store)['Ops'], ['ops@example.com'])
                app.session_state['problems-table-' + key] = {'edited_rows': {0: {'Automate': True, 'Inform': 'Ops'}}, 'added_rows': [], 'deleted_rows': []}
                next(button for button in app.button if button.label == 'Save problem actions').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertTrue(policy(store, key)['automate'])
                self.assertEqual(policy(store, key)['solution_id'], sid)
                self.assertEqual(policy(store, key)['inform'], 'Ops')
                table = next(item.value for item in app.dataframe if 'Automate' in item.value.columns)
                self.assertTrue(table.iloc[0]['Automate'])
                from core.problem_actions import process
                from core.reporting import SessionReport
                event = ErrorEvent('/tmp/log', 'ERROR service example failed', 'now')
                report = SessionReport(event, store.context)
                with patch('core.problem_actions.run_command', return_value=(True, 'repair trace output')), patch('core.problem_actions.send_notification'):
                    self.assertTrue(process(store, event, report))
                report.finish()
                store.save_investigation(report.data)
                app.run()
                self.assertFalse(app.exception)
                self.assertTrue(any(item.label == 'Automatic execution and notification log' for item in app.expander))
                self.assertTrue(any('repair trace output' in item.value for item in app.code))


if __name__ == '__main__':
    unittest.main()
