"""Synthetic-only Candidate continuity demo through the normal product runtime.

No crawl, application-model call, external authentication or submission. Start:
    python -B scripts/candidate_continuity_demo.py
"""
import argparse
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


def serve(directory, *, owner='first'):
    from scripts.local_recovery_login import existing_owner_local_login
    from tests.google_oidc_gateway_test_support import ManualClock
    from tests.durable_google_login_browser_test_support import _running_https_browser_handler
    from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
    directory = Path(directory).resolve(strict=True)
    marker = json.loads((directory/'continuity-demo.json').read_text(encoding='utf-8'))
    if marker.get('synthetic_candidate_continuity_v1') is not True:
        raise ValueError('Only a generated continuity fixture is accepted.')
    configuration = directory/'runtime.json'
    config_doc = json.loads(configuration.read_text(encoding='utf-8'))
    if Path(config_doc['database_path']).parent != directory or config_doc['bind_port'] == 8802:
        raise ValueError('Invalid synthetic fixture boundary.')
    with patch.dict(os.environ, {'WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED':'0', 'WAHOJOBS_OPENAI_ENRICHMENT':'0'}), \
         existing_owner_local_login(configuration, account_id=marker['owners'][owner],
             clock=ManualClock(datetime.fromisoformat(marker['now']))) as (config, app):
        if marker.get('candidate_decision_v1'):
            from tests.candidate_decision_support import publish_demo_certificate
            certificate_capture = publish_demo_certificate(directory)
        else:
            certificate_capture = nullcontext()
        with certificate_capture, _running_https_browser_handler(config, make_durable_product_browser_handler(app)):
            (directory/'continuity-ready.json').write_text(json.dumps({'pid':os.getpid(),
                'origin':config_doc['public_origin'], 'owner':owner}), encoding='utf-8')
            print('Synthetic Wahojobs: '+config_doc['public_origin']+'/login?next=/find-matches',flush=True)
            print('Use the local controlled login. Save -> Mark as applied -> Remind me later -> Not interested -> My Jobs -> Hidden -> View job details -> Show again.',flush=True)
            print('Synthetic owners and inventory. Employer wording includes preserved public fixtures; no employer links need to be opened.'
                  if marker.get('candidate_decision_v1') else 'Only synthetic jobs and owners. Employer links are example.test; do not open them.',flush=True)
            while not (directory/'continuity-stop').exists():
                time.sleep(.2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-state',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--owner',choices=('first','second'),default='first',help=argparse.SUPPRESS)
    parser.add_argument('--port',type=int,default=None)
    args=parser.parse_args()
    if args.serve_state:
        serve(args.serve_state,owner=args.owner)
    else:
        from tests.candidate_continuity_support import synthetic_state
        with synthetic_state(port=args.port,competitors=10) as state:
            try: serve(state.directory)
            except KeyboardInterrupt: pass
    return 0


if __name__=='__main__': raise SystemExit(main())
