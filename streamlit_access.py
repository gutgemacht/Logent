"""Run: .venv/bin/python -m streamlit run streamlit_access.py"""
from __future__ import annotations

from datetime import datetime
from importlib.metadata import version
import json
import traceback
from uuid import uuid4
from pathlib import Path
import re

import streamlit as st
from streamlit.connections import BaseConnection

import config
from core.history import HistoryStore, normalize_error
from core.watcher import ErrorEvent
from core.watchdog_config import default_rules, load_rules, rule_pattern, save_rules, validate_rules, match_rules
from core.monitoring import monitor_snapshots, source_status
from core.log_sources import parse_sources, save_sources
from core.reporting import PIPELINE, OUTCOMES
from core.error_context import problem_kind, reported_paths
from core.knowledge import SYMPTOMS, CAUSES, render_commands


def check_database_version() -> None:
    chroma_version = version('chromadb')
    if chroma_version.split('.', 1)[0] != '1':
        raise ValueError(
            f'Incompatible ChromaDB {chroma_version}. Logent requires chromadb>=1.0,<2. '
            'Stop this Streamlit server and run ./streamlit_access.sh from the Logent directory '
            'to use the project virtual environment. The streamlit-chromadb-connection package '
            'pins an older ChromaDB version and should use a separate environment.'
        )
    streamlit_version = version('streamlit')
    major, minor = (int(part) for part in streamlit_version.split('.')[:2])
    if major != 1 or minor < 65:
        raise ValueError(f'Streamlit {streamlit_version} is too old for this dashboard. Install requirements-dashboard.txt in Logent’s .venv and use ./streamlit_access.sh.')


class LogentConnection(BaseConnection[HistoryStore]):
    """Use the same Chroma version, embeddings and statistics as the monitor."""

    def _connect(self, **kwargs) -> HistoryStore:
        check_database_version()
        path = Path(kwargs['path']).expanduser().resolve()
        if not (path / 'chroma.sqlite3').is_file():
            raise ValueError(f'No existing Logent database found at {path}')
        return HistoryStore(str(path))

    @property
    def history(self) -> HistoryStore:
        return self._instance


def command_entries(text: str) -> list[dict]:
    """Store pasted commands as data; this dashboard never executes them."""
    return [{'command': line.strip(), 'description': 'User supplied command',
             'criticality': 'warning', 'sudo': False}
            for line in text.splitlines() if line.strip()]


def command_text(solution: dict) -> str:
    return '\n'.join(('sudo ' if item.get('sudo') and not item['command'].startswith('sudo ') else '')
                     + item['command'] for item in solution['commands'])


def flash(message):
    st.session_state['notice'] = message
    st.rerun()


def problem_rows(store: HistoryStore, query: str, semantic: bool, limit: int, current_environment: bool = False):
    if semantic and query:
        eligible = problem_rows(store, '', False, 0)
        if current_environment:
            eligible = [row for row in eligible if row['metadata'].get('environment_id') == store.environment_id]
        return store.search_problems(query, limit, eligible_ids=[row['id'] for row in eligible])
    result = store.errors.get(include=['documents', 'metadatas'])
    return [dict(id=id_, error=doc, metadata=meta or {})
            for id_, doc, meta in zip(result['ids'], result['documents'], result['metadatas'])
            if not query or query.casefold() in doc.casefold()]


def solution_editor(store: HistoryStore, key: str, solution: dict | None):
    suffix = solution['id'] if solution else 'new-' + key
    with st.form('solution-' + suffix):
        notes = st.text_area('Solution description / notes', value=solution['notes'] if solution else '')
        commands = st.text_area('Commands, one per line', value=command_text(solution) if solution else '',
                                help='Commands are saved as text. They are never executed by this page.')
        instructions = st.text_area('Manual repair instructions, one per line', value='\n'.join(solution.get('manual_steps', [])) if solution else '')
        submitted = st.form_submit_button('Save new version' if solution else 'Add solution')
    if submitted:
        if not notes.strip() and not commands.strip() and not instructions.strip():
            st.error('Enter notes or at least one command.')
            return
        entries = command_entries(commands)
        try:
            if solution:
                if notes.strip() == solution['notes'].strip() and commands == command_text(solution) and instructions == '\n'.join(solution.get('manual_steps', [])):
                    flash('No changes to save.')
                # Preserve command metadata on notes-only edits.
                if commands == command_text(solution):
                    entries = solution['commands']
                new_id = store.edit_solution(solution['id'], entries, notes.strip(), manual_steps=[line.strip() for line in instructions.splitlines() if line.strip()])
                flash('No changes to save.' if new_id == solution['id'] else 'New untested version saved; previous version and ratings preserved.')
            else:
                store.save_solution(key, entries, notes.strip(), 'user', manual_steps=[line.strip() for line in instructions.splitlines() if line.strip()])
                flash('Solution saved. Add feedback separately if it has been tested.')
        except Exception as exc:
            st.error(f'Could not save solution: {exc}')


def show_solution(store: HistoryStore, key: str, solution: dict, number: int):
    with st.expander(f"Solution {number}: {solution['notes'][:90] or 'Commands'}" + (' — obsolete' if solution.get('obsolete') else ''), expanded=number == 1):
        st.text(solution['notes'])
        if solution['commands']:
            st.code(command_text(solution), language='bash')
        else:
            st.caption('Notes only; no commands saved.')
        if solution.get('manual_steps'):
            st.markdown('**Manual instructions**')
            for index, instruction in enumerate(solution['manual_steps'], 1):
                st.text(f'{index}. {instruction}')
        cause = solution.get('diagnosis', {}).get('cause') or {}
        if cause:
            st.text('Saved cause: ' + (cause.get('description') or cause.get('explanation', '')))
        st.caption('Fresh symptom and cause diagnostics are required before any saved repair can be applied.')
        columns = st.columns(4)
        for column, title, value in zip(columns, ['Worked', 'Still failing', 'Cannot verify', 'Success rate'],
                                        [solution['helped'], solution['failed'], solution['unsure'],
                                         f"{solution['success_rate']:.0%}" if solution['success_rate'] is not None else 'Untested']):
            column.metric(title, value)
        st.caption('Success rate = Worked / (Worked + Still failing). One rating per application attempt; repeated log lines do not add votes.')
        st.caption('Last worked: ' + (solution.get('last_worked') or 'Never'))
        st.caption(f"Worked {solution['helped']} of {solution['helped'] + solution['failed']} reported attempts. "
                   f"Successful automatic verifications: {solution.get('verified_successes', 0)}. "
                   f"Last successful verification: {solution.get('last_verified') or 'None recorded'}.")
        if solution.get('context_valid') is False:
            st.warning('Unsuitable for this case: ' + solution['context_reason'])
        edit_tab, details_tab = st.tabs(['Edit solution', 'Stored details'])
        with edit_tab:
            solution_editor(store, key, solution)
        with details_tab:
            st.json(solution)
        obsolete = solution.get('obsolete', False)
        if st.button('Mark active' if obsolete else 'Mark obsolete', key='obsolete-' + solution['id']):
            try:
                store.set_obsolete(solution['id'], not obsolete)
                flash('Solution status updated.')
            except Exception as exc:
                st.error(f'Could not update status: {exc}')


