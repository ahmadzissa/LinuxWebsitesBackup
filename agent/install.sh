#!/usr/bin/env bash
#
# webbackup installer — run on a fresh Linux server:
#   sudo bash install.sh
#
set -u

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; OFF=$'\033[0m'
die() { echo "${RED}✘ $*${OFF}" >&2; exit 1; }
ok()  { echo "${GRN}✔${OFF} $*"; }

[ "$(id -u)" -eq 0 ] || die "Please run as root:  sudo bash install.sh"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC_DIR/webbackup" ] || die "webbackup script not found next to install.sh"

echo "Installing dependencies (rsync, ssh client, cron, curl, gzip)..."
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync openssh-client cron curl gzip coreutils >/dev/null
    systemctl enable --now cron >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q rsync openssh-clients cronie curl gzip coreutils >/dev/null
    systemctl enable --now crond >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q rsync openssh-clients cronie curl gzip coreutils >/dev/null
    systemctl enable --now crond >/dev/null 2>&1 || true
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache -q rsync openssh-client dcron curl gzip coreutils bash flock
    rc-update add dcron default >/dev/null 2>&1 || true
    /etc/init.d/dcron start >/dev/null 2>&1 || true
else
    echo "Unknown package manager — make sure rsync, ssh, cron, curl are installed."
fi
ok "Dependencies ready."

install -m 755 "$SRC_DIR/webbackup" /usr/local/bin/webbackup
mkdir -p /etc/webbackup /var/lib/webbackup /var/backups/webbackup
ok "Installed /usr/local/bin/webbackup"

echo
if [ -t 0 ]; then
    read -r -p "Run the setup wizard now? [Y/n]: " ans || true
    case "${ans:-y}" in
        [Nn]*) echo "Later, run:  webbackup setup" ;;
        *)     exec webbackup setup ;;
    esac
else
    echo "Next step:  webbackup setup"
fi
