"""Select bounded, verbatim diagnostic evidence without another LLM call."""

import re

_STOP_WORDS = set('the a an for of to is in on and or with file command check verify output error expected result'.split())
_ALERT = re.compile(r'permission denied|no such file|not found|failed|failure|fatal|critical|exception|\[stderr\]|\berror\b', re.I)


def relevant_output(output: str, limit: int, context: str = '') -> str:
    """Keep relevant lines in original order; explicitly mark every omission.

    Selection is heuristic, not a diagnosis. Short output is kept unchanged.
    """
    if len(output) <= limit:
        return output
    marker = '[… omitted …]'
    if limit < len(marker):
        return marker[:max(0, limit)]
    terms = {term.lower() for term in re.findall(r'[\w./-]{3,}', context)
             if term.lower() not in _STOP_WORDS}
    # ls and similar commands often print a basename rather than a full path.
    terms.update(term.rsplit('/', 1)[-1] for term in list(terms)
                 if '/' in term and len(term.rsplit('/', 1)[-1]) >= 3)
    lines = output.splitlines()
    ranked = sorted(range(len(lines)), key=lambda i: (
        - (20 * bool(_ALERT.search(lines[i]))
           + 5 * sum(term in lines[i].lower() for term in terms)
           + 2 * (i == 0 or i == len(lines) - 1)), i))
    selected = {}

    def render():
        pieces = []
        previous = -1
        for index in sorted(selected):
            if index > previous + 1:
                pieces.append(marker)
            pieces.append(selected[index])
            previous = index
        if previous < len(lines) - 1:
            pieces.append(marker)
        return '\n'.join(pieces)

    for index in ranked:
        line = lines[index]
        selected[index] = line
        if len(render()) <= limit:
            continue
        del selected[index]
        # A very long relevant line still contributes a literal excerpt.
        if not selected:
            room = limit - 3 * (len(marker) + 1)
            if room > 0:
                lower = line.lower()
                matches = [lower.find(t) for t in terms if t in lower]
                alert = _ALERT.search(line)
                if alert:
                    matches.append(alert.start())
                start = max(0, min(matches, default=0) - room // 4)
                selected[index] = (marker if start else '') + line[start:start + room] + marker
                if len(render()) > limit:
                    del selected[index]
    return render() or marker