def feedback_form(store: HistoryStore, key: str, solutions: list[dict]):
    occurrences = store.occurrences.get(where={'error_key': key}, include=['documents'])
    if not occurrences['ids']:
        st.info('No recorded occurrence is available to rate.')
        return
    labels = {id_: f"{json.loads(doc)['timestamp']} — {json.loads(doc)['filepath']} — {id_[:8]}"
              for id_, doc in zip(occurrences['ids'], occurrences['documents'])}
    options = {item['id']: item['notes'][:90] or item['id'][:12] for item in solutions}
    attempts = store.problem_details(key)['attempts']
    attempt_options = {item['id']: item for item in attempts}
    tested = st.selectbox('Existing application attempt', [None] + list(attempt_options),
        format_func=lambda id_: 'New manually performed attempt' if id_ is None else attempt_options[id_]['timestamp'] + ' · ' + OUTCOMES.get(attempt_options[id_]['outcome'], 'No feedback') + ' · ' + id_[:8])
    chosen = attempt_options.get(tested, {})
    with st.form('feedback-' + key):
        occurrence = st.selectbox('Occurrence tested', occurrences['ids'], format_func=labels.get,
            index=occurrences['ids'].index(chosen['occurrence_id']) if chosen.get('occurrence_id') in occurrences['ids'] else 0, disabled=bool(chosen))
        solution_ids = [None] + list(options)
        solution_id = st.selectbox('Solution tested', solution_ids,
            index=solution_ids.index(chosen.get('solution_id')) if chosen.get('solution_id') in solution_ids else 0, disabled=bool(chosen),
            format_func=lambda id_: options[id_] if id_ else 'Solution not saved / other fix')
        outcome = st.radio('Report the outcome', ['Cannot verify', 'Fixed', 'Still failing', 'Skip feedback'],
            index={'yes':1, 'no':2}.get(chosen.get('outcome'), 0), horizontal=True)
        submitted = st.form_submit_button('Record feedback')
    if submitted:
        try:
            value = {'Fixed': 'yes', 'Still failing': 'no', 'Cannot verify': 'unsure', 'Skip feedback': 'skipped'}[outcome]
            if tested:
                store.update_feedback(tested, value)
            elif value != 'skipped':
                draft = f'feedback-draft-{key}-{occurrence}-{solution_id}'
                id_ = st.session_state.setdefault(draft, str(uuid4()))
                store.record_attempt(key, occurrence, solution_id, value, attempt_id=id_)
            flash('Feedback skipped; ratings unchanged.' if outcome == 'Skip feedback' else 'Feedback recorded; ratings recalculated.')
        except Exception as exc:
            st.error(f'Could not record feedback: {exc}')
    st.caption('An existing attempt keeps its original solution and occurrence. Updating it preserves the feedback audit trail. Repeated submission updates the same draft.')
    if st.button('Start another manually performed attempt', key='new-attempt-' + key):
        for state_key in list(st.session_state):
            if state_key.startswith('feedback-draft-' + key):
                del st.session_state[state_key]
        flash('New attempt ready. Record feedback only after testing it.')


def environment_label(store: HistoryStore, row: dict) -> str:
    environment = row['metadata'].get('environment_id')
    if environment == store.environment_id:
        return f"Current machine · {store.context['os']} · {store.context['user']}"
    metadata = row['metadata']
    if metadata.get('hostname'):
        return ' · '.join(metadata.get(field, '') for field in ('hostname', 'os', 'user') if metadata.get(field))
    return 'Other saved machine / OS / user' if environment else 'Older record · environment unknown'


def table_entry(store: HistoryStore, row: dict, summary: dict) -> dict:
    active = [solution for solution in summary['solutions'] if not solution.get('obsolete')]
    suitable = [solution for solution in active if solution.get('context_valid', True)]
    best = suitable[0] if suitable else None
    if best:
        solution_text = '\n'.join(part for part in [best['notes'], command_text(best)] if part)
        rating = f"Worked: {best['helped']} of {best['helped'] + best['failed']} · Still failing: {best['failed']} · Cannot verify: {best['unsure']}"
        if best['success_rate'] is None:
            rating += ' · Untested'
    else:
        solution_text = 'Saved solutions need review' if active else 'No active solution saved'
        rating = '—'
    from core.problem_actions import policy
    actions = policy(store, row['id'])
    return {'Error': row['error'], 'Solution': solution_text, 'Solution rating': rating,
            'Automate': actions.get('automate', False), 'Inform': actions.get('inform', 'None'),
            'Occurrences': summary['seen'], 'Solved': summary['solved'],
            'Saved solutions': len(active), 'Environment': environment_label(store, row),
            'Category': problem_kind(row['error']).replace('_', ' '),
            **({'Distance (lower is closer)': row['distance'], 'Search match':row.get('match_reason', 'Semantic similarity')} if 'distance' in row else {})}


def filtered_problems(rows, summaries, view='All problems', category='All categories', environment=None, since=None, dates=None):
    result = []
    for row in rows:
        summary = summaries[row['id']]
        active = [s for s in summary['solutions'] if not s.get('obsolete') and s.get('context_valid', True)]
        if view == 'Unresolved occurrences' and summary['seen'] <= summary['solved']:
            continue
        if view == 'Without an active solution' and active:
            continue
        if view == 'With tested solutions' and not any(s['helped'] + s['failed'] for s in active):
            continue
        if view == 'With successful solutions' and not any(s['helped'] for s in active):
            continue
        if view == 'Awaiting feedback' and not summary['unreported']:
            continue
        if category != 'All categories' and problem_kind(row['error']) != category:
            continue
        if environment is not None and row['metadata'].get('environment_id', '') != environment:
            continue
        if since and not any(date >= since for date in (dates or {}).get(row['id'], [])):
            continue
        result.append(row)
    return result


def deletion_controls(store, *, problem_id=None, case_id=None, prefix=''):
    target = problem_id or case_id
    label = 'Delete saved problem' if problem_id else 'Delete investigation case'
    with st.expander(label):
        try:
            preview = store.deletion_preview(problem_id=problem_id, case_id=case_id)
        except ValueError as exc:
            st.info(str(exc))
            return
        st.text('Selected: ' + preview['title'])
        st.caption('Records to remove: ' + ' · '.join(f'{name}: {count}' for name, count in preview['counts'].items()))
        if preview['blocked']:
            st.info('Finish or cancel the active investigation before deleting its history.')
            return
        st.caption('Deleting a problem removes its occurrences, cases, solutions and ratings. Future log occurrences can create new history.' if problem_id else
                   'Deleting a case removes its timeline, ratings and unshared occurrences. The problem, shared solutions and other cases remain. Statistics are recalculated.')
        st.caption('Bundled Linux references and independent user-authored diagnostic scenarios are preserved. Exported report files are not removed.')
        with st.form(prefix + 'delete-form-' + target):
            confirmed = st.checkbox('I understand that these records will be permanently deleted', key=prefix + 'delete-confirm-' + target)
            submitted = st.form_submit_button('Delete problem permanently' if problem_id else 'Delete case permanently')
        if submitted:
            if not confirmed:
                st.error('Select the confirmation checkbox before deleting.')
                return
            try:
                counts = store.delete_problem(problem_id) if problem_id else store.delete_case(case_id)
            except (ValueError, RuntimeError) as exc:
                st.error('Deletion failed: ' + str(exc))
                return
            # Do not reuse a selection that referred to deleted rows.
            st.session_state.pop('last-table-problem', None)
            flash('Deleted: ' + ' · '.join(f'{name}: {count}' for name, count in counts.items()))


