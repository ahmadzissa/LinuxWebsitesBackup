#!/usr/bin/env bash
#
# webbackup panel updater — apply a new version WITHOUT touching your
# config, login, servers or SSH keys. Run from the extracted project folder:
#   sudo bash update.sh
#
set -eu
RED=$'\033[0;31m'; GRN=$'\033[0;32m'; OFF=$'\033[0m'
die() { echo "${RED}✘ $*${OFF}" >&2; exit 1; }
ok()  { echo "${GRN}✔${OFF} $*"; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo bash update.sh"
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="/opt/webbackup-dashboard"
[ -d "$APP" ] || die "Panel not installed yet — run dashboard/install.sh first."
[ -f "$SRC/dashboard/app.py" ] || die "Run this from the LinuxWebsitesBackup folder (dashboard/ + agent/ must be next to it)."

for file in dashboard/app.py dashboard/sshops.py dashboard/requirements.txt \
            dashboard/templates/base.html dashboard/templates/_ui.html \
            dashboard/templates/_schedule.html dashboard/templates/snapshot_files.html \
            dashboard/templates/restore_review.html dashboard/static/style.css \
            dashboard/templates/job.html dashboard/static/job-output.js \
            dashboard/static/dashboard.js dashboard/static/snapshot-files.js \
            dashboard/static/snapshot-files.css agent/webbackup agent/install.sh agent/archiveops.py; do
    [ -f "$SRC/$file" ] || die "Incomplete project upload: $file is missing."
done
"$APP/venv/bin/python" - "$SRC" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for name in ("dashboard/app.py", "dashboard/sshops.py", "agent/archiveops.py"):
    path = root / name
    compile(path.read_bytes(), str(path), "exec")
PY

# Dependency failures stop the update before any installed files are replaced.
"$APP/venv/bin/pip" install -q -r "$SRC/dashboard/requirements.txt"

mkdir -p "$APP/agent"
cp "$SRC/agent/webbackup" "$SRC/agent/install.sh" "$SRC/agent/archiveops.py" "$APP/agent/"
cp "$SRC/dashboard/sshops.py" "$SRC/dashboard/requirements.txt" "$APP/"
cp -r "$SRC/dashboard/templates" "$SRC/dashboard/static" "$APP/"
cp "$SRC/dashboard/app.py" "$APP/"
ok "Files updated (config, database and keys untouched)."

systemctl restart webbackup-dashboard
sleep 1
if systemctl is-active --quiet webbackup-dashboard; then
    ok "Panel restarted — update complete. Hard-refresh your browser (Ctrl+F5)."
else
    die "Service failed to start — check: journalctl -u webbackup-dashboard -n 30"
fi
