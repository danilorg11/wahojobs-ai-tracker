"""Create or resume one clearly labelled synthetic beta rehearsal; no external calls."""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True,help='Explicit disposable synthetic state directory')
    parser.add_argument('--create',action='store_true',help='Create a new fresh fixture; fails if state already exists')
    parser.add_argument('--samples',action='store_true',help='On create only, enable fixed synthetic practice accounts; profiles still require normal form confirmation')
    parser.add_argument('--port',type=int,default=8861)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--diagnostics-directory',type=Path)
    args=parser.parse_args(argv)
    if args.samples and not args.create:
        raise ValueError('sample_accounts_configured_only_on_create')
    from tests.private_beta_demo_support import preserve_fresh_fixture,reopen_fixture,beta_application
    from wahojobs.request_diagnostics import diagnostic_log
    state=(preserve_fresh_fixture(args.state,port=args.port,samples=args.samples) if args.create else reopen_fixture(args.state))
    if (state.directory/'beta-demo.stop').exists():
        raise ValueError('remove_only_this_scoped_stop_marker_before_explicit_restart')
    with (diagnostic_log(args.diagnostics_directory) if args.diagnostics_directory else nullcontext()) as diagnostics:
        with beta_application(state,diagnostics=diagnostics):
            paths=json.loads(os.environ.get('WAHOJOBS_TEST_TRACKED_SOURCE_PATHS','[]'))
            receipt=dict(pid=os.getpid(),origin=state.public_origin,source=str(ROOT),storage=str(state.directory),
                login_url=state.public_origin+'/login?next=/account/profile',executable=sys.executable,
                hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
                loaded_modules={name:str(getattr(module,'__file__','')) for name,module in sys.modules.items()
                    if name.startswith(('wahojobs.','scripts.','tests.private_beta','tests.recommendation'))},
                replay_clock=state.clock().isoformat(),
                sample_accounts='recommendation_samples' in json.loads((state.directory/'private-beta-demo.json').read_text()),
                synthetic=True,external_requests=False,seed_was_unconfirmed=True,created_this_launch=args.create)
            args.receipt.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
            print(receipt['login_url'],flush=True)
            print('Synthetic invitation and sources. Normal stop: create beta-demo.stop in the declared state directory.',flush=True)
            try:
                while not (state.directory/'beta-demo.stop').exists(): time.sleep(.2)
            except KeyboardInterrupt: pass
    return 0


if __name__=='__main__': raise SystemExit(main())
