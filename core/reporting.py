"""Export troubleshooting sessions without hiding unverified outcomes."""
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class SessionReport:
    def __init__(self, event, context: dict):
        self.data = {'id': str(uuid4()), 'started': datetime.now().astimezone().isoformat(),
                     'error': event.line, 'source': event.filepath, 'detected': event.timestamp,
                     'system': context, 'steps': [], 'outcome': 'not_reported'}

    def add(self, stage: str, data):
        self.data['steps'].append({'time': datetime.now().astimezone().isoformat(), 'stage': stage, 'data': data})

    def export(self, directory: str) -> Path:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + self.data['id'][:8]
        json_path = folder / (name + '.json')
        json_path.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        md = folder / (name + '.md')
        # Long fences keep pasted code blocks intact.
        md.write_text('# Troubleshooting report\n\n' + '\n\n'.join([
            'Error: ' + self.data['error'], 'Source: ' + self.data['source'],
            'User outcome: ' + self.data['outcome'],
            '````json\n' + json.dumps(self.data, indent=2) + '\n````',
        ]), encoding='utf-8')
        return md
