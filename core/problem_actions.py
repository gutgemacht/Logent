"""Explicit per-problem automation and SMTP notification settings."""
import hashlib
import json
import re
import smtplib
import ssl
from email.message import EmailMessage
from uuid import uuid4

import config
from core.history import normalize_error
from core.executor import run_command
from core.error_context import validate_solution_context
from core.system_context import check_conditions, file_facts


def collection(store):
    return store.client.get_or_create_collection('logent_problem_actions', embedding_function=None)


def read(store, id_, default=None):
    rows = collection(store).get(ids=[id_], include=['documents'])
    return json.loads(rows['documents'][0]) if rows['ids'] else (default or {})


def write(store, id_, data):
    collection(store).upsert(ids=[id_], documents=[json.dumps(data)], embeddings=[[0.0]])


def groups(store):
    return read(store, 'email-groups', {'groups': {}})['groups']


def save_groups(store, rows):
    result = {}
    for row in rows:
        name = str(row.get('Group', '')).strip()
        raw = str(row.get('Email addresses', '')).strip()
        if not name and not raw:
            continue
        if not name or name == 'None' or name in result or '\n' in name or '\r' in name:
            raise ValueError('Enter unique group names (other than None).')
        from core.email_config import contacts
        known = {item['Alias']: item['Email address'] for item in contacts(store)}
        addresses = list(dict.fromkeys(known.get(part.strip(), part.strip()) for part in re.split(r'[,;\n]', raw) if part.strip()))
        if not addresses or any(not re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+', address) for address in addresses):
            raise ValueError(f'{name}: enter valid email addresses separated by commas.')
        result[name] = addresses
    write(store, 'email-groups', {'groups': result})


def policy(store, key):
    return read(store, 'problem-' + key, {'automate': False, 'inform': 'None'})


def save_policy(store, key, automate, inform, solution_id=None):
    if inform != 'None' and inform not in groups(store):
        raise ValueError('Email group no longer exists.')
    previous = policy(store, key)
    if automate:
        solution = store.get_solution(solution_id)
        if solution['error_key'] != key or solution.get('obsolete') or not solution.get('commands') or solution.get('manual_steps'):
            raise ValueError('Automation requires an active solution with commands and no manual steps.')
        problem = store.errors.get(ids=[key], include=['metadatas', 'documents'])
        if not problem['ids'] or problem['metadatas'][0].get('environment_id') != store.environment_id:
            raise ValueError('Automation can only be enabled for this machine / OS / user.')
        validate_solution_context(problem['documents'][0], solution['commands'])
    write(store, 'problem-' + key, {'automate': bool(automate), 'inform': inform,
          'solution_id': solution_id if automate else previous.get('solution_id'), 'environment_id': store.environment_id})


def send_notification(addresses, event, session, *, templates=None):
    from core.email_config import load_smtp
    settings = load_smtp()
    host, sender = settings['host'], settings['sender']
    if not host or not sender:
        raise ValueError('Configure SMTP on the Watchdog configuration page to send notifications.')
    mode = settings['security']
    if mode not in ('starttls', 'ssl', 'plain'):
        raise ValueError('SMTP security must be starttls, ssl or plain.')
    port = int(settings['port'])
    message = EmailMessage()
    message['From'] = sender
    message['To'] = ', '.join(addresses)
    message['Subject'] = 'Logent detected problem — ' + session.data['case_number']
    message.set_content(f"Problem: {session.data['error']}\nSource: {event.filepath}\nDetected: {event.timestamp}\n\nTrace:\n" + json.dumps(session.data['steps'], indent=2))
    factory = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
    kwargs = {'context': ssl.create_default_context()} if mode == 'ssl' else {}
    with factory(host, port, timeout=20, **kwargs) as client:
        if mode == 'starttls':
            client.starttls(context=ssl.create_default_context())
        username = settings.get('username')
        if username:
            client.login(username, settings.get('password', ''))
        if templates:
            from core.email_config import DEFAULT_TEMPLATE
            for address in addresses:
                contact = templates.get(address.casefold(), {})
                personal = EmailMessage()
                personal['From'] = sender
                personal['To'] = address
                personal['Subject'] = message['Subject']
                personal.set_content(contact.get('Template', DEFAULT_TEMPLATE).format_map({
                    'alias': contact.get('Alias', address), 'error': session.data['error'],
                    'source': event.filepath, 'detected': event.timestamp,
                    'case_number': session.data['case_number'], 'trace': json.dumps(session.data['steps'], indent=2)}))
                rejected = client.send_message(personal)
                if rejected:
                    raise RuntimeError('SMTP rejected recipients: ' + ', '.join(rejected))
        else:
            rejected = client.send_message(message)
            if rejected:
                raise RuntimeError('SMTP rejected recipients: ' + ', '.join(rejected))


def process(store, event, session, batch=None):
    """Return True when an opted-in repair was handled, without terminal prompts."""
    key = hashlib.sha256((store.environment_id + ':' + normalize_error(event.line)).encode()).hexdigest()
    settings = policy(store, key)
    if not settings.get('automate') and settings.get('inform', 'None') == 'None':
        return False
    session.data['error_key'] = key
    handled = bool(settings.get('automate'))
    if handled:
        occurrences = [store.record_occurrence(item)[1] for item in (batch or [event])]
        session.data.update(occurrence_ids=occurrences, stage='Automatic repair', automated=True)
        store.save_investigation(session.data)
        execution = []
        attempt_id = str(uuid4())
        solution = None
        try:
            if settings.get('environment_id') != store.environment_id:
                raise ValueError('Automation belongs to a different environment.')
            solution = store.get_solution(settings['solution_id'])
            if solution['error_key'] != key or solution.get('obsolete') or not solution.get('commands') or solution.get('manual_steps'):
                raise ValueError('Selected solution was changed, removed, obsolete, or requires manual steps. Re-enable automation after review.')
            validate_solution_context(event.line, solution['commands'])
            if solution.get('conditions'):
                valid, reason = check_conditions(solution['conditions'], event.line, file_facts(event.line), store.context)
                if not valid:
                    raise ValueError(reason)
            session.add('automatic_solution', {'solution_id': solution['id'], 'notes': solution['notes']})
            session.data.update(solution_id=solution['id'], attempt_id=attempt_id)
            store.record_attempt(key, occurrences[0], solution['id'], 'not_reported', case_id=session.data['id'], attempt_id=attempt_id, occurrence_ids=occurrences)
            for item in solution['commands']:
                command = item['command']
                if command.startswith('sudo '):
                    command = 'sudo -n ' + command[5:]
                elif item.get('sudo'):
                    command = 'sudo -n ' + command
                success, output = run_command(command, timeout=config.COMMAND_TIMEOUT)
                result = {'command': command, 'success': success, 'output': output}
                execution.append(result)
                session.add('automatic_fix_result', result)
                store.record_attempt(key, occurrences[0], solution['id'], 'not_reported', execution, case_id=session.data['id'], attempt_id=attempt_id, occurrence_ids=occurrences)
                store.save_investigation(session.data)
                if not success:
                    break
            session.add('automatic_result', 'Commands completed; resolution needs verification.' if all(r['success'] for r in execution) else 'Command failed; remaining commands stopped.')
        except Exception as exc:
            session.add('automation_failed', str(exc))
        session.data['stage'] = 'Automatic repair finished'
    group = settings.get('inform', 'None')
    if group != 'None':
        try:
            addresses = groups(store).get(group)
            if not addresses:
                raise ValueError('Selected email group was removed or is empty.')
            from core.email_config import contacts
            templates = {item['Email address'].casefold(): item for item in contacts(store)}
            send_notification(addresses, event, session, templates=templates)
            session.add('email_notification', {'group': group, 'status': 'sent', 'recipients': len(addresses)})
        except Exception as exc:
            session.add('email_notification', {'group': group, 'status': 'failed', 'error': str(exc)})
    return handled