@st.dialog('Saved problem details', width='large')
def problem_dialog(store, row, summary):
    st.text(row['error'])
    st.caption('Environment: ' + environment_label(store, row))
    st.caption(f"Occurrences: {summary['seen']} · User-reported solved: {summary['solved']} · No feedback: {summary['unreported']}")
    for number, solution in enumerate(summary['solutions'], 1):
        with st.container(border=True):
            st.markdown(f"**Solution {number} — {'Obsolete' if solution.get('obsolete') else 'Active'}**")
            st.text(solution['notes'])
            if solution['commands']:
                st.code(command_text(solution), language='bash')
            st.caption(f"Worked {solution['helped']} of {solution['helped'] + solution['failed']} reported attempts · "
                       f"Last worked: {solution.get('last_worked') or 'Never'} · Last verified: {solution.get('last_verified') or 'None recorded'}")
            if solution.get('context_valid') is False:
                st.warning(solution['context_reason'])
    if not summary['solutions']:
        st.info('No saved solution. Add a solution in the details below the main table.')
    cases = store.case_records(row['id'])
    if cases:
        options = {case['id']: case for case in cases}
        selected = st.selectbox('Case timeline', list(options), key='dialog-case-' + row['id'],
                                format_func=lambda item: options[item]['case_number'] + ' · ' + options[item]['status'])
        show_case(options[selected], key_prefix='dialog-')
    else:
        st.caption('No investigation timeline was recorded for this historical problem.')
    st.caption('Close this view to edit solutions or add feedback in the main page.')


def add_problem_form(store: HistoryStore):
    st.subheader('Add a problem and its solution')
    with st.form('new-problem'):
        error = st.text_area('Error message', key='new-error')
        source = st.text_input('Source', value='Manual entry')
        notes = st.text_area('Solution description / notes (optional)', key='new-notes')
        commands = st.text_area('Solution commands, one per line (optional)', key='new-commands',
                                help='Stored as text; never executed by this page.')
        st.caption('A repeated error adds an occurrence. New solutions are untested until you record feedback.')
        submitted = st.form_submit_button('Save problem and solution')
    if submitted:
        if not error.strip():
            st.error('Enter an error message.')
            return
        saved_problem = False
        try:
            key, _ = store.record_occurrence(ErrorEvent(source.strip() or 'Manual entry', error.strip(), datetime.now().astimezone().isoformat()))
            saved_problem = True
            if notes.strip() or commands.strip():
                store.save_solution(key, command_entries(commands), notes.strip(), 'user')
                flash('Problem occurrence and untested solution saved.')
            flash('Problem occurrence saved without a solution.')
        except Exception as exc:
            st.error(f'Problem occurrence saved, but solution could not be saved: {exc}' if saved_problem
                     else f'Could not save problem: {exc}')


