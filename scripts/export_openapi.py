"""Export the actual FastAPI schema without starting the app or connecting to data."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BACKEND.parent / 'analytics-frontend' / 'openapi.json')
    args = parser.parse_args()
    from app.main import app
    target = args.output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Exported {len(app.openapi()["paths"])} API paths to {target}')


if __name__ == '__main__':
    main()
