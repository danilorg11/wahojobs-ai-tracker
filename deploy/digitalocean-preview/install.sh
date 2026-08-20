#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 || "$#" -ne 4 ]]; then
  echo "usage: install.sh RELEASE_DIR DATABASE CONFIGURATION ENVIRONMENT" >&2
  exit 2
fi

release_source="$(realpath -e -- "$1")"
database_source="$(realpath -e -- "$2")"
configuration_source="$(realpath -e -- "$3")"
environment_source="$(realpath -e -- "$4")"

for staged in "$release_source" "$database_source" "$configuration_source" "$environment_source"; do
  case "$staged" in
    /tmp/wahojobs-preview-stage/*) ;;
    *) echo "staged input is outside the dedicated staging directory" >&2; exit 3 ;;
  esac
done

release_id="$(git -C "$release_source" rev-parse HEAD 2>/dev/null || true)"
if [[ ! "$release_id" =~ ^[0-9a-f]{40}$ ]]; then
  release_id="$(sha256sum "$release_source/wahojobs/public_catalog_origin.py" | cut -d' ' -f1)"
fi
release_target="/opt/wahojobs-preview/releases/$release_id"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends caddy ca-certificates curl python3 python3-venv

if ! id wahojobs-preview >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin wahojobs-preview
fi

install -d -o root -g root -m 0755 /opt/wahojobs-preview/releases
install -d -o root -g root -m 0755 /opt/wahojobs-preview
install -d -o root -g wahojobs-preview -m 0750 /etc/wahojobs-preview
install -d -o wahojobs-preview -g wahojobs-preview -m 0700 /var/lib/wahojobs-preview
install -d -o root -g root -m 0755 /etc/systemd/system/caddy.service.d

if [[ ! -d "$release_target" ]]; then
  mkdir "$release_target"
  cp -a "$release_source/." "$release_target/"
  chown -R root:root "$release_target"
  chmod -R go-w "$release_target"
fi

if [[ ! -x /opt/wahojobs-preview/venv/bin/python ]]; then
  python3 -m venv /opt/wahojobs-preview/venv
  /opt/wahojobs-preview/venv/bin/pip install --disable-pip-version-check --require-hashes -r "$release_target/requirements.lock"
fi

if [[ -e /var/lib/wahojobs-preview/catalog.sqlite3 ]]; then
  echo "persistent catalog database already exists; refusing replacement" >&2
  exit 4
fi
install -o wahojobs-preview -g wahojobs-preview -m 0600 "$database_source" /var/lib/wahojobs-preview/catalog.sqlite3
install -o root -g wahojobs-preview -m 0640 "$configuration_source" /etc/wahojobs-preview/origin.json
install -o root -g root -m 0600 "$environment_source" /etc/wahojobs-preview/origin.env
install -o root -g root -m 0644 "$release_target/deploy/digitalocean-preview/Caddyfile" /etc/caddy/Caddyfile
install -o root -g root -m 0644 "$release_target/deploy/digitalocean-preview/caddy-environment.conf" /etc/systemd/system/caddy.service.d/wahojobs-preview.conf
install -o root -g root -m 0644 "$release_target/deploy/digitalocean-preview/wahojobs-public-catalog.service" /etc/systemd/system/wahojobs-public-catalog.service

ln -sfn "$release_target" /opt/wahojobs-preview/current.next
mv -Tf /opt/wahojobs-preview/current.next /opt/wahojobs-preview/current

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable --now wahojobs-public-catalog.service
systemctl enable caddy.service
systemctl restart caddy.service

set -a
source /etc/wahojobs-preview/origin.env
set +a
curl --fail --silent --show-error \
  -H "X-Wahojobs-Origin-Auth: ${WAHOJOBS_ORIGIN_AUTH_TOKEN}" \
  "http://127.0.0.1:${WAHOJOBS_ORIGIN_PORT}/__origin/ready" >/dev/null

echo "WAHOJOBS_PREVIEW_ORIGIN_READY release=${release_id}"