def history_page():
    path = str(Path(config.CHROMA_PATH).expanduser().resolve())
    try:
        # Check on every rerun, including sessions with a cached connection.
        check_database_version()
        connection = st.connection('logent_history', type=LogentConnection, path=path)
        store = connection.history
    except Exception as exc:
        st.error(f'Could not open Logent history: {exc}')
        st.stop()

    try:
        all_rows = problem_rows(store, '', False, 0)
        summaries = {row['id']: store.summary(row['id']) for row in all_rows}
        totals = {field: sum(summary[field] for summary in summaries.values())
                  for field in ('seen', 'solved', 'failed', 'unsure', 'unreported')}
        st.subheader('Database overview')
        with st.container(horizontal=True):
            for label, value in [('Saved problems', len(all_rows)), ('Saved solutions', store.solutions.count()),
                                 ('Occurrences recorded', totals['seen']), ('Solved occurrences', totals['solved'])]:
                st.metric(label, value, border=True)
        without_solution = sum(not any(not s.get('obsolete') and s.get('context_valid', True)
                                       for s in summary['solutions']) for summary in summaries.values())
        st.caption(f"All saved environments: {totals['failed']} still failing · {totals['unsure']} cannot verify · "
                   f"{totals['unreported']} without feedback · {without_solution} problems without a suitable active solution. "
                   'Saved solutions include obsolete versions. Solved counts come from user feedback.')
    except Exception as exc:
        st.error(f'Could not read history: {exc}')
        with st.expander('Error details'):
            st.code(traceback.format_exc(), language='text')
        st.stop()

    with st.container(horizontal=True):
        for title, value in [('Investigation incidents', store.investigations.count()), ('Application attempts', sum(item['attempts'] for item in summaries.values())), ('Diagnostic scenarios', store.playbooks.count())]:
            st.metric(title, value, border=True)
    st.caption('Events count log messages; incidents count investigations; attempts count individual applications. Older events may have no incident record.')
    st.caption('Database: ' + path)
    if st.button('Refresh data'):
        st.rerun()
    st.subheader('Saved problems and solutions')
    query = st.text_input('Search problems', placeholder='connection timeout, out of memory, /etc/resolv.conf…')
    semantic = st.checkbox('Search by meaning', help='Searches errors and active solution text using CPU embeddings, symptom compatibility and matching words. Common Linux terms in English, German and Russian are supported. Similarity is not proof of the same cause.')
    with st.expander('Search settings'):
        limit = st.slider('Semantic search results', 1, 50, 10)
        scope = st.checkbox('Only current machine / OS / user', value=False)
        view = st.selectbox('Show problems', ['All problems', 'Unresolved occurrences', 'Without an active solution', 'With tested solutions', 'With successful solutions', 'Awaiting feedback'])
        category = st.selectbox('Error category', ['All categories'] + sorted({problem_kind(row['error']) for row in all_rows}),
                                format_func=lambda name: name.replace('_', ' '))
        environments = {row['metadata'].get('environment_id', ''): environment_label(store, row) for row in all_rows}
        environment = st.selectbox('Saved environment', [None] + list(environments), format_func=lambda item: 'All environments' if item is None else environments[item])
        since = st.date_input('Occurrences recorded from', value=None)
    try:
        # Filters must apply before semantic top-K ranking, otherwise valid
        # category/environment matches can disappear behind unrelated results.
        rows = all_rows if semantic and query.strip() else problem_rows(store, query.strip(), False, limit, scope)
        if scope:
            rows = [row for row in rows if row['metadata'].get('environment_id') == store.environment_id]
        dates = {}
        if since:
            occurrences = store.occurrences.get(include=['documents'])['documents']
            for document in occurrences:
                item = json.loads(document)
                try:
                    dates.setdefault(item['error_key'], []).append(datetime.fromisoformat(item['timestamp']).date())
                except ValueError:
                    pass
        rows = filtered_problems(rows, summaries, view, category, environment, since, dates)
        if semantic and query.strip():
            with st.spinner('Searching error and solution knowledge…'):
                rows = store.search_problems(query.strip(), limit, eligible_ids=[row['id'] for row in rows])
            st.caption('Ranked by compatible symptoms, semantic similarity and matching words in errors or solutions. Distance is not a success rating. Filters apply before the result limit.')
        st.caption(f'{len(rows)} matching problems. The solution column shows the highest-rated suitable active solution; inspect a problem below to see every version.')
        if rows:
            from core.problem_actions import groups, policy, save_policy
            table = [table_entry(store, row, summaries[row['id']]) for row in rows]
            with st.form('problem-actions-form'):
                edited = st.data_editor(table, width='stretch', hide_index=True,
                         alt='Saved problems with automation and email notification settings',
                         disabled=[column for column in table[0] if column not in ('Automate', 'Inform')],
                         key='problems-table-' + '-'.join(row['id'] for row in rows),
                         column_config={'Error': st.column_config.TextColumn(width='large'),
                                        'Solution': st.column_config.TextColumn(width='large'),
                                        'Automate': st.column_config.CheckboxColumn('Automate', help='Execute the displayed saved solution when this exact problem recurs on this machine.'),
                                        'Inform': st.column_config.SelectboxColumn('Inform', options=['None'] + list(groups(store)), required=True)})
                submitted_actions = st.form_submit_button('Save problem actions')
            if submitted_actions:
                changes = []
                for row, before, after in zip(rows, table, edited):
                    if (before['Automate'], before['Inform']) == (after['Automate'], after['Inform']):
                        continue
                    current = policy(store, row['id'])
                    suitable = [item for item in summaries[row['id']]['solutions'] if not item.get('obsolete') and item.get('context_valid', True)]
                    solution_id = current.get('solution_id') if before['Automate'] else (suitable[0]['id'] if suitable else None)
                    changes.append((row['id'], after['Automate'], after['Inform'], solution_id))
                for problem_id, automate, inform, solution_id in changes:
                    save_policy(store, problem_id, automate, inform, solution_id)
                flash('Problem automation and notification settings saved.')
            st.caption('Save changes to enable automatic execution on the running monitor. Automation uses the displayed solution at the time it is enabled; edited or obsolete solutions require review. Inform sends one email per grouped incident.')
        else:
            st.info('No matching problems. Clear the search or add a problem below.')
        st.caption('Environment identifies the machine, operating system and user for a saved problem. '
                   'It helps keep fixes from different systems separate. Older records may have no environment information.')
    except Exception as exc:
        st.error(f'Could not read history: {exc}')
        with st.expander('Error details'):
            st.code(traceback.format_exc(), language='text')
        st.stop()
    from core.problem_actions import groups, save_groups
    with st.expander('Email groups'):
        st.caption('Create named groups, then select a group in the Inform column. Separate email addresses with commas.')
        with st.form('email-groups-form'):
            email_rows = [{'Group': name, 'Email addresses': ', '.join(addresses)} for name, addresses in groups(store).items()]
            if not email_rows:
                email_rows = [{'Group': '', 'Email addresses': ''}]
            edited_groups = st.data_editor(email_rows, num_rows='dynamic', hide_index=True, key='email-groups-editor', alt='Email group names and recipient addresses')
            save_email_groups = st.form_submit_button('Save email groups')
        if save_email_groups:
            try:
                save_groups(store, edited_groups)
                flash('Email groups saved.')
            except ValueError as exc:
                st.error(str(exc))
        st.caption('SMTP settings, email aliases and templates are available in Watchdog configuration → Email configuration.')
    add_problem_form(store)
    if not rows:
        return
    st.subheader('Inspect a saved problem')
    options = {row['id']: row for row in rows}
    if st.session_state.get('problem-choice') not in options:
        st.session_state['problem-choice'] = rows[0]['id']
    key = st.selectbox('Problem to inspect', list(options), key='problem-choice',
                       format_func=lambda item: options[item]['error'][:160] + ' · ' + environment_label(store, options[item]))
    selected = rows.index(options[key])
    summary = summaries[key]
    st.subheader('Selected problem')
    st.text(rows[selected]['error'])
    columns = st.columns(5)
    for column, title, value in zip(columns, ['Occurrences', 'Solved', 'Still failing', 'Cannot verify', 'No feedback'],
                                    [summary['seen'], summary['solved'], summary['failed'], summary['unsure'], summary['unreported']]):
        column.metric(title, value)
    st.caption(f"Solved without an available saved solution: {summary['solved_without_saved_solution']}. Counts are user-reported; missing feedback is not failure.")
    st.subheader('Saved solutions')
    if not summary['solutions']:
        st.info('No solution saved for this problem.')
    for number, solution in enumerate(summary['solutions'], 1):
        show_solution(store, key, solution, number)
    with st.expander('Add a solution'):
        solution_editor(store, key, None)
    with st.expander('Record solution feedback'):
        feedback_form(store, key, summary['solutions'])
    with st.expander('Why similar cases match'):
        st.caption('Semantic similarity only suggests related symptoms. Other environments provide reference knowledge and require compatibility checks and fresh diagnostics.')
        if st.button('Find related cases', key='related-' + key):
            saved_environment = options[key]['metadata'].get('environment_id')
            matches = store.related(options[key]['error'], key, environment_id=saved_environment, include_other_environments=True)
            if not matches:
                st.info('No related cases found for this saved environment.' if saved_environment else 'The environment of this older record is unknown; automatic related-case matching is unavailable.')
            for match in matches:
                st.text(match['error'])
                st.caption('Why matched: ' + match['match_reason'])
                st.caption('Differences: ' + match['difference'])
                st.caption(f"Occurrences: {match['summary']['seen']} · Solved: {match['summary']['solved']} · Distance: {match['distance']:.3f}")
    details = store.problem_details(key)
    st.subheader('Investigation history')
    if not details['investigations']:
        st.info('No investigation timeline was recorded for this problem. Earlier occurrences and feedback are shown below.')
    for case in details['investigations']:
        with st.expander(f"{case['case_number']} · {case['started']} · {case['status']}"):
            show_case(case, key_prefix='problem-')
    with st.expander('Occurrence and feedback records'):
        if details['occurrences']:
            st.dataframe([{'Detected': item['timestamp'], 'Source': item['filepath'], 'Error': item['line'], 'Matched triggers': ' | '.join(item.get('triggers', []))} for item in details['occurrences']], hide_index=True, alt='Recorded problem occurrences')
        if details['attempts']:
            st.dataframe([{'Reported': item['timestamp'], 'Outcome': OUTCOMES[item['outcome']], 'Solution': item['solution_id'] or 'Not saved',
                           'Case': 'Case #' + item['case_id'][:8].upper() if item.get('case_id') else 'Older record'} for item in details['attempts']], hide_index=True, alt='User feedback records')
    with st.expander('Problem details'):
        st.json(rows[selected])
    st.download_button('Export selected case as JSON',
                       json.dumps(dict(problem=rows[selected], statistics=summary, history=details), indent=2),
                       file_name=f'logent-case-{key[:12]}.json', mime='application/json')
    deletion_controls(store, problem_id=key, prefix='problem-')


