"""Synthetic invited account demo using the existing controlled product composition."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8850)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.port == 8802: parser.error('Recovery port is protected')
    from tests.first_time_candidate_support import new_candidate_state, candidate_application
    with new_candidate_state(port=args.port) as state:
        marker = json.loads((state.directory/'first-time-candidate.json').read_text(encoding='utf-8'))
        with candidate_application(state):
            receipt = dict(marker, pid=os.getpid(), source=str(ROOT), executable=sys.executable,
                           origin=state.public_origin, storage=str(state.directory),
                           files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in (ROOT/'scripts/first_time_candidate_demo.py',
                                            ROOT/'wahojobs/authenticated_profile_matches.py',
                                            ROOT/'wahojobs/manual_profile_drafts.py')})
            args.receipt.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
            print('Synthetic-only new candidate: '+state.public_origin+'/login?next=/account/profile',flush=True)
            print('Invitation: '+marker['invitation'],flush=True)
            print('Controlled local identity only. No email delivery or external identity provider.',flush=True)
            try:
                while not args.receipt.with_suffix('.stop').exists(): time.sleep(.2)
            except KeyboardInterrupt: pass


if __name__ == '__main__': main()
