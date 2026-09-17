#!/usr/bin/env bash
# Run only on the separately authorized NEW dedicated host. Does not start a
# listener, request a certificate, initialize storage or read provider secrets.
set -euo pipefail
[[ $EUID == 0 && $# == 2 ]] || { echo 'usage: install.sh STAGED_RELEASE FULL_COMMIT' >&2; exit 2; }
source_dir=$(realpath -e -- "$1")
release_id=$2
[[ $release_id =~ ^[a-f0-9]{40}$ ]] || exit 2
case "$source_dir" in /tmp/wahojobs-beta-stage/*) ;; *) exit 2 ;; esac
[[ -f /etc/wahojobs-beta-dedicated-host ]] || { echo 'Dedicated-host authorization marker required' >&2; exit 3; }
[[ ! -e /opt/wahojobs-beta && ! -e /etc/wahojobs-beta ]] || { echo 'Fresh install only; preserve existing installation' >&2; exit 3; }
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
command -v caddy >/dev/null
useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin wahojobs-beta
install -d -o root -g root -m 0755 /opt/wahojobs-beta/releases
install -d -o root -g root -m 0700 /etc/wahojobs-beta
install -d -o wahojobs-beta -g wahojobs-beta -m 0700 /var/lib/wahojobs-beta /var/log/wahojobs-beta
target=/opt/wahojobs-beta/releases/$release_id
mkdir "$target"
cp -a "$source_dir/." "$target/"
chown -R root:root "$target"
chmod -R go-w "$target"
python3.12 -m venv "$target/.venv"
"$target/.venv/bin/python" -m pip install --require-hashes -r "$target/requirements.lock"
ln -s "$target" /opt/wahojobs-beta/current
install -m 0644 "$target/deploy/private-beta/wahojobs-beta.service" /etc/systemd/system/wahojobs-beta.service
systemctl daemon-reload
echo 'Code installed only. Explicit fresh storage, private config, Caddy validation and approved activation remain.'