def email_configuration():
    from core.email_config import load_smtp, save_smtp, contacts, save_contacts, DEMO_CONTACTS
    from core.problem_actions import groups, save_groups
    st.subheader('SMTP settings')
    try:
        settings = load_smtp()
        store = st.connection('logent_history', type=LogentConnection, path=str(Path(config.CHROMA_PATH).resolve())).history
    except Exception as exc:
        st.error(f'Could not load email configuration: {exc}')
        return
    st.caption('Grayed-out examples are demo values. Save your settings to enable email delivery.')
    if st.session_state.pop('clear-smtp-password-input', False):
        st.session_state['smtp-password'] = ''
    with st.form('smtp-settings-form'):
        host = st.text_input('SMTP server', value=settings.get('host', ''), placeholder='smtp.example.com')
        port = st.number_input('SMTP port', min_value=1, max_value=65535, value=settings.get('port', 587))
        sender = st.text_input('Sender email address', value=settings.get('sender', ''), placeholder='logent@example.com')
        modes = ['starttls', 'ssl', 'plain']
        security = st.selectbox('SMTP security', modes, index=modes.index(settings.get('security', 'starttls')), format_func=lambda item: {'starttls': 'STARTTLS', 'ssl': 'SSL / TLS', 'plain': 'Plain'}[item])
        username = st.text_input('SMTP username', value=settings.get('username', ''), placeholder='logent@example.com')
        password = st.text_input('SMTP password', type='password', key='smtp-password', placeholder='Leave blank to keep the saved password', help='Stored in a local file readable only by the current user.')
        clear_password = st.checkbox('Clear saved SMTP password')
        submitted = st.form_submit_button('Save SMTP settings')
    if submitted:
        try:
            save_smtp(dict(host=host, port=port, sender=sender, security=security, username=username, password=password), clear_password=clear_password)
            st.session_state['clear-smtp-password-input'] = True
            flash('SMTP settings saved. The monitor reads them for the next notification.')
        except (ValueError, OSError) as exc:
            st.error(f'Could not save SMTP settings: {exc}')
    st.subheader('Email addresses and templates')
    st.caption('Demo configuration — disabled examples; these addresses are not active recipients.')
    st.data_editor(DEMO_CONTACTS, disabled=True, hide_index=True, key='email-addresses-demo',
                   column_config={'Alias': st.column_config.TextColumn('Alias'),
                                  'Email address': st.column_config.TextColumn('Email address'),
                                  'Template': st.column_config.TextColumn('Template', width='large')},
                   alt='Disabled demo email address and notification template')
    st.caption('Template placeholders: {alias}, {error}, {source}, {detected}, {case_number}, {trace}. A blank template uses the demo template.')
    with st.form('email-addresses-form'):
        address_rows = contacts(store) or [{'Alias': '', 'Email address': '', 'Template': ''}]
        edited_addresses = st.data_editor(address_rows, num_rows='dynamic', hide_index=True, key='email-addresses-editor',
                                         column_config={'Template': st.column_config.TextColumn(width='large')},
                                         alt='Editable email aliases, addresses and notification templates')
        submitted_addresses = st.form_submit_button('Save email addresses')
    if submitted_addresses:
        try:
            save_contacts(store, edited_addresses)
            flash('Email addresses and templates saved.')
        except ValueError as exc:
            st.error(str(exc))
    with st.expander('Email groups for Inform'):
        st.caption('Enter recipient aliases from the list above or email addresses, separated by commas. Groups save the resolved addresses; update a group after changing a contact address.')
        with st.form('configuration-email-groups-form'):
            group_rows = [{'Group': name, 'Email addresses': ', '.join(addresses)} for name, addresses in groups(store).items()] or [{'Group': '', 'Email addresses': ''}]
            edited_groups = st.data_editor(group_rows, num_rows='dynamic', hide_index=True, key='configuration-email-groups-editor', alt='Email groups selectable in the Inform column')
            submitted_groups = st.form_submit_button('Save configuration email groups')
        if submitted_groups:
            try:
                save_groups(store, edited_groups)
                flash('Email groups saved.')
            except ValueError as exc:
                st.error(str(exc))


def watchdog_page():
    st.subheader('Watchdog detection triggers')
    st.markdown('A log line is detected when it matches **any enabled trigger**. Matching ignores letter case. '
                'Use **Keyword** for a whole word or phrase, or **Regex** for an advanced pattern.')
    st.caption('An enabled Ignore rule overrides Detect rules for that line. Ignored matches remain visible in monitoring health.')
    st.caption('Examples: error, err, warn, warning, kernel panic, exception, severe. '
               'Warnings may include harmless events; disable them if they create too much noise.')
    try:
        rules = load_rules()
    except Exception as exc:
        st.error(f'Could not read saved triggers: {exc}. Default rules are shown; save to replace the invalid file.')
        rules = default_rules()
    st.caption(f"{sum(rule['enabled'] for rule in rules)} enabled triggers · Configuration: {config.WATCHDOG_RULES_FILE}")
    st.markdown('Edit cells directly. Uncheck **Enabled** to pause a rule. Select a row and delete it to remove it; '
                'use the empty row at the bottom to add one. Changes apply only after **Save triggers**.')
    with st.form('watchdog-settings'):
        edited = st.data_editor([dict(rule, action=rule.get('action', 'Detect')) for rule in rules], key='watchdog-editor', num_rows='dynamic', hide_index=True,
                                column_config={'enabled': st.column_config.CheckboxColumn('Enabled', default=True),
                                               'kind': st.column_config.SelectboxColumn('Match type', options=['Keyword', 'Regex'], default='Keyword', required=True),
                                               'action': st.column_config.SelectboxColumn('Action', options=['Detect', 'Ignore'], default='Detect', required=True),
                                               'pattern': st.column_config.TextColumn('Keyword or regex', width='large', required=True)},
                                alt='Editable watchdog detection triggers')
        sample = st.text_area('Test log lines', help='Paste one or more lines to preview which unsaved triggers match.')
        test = st.form_submit_button('Test triggers')
        save = st.form_submit_button('Save triggers')
    if test or save:
        try:
            valid = validate_rules(edited)
            if save:
                save_rules(valid)
                st.success('Triggers saved. Monitors started with this version reload them when the next log content arrives. '
                           'Restart any monitor that was already running before this feature was installed.')
                if not any(rule['enabled'] for rule in valid):
                    st.warning('All triggers are disabled. No new log lines will be detected.')
            if test:
                preview = []
                for line in sample.splitlines():
                    matches, excluded = match_rules(valid, line)
                    preview.append({'Log line': line, 'Detected': bool(matches) and not bool(excluded),
                                    'Matched triggers': ' | '.join(rule['pattern'] for rule in matches),
                                    'Ignore rules': ' | '.join(rule['pattern'] for rule in excluded)})
                if preview:
                    st.dataframe(preview, hide_index=True, alt='Trigger matches for test log lines')
                else:
                    st.info('Paste a log line to test.')
        except Exception as exc:
            st.error(f'Could not apply triggers: {exc}')
    with st.expander('Default Linux detection rules'):
        st.caption('Includes memory exhaustion, kernel crashes, filesystem faults, network and service failures. '
                   'Restoring defaults replaces your saved trigger list.')
        if st.button('Restore default triggers'):
            try:
                save_rules(default_rules())
                st.session_state.pop('watchdog-editor', None)
                st.rerun()
            except Exception as exc:
                st.error(f'Could not restore triggers: {exc}')
    with st.expander('Monitored log sources'):
        st.caption('One file or directory per line. Directories are monitored recursively. Use # for comments. '
                   'Missing paths are skipped by the monitor. Source changes require a monitor restart.')
        try:
            text = Path(config.LOGS_CONFIG_FILE).read_text()
            with st.form('log-sources'):
                edited_sources = st.text_area('Files and directories to monitor', value=text, height=180)
                check = st.form_submit_button('Check sources')
                save = st.form_submit_button('Save sources')
            paths = parse_sources(edited_sources, config.LOGS_CONFIG_FILE)
            if check or save:
                if save:
                    save_sources(edited_sources, config.LOGS_CONFIG_FILE)
                    st.success('Log sources saved. Restart the terminal monitor to use this list.')
                st.dataframe(source_status(paths), hide_index=True, alt='Log source availability and readability')
        except OSError as exc:
            st.error(f'Could not read log sources: {exc}')
        except ValueError as exc:
            st.error(str(exc))

    with st.expander('Email configuration'):
        email_configuration()


