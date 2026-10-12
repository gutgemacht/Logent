"""Structured troubleshooting knowledge, conservative checks and bounded context."""
from __future__ import annotations

import json
import re
import shlex
from core.diagnostic_commands import validate_diagnostic_commands
from core.error_context import problem_kind, reported_paths

SCHEMA_VERSION = 2
SYMPTOMS = {'confirmed': 'Confirmed', 'not_reproduced': 'Not reproduced', 'inconclusive': 'Inconclusive'}
CAUSES = {'confirmed': 'Confirmed', 'rejected': 'Rejected', 'inconclusive': 'Inconclusive'}


def redact(value):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.search(r'^(?:password|passwd|token|secret|api[_-]?key|authorization)$', key, re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(\b(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)([^\s,;]+)', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(\bauthorization\s*:\s*)(?:bearer|basic)\s+\S+', r'\1[REDACTED]', value)
        value = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', value)
    return value


def assessment(verdict):
    """Read old exported verdicts without migrating or inventing new evidence."""
    if 'symptom' in verdict and 'cause' in verdict:
        return verdict
    status = verdict.get('status', 'inconclusive')
    explanation = verdict.get('explanation', '')
    return dict(verdict, symptom={'status': {'disproved': 'not_reproduced'}.get(status, status), 'explanation': explanation},
                cause={'status': {'disproved': 'rejected'}.get(status, status), 'description': '', 'explanation': explanation}, legacy=True)


def template_commands(commands, error):
    """Create reusable read-only templates only for a single unambiguous target."""
    paths = reported_paths(error)
    result = []
    for item in commands:
        template = dict(item)
        argv = shlex.split(item['command'])
        if len(paths) == 1 and paths[0] in argv:
            template['command'] = ' '.join('{target_path}' if arg == paths[0] else shlex.quote(arg) for arg in argv)
        result.append(template)
    return result


def render_commands(commands, target):
    result = []
    for item in commands:
        argv = shlex.split(item['command'])
        for index, arg in enumerate(argv):
            if arg == '{target_path}':
                if not target or '\x00' in target or '\n' in target:
                    raise ValueError('An unambiguous target path is required')
                argv[index] = target
            elif '{' in arg or '}' in arg:
                raise ValueError('Only a standalone {target_path} parameter is supported')
        result.append(dict(item, command=shlex.join(argv)))
    validate_diagnostic_commands(result)
    return result


def diagnostic_checks(plan):
    checks = plan.get('verification_checks', [])
    if not isinstance(checks, list):
        raise ValueError('Verification checks must be an array')
    for check in checks:
        if not isinstance(check, dict) or check.get('kind') not in {'file_exists', 'file_mode'} or check.get('target') not in reported_paths(plan.get('_error', '')):
            raise ValueError('Unsupported or unrelated verification criterion')
        if not isinstance(check.get('basis'), str) or not check['basis'].strip():
            raise ValueError('Verification criteria require a stated source for the expected state')
        if check['kind'] == 'file_exists' and not isinstance(check.get('expected'), bool):
            raise ValueError('file_exists expects a boolean')
        if check['kind'] == 'file_mode' and not re.fullmatch(r'0?[0-7]{3,4}', str(check.get('expected', ''))):
            raise ValueError('file_mode expects an octal mode')
        if check['kind'] == 'file_mode':
            requirements = re.findall(r'(?:expected|required|should be|must be)\s*[:=]?\s*(0?[0-7]{3,4})\b', plan.get('_error', ''), re.I)
            supplied = any(item.get('kind') == check['kind'] and item.get('target') == check['target'] and
                           str(item.get('expected')) == str(check['expected']) and item.get('basis') == check['basis']
                           for item in plan.get('_trusted_checks', []))
            if not supplied and not any(int(mode, 8) == int(str(check['expected']), 8) for mode in requirements):
                raise ValueError('Required file mode must be explicitly stated in the current error; do not invent a policy')
    return checks


def trim_evidence(text, limit=12000):
    text = redact(text)
    if len(text) <= limit:
        return text
    marker = '\n[Older evidence omitted from this request; full case timeline retained]\n'
    return text[:2000] + marker + text[-(limit - 2000 - len(marker)):]


def local_check_results(checks, facts):
    results = []
    for check in checks:
        fact = facts.get(check['target'], {})
        actual = fact.get('exists') if check['kind'] == 'file_exists' else fact.get('mode') if fact.get('exists') else None
        expected = check['expected']
        if check['kind'] == 'file_mode' and actual is not None:
            actual, expected = int(actual, 8), int(str(expected), 8)
        results.append(dict(check, actual=actual, state='unknown' if actual is None else 'expected' if actual == expected else 'unexpected'))
    return results


def knowledge_card(error, summary, environment=None):
    solutions = [item for item in summary['solutions'] if not item.get('obsolete') and item.get('context_valid', True)][:2]
    return redact({'error': error, 'category': problem_kind(error), 'environment': environment,
                   'occurrences': summary['seen'], 'incidents': summary.get('incidents'),
                   'solutions': [{key: item.get(key) for key in ('id', 'notes', 'commands', 'manual_steps', 'diagnosis', 'conditions',
                       'verification_commands', 'helped', 'failed', 'unsure', 'last_worked')} for item in solutions]})


def bounded_context(data, limit=8000):
    text = json.dumps(redact(data), ensure_ascii=False)
    if len(text) <= limit:
        return text
    # Do not pass malformed truncated JSON or cut command arguments to the model.
    excerpt = text[:limit // 2]
    while len(json.dumps({'context_excerpt': excerpt, 'omitted': True}, ensure_ascii=False)) > limit:
        excerpt = excerpt[:len(excerpt) // 2]
    return json.dumps({'context_excerpt': excerpt, 'omitted': True}, ensure_ascii=False)
