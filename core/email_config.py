"""Local SMTP credentials and reusable notification templates."""
import json
import os
from pathlib import Path
import re
from string import Formatter
import tempfile

import config

DEFAULT_TEMPLATE = 'Hello {alias},\n\nLogent detected a problem:\n{error}\n\nSource: {source}\nDetected: {detected}\nCase: {case_number}\n\n{trace}'
TEMPLATE_FIELDS = {'alias', 'error', 'source', 'detected', 'case_number', 'trace'}
DEMO_CONTACTS = [{'Alias': 'Operations', 'Email address': 'operations@example.com', 'Template': DEFAULT_TEMPLATE}]


def smtp_path():
    return Path(config.RUNTIME_PATH) / 'email' / 'smtp-settings.json'


def load_smtp():
    path = smtp_path()
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    security = os.environ.get('LOGENT_SMTP_SECURITY', 'starttls')
    return {'host': os.environ.get('LOGENT_SMTP_HOST', ''), 'port': int(os.environ.get('LOGENT_SMTP_PORT', '465' if security == 'ssl' else '587')),
            'sender': os.environ.get('LOGENT_SMTP_FROM', ''), 'security': security,
            'username': os.environ.get('LOGENT_SMTP_USER', ''), 'password': os.environ.get('LOGENT_SMTP_PASSWORD', '')}


def valid_address(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+', value))


def save_smtp(values, *, clear_password=False):
    data = {field: str(values.get(field, '')).strip() for field in ('host', 'sender', 'username', 'security')}
    if not data['host'] or any(char.isspace() for char in data['host']) or not valid_address(data['sender']):
        raise ValueError('Enter an SMTP hostname and a valid sender email address.')
    if data['security'] not in ('starttls', 'ssl', 'plain'):
        raise ValueError('Select STARTTLS, SSL or plain transport.')
    if '\r' in data['username'] or '\n' in data['username']:
        raise ValueError('SMTP username must be a single line.')
    data['port'] = int(values['port'])
    if not 1 <= data['port'] <= 65535:
        raise ValueError('SMTP port must be between 1 and 65535.')
    data['password'] = '' if clear_password else (values.get('password') or load_smtp().get('password', ''))
    path = smtp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.smtp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def contacts(store):
    from core.problem_actions import read
    return read(store, 'email-contacts', {'contacts': []})['contacts']


def save_contacts(store, rows):
    from core.problem_actions import write
    result, aliases, addresses = [], set(), set()
    for row in rows:
        alias = str(row.get('Alias', '')).strip()
        address = str(row.get('Email address', '')).strip()
        template = str(row.get('Template', '')).strip()
        if not alias and not address and not template:
            continue
        if not alias or alias in aliases or '\r' in alias or '\n' in alias:
            raise ValueError('Enter unique, single-line aliases.')
        if not valid_address(address) or address.casefold() in addresses:
            raise ValueError(f'{alias}: enter a valid, unique email address.')
        template = template or DEFAULT_TEMPLATE
        try:
            for _, field, spec, conversion in Formatter().parse(template):
                if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                    raise ValueError('Use only the supported template placeholders, without format modifiers.')
        except ValueError as exc:
            raise ValueError(f'{alias}: invalid template: {exc}') from exc
        aliases.add(alias)
        addresses.add(address.casefold())
        result.append({'Alias': alias, 'Email address': address, 'Template': template})
    write(store, 'email-contacts', {'contacts': result})