def show_case(case, key_prefix='case-'):
    st.markdown(' → '.join('**' + stage + '**' if stage == case['stage'] else stage for stage in PIPELINE))
    st.text(case['error'])
    st.caption(f"{case['case_number']} · Source: {case['source']} · Detected: {case['detected']}")
    if case.get('recurrence_of_case'):
        st.warning('Reported again after ' + case['recurrence_of_case'] + ' was marked resolved. This does not automatically assign failure to the earlier fix.')
    st.markdown(f"**Status:** {case['status']} · **Conclusion:** {case['conclusion']} · **Feedback:** {OUTCOMES.get(case['outcome'], case['outcome'])}")
    st.text('Cause: ' + case['cause'])
    with st.container(horizontal=True):
        st.metric('Symptom', SYMPTOMS.get(case.get('symptom_assessment', {}).get('status'), 'Not recorded'), border=True)
        st.metric('Cause', CAUSES.get(case.get('cause_assessment', {}).get('status'), 'Not recorded'), border=True)
    for field in ('symptom_assessment', 'cause_assessment'):
        if case.get(field):
            st.text(case[field].get('explanation', ''))
    if case.get('feedback_scope') == 'custom_solution':
        st.caption('The final feedback concerns a user-supplied solution. Automatic verification, if present, concerns the commands applied earlier; see the timeline for both outcomes.')
    if case.get('conflicting_outcomes'):
        st.warning('User feedback and automatic checks disagree. This case needs review; neither result is hidden.')
    st.caption('Next: ' + case['next_step'])
    if case.get('verification'):
        status = {'disproved': 'Original problem no longer observed', 'confirmed': 'Original problem still present', 'inconclusive': 'Cannot verify'}[case['verification']['status']]
        st.text('Automatic verification: ' + status)
        st.text(case['verification']['explanation'])
    else:
        st.caption('No automatic verification recorded. User feedback is not an automatic verification.')
    st.caption(f"Occurrences grouped: {case.get('incident_count', len(case.get('occurrence_ids', [])))}. Trigger matches: " + (' | '.join(case.get('triggers', [])) or 'Not recorded'))
    steps = case['steps']
    action_steps = [step for step in steps if step['stage'].startswith('automatic_') or step['stage'] in ('automation_failed', 'email_notification')]
    if action_steps:
        with st.expander('Automatic execution and notification log', expanded=True):
            for step in action_steps:
                st.caption(step['time'] + ' · ' + step['stage'].replace('_', ' '))
                data = step['data']
                if step['stage'] == 'automatic_fix_result':
                    st.code('$ ' + data['command'] + '\n' + (data.get('output') or '(no output)'), language='text')
                    st.text('Command succeeded' if data['success'] else 'Command failed')
                elif isinstance(data, str):
                    st.text(data)
                else:
                    st.json(data)
    if steps:
        st.dataframe([{'Time': step['time'], 'Step': step['stage'].replace('_', ' ')} for step in steps], hide_index=True, alt='Investigation timeline')
        selected = st.selectbox('Timeline step', range(len(steps)), format_func=lambda index: f"{steps[index]['time']} · {steps[index]['stage'].replace('_', ' ')}",
                                key=key_prefix + case['id'] + '-step')
        step = steps[selected]
        if step['stage'].endswith('_complete'):
            for item in step['data']:
                st.code('$ ' + item['command'], language='bash')
                st.caption(f"Captured {item['original_characters']} characters; selected {item['selected_characters']} for the LLM. "
                           + ('Output was shortened.' if item['shortened'] else 'All output included.'))
                selected_tab, complete_tab = st.tabs(['Selected evidence for assessment', 'Complete captured output'])
                with selected_tab:
                    st.code(item['selected_output'], language='text')
                with complete_tab:
                    st.code(item['full_output'] or '(no output)', language='text')
        elif isinstance(step['data'], str):
            st.code(step['data'], language='text')
        else:
            st.json(step['data'])
    st.download_button('Export investigation JSON', json.dumps(case, indent=2),
                       file_name=case['case_number'].replace(' #', '-') + '.json', mime='application/json', key=key_prefix + case['id'] + '-export')


def cases_page():
    st.subheader('Traced problem')
    st.caption('A case is one review of a problem. It can contain several repeated occurrences. '
               'The case number is shared with the terminal and exported reports.')
    try:
        check_database_version()
        store = st.connection('logent_history', type=LogentConnection, path=str(Path(config.CHROMA_PATH).resolve())).history
        cases = store.case_records()
    except Exception as exc:
        st.error(f'Could not load investigations: {exc}')
        return
    query = st.text_input('Find a case', placeholder='Case number, error or source')
    status = st.selectbox('Investigation status', ['All', 'in_progress', 'resolved', 'unresolved', 'needs_review', 'watching', 'ignored', 'cancelled'])
    cases = [case for case in cases if (status == 'All' or case['status'] == status) and
             (not query or query.casefold() in ' '.join([case['case_number'], case['error'], case['source']]).casefold())]
    if not cases:
        st.info('No matching investigations. New terminal investigations will record their timelines here. Existing problem and solution history remains on the first tab.')
        return
    st.dataframe([{'Case': case['case_number'], 'Started': case['started'], 'Error': case['error'], 'Status': case['status'], 'Stage': case['stage']} for case in cases], hide_index=True, alt='Investigation cases')
    options = {case['id']: case for case in cases}
    selected = st.selectbox('Investigation to inspect', list(options), format_func=lambda item: options[item]['case_number'] + ' · ' + options[item]['error'][:120])
    show_case(options[selected])
    deletion_controls(store, case_id=selected, prefix='case-')


