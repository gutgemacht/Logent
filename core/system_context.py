"""Read-only machine identity, file facts and reusable-solution conditions."""
from __future__ import annotations

import getpass
import hashlib
import os
import platform
import re
import stat
from pathlib import Path

from core.error_context import problem_kind, reported_paths


def system_context() -> dict:
    release = platform.freedesktop_os_release() if Path('/etc/os-release').exists() else {}
    identity = platform.node()
    try:
        identity = Path('/etc/machine-id').read_text().strip()
    except OSError:
        pass
    return {'machine_id': hashlib.sha256(identity.encode()).hexdigest()[:24],
            'hostname': platform.node(), 'os': release.get('PRETTY_NAME', platform.system()),
            'kernel': platform.release(), 'user': getpass.getuser(),
            'uid': os.getuid() if hasattr(os, 'getuid') else None}


def file_facts(error: str) -> dict:
    facts = {}
    for name in reported_paths(error):
        path = Path(name)
        try:
            info = path.stat()
            facts[name] = {'exists': True, 'mode': format(stat.S_IMODE(info.st_mode), '04o'),
                           'uid': info.st_uid, 'gid': info.st_gid,
                           'type': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                           'symlink': path.is_symlink()}
        except FileNotFoundError:
            facts[name] = {'exists': False, 'symlink': path.is_symlink()}
        except OSError as exc:
            facts[name] = {'exists': None, 'error': str(exc)}
    return facts


def reported_services(error: str) -> list[str]:
    return sorted(set(re.findall(r'\b[\w@.-]+\.service\b', error)))


def solution_conditions(error: str, facts: dict, context: dict) -> dict:
    return {'machine_id': context['machine_id'], 'os': context['os'], 'uid': context['uid'],
            'problem_kind': problem_kind(error), 'files': facts, 'services': reported_services(error)}


def check_conditions(conditions: dict, error: str, facts: dict, context: dict) -> tuple[bool, str]:
    if not conditions:
        return False, 'No saved conditions; diagnose again'
    for field in ('machine_id', 'os', 'uid'):
        if conditions.get(field) != context.get(field):
            return False, f'{field} differs from the saved environment'
    if conditions.get('services', []) != reported_services(error):
        return False, 'Different service context'
    if conditions.get('problem_kind') != problem_kind(error):
        return False, 'Different problem type'
    if not conditions.get('files'):
        return False, 'No file conditions; fresh diagnosis required'
    for path, expected in conditions['files'].items():
        current = facts.get(path, {})
        if current.get('exists') is None:
            return False, f'Cannot verify {path}'
        for field in ('exists', 'mode', 'uid', 'gid', 'type', 'symlink'):
            if field in expected and expected[field] != current.get(field):
                return False, f'{path}: {field} changed'
    return True, 'Saved conditions match the current machine and files'
