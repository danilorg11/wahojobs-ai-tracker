"""Synthetic Candidate decision experience; no real data or external calls."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8846)
    args=parser.parse_args()
    if args.port==8802: parser.error('Recovery port is excluded.')
    from tests.candidate_decision_support import decision_state
    from scripts.candidate_continuity_demo import serve
    with decision_state(port=args.port) as state:
        print('Candidate decision demo. Synthetic candidate; preserved employer wording plus labelled practice opportunities.',flush=True)
        print('Compare Matches -> Python opportunity -> Update profile -> review experience details -> confirm -> return -> Save/Applied -> My Jobs.',flush=True)
        print('No employer links need to be opened. Stop with Ctrl+C.',flush=True)
        try: serve(state.directory)
        except KeyboardInterrupt: pass
    return 0


if __name__=='__main__': raise SystemExit(main())