@st.fragment(run_every='3s')
def health_page():
    st.subheader('Monitoring health')
    st.caption('Refreshes every three seconds. A running heartbeat means a monitor process is alive; it does not mean every source is readable.')
    snapshots = monitor_snapshots(config.RUNTIME_PATH)
    live = [snapshot for snapshot in snapshots if snapshot['live']]
    if not live:
        st.info('No current monitor heartbeat. Start or restart ./run.sh in an interactive terminal to enable monitoring and this health view.')
    try:
        rules = load_rules()
        with st.container(horizontal=True):
            st.metric('Running monitors', len(live), border=True)
            st.metric('Configured detection triggers', sum(rule['enabled'] and rule.get('action', 'Detect') == 'Detect' for rule in rules), border=True)
            st.metric('Ignore rules', sum(rule['enabled'] and rule.get('action') == 'Ignore' for rule in rules), border=True)
    except Exception as exc:
        st.error(f'Could not read trigger settings: {exc}')
    for snapshot in snapshots:
        with st.container(border=True):
            st.markdown(f"**Monitor {snapshot['pid']} — {'Running' if snapshot['live'] else 'Stopped' if snapshot['state'] == 'Stopped' else 'Heartbeat stale'}**")
            st.caption('Last heartbeat: ' + datetime.fromtimestamp(snapshot['heartbeat']).astimezone().isoformat())
            st.text('Stage: ' + snapshot['stage'])
            if snapshot.get('active_rules'):
                rules = snapshot['active_rules']
                st.caption(f"Loaded detection rules: {rules['detection_count']} · Loaded ignore rules: {rules['ignore_count']} · Last loaded: {rules['loaded_at'] or 'Startup defaults'}")
                if rules['error']:
                    st.warning('Rules could not be reloaded; the monitor retained its previous rules: ' + rules['error'])
            if snapshot.get('current_case'):
                case = snapshot['current_case']
                st.text(case['case_number'] + ' · ' + case['error'])
                st.caption('Next: ' + case['next_step'])
            st.caption(f"Detected: {snapshot['detected_count']} · Excluded by ignore rules: {snapshot['ignored_count']} · "
                       f"Queued: {snapshot.get('queued_count', 0)} · Waiting in inbox: {snapshot.get('pending_count', len(snapshot.get('pending', [])))}")
            st.dataframe(snapshot['sources'], hide_index=True, alt='Sources watched by this monitor')
            pending = snapshot.get('pending', []) + snapshot.get('queued_events', [])
            if pending:
                st.markdown('**Pending events**')
                st.dataframe(pending, hide_index=True, alt='Events awaiting investigation')
            if snapshot['recent_events']:
                st.markdown('**Recent trigger decisions**')
                st.dataframe([{'Detected': event['detected'], 'Error': event['error'], 'Source': event['source'],
                               'Decision': 'Ignored by rule' if event['ignored'] else 'Detected',
                               'Matched triggers': ' | '.join(event['triggers']), 'Ignore rules': ' | '.join(event.get('ignore_rules', []))}
                              for event in reversed(snapshot['recent_events'])], hide_index=True, alt='Recent log events and their matching triggers')
    if not live:
        try:
            paths = parse_sources(Path(config.LOGS_CONFIG_FILE).read_text(), config.LOGS_CONFIG_FILE)
            st.dataframe(source_status(paths), hide_index=True, alt='Configured source availability checked by the dashboard')
        except (OSError, ValueError) as exc:
            st.error(f'Could not check configured sources: {exc}')


def diagnostics_page():
    st.subheader('Diagnostic scenarios')
    st.caption('Reusable read-only checks establish symptoms and test causes. The agent adapts them to the current environment; each concrete command still requires approval.')
    try:
        check_database_version()
        store = st.connection('logent_history', type=LogentConnection, path=str(Path(config.CHROMA_PATH).resolve())).history
        scenarios = store.list_playbooks()
    except Exception as exc:
        st.error('Could not load diagnostic scenarios: ' + str(exc))
        return
    query = st.text_input('Search diagnostic scenarios', placeholder='Symptom, hypothesis or application')
    rows = [item for item in scenarios if not query or query.casefold() in json.dumps(item).casefold()]
    if rows:
        st.dataframe([{'Scenario': item['title'], 'Category': item['category'], 'Application': item.get('application') or 'Not specified',
                       'Version': item.get('application_version') or 'Not specified', 'Active': item['active'],
                       'Symptom': SYMPTOMS.get(item['diagnosis'].get('symptom', {}).get('status'), 'Not recorded'),
                       'Cause': CAUSES.get(item['diagnosis'].get('cause', {}).get('status'), 'Not recorded'),
                       'Cases': len(item.get('case_ids', []))} for item in rows], hide_index=True, alt='Saved diagnostic scenarios and evidence status')
        by_id = {item['id']: item for item in rows}
        choice = st.selectbox('Scenario to inspect or edit', [None] + list(by_id),
            format_func=lambda id_: 'Create a diagnostic scenario' if id_ is None else by_id[id_]['title'])
        selected = by_id.get(choice)
    else:
        st.info('No matching diagnostic scenarios. New investigations save tested plans here, including rejected hypotheses. You can also add your own checks below.')
        selected = None
    if selected:
        st.text('Reported symptom: ' + selected['symptom'])
        st.text('Hypothesis: ' + selected['title'])
        st.text('Recorded environment: ' + selected['environment'].get('os', 'Unknown'))
        st.caption('Historical observations describe the saved case, not the current system.')
        st.json(selected['diagnosis'])
        if st.button('Deactivate scenario' if selected['active'] else 'Activate scenario', key='active-scenario-' + selected['id']):
            store.set_playbook_active(selected['id'], not selected['active'])
            flash('Diagnostic scenario status updated.')
        st.download_button('Export diagnostic scenario', json.dumps(selected, indent=2), file_name='diagnostic-scenario.json', mime='application/json')
    st.markdown('**Edit as a new version**' if selected else '**Add a diagnostic scenario**')
    with st.form('diagnostic-scenario-editor'):
        symptom = st.text_area('Example error / symptom', value=selected['symptom'] if selected else '')
        title = st.text_input('Hypothesis to test', value=selected['title'] if selected else '')
        app = st.text_input('Application / service', value=selected.get('application', '') if selected else '')
        app_version = st.text_input('Supported application version', value=selected.get('application_version', '') if selected else '')
        commands = st.text_area('Read-only command templates, one per line',
            value='\n'.join(item['command'] for item in selected['commands']) if selected else '',
            help='Use {target_path} for a path parameter. Shell wrappers, repairs, pipes and redirects are rejected.')
        question = st.text_input('Question answered by these checks', value=selected['commands'][0].get('question', '') if selected else '')
        expected = st.text_area('Evidence that would confirm or reject the hypothesis', value=selected['commands'][0].get('expected_result', '') if selected else '')
        target = st.text_input('Target path for validation / preview', value=(reported_paths(symptom) or [''])[0])
        original = (selected.get('verification_checks', []) or [{}])[0] if selected else {}
        kind = st.selectbox('Objective repair criterion', ['No objective file criterion', 'File must exist', 'Required file mode'],
            index={'file_exists':1, 'file_mode':2}.get(original.get('kind'), 0))
        required_mode = st.text_input('Required octal file mode', value=str(original.get('expected', '')) if original.get('kind') == 'file_mode' else '',
            help='Provide a documented application requirement; do not assume 0644 for every file.')
        basis = st.text_input('Source of required state / policy', value=original.get('basis', ''),
            help='For example, an explicit error requirement or a user-verified application configuration.')
        preview = st.form_submit_button('Validate and preview commands')
        save = st.form_submit_button('Save diagnostic scenario')
    if preview or save:
        try:
            if not symptom.strip() or not title.strip() or not commands.strip() or not question.strip() or not expected.strip():
                raise ValueError('Enter the symptom, hypothesis, commands, question and evidence criteria.')
            templates = [dict(command=line.strip(), description=question, question=question, expected_result=expected)
                         for line in commands.splitlines() if line.strip()]
            concrete = render_commands(templates, target)
            for item in concrete:
                st.code(item['command'], language='bash')
            st.success('Read-only command validation passed. No commands were executed.')
            if save:
                verdict = {'symptom': {'status':'inconclusive', 'explanation':'User-authored scenario; no new execution evidence'},
                           'cause': {'status':'inconclusive', 'description':title, 'explanation':'Hypothesis requires fresh checks'},
                           'observations':[], 'uncertainties':['Not executed by this page']}
                # Editing criteria creates a new scenario; historical evidence is retained on the old version.
                checks = []
                if kind != 'No objective file criterion':
                    if not target or not basis.strip():
                        raise ValueError('A target and policy source are required for an objective criterion.')
                    if kind == 'Required file mode' and not re.fullmatch(r'0?[0-7]{3,4}', required_mode):
                        raise ValueError('Enter a valid octal file mode, such as 0640.')
                    checks = [{'kind':'file_exists' if kind == 'File must exist' else 'file_mode', 'target':target,
                               'expected':True if kind == 'File must exist' else required_mode, 'basis':basis}]
                plan = {'hypothesis': title, 'commands': concrete, 'verification_checks':checks}
                id_ = store.save_playbook(symptom, plan, verdict, source='user', application=app, application_version=app_version)
                if selected and selected['id'] != id_:
                    store.set_playbook_active(selected['id'], False)
                flash('Diagnostic scenario saved. Fresh execution and command approval are required before use.')
        except (ValueError, OSError) as exc:
            st.error(str(exc))


