#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 || "$#" -ne 3 ]]; then
  echo "usage: update.sh RELEASE_DIR DATABASE CONFIGURATION" >&2
  exit 2
fi

release_source="$(realpath -e -- "$1")"
database_source="$(realpath -e -- "$2")"
configuration_source="$(realpath -e -- "$3")"

for staged in "$release_source" "$database_source" "$configuration_source"; do
  case "$staged" in
    /tmp/wahojobs-preview-stage/*) ;;
    *) echo "staged input is outside the dedicated staging directory" >&2; exit 3 ;;
  esac
done

for required in \
  /etc/wahojobs-preview/origin.env \
  /etc/wahojobs-preview/origin.json \
  /etc/caddy/Caddyfile \
  /var/lib/wahojobs-preview/catalog.sqlite3 \
  /opt/wahojobs-preview/current; do
  [[ -e "$required" ]] || { echo "existing preview installation is incomplete" >&2; exit 4; }
done

code_release_id="$(git -C "$release_source" rev-parse HEAD 2>/dev/null || true)"
if [[ ! "$code_release_id" =~ ^[0-9a-f]{40}$ ]]; then
  echo "staged release must be one committed checkout" >&2
  exit 5
fi
release_target="/opt/wahojobs-preview/releases/$code_release_id"

if [[ ! -d "$release_target" ]]; then
  mkdir "$release_target"
  cp -a "$release_source/." "$release_target/"
  chown -R root:root "$release_target"
  chmod -R go-w "$release_target"
fi

/opt/wahojobs-preview/venv/bin/pip install \
  --disable-pip-version-check --require-hashes \
  -r "$release_target/requirements.lock" >/dev/null

set -a
source /etc/wahojobs-preview/origin.env
set +a
caddy validate \
  --config "$release_target/deploy/digitalocean-preview/Caddyfile" \
  --adapter caddyfile >/dev/null

attest_configuration="${configuration_source}.attest-${code_release_id}"
cp -- "$configuration_source" "$attest_configuration"
python3 - "$attest_configuration" "$database_source" <<'PY'
import json
from pathlib import Path
import sys

configuration_path = Path(sys.argv[1])
database_path = str(Path(sys.argv[2]).resolve(strict=True))
document = json.loads(configuration_path.read_text(encoding="utf-8"))
if document.get("database_path") != "/var/lib/wahojobs-preview/catalog.sqlite3":
    raise SystemExit("invalid runtime database path")
document["database_path"] = database_path
configuration_path.write_text(
    json.dumps(document, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
(
  cd "$release_target"
  /opt/wahojobs-preview/venv/bin/python -B - \
    "$attest_configuration" <<'PY'
import sys
from wahojobs.public_catalog_origin import (
    attest_public_projection,
    load_public_catalog_origin_configuration,
)

configuration = load_public_catalog_origin_configuration(sys.argv[1])
attestation = attest_public_projection(configuration)
print(
    "WAHOJOBS_PREVIEW_STAGED_ATTESTATION_OK "
    f"release={attestation.release_id} database={attestation.database_sha256}"
)
PY
)

runtime_database_next="/var/lib/wahojobs-preview/catalog.sqlite3.next"
runtime_configuration_next="/etc/wahojobs-preview/origin.json.next"
runtime_caddy_next="/etc/caddy/Caddyfile.next"
install -o wahojobs-preview -g wahojobs-preview -m 0600 \
  "$database_source" "$runtime_database_next"
install -o root -g wahojobs-preview -m 0640 \
  "$configuration_source" "$runtime_configuration_next"
install -o root -g root -m 0644 \
  "$release_target/deploy/digitalocean-preview/Caddyfile" "$runtime_caddy_next"

old_release_target="$(readlink -f /opt/wahojobs-preview/current)"
backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_directory="/var/lib/wahojobs-preview/rollback/${backup_stamp}-${code_release_id}"
install -d -o root -g root -m 0700 "$backup_directory"
cp -a /var/lib/wahojobs-preview/catalog.sqlite3 "$backup_directory/catalog.sqlite3"
cp -a /etc/wahojobs-preview/origin.json "$backup_directory/origin.json"
cp -a /etc/caddy/Caddyfile "$backup_directory/Caddyfile"
printf '%s\n' "$old_release_target" >"$backup_directory/release-target"

rollback_required=1
rollback_update() {
  exit_status=$?
  trap - ERR
  if [[ "$rollback_required" -eq 1 ]]; then
    set +e
    systemctl stop wahojobs-public-catalog.service
    cp -a "$backup_directory/catalog.sqlite3" /var/lib/wahojobs-preview/catalog.sqlite3
    cp -a "$backup_directory/origin.json" /etc/wahojobs-preview/origin.json
    cp -a "$backup_directory/Caddyfile" /etc/caddy/Caddyfile
    ln -sfn "$old_release_target" /opt/wahojobs-preview/current.rollback
    mv -Tf /opt/wahojobs-preview/current.rollback /opt/wahojobs-preview/current
    systemctl daemon-reload
    systemctl restart wahojobs-public-catalog.service
    systemctl restart caddy.service
    set -e
  fi
  exit "$exit_status"
}
trap rollback_update ERR

systemctl stop wahojobs-public-catalog.service
mv -f "$runtime_database_next" /var/lib/wahojobs-preview/catalog.sqlite3
mv -f "$runtime_configuration_next" /etc/wahojobs-preview/origin.json
mv -f "$runtime_caddy_next" /etc/caddy/Caddyfile
install -o root -g root -m 0644 \
  "$release_target/deploy/digitalocean-preview/wahojobs-public-catalog.service" \
  /etc/systemd/system/wahojobs-public-catalog.service
ln -sfn "$release_target" /opt/wahojobs-preview/current.next
mv -Tf /opt/wahojobs-preview/current.next /opt/wahojobs-preview/current
systemctl daemon-reload
systemctl restart wahojobs-public-catalog.service
systemctl restart caddy.service

curl --fail --silent --show-error \
  --retry 20 --retry-delay 1 --retry-connrefused \
  -H "X-Wahojobs-Origin-Auth: ${WAHOJOBS_ORIGIN_AUTH_TOKEN}" \
  "http://127.0.0.1:${WAHOJOBS_ORIGIN_PORT}/__origin/ready" >/dev/null

projection_release_id="$(python3 -c 'import json; print(json.load(open("/etc/wahojobs-preview/origin.json"))["release_manifest"]["release_id"])')"
while IFS= read -r published_path; do
  curl --fail --silent --show-error \
    -H "X-Wahojobs-Origin-Auth: ${WAHOJOBS_ORIGIN_AUTH_TOKEN}" \
    -H "X-Wahojobs-Release-Id: ${projection_release_id}" \
    "http://127.0.0.1:${WAHOJOBS_ORIGIN_PORT}${published_path}" >/dev/null
done < <(
  python3 -c 'import json; d=json.load(open("/etc/wahojobs-preview/origin.json")); print("\n".join(item["path"] for item in d["release_manifest"]["published_details"]))'
)

rollback_required=0
trap - ERR
echo "WAHOJOBS_PREVIEW_ORIGIN_UPDATED code=${code_release_id} release=${projection_release_id} backup=${backup_directory}"
