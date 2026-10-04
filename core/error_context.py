"""Conservative distinctions between common file-related error reports."""
import re


def problem_kind(line: str) -> str:
    missing = re.search(r'no such file|not found|not present(?:ed)?|does not exist|doesn.t exist|not exist|\bmissing\b|\babsent\b', line, re.I)
    permissions = re.search(r'permission|access denied|\beacces\b|\beperm\b', line, re.I)
    if missing and not permissions:
        return 'missing_path'
    if permissions and not missing:
        return 'permissions'
    return 'unknown'


def compatible_errors(current: str, previous: str) -> bool:
    left, right = problem_kind(current), problem_kind(previous)
    return left == right or 'unknown' in (left, right)


def reported_paths(line: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r'(?<!\w)/[\w/-](?:[\w./-]*[\w/-])?', line)))


def validate_solution_context(error: str, commands: list[dict]) -> None:
    if problem_kind(error) == 'missing_path':
        for command in commands:
            if re.search(r'\b(?:chmod|chown|setfacl)\b', command.get('command', '')):
                raise ValueError('A permissions change cannot fix a missing file. Verify existence and diagnose restoration first.')
