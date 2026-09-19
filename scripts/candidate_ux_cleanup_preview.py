"""Isolated synthetic owner-review preview, using the existing product fixture."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8875)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.port in {8802, 8861, 8846, 8873}:
        parser.error('Choose a separate preview port.')
    from tests.candidate_decision_support import decision_state
    from scripts.candidate_continuity_demo import serve
    with decision_state(port=args.port, typed_preferences=True) as state:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps({'synthetic_only': True, 'directory': str(state.directory),
            'origin': state.public_origin, 'source': str(ROOT)}, indent=2), encoding='utf-8')
        print('Candidate UX Cleanup V1 — synthetic review data only.', flush=True)
        print(state.public_origin + '/login?next=/account/profile', flush=True)
        print('Use the controlled local login, then My profile > Edit profile > Review changes > Save changes.', flush=True)
        print('No external employer links are needed; tracking actions never submit applications.', flush=True)
        try:
            serve(state.directory)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
