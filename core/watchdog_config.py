"""Validated detection rules shared by the dashboard and live monitor."""
import json
import os
from pathlib import Path
import re
import tempfile

import config


def default_rules() -> list[dict]:
    keywords = ['error', 'err', 'warn', 'warning', 'kernel panic', 'exception', 'severe']
    return [dict(enabled=True, kind='Keyword', pattern=word) for word in keywords] + [
        dict(enabled=True, kind='Regex', pattern=pattern) for pattern in config.ERROR_PATTERNS]


def validate_rules(rules: list[dict]) -> list[dict]:
    if not isinstance(rules, list):
        raise ValueError('Rules must be a list.')
    result = []
    for index, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise ValueError(f'Rule {index}: invalid row.')
        pattern = rule.get('pattern', '')
        kind = rule.get('kind')
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f'Rule {index}: enter a keyword or regex, or delete the empty row.')
        if kind not in ('Keyword', 'Regex') or not isinstance(rule.get('enabled'), bool):
            raise ValueError(f'Rule {index}: select Keyword or Regex and an enabled state.')
        item = dict(enabled=rule['enabled'], kind=kind, pattern=pattern.strip())
        if 'action' in rule:
            if rule['action'] not in ('Detect', 'Ignore'):
                raise ValueError(f'Rule {index}: action must be Detect or Ignore.')
            item['action'] = rule['action']
        try:
            re.compile(rule_pattern(item), re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f'Rule {index}: invalid regex: {exc}') from exc
        result.append(item)
    return result


def rule_pattern(rule: dict) -> str:
    # Whole words avoid matching "err" in "berry" or "warn" in "prewarning".
    return r'(?<!\w)' + re.escape(rule['pattern']) + r'(?!\w)' if rule['kind'] == 'Keyword' else rule['pattern']


def load_rules(path=None) -> list[dict]:
    path = Path(path or config.WATCHDOG_RULES_FILE)
    if not path.exists():
        return default_rules()
    return validate_rules(json.loads(path.read_text(encoding='utf-8'))['rules'])


def match_rules(rules, line):
    matched = [rule for rule in rules if rule['enabled'] and re.search(rule_pattern(rule), line, re.IGNORECASE)]
    detected = [rule for rule in matched if rule.get('action', 'Detect') == 'Detect']
    ignored = [rule for rule in matched if rule.get('action') == 'Ignore']
    return detected, ignored


def save_rules(rules: list[dict], path=None) -> None:
    rules = validate_rules(rules)
    path = Path(path or config.WATCHDOG_RULES_FILE)
    # Atomic replacement prevents the watcher seeing a partially written file.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.watchdog-rules-')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'rules': rules}, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
