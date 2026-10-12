"""Export troubleshooting sessions without hiding unverified outcomes."""
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from core.knowledge import redact

PIPELINE = ['Detected', 'History checked', 'Diagnosing', 'Fix proposed', 'Verification', 'Outcome saved']
OUTCOMES = {'yes': 'Fixed', 'no': 'Still failing', 'unsure': 'Cannot verify', 'skipped': 'Feedback skipped',
            'not_reported': 'No feedback', 'ignored': 'Ignored', 'cancelled': 'Cancelled', 'watching': 'Watch for recurrence'}


class SessionReport:
    def __init__(self, event, context: dict):
        self.data = {'id': str(uuid4()), 'started': datetime.now().astimezone().isoformat(),
                     'error': redact(event.line), 'source': event.filepath, 'detected': event.timestamp,
                     'system': redact(context), 'steps': [], 'outcome': 'not_reported'}
        self.data.update(case_number='Case #' + self.data['id'][:8].upper(), stage='Detected',
                         conclusion='Suspected', cause='Not diagnosed yet', next_step='Review the detected error',
                         status='in_progress', triggers=list(getattr(event, 'triggers', ())), occurrence_ids=[])

    def add(self, stage: str, data):
        self.data['steps'].append({'time': datetime.now().astimezone().isoformat(), 'stage': stage, 'data': redact(data)})

    def finish(self):
        outcome = self.data['outcome']
        verification = self.data.get('verification', {})
        resolved = verification.get('status') == 'disproved'
        conflict = self.data.get('feedback_scope') != 'custom_solution' and ((outcome == 'no' and resolved) or (outcome == 'yes' and verification.get('status') == 'confirmed'))
        self.data.update(ended=datetime.now().astimezone().isoformat(),
                         status=outcome if outcome in ('ignored', 'cancelled', 'watching') else 'needs_review' if conflict else 'resolved' if outcome == 'yes' or resolved else 'unresolved',
                         conflicting_outcomes=conflict,
                         next_step='Monitoring continues')
        return self.data

    def export(self, directory: str) -> Path:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + self.data['id'][:8]
        json_path = folder / (name + '.json')
        json_path.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        md = folder / (name + '.md')
        # Long fences keep pasted code blocks intact.
        md.write_text('# Troubleshooting report\n\n' + '\n\n'.join([
            self.data['case_number'], 'Error: ' + self.data['error'], 'Source: ' + self.data['source'],
            'User outcome: ' + self.data['outcome'],
            '````json\n' + json.dumps(self.data, indent=2) + '\n````',
        ]), encoding='utf-8')
        return md
