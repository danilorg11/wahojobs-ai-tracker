"""Local token-authenticated readiness; no provider request or secret output."""
from pathlib import Path
import argparse
from http.client import HTTPConnection
import sys
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))


def main(argv=None):
    from wahojobs.workos_authkit_staging import load_workos_authkit_staging_configuration
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args=parser.parse_args(argv)
    config=None
    try:
        config=load_workos_authkit_staging_configuration(args.config,remote_beta=True)
        for attempt in range(20):
            client=HTTPConnection(*config.bind_address,timeout=1)
            try:
                client.request('GET','/_ops/ready',headers={
                    'Host':urlsplit(config.public_origin).netloc,'X-Wahojobs-Proxy':config.proxy_secret})
                reply=client.getresponse()
                if reply.status==200 and reply.read(32)==b'ready\n':
                    print('private_beta_ready'); return 0
            except OSError: pass
            finally: client.close()
            time.sleep(0.5)
    except Exception: pass
    finally:
        if config: config.clear_secrets()
    print('private_beta_not_ready',file=sys.stderr)
    return 2


if __name__=='__main__': raise SystemExit(main())
