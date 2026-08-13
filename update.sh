#!/usr/bin/env bash
#
# webbackup panel updater — apply a new version WITHOUT touching your
# config, login, servers or SSH keys. Run from the extracted project folder:
#   sudo bash update.sh
#
set -u
RED=$'\033[0;31m'; GRN=$'\033[0;32m'; OFF=$'\033[0m'
die() { echo "${RED}✘ $*${OFF}" >&2; exit 1; }
ok()  { echo "${GRN}✔${OFF} $*"; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo bash update.sh"
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="/opt/webbackup-dashboard"
[ -d "$APP" ] || die "Panel not installed yet — run dashboard/install.sh first."
[ -f "$SRC/dashboard/app.py" ] || die "Run this from the LinuxWebsitesBackup folder (dashboard/ + agent/ must be next to it)."

cp "$SRC/dashboard/app.py" "$SRC/dashboard/sshops.py" "$APP/"
cp -r "$SRC/dashboard/templates" "$SRC/dashboard/static" "$APP/"
cp "$SRC/agent/webbackup" "$SRC/agent/install.sh" "$APP/agent/"
ok "Files updated (config, database and keys untouched)."

# install any new Python dependencies quietly (no-op most of the time)
"$APP/venv/bin/pip" install -q -r "$SRC/dashboard/requirements.txt" 2>/dev/null || true

systemctl restart webbackup-dashboard
sleep 1
if systemctl is-active --quiet webbackup-dashboard; then
    ok "Panel restarted — update complete. Hard-refresh your browser (Ctrl+F5)."
else
    die "Service failed to start — check: journalctl -u webbackup-dashboard -n 30"
fi
