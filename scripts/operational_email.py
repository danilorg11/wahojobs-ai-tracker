"""Dormant Resend adapter for the existing daily health command boundary."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from scripts.daily_inventory import private_policy
from wahojobs import operational_email as email


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args()
    config = private_policy(args.settings)
    if config != dict(version=1, enabled=True, provider='resend', plan='free', paid_overages=False,
            sender=email.SENDER, recipient=email.ALERT_RECIPIENT, domain='ops.wahojobs.com',
            state_directory='/var/lib/wahojobs-beta/daily-inventory-v1'):
        raise ValueError('approved_resend_settings_required')
    raw = sys.stdin.buffer.read(email.MAX_PACKET_BYTES + 1)
    if len(raw) > email.MAX_PACKET_BYTES: raise ValueError('delivery_packet_too_large')
    email.send_packet(json.loads(raw), email.systemd_credential(), config['state_directory'])


if __name__ == '__main__':
    try: main()
    except Exception:
        print('operational_delivery_failed_or_uncertain', file=sys.stderr)
        raise SystemExit(2)
