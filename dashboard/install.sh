#!/usr/bin/env bash
#
# webbackup dashboard installer — run on the server that will act as the
# central control panel:
#   sudo bash install.sh
#
set -u

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; OFF=$'\033[0m'
die() { echo "${RED}✘ $*${OFF}" >&2; exit 1; }
ok()  { echo "${GRN}✔${OFF} $*"; }

[ "$(id -u)" -eq 0 ] || die "Please run as root:  sudo bash install.sh"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SRC_DIR/.." && pwd)"
APP_DIR="/opt/webbackup-dashboard"
CFG_DIR="/etc/webbackup-dashboard"
CFG="$CFG_DIR/dashboard.conf"
KEY="$CFG_DIR/id_ed25519"
PORT_DEFAULT=8800

[ -f "$SRC_DIR/app.py" ] || die "Run this from the dashboard/ folder of the project."
[ -f "$REPO_DIR/agent/webbackup" ] || die "agent/webbackup not found — clone the full repository."

echo "Installing Python and dependencies..."
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip openssh-client >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip openssh-clients >/dev/null
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 python3-pip openssh-clients >/dev/null
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache -q python3 py3-pip py3-virtualenv openssh-client bash
fi
command -v python3 >/dev/null 2>&1 || die "python3 could not be installed."
ok "Python 3 ready: $(python3 --version)"

echo "Installing dashboard to $APP_DIR ..."
mkdir -p "$APP_DIR" "$CFG_DIR" /var/lib/webbackup-dashboard
cp -r "$SRC_DIR/app.py" "$SRC_DIR/sshops.py" "$SRC_DIR/requirements.txt" \
      "$SRC_DIR/templates" "$SRC_DIR/static" "$APP_DIR/"
mkdir -p "$APP_DIR/agent"
cp "$REPO_DIR/agent/webbackup" "$REPO_DIR/agent/install.sh" "$APP_DIR/agent/"

python3 -m venv "$APP_DIR/venv" || die "could not create virtualenv (install python3-venv)"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" || die "pip install failed"
ok "Python environment ready (Flask + paramiko in a private venv)."

# SSH key
if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -N "" -C "webbackup-dashboard@$(hostname)" -f "$KEY" -q
    ok "Dashboard SSH key generated."
fi

# Admin credentials + config
if [ ! -f "$CFG" ]; then
    echo
    read -r -p "Dashboard admin username [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"
    while true; do
        read -r -s -p "Dashboard admin password: " P1; echo
        read -r -s -p "Repeat password: " P2; echo
        [ -n "$P1" ] && [ "$P1" = "$P2" ] && break
        echo "Passwords empty or don't match — try again."
    done
    read -r -p "Dashboard port [$PORT_DEFAULT]: " PORT
    PORT="${PORT:-$PORT_DEFAULT}"
    HASH="$(WBD_PW="$P1" "$APP_DIR/venv/bin/python" - <<'PY'
import os
from werkzeug.security import generate_password_hash
print(generate_password_hash(os.environ["WBD_PW"]))
PY
)"
    SECRET="$(head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n')"
    cat > "$CFG" <<EOF
PORT=$PORT
HOST=0.0.0.0
SECRET_KEY=$SECRET
ADMIN_USER=$ADMIN_USER
ADMIN_HASH=$HASH
SSH_KEY=$KEY
DB_PATH=/var/lib/webbackup-dashboard/dashboard.db
AGENT_DIR=$APP_DIR/agent
EOF
    chmod 600 "$CFG"
    ok "Config written to $CFG"
else
    PORT="$(grep '^PORT=' "$CFG" | cut -d= -f2)"
    ok "Existing config kept ($CFG)."
fi

# systemd service
cat > /etc/systemd/system/webbackup-dashboard.service <<EOF
[Unit]
Description=webbackup dashboard
After=network.target

[Service]
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/app.py
WorkingDirectory=$APP_DIR
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now webbackup-dashboard >/dev/null 2>&1
sleep 1
if systemctl is-active --quiet webbackup-dashboard; then
    ok "Service running."
else
    die "Service failed to start — check: journalctl -u webbackup-dashboard -n 30"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
ok "Dashboard installed!"
echo
echo "  Open:   http://${IP:-<server-ip>}:$PORT"
echo "  Login:  the admin user you just created"
echo
echo "  First steps in the browser:"
echo "    1. Settings → enter your Synology NAS details"
echo "    2. Servers → '+ Link a server' → enter a server's IP + root password"
echo
echo "  Tip: keep this port firewalled to your own IP, or put it behind"
echo "  an HTTPS reverse proxy, since it controls your servers."
