#!/usr/bin/env bash
# One-time root installation. Run from the reviewed backend/deploy directory.
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo 'Run as root on the production server.' >&2
  exit 1
fi
cd -- "$(dirname -- "$0")"
getent passwd videohub >/dev/null
getent passwd videohub-deploy >/dev/null
command -v /usr/local/bin/uv >/dev/null
command -v docker >/dev/null
command -v visudo >/dev/null
test -f /etc/videohub/backend.env
# Keep existing backend, Python, storage and credentials. Do not start services yet.
chown root:root /opt/videohub
chmod 0755 /opt/videohub
install -d -o root -g root -m 0755 /opt/videohub/releases
install -d -o root -g root -m 0700 /var/lib/videohub-deploy /var/lib/videohub-deploy/backups
install -d -o videohub-deploy -g videohub-deploy -m 0700 /home/videohub-deploy/incoming
install -d -o root -g root -m 0755 /usr/local/lib/videohub
install -o root -g root -m 0755 deploy_release.py /usr/local/sbin/videohub-deploy
install -o root -g root -m 0644 runtime.py /usr/local/lib/videohub/runtime.py
for unit in videohub-*.service videohub-*.timer; do
  install -o root -g root -m 0644 "$unit" "/etc/systemd/system/$unit"
done
rule=$(mktemp)
trap 'rm -f "$rule"' EXIT
printf '%s\n' 'videohub-deploy ALL=(root) NOPASSWD: /usr/local/sbin/videohub-deploy *' > "$rule"
visudo -cf "$rule"
install -o root -g root -m 0440 "$rule" /etc/sudoers.d/videohub-deploy
systemctl daemon-reload
systemctl enable videohub-api.service videohub-worker.service videohub-cleanup.timer videohub-reconcile.timer
printf '%s\n' 'Deployment helper installed. Services have NOT been started. Existing env and database preserved.'