def linux_knowledge_page():
    st.subheader('Linux reference knowledge')
    st.caption('Built-in Linux x64 checks and guidance. A match supplies a plan; fresh evidence must establish the current problem. Commands always require approval in the monitor.')
    try:
        check_database_version()
        store = st.connection('logent_history', type=LogentConnection, path=str(Path(config.CHROMA_PATH).resolve())).history
        rows = store.list_linux_library()
    except Exception as exc:
        st.error('Could not load Linux reference knowledge: ' + str(exc))
        return
    with st.container(horizontal=True):
        st.metric('Reference scenarios', len(rows))
        st.metric('Active references', sum(item['active'] for item in rows))
        st.metric('Reference categories', len({item['category'] for item in rows}))
    st.info('Reference examples are not recorded incidents and have no success ratings. Ratings come from your actual investigations and repair feedback.')
    category = st.selectbox('Linux reference category', ['All categories'] + sorted({item['category'] for item in rows}), key='linux-category')
    query = st.text_input('Search Linux references', placeholder='OOM, missing file, DNS, permissions…', key='linux-search')
    semantic = st.checkbox('Search Linux references by meaning', key='linux-semantic', help='Use the same CPU semantic retrieval and common Linux vocabulary as problem search. No LLM request.')
    eligible = [item for item in rows if category == 'All categories' or item['category'] == category]
    if semantic and query.strip():
        visible = store.search_linux_references(query.strip(), eligible_ids=[item['id'] for item in eligible])
    else:
        visible = sorted([item for item in eligible if not query or query.casefold() in json.dumps(item).casefold()], key=lambda item: (item['category'], item['title']))
    if visible:
        st.dataframe([{'Problem': item['title'], 'Category': item['category'], 'Example': item['symptom'],
                       'Active': item['active'], 'Assessment': 'Local evidence rule' if item['rule'] else 'LLM evidence assessment',
                       'Version': item['version'], **({'Search match':item['match_reason']} if 'match_reason' in item else {})} for item in visible], hide_index=True, alt='Categorized Linux reference checks and guidance')
        by_id = {item['id']: item for item in visible}
        selected_id = st.selectbox('Linux reference to inspect', list(by_id), format_func=lambda id_: by_id[id_]['title'], key='linux-reference')
        selected = by_id[selected_id]
        with st.container(border=True):
            st.markdown('**' + selected['title'] + '**')
            st.text('Example: ' + selected['symptom'])
            st.caption('Read-only templates; parameters are filled only when the error identifies an unambiguous target.')
            for command in selected['commands']:
                st.code(command['command'], language='bash')
                st.caption(command['description'])
            st.markdown('**Repair guidance — requires a confirmed cause**')
            for step in selected['guidance']:
                st.write('• ' + step)
            st.markdown('**Reference documentation**')
            for url in selected['sources']:
                st.markdown('[Documentation](' + url + ')')
            if st.button('Disable Linux reference' if selected['active'] else 'Enable Linux reference', key='linux-active-' + selected_id):
                store.set_linux_reference_active(selected_id, not selected['active'])
                flash('Linux reference status updated. The next investigation will use this setting.')
            st.download_button('Export Linux reference', json.dumps(selected, indent=2), file_name=selected_id + '.json', mime='application/json')
        st.caption('To customize checks, add your own version in Diagnostic scenarios. Disabling a reference preserves its data and previous investigations.')
    else:
        st.info('No Linux references match these filters.')
    with st.form('linux-match-preview'):
        sample = st.text_input('Test an error against local knowledge', placeholder='ERROR: screen is not installed.')
        preview = st.form_submit_button('Preview local diagnostic plan')
    if preview and sample.strip():
        plan = store.linux_diagnostic_plan(sample)
        if plan:
            st.success('Local plan found: ' + plan['library_category'] + ' — ' + plan['hypothesis'])
            for command in plan['commands']:
                st.code(command['command'], language='bash')
            st.caption('Preview only: no commands or LLM requests were executed. Evidence may still require LLM assessment.')
        else:
            st.info('No unambiguous applicable local plan. The monitor will ask the LLM after you confirm this error.')


def main():
    st.set_page_config(page_title='Logent history', page_icon='📋', layout='wide')
    st.title('Logent — problems and solutions')
    st.caption('Browse saved errors, inspect ratings, and maintain solutions.')
    if 'notice' in st.session_state:
        st.success(st.session_state.pop('notice'))
    history_tab, watchdog_tab, cases_tab, health_tab, diagnostics_tab, linux_tab = st.tabs(['Problems and solutions', 'Watchdog configuration', 'Traced problem', 'Monitoring health', 'Diagnostic scenarios', 'Linux knowledge'])
    with history_tab:
        history_page()
    with cases_tab:
        cases_page()
    with health_tab:
        health_page()
    with watchdog_tab:
        watchdog_page()
    with diagnostics_tab:
        diagnostics_page()
    with linux_tab:
        linux_knowledge_page()


if __name__ == '__main__':
    main()
