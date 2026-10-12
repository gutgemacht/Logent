"""Import bundled Linux references into Logent Chroma without adding fake incidents."""
import argparse
from pathlib import Path

import config
from core.history import HistoryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', default=config.CHROMA_PATH, help='Logent Chroma database directory')
    args = parser.parse_args()
    store = HistoryStore(str(Path(args.path).expanduser().resolve()))
    rows = store.list_linux_library()
    print(f'Linux references: {len(rows)} | Active: {sum(row["active"] for row in rows)} | Categories: {len({row["category"] for row in rows})}')
    print('Existing occurrences, solutions and ratings are preserved. Repeated imports keep disabled entries disabled.')


if __name__ == '__main__':
    main()
