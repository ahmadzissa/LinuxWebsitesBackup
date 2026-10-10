#!/usr/bin/env python3
"""webbackup dashboard — central control panel for webbackup servers.

Runs as a small Flask service. Talks to linked servers over SSH (paramiko)
using its own key, deploys/configures the webbackup agent, triggers backups
and restores, and pushes schedule/retention settings to selected servers.
"""
import importlib
import json
import os
import re
import secrets
import shutil
import smtplib
import stat as statmod
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from functools import wraps
from collections import OrderedDict
from contextlib import closing

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import sshops

# ----------------------------------------------------------------- config ---
CONFIG_PATH = os.environ.get("WBD_CONFIG", "/etc/webbackup-dashboard/dashboard.conf")

CFG = {
    "PORT": "8800",
    "HOST": "0.0.0.0",
    "SECRET_KEY": "dev-insecure",
    "ADMIN_USER": "admin",
    "ADMIN_HASH": "",           # set by installer
    "SSH_KEY": "/etc/webbackup-dashboard/id_ed25519",
    "DB_PATH": "/var/lib/webbackup-dashboard/dashboard.db",
    "AGENT_DIR": "/opt/webbackup-dashboard/agent",
}
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                CFG[k.strip()] = v.strip()

# The same standard-library helper is deployed to the dashboard and web servers.
sys.path.insert(0, CFG["AGENT_DIR"] if os.path.isdir(CFG["AGENT_DIR"])
                else os.path.join(os.path.dirname(__file__), "..", "agent"))
try:
    import archiveops
except ModuleNotFoundError as exc:
    if exc.name != "archiveops":
        raise
    # An older updater does not copy this new helper. Keep the panel reachable
    # so its new updater can finish installing the complete project.
    archiveops = None

app = Flask(__name__)
app.secret_key = CFG["SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # update bundles
# honor X-Forwarded-Proto/Host from the reverse proxy (CloudPanel nginx), so
# generated URLs (enroll command) correctly say https:// and the right host
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def require_archive_helper():
    global archiveops
    if archiveops is None:
        try:
            archiveops = importlib.import_module("archiveops")
        except ModuleNotFoundError as exc:
            if exc.name != "archiveops":
                raise
            raise RuntimeError("Archive helper is missing. Upload the full project, "
                               "including agent/archiveops.py, then apply the "
                               "project update in Settings.") from exc
    return archiveops


@app.context_processor
def update_health():
    return {"archive_helper_missing": archiveops is None}

DEFAULT_SETTINGS = {
    "syno_host": "", "syno_port": "22", "syno_user": "",
    "syno_path": "/volume1/WebBackups",
    "syno_pass": "",
    "default_cron": "30 3 * * *",
    "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "6",
    "ts_authkey": "",
    "ts_expiry": "",
    "repo_dir": "",
    "notify_email": "",
    "notify_webhook": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
}

# --------------------------------------------------------------------- db ---
def db():
    conn = sqlite3.connect(CFG["DB_PATH"], timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(CFG["DB_PATH"]), exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS servers(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER DEFAULT 22,
            ssh_user TEXT DEFAULT 'root',
            status TEXT DEFAULT 'new',
            last_backup TEXT DEFAULT '',
            last_check TEXT DEFAULT '',
            created TEXT
        );
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY,
            server_id INTEGER,
            server_name TEXT,
            type TEXT,
            status TEXT DEFAULT 'running',
            output TEXT DEFAULT '',
            created TEXT,
            finished TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
    # migration: column for de-duplicating failure notifications
    try:
        with db() as c:
            c.execute("ALTER TABLE servers ADD COLUMN notified_stamp TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # already exists


def get_setting(key):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enroll_token():
    """Persistent secret used by servers to enroll themselves with the panel."""
    tok = get_setting("enroll_token")
    if not tok:
        tok = secrets.token_hex(16)
        set_setting("enroll_token", tok)
    return tok


# ------------------------------------------------------------------- auth ---
FAILED = {}  # ip -> [count, locked_until]

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    if request.method == "POST":
        cnt, until = FAILED.get(ip, [0, 0])
        if time.time() < until:
            wait = int((until - time.time()) / 60) + 1
            flash("Too many attempts — try again in %d minute(s)." % wait)
            return render_template("login.html")
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == CFG["ADMIN_USER"] and CFG["ADMIN_HASH"] and \
                check_password_hash(CFG["ADMIN_HASH"], p):
            session["auth"] = True
            session.permanent = True
            FAILED.pop(ip, None)
            return redirect(request.args.get("next") or url_for("index"))
        cnt += 1
        FAILED[ip] = [cnt, time.time() + 300 if cnt >= 5 else 0]  # 5 fails → 5 min
        if cnt >= 5:
            flash("Wrong username or password. Too many attempts — locked for 5 minutes.")
        else:
            flash("Wrong username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------- central notifications ----
def notify_send(subject, message):
    """Send a failure alert from the PANEL (no per-server mail setup needed).
    Webhook: works out of the box. Email: uses the SMTP settings if set."""
    sent = False
    wh = get_setting("notify_webhook")
    if wh:
        try:
            data = json.dumps({"text": message, "content": message}).encode()
            req = urllib.request.Request(
                wh, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
            sent = True
        except Exception:
            try:  # some webhook targets (e.g. ntfy) prefer a plain body
                urllib.request.urlopen(
                    urllib.request.Request(wh, data=message.encode()), timeout=15)
                sent = True
            except Exception:
                pass
    email = get_setting("notify_email")
    host = get_setting("smtp_host")
    if email and host:
        try:
            msg = MIMEText(message)
            msg["Subject"] = subject
            msg["From"] = (get_setting("smtp_from") or get_setting("smtp_user")
                           or "webbackup-panel@localhost")
            msg["To"] = email
            port = int(get_setting("smtp_port") or 587)
            s = smtplib.SMTP(host, port, timeout=20)
            try:
                s.starttls()
            except smtplib.SMTPException:
                pass
            user = get_setting("smtp_user")
            if user:
                s.login(user, get_setting("smtp_pass"))
            s.sendmail(msg["From"], [email], msg.as_string())
            s.quit()
            sent = True
        except Exception:
            pass
    return sent


def monitor_loop():
    """Panel-side watchdog: every 15 min read each server's last-run state;
    on a NEW failure, send one central alert (webhook/email)."""
    time.sleep(90)  # let the service settle after boot
    while True:
        try:
            with db() as c:
                rows = c.execute("SELECT * FROM servers").fetchall()
            for r in rows:
                try:
                    client = sshops.connect(r["host"], r["port"], r["ssh_user"],
                                            keyfile=CFG["SSH_KEY"], timeout=10)
                    rc, out = sshops.run(
                        client, "/usr/local/bin/webbackup laststate", timeout=20)
                    client.close()
                    out = out.strip()
                    if rc != 0 or not out or out == "none":
                        continue
                    parts = out.split("|")
                    st = parts[0]
                    stamp = parts[1] if len(parts) > 1 else ""
                    with db() as c:
                        c.execute("UPDATE servers SET status=?, last_backup=?, "
                                  "last_check=? WHERE id=?",
                                  ("ok" if st == "ok" else "fail",
                                   stamp, now(), r["id"]))
                    if st != "ok" and stamp and r["notified_stamp"] != stamp:
                        reason = parts[4] if len(parts) > 4 else "backup failed"
                        notify_send(
                            "❌ Backup FAILED on %s" % r["name"],
                            "❌ Backup FAILED on %s (%s): %s\nPanel: check the "
                            "server page for details." % (r["name"], stamp, reason))
                        with db() as c:
                            c.execute("UPDATE servers SET notified_stamp=? WHERE id=?",
                                      (stamp, r["id"]))
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(900)


# ------------------------------------------------------------ job running ---
def create_job(server_id, server_name, jtype):
    with db() as c:
        cur = c.execute(
            "INSERT INTO jobs(server_id,server_name,type,created) VALUES(?,?,?,?)",
            (server_id, server_name, jtype, now()))
        return cur.lastrowid


def job_appender(job_id):
    lock = threading.Lock()
    def append(text, raw=False):
        if not raw:
            text = text.rstrip("\n") + "\n"
        with lock, db() as c:
            c.execute("UPDATE jobs SET output = output || ? WHERE id=?", (text, job_id))
    return append


def finish_job(job_id, ok):
    with db() as c:
        c.execute("UPDATE jobs SET status=?, finished=? WHERE id=?",
                  ("ok" if ok else "fail", now(), job_id))
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    # immediate central alert when a backup-type job fails
    if not ok and row and row["type"] in ("backup", "auto setup (after enroll)"):
        notify_send("❌ %s failed on %s" % (row["type"], row["server_name"]),
                    "❌ Job #%s (%s) FAILED on %s — open the panel job log for "
                    "details." % (job_id, row["type"], row["server_name"]))


def start_job(server_id, server_name, jtype, work):
    """Create a job row and run work(append) in a background thread."""
    job_id = create_job(server_id, server_name, jtype)
    append = job_appender(job_id)

    def runner():
        try:
            ok = work(append)
        except Exception as exc:  # noqa: BLE001 — report any failure into the job log
            append("\nERROR: %s" % exc)
            ok = False
        finish_job(job_id, bool(ok))

    threading.Thread(target=runner, daemon=True).start()
    return job_id


# ------------------------------------------------------------- ssh helpers --
def server_row(server_id):
    with db() as c:
        row = c.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    if not row:
        abort(404)
    return row


def connect_server(row, password=None):
    return sshops.connect(
        row["host"], row["port"], row["ssh_user"],
        password=password,
        keyfile=None if password else CFG["SSH_KEY"],
    )


def dashboard_pubkey():
    with open(CFG["SSH_KEY"] + ".pub") as fh:
        return fh.read().strip()


def build_agent_conf():
    """webbackup.conf contents from the dashboard's global settings."""
    return """# webbackup configuration — managed by webbackup dashboard
SYNO_HOST="{syno_host}"
SYNO_PORT="{syno_port}"
SYNO_USER="{syno_user}"
SYNO_PATH="{syno_path}"
SSH_KEY="/root/.ssh/webbackup_ed25519"

BACKUP_FILES="yes"
WEB_ROOTS="auto"
EXTRA_DIRS=""
EXCLUDES="node_modules .git cache/* *.log tmp/*"

BACKUP_MYSQL="auto"
MYSQL_DBS="all"
BACKUP_PGSQL="auto"

BACKUP_CONFIGS="yes"

KEEP_DAILY="{keep_daily}"
KEEP_WEEKLY="{keep_weekly}"
KEEP_MONTHLY="{keep_monthly}"

NAS_RETRY_MIN="10"
NAS_RETRY_HOURS="6"
RESTORE_KEEP_DAYS="3"

STAGING_DIR="/var/backups/webbackup"
BWLIMIT="0"
SCHEDULE_CRON="{default_cron}"
NOTIFY_WEBHOOK="{notify_webhook}"
NOTIFY_EMAIL="{notify_email}"
NOTIFY_ON="failure"
""".format(**{k: get_setting(k) for k in DEFAULT_SETTINGS})


def cron_file(cron_expr):
    return ("# webbackup schedule — managed by webbackup dashboard\n"
            "SHELL=/bin/bash\n"
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            "%s root /usr/local/bin/webbackup run --cron\n" % cron_expr)


def set_conf_values(client, changes):
    """Update KEY="value" lines in the remote webbackup.conf."""
    cmds = []
    for key, value in changes.items():
        if not re.fullmatch(r"[A-Z_]+", key):
            continue
        safe = value.replace("\\", "\\\\").replace("|", "\\|").replace('"', '\\"')
        cmds.append('sed -i "s|^{k}=.*|{k}=\\"{v}\\"|" /etc/webbackup/webbackup.conf'
                    .format(k=key, v=safe))
    if cmds:
        return sshops.run(client, " && ".join(cmds), timeout=30)
    return 0, ""


def refresh_server_state(client, server_id, append=None):
    """Read the agent's last-run state and cache it in the DB."""
    rc, out = sshops.run(client, "/usr/local/bin/webbackup laststate", timeout=30)
    out = out.strip()
    status, last_backup = "unknown", ""
    if rc == 0 and out and out != "none":
        parts = out.split("|")
        if len(parts) >= 2:
            status = "ok" if parts[0] == "ok" else "fail"
            last_backup = parts[1]
    elif out == "none":
        # never clobber the 'enrolled' marker before setup is completed
        with db() as c:
            cur = c.execute("SELECT status FROM servers WHERE id=?",
                            (server_id,)).fetchone()
        status = "enrolled" if (cur and cur["status"] == "enrolled") else "no-backup-yet"
    with db() as c:
        c.execute("UPDATE servers SET status=?, last_backup=?, last_check=? WHERE id=?",
                  (status, last_backup, now(), server_id))
    if append:
        append("State: %s (last backup: %s)" % (status, last_backup or "never"))
    return status


# -------------------------------------------------------- self-enrollment ---
ENROLL_SH = r"""#!/usr/bin/env bash
# webbackup self-enroll — run on a web server to connect it to the panel:
#   curl -fsSL http://PANEL:8800/enroll.sh | sudo bash -s -- http://PANEL:8800 TOKEN [IP] [SSH_PORT]
set -eu
PANEL="${1:?usage: enroll.sh PANEL_URL TOKEN [IP] [SSH_PORT]}"
TOKEN="${2:?enroll token required}"
IP="${3:-}"
SSH_PORT="${4:-22}"
[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)."; exit 1; }
command -v curl >/dev/null 2>&1 || {
  apt-get install -y curl 2>/dev/null || dnf install -y curl 2>/dev/null || \
  yum install -y curl 2>/dev/null || apk add curl 2>/dev/null; }

echo "[1/5] Downloading backup agent from the panel ..."
mkdir -p /tmp/wb-agent
curl -fsSL "$PANEL/api/agent/webbackup?token=$TOKEN"  -o /tmp/wb-agent/webbackup
curl -fsSL "$PANEL/api/agent/install.sh?token=$TOKEN" -o /tmp/wb-agent/install.sh
curl -fsSL "$PANEL/api/agent/archiveops.py?token=$TOKEN" -o /tmp/wb-agent/archiveops.py
head -c1 /tmp/wb-agent/webbackup | grep -q '#' || {
  echo "Download check failed — got an HTML page instead of the agent."
  echo "Make sure you used the exact command from the panel (https://...)."; exit 1; }

echo "[2/5] Installing agent and dependencies ..."
bash /tmp/wb-agent/install.sh < /dev/null

echo "[3/5] Tailscale (private connection to the NAS) ..."
TSKEY="$(curl -fsSL "$PANEL/api/tskey?token=$TOKEN" 2>/dev/null || true)"
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  echo "      already installed and connected — keeping as is."
elif [ -n "$TSKEY" ]; then
  command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up --authkey="$TSKEY" && echo "      Tailscale connected." || \
    echo "      WARNING: tailscale up failed — check the auth key in panel Settings."
else
  echo "      no Tailscale auth key set in panel Settings — skipped."
  echo "      (fine if this server reaches the NAS another way)"
fi

echo "[4/5] Registering this server with the panel ..."
if [ -z "$IP" ]; then
  IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  [ -n "$IP" ] || IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  [ -n "$IP" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
PUB="$(curl -fsS -X POST "$PANEL/api/enroll" \
        --data-urlencode "token=$TOKEN" \
        --data-urlencode "hostname=$(hostname)" \
        --data-urlencode "ip=$IP" \
        --data-urlencode "port=$SSH_PORT")"
case "$PUB" in
  ssh-*|ecdsa-*) ;;
  *) echo "Enrollment failed: $PUB"; exit 1 ;;
esac

echo "[5/5] Authorizing the panel's SSH key ..."
mkdir -p /root/.ssh && touch /root/.ssh/authorized_keys
grep -qxF "$PUB" /root/.ssh/authorized_keys || echo "$PUB" >> /root/.ssh/authorized_keys
chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys

echo ""
echo "✔ Connected as $(hostname) ($IP:$SSH_PORT)."
echo "  Now open the panel in your browser and click 'Complete setup' on this server."
"""


@app.route("/enroll.sh")
def enroll_script():
    # No secrets in the script itself — the token is passed as an argument.
    return Response(ENROLL_SH, mimetype="text/x-shellscript")


@app.route("/api/agent/<name>")
def agent_file(name):
    if request.args.get("token", "") != enroll_token():
        abort(403)
    if name not in ("webbackup", "install.sh", "archiveops.py"):
        abort(404)
    return send_from_directory(CFG["AGENT_DIR"], name)


@app.route("/api/tskey")
def api_tskey():
    if request.args.get("token", "") != enroll_token():
        abort(403)
    return get_setting("ts_authkey") or ""


@app.route("/api/enroll", methods=["POST"])
def api_enroll():
    if request.form.get("token", "") != enroll_token():
        return "bad token", 403
    hostname = re.sub(r"[^A-Za-z0-9._-]", "", request.form.get("hostname", ""))[:80]
    ip = re.sub(r"[^A-Za-z0-9.:_-]", "", request.form.get("ip", ""))[:100]
    try:
        port = int(request.form.get("port") or 22)
    except ValueError:
        port = 22
    if not ip:
        return "no ip supplied", 400
    with db() as c:
        # match by IP OR hostname, so a server that re-enrolls with a new IP
        # (e.g. after joining Tailscale) updates its row instead of duplicating
        if hostname:
            row = c.execute("SELECT id FROM servers WHERE host=? OR name=?",
                            (ip, hostname)).fetchone()
        else:
            row = c.execute("SELECT id FROM servers WHERE host=?", (ip,)).fetchone()
        if row:
            c.execute("UPDATE servers SET name=?, host=?, port=? WHERE id=?",
                      (hostname or ip, ip, port, row["id"]))
        else:
            c.execute("INSERT INTO servers(name,host,port,ssh_user,created,status) "
                      "VALUES(?,?,?,?,?,'enrolled')",
                      (hostname or ip, ip, port, "root", now()))
    # If everything needed is saved in Settings, finish the setup
    # automatically — no clicks in the panel required.
    if get_setting("syno_host") and get_setting("syno_user") and get_setting("syno_pass"):
        with db() as c:
            row = c.execute("SELECT * FROM servers WHERE host=?", (ip,)).fetchone()
        if row and not os.environ.get("WBD_NO_AUTOSETUP"):
            start_job(row["id"], row["name"], "auto setup (after enroll)",
                      configure_server_work(row, get_setting("syno_pass"),
                                            retry_connect=True))
    try:
        return dashboard_pubkey() + "\n"
    except OSError:
        return "panel has no SSH key (run the installer)", 500


def configure_server_work(row, nas_pass, retry_connect=False):
    """Finish setup for an enrolled server: config, NAS key, schedule, test.
    With retry_connect (auto-setup right after enroll), waits for the enroll
    script to finish authorizing the panel's key on the server."""
    server_id = row["id"]

    def work(append):
        append("[1/4] Connecting with the panel's SSH key ...")
        attempts = 8 if retry_connect else 1
        client = None
        for i in range(attempts):
            try:
                client = connect_server(row)
                break
            except Exception as exc:
                if i == attempts - 1:
                    raise
                append("      not ready yet (%s) — retrying in 10s (%d/%d)"
                       % (exc, i + 1, attempts))
                time.sleep(10)
        append("      connected.")

        append("[2/4] Writing configuration and schedule ...")
        sshops.run(client, "mkdir -p /etc/webbackup", timeout=15)
        sshops.sftp_put_data(client, build_agent_conf(), "/etc/webbackup/webbackup.conf", 0o600)
        sshops.sftp_put_data(client, cron_file(get_setting("default_cron")),
                             "/etc/cron.d/webbackup", 0o644)

        append("[3/4] Setting up this server's key on the Synology NAS ...")
        pubkey = sshops.ensure_server_keypair(client)
        if nas_pass:
            try:
                nas = sshops.connect(get_setting("syno_host"), get_setting("syno_port"),
                                     get_setting("syno_user"), password=nas_pass)
                sshops.install_authorized_key(nas, pubkey)
                nas.close()
                append("      NAS key installed for user '%s'." % get_setting("syno_user"))
            except Exception as exc:
                append("      NAS key install FAILED (%s)." % exc)
                append("      Add this key to the NAS user's ~/.ssh/authorized_keys manually:")
                append("      " + pubkey)
        else:
            append("      No NAS password given — add this key manually on the NAS:")
            append("      " + pubkey)

        append("[4/4] Testing server → NAS connection ...")
        rc, out = sshops.run(client, "/usr/local/bin/webbackup test", timeout=120)
        append(out)
        with db() as c:
            c.execute("UPDATE servers SET status=? WHERE id=?",
                      ("linked" if rc == 0 else "nas-issue", server_id))
        client.close()
        if rc == 0:
            append("\nServer fully configured and working ✔")
        else:
            append("\nConfigured, but the NAS connection needs attention — "
                   "fix and press Test.")
        return True
    return work


# ------------------------------------------------------------ link server ---
def link_server_work(name, host, port, ssh_user, root_pass, nas_pass):
    def work(append):
        append("[1/6] Connecting to %s:%s with password ..." % (host, port))
        client = sshops.connect(host, port, ssh_user, password=root_pass)
        append("      connected.")

        append("[2/6] Installing dashboard SSH key ...")
        sshops.install_authorized_key(client, dashboard_pubkey())
        client.close()
        client = sshops.connect(host, port, ssh_user, keyfile=CFG["SSH_KEY"])
        append("      key works — password no longer needed (and was not stored).")

        append("[3/6] Deploying webbackup agent ...")
        sshops.run(client, "mkdir -p /tmp/wb-agent", timeout=30)
        for name in ("webbackup", "install.sh", "archiveops.py"):
            sshops.sftp_put_file(client, os.path.join(CFG["AGENT_DIR"], name),
                                 "/tmp/wb-agent/" + name, 0o755)
        rc, out = sshops.run(client, "bash /tmp/wb-agent/install.sh < /dev/null", timeout=600)
        append(out)
        if rc != 0:
            raise RuntimeError("agent install failed")

        append("[4/6] Writing configuration and schedule ...")
        sshops.run(client, "mkdir -p /etc/webbackup", timeout=15)
        sshops.sftp_put_data(client, build_agent_conf(), "/etc/webbackup/webbackup.conf", 0o600)
        sshops.sftp_put_data(client, cron_file(get_setting("default_cron")),
                             "/etc/cron.d/webbackup", 0o644)

        append("[5/6] Setting up this server's key on the Synology NAS ...")
        pubkey = sshops.ensure_server_keypair(client)
        if nas_pass:
            try:
                nas = sshops.connect(get_setting("syno_host"), get_setting("syno_port"),
                                     get_setting("syno_user"), password=nas_pass)
                sshops.install_authorized_key(nas, pubkey)
                nas.close()
                append("      NAS key installed for user '%s'." % get_setting("syno_user"))
            except Exception as exc:
                append("      NAS key install FAILED (%s)." % exc)
                append("      Add this key to the NAS user's ~/.ssh/authorized_keys manually:")
                append("      " + pubkey)
        else:
            append("      No NAS password given — add this key manually on the NAS:")
            append("      " + pubkey)

        append("[6/6] Testing server → NAS connection ...")
        rc, out = sshops.run(client, "/usr/local/bin/webbackup test", timeout=120)
        append(out)

        with db() as c:
            c.execute("INSERT INTO servers(name,host,port,ssh_user,created,status) "
                      "VALUES(?,?,?,?,?,?)",
                      (name, host, port, ssh_user, now(),
                       "linked" if rc == 0 else "nas-issue"))
        client.close()
        if rc == 0:
            append("\nServer linked and fully working ✔")
        else:
            append("\nServer linked, but the NAS connection needs attention "
                   "(finish NAS setup, then use Test).")
        return True
    return work


# ------------------------------------------------------------------ routes --
@app.route("/")
@login_required
def index():
    with db() as c:
        servers = c.execute("SELECT * FROM servers ORDER BY name").fetchall()
        jobs = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
    return render_template("index.html", servers=servers, jobs=jobs,
                           settings={k: get_setting(k) for k in DEFAULT_SETTINGS})


@app.route("/servers/add", methods=["GET", "POST"])
@login_required
def add_server():
    if request.method == "POST":
        f = request.form
        name = f.get("name") or f.get("host")
        nas_pass = f.get("nas_password", "") or get_setting("syno_pass")
        job_id = start_job(None, name, "link server",
                           link_server_work(name, f["host"], int(f.get("port") or 22),
                                            f.get("ssh_user") or "root",
                                            f["password"], nas_pass))
        return redirect(url_for("job", job_id=job_id))
    nas_ready = bool(get_setting("syno_host") and get_setting("syno_user"))
    panel_url = request.host_url.rstrip("/")
    return render_template("add_server.html", nas_ready=nas_ready,
                           panel_url=panel_url, token=enroll_token())


@app.route("/servers/<int:server_id>")
@login_required
def server(server_id):
    row = server_row(server_id)
    with db() as c:
        jobs = c.execute("SELECT * FROM jobs WHERE server_id=? ORDER BY id DESC LIMIT 10",
                         (server_id,)).fetchall()
    snapshots, snap_sizes, conf, error = [], {}, "", ""
    try:
        client = connect_server(row)
        rc, out = sshops.run(client, "/usr/local/bin/webbackup snapshots", timeout=60)
        if rc == 0:
            for line in out.splitlines():
                toks = line.split()
                if toks and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}", toks[0]):
                    snapshots.append(toks[0])
                    if len(toks) > 1 and toks[1].isdigit():
                        snap_sizes[toks[0]] = human_bytes(int(toks[1]))
        conf = sshops.read_file(client, "/etc/webbackup/webbackup.conf") or ""
        client.close()
    except Exception as exc:
        error = str(exc)
    return render_template("server.html", s=row, jobs=jobs,
                           snapshots=list(reversed(snapshots)),
                           snap_sizes=snap_sizes, conf=conf, error=error)


@app.route("/servers/<int:server_id>/action", methods=["POST"])
@login_required
def server_action(server_id):
    row = server_row(server_id)
    action = request.form.get("action")

    def make(cmd, timeout=7200, then_refresh=True):
        def work(append):
            client = connect_server(row)
            rc, _ = sshops.run(client, cmd, timeout=timeout, append=append)
            if then_refresh:
                refresh_server_state(client, server_id, append)
            client.close()
            return rc == 0
        return work

    if action == "backup":
        # plain 'run' (not --cron) so queue/wait messages stream into this log;
        # generous timeout to allow NAS-down queueing (up to 6h) + the backup
        job_id = start_job(server_id, row["name"], "backup",
                           make("/usr/local/bin/webbackup run && echo BACKUP-OK",
                                timeout=10 * 3600))
    elif action == "update_agent":
        def work(append):
            client = connect_server(row)
            folder = "/tmp/wb-agent-" + secrets.token_hex(12)
            try:
                rc, out = sshops.run(client, "mkdir -m 700 -- %s" % sshops.shq(folder), timeout=30)
                if rc != 0:
                    raise RuntimeError(out)
                for name in ("webbackup", "install.sh", "archiveops.py"):
                    sshops.sftp_put_file(client, os.path.join(CFG["AGENT_DIR"], name),
                                         folder + "/" + name, 0o755)
                rc, _ = sshops.run(client, "bash %s < /dev/null" % sshops.shq(folder + "/install.sh"),
                                  timeout=600, append=append)
                if rc != 0:
                    return False
                rc, out = sshops.run(client, "command -v python3 >/dev/null && "
                                    "test -r /usr/local/lib/webbackup/archiveops.py && "
                                    "/usr/local/bin/webbackup --version", timeout=30)
                if rc != 0:
                    raise RuntimeError("Agent installation did not provide Python 3 "
                                       "and the archive helper: " + out)
                append("Installed %s; selected-file restore support is ready." % out.strip())
                return True
            finally:
                sshops.run(client, "rm -rf -- %s" % sshops.shq(folder), timeout=30)
                client.close()
        job_id = start_job(server_id, row["name"], "update backup agent", work)
    elif action == "complete":
        nas_pass = request.form.get("nas_password", "") or get_setting("syno_pass")
        job_id = start_job(server_id, row["name"], "complete setup",
                           configure_server_work(row, nas_pass))
    elif action == "test":
        job_id = start_job(server_id, row["name"], "test NAS connection",
                           make("/usr/local/bin/webbackup test", 120, False))
    elif action == "clear_restore":
        job_id = start_job(server_id, row["name"], "clear restore files",
                           make("/usr/local/bin/webbackup clear-restore", 300, False))
    elif action == "refresh":
        def work(append):
            client = connect_server(row)
            refresh_server_state(client, server_id, append)
            client.close()
            return True
        job_id = start_job(server_id, row["name"], "refresh status", work)
    elif action == "restore":
        snap = request.form.get("snapshot", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}", snap):
            abort(400)
        job_id = start_job(server_id, row["name"], "restore " + snap,
                           make("/usr/local/bin/webbackup restore %s" % snap, 7200, False))
    elif action == "schedule":
        cron = build_cron(request.form)
        if not valid_cron(cron):
            flash("Invalid cron expression.")
            return redirect(url_for("server", server_id=server_id))
        def work(append):
            client = connect_server(row)
            sshops.sftp_put_data(client, cron_file(cron), "/etc/cron.d/webbackup", 0o644)
            set_conf_values(client, {"SCHEDULE_CRON": cron})
            append("Schedule set to: %s" % cron)
            client.close()
            return True
        job_id = start_job(server_id, row["name"], "set schedule", work)
    elif action == "save_conf":
        # browsers submit textareas with Windows line endings — normalize,
        # or bash on the server chokes on the \r characters
        conf_text = request.form.get("conf", "").replace("\r\n", "\n").replace("\r", "\n")
        if not conf_text.endswith("\n"):
            conf_text += "\n"
        def work(append):
            client = connect_server(row)
            sshops.sftp_put_data(client, conf_text, "/etc/webbackup/webbackup.conf", 0o600)
            append("Configuration saved.")
            client.close()
            return True
        job_id = start_job(server_id, row["name"], "save config", work)
    elif action == "restore_site":
        snap = request.form.get("snapshot", "")
        item = request.form.get("item", "")
        if not SNAP_RE.fullmatch(snap) or not re.fullmatch(r"[A-Za-z0-9._\-]+\.tar\.gz", item):
            abort(400)
        job_id = start_job(server_id, row["name"], "restore website %s" % item,
                           make("/usr/local/bin/webbackup restore-site %s %s"
                                % (snap, sshops.shq(item)), 7200, False))
    elif action == "restore_file":
        snap = request.form.get("snapshot", "")
        item = request.form.get("item", "")
        fpath = request.form.get("fpath", "").strip().lstrip("/")
        if (not SNAP_RE.fullmatch(snap)
                or not re.fullmatch(r"[A-Za-z0-9._\-]+\.tar\.gz", item)
                or not re.fullmatch(r"[A-Za-z0-9._/\-]+", fpath) or ".." in fpath):
            abort(400)
        job_id = start_job(server_id, row["name"], "restore file %s" % fpath,
                           make("/usr/local/bin/webbackup restore-file %s %s %s"
                                % (snap, sshops.shq(item), sshops.shq(fpath)), 3600, False))
    elif action == "restore_db":
        snap = request.form.get("snapshot", "")
        dbname = request.form.get("db", "")
        if not SNAP_RE.fullmatch(snap) or not re.fullmatch(r"[A-Za-z0-9_\-]+", dbname):
            abort(400)
        job_id = start_job(server_id, row["name"], "restore database %s" % dbname,
                           make("/usr/local/bin/webbackup restore-db %s %s"
                                % (snap, sshops.shq(dbname)), 7200, False))
    elif action == "delete_snapshot":
        snap = request.form.get("snapshot", "")
        if not SNAP_RE.fullmatch(snap):
            abort(400)

        def work(append):
            host = server_hostname(row)
            base = get_setting("syno_path").rstrip("/")
            if not base.startswith("/volume") or not host:
                raise RuntimeError("refusing: unexpected NAS path")
            nas = nas_connect()
            rc, out = sshops.run(nas, "rm -rf -- %s"
                                 % sshops.shq("%s/%s/%s" % (base, host, snap)),
                                 timeout=300)
            nas.close()
            append("Snapshot %s deleted from the NAS." % snap if rc == 0
                   else "Delete failed: %s" % out)
            return rc == 0
        job_id = start_job(server_id, row["name"], "delete snapshot " + snap, work)
    elif action == "unlink":
        if request.form.get("delete_backups"):
            # destructive path: requires the typed server name to match
            if request.form.get("confirm_name", "") != row["name"]:
                flash("Confirmation name did not match — nothing was deleted.")
                return redirect(url_for("server", server_id=server_id))

            def work(append):
                try:
                    host = server_hostname(row)
                except Exception:
                    host = row["name"]
                    append("Server unreachable — using its name '%s' as the NAS folder." % host)
                base = get_setting("syno_path").rstrip("/")
                if not base.startswith("/volume") or not host:
                    raise RuntimeError("refusing: unexpected NAS path")
                append("Deleting ALL backups: %s/%s ..." % (base, host))
                nas = nas_connect()
                rc, out = sshops.run(nas, "rm -rf -- %s"
                                     % sshops.shq("%s/%s" % (base, host)),
                                     timeout=1800)
                nas.close()
                if rc != 0:
                    append("NAS delete failed: %s — server NOT removed from panel." % out)
                    return False
                append("All backups deleted from the NAS.")
                with db() as c:
                    c.execute("DELETE FROM servers WHERE id=?", (server_id,))
                append("Server removed from the dashboard.")
                return True
            job_id = start_job(server_id, row["name"], "remove server + delete backups", work)
            return redirect(url_for("job", job_id=job_id))
        with db() as c:
            c.execute("DELETE FROM servers WHERE id=?", (server_id,))
        flash("Server removed from dashboard (nothing was changed on the server or NAS).")
        return redirect(url_for("index"))
    else:
        abort(400)
    return redirect(url_for("job", job_id=job_id))


CRON_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")

def valid_cron(expr):
    return bool(CRON_RE.match(expr)) and all(
        re.fullmatch(r"[\d*,/-]+", f) for f in expr.split())


def build_cron(form):
    """Build a cron expression from the friendly schedule picker fields.
    Falls back to the raw 'cron' field when type is custom."""
    t = form.get("sched_type", "custom")
    if t == "custom":
        return form.get("cron", "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", form.get("sched_time") or "03:30")
    hh, mm = (int(m.group(1)) % 24, int(m.group(2)) % 60) if m else (3, 30)
    if t == "daily":
        return "%d %d * * *" % (mm, hh)
    if t == "twice":
        return "%d %d,%d * * *" % (mm, hh, (hh + 12) % 24)
    if t == "every6h":
        return "15 */6 * * *"
    if t == "hourly":
        return "5 * * * *"
    if t == "weekly":
        dow = form.get("sched_dow", "0")
        return "%d %d * * %s" % (mm, hh, dow if dow in tuple("0123456") else "0")
    if t == "monthly":
        try:
            dom = max(1, min(28, int(form.get("sched_dom") or 1)))
        except ValueError:
            dom = 1
        return "%d %d %d * *" % (mm, hh, dom)
    return ""


@app.route("/push", methods=["POST"])
@login_required
def push():
    """Push schedule and/or retention settings to the selected servers."""
    ids = [int(x) for x in request.form.getlist("server_ids")]
    if not ids:
        flash("No servers selected.")
        return redirect(url_for("index"))

    cron = build_cron(request.form)
    push_sched = bool(cron) and request.form.get("push_schedule")
    push_ret = request.form.get("push_retention")
    push_notify = request.form.get("push_notify")
    if push_sched and not valid_cron(cron):
        flash("Invalid cron expression.")
        return redirect(url_for("index"))

    keep = {k: request.form.get(k, get_setting(k))
            for k in ("keep_daily", "keep_weekly", "keep_monthly")}

    if push_sched:
        set_setting("default_cron", cron)
    if push_ret:
        for k, v in keep.items():
            set_setting(k, v)
    notify_email = request.form.get("notify_email", get_setting("notify_email")).strip()
    notify_webhook = request.form.get("notify_webhook", get_setting("notify_webhook")).strip()
    if push_notify:
        set_setting("notify_email", notify_email)
        set_setting("notify_webhook", notify_webhook)

    with db() as c:
        rows = [c.execute("SELECT * FROM servers WHERE id=?", (i,)).fetchone() for i in ids]
    rows = [r for r in rows if r]

    def work(append):
        ok = True
        for r in rows:
            append("— %s (%s):" % (r["name"], r["host"]))
            try:
                client = sshops.connect(r["host"], r["port"], r["ssh_user"],
                                        keyfile=CFG["SSH_KEY"])
                if push_sched:
                    sshops.sftp_put_data(client, cron_file(cron),
                                         "/etc/cron.d/webbackup", 0o644)
                    set_conf_values(client, {"SCHEDULE_CRON": cron})
                    append("    schedule → %s" % cron)
                if push_ret:
                    set_conf_values(client, {
                        "KEEP_DAILY": keep["keep_daily"],
                        "KEEP_WEEKLY": keep["keep_weekly"],
                        "KEEP_MONTHLY": keep["keep_monthly"]})
                    append("    retention → %s recent / %s weekly / %s monthly"
                           % (keep["keep_daily"], keep["keep_weekly"], keep["keep_monthly"]))
                if push_notify:
                    set_conf_values(client, {
                        "NOTIFY_EMAIL": notify_email,
                        "NOTIFY_WEBHOOK": notify_webhook,
                        "NOTIFY_ON": "failure"})
                    append("    notifications → email: %s · webhook: %s (on failure)"
                           % (notify_email or "—", "set" if notify_webhook else "—"))
                client.close()
            except Exception as exc:
                append("    FAILED: %s" % exc)
                ok = False
        return ok

    job_id = start_job(None, "%d servers" % len(rows), "push settings", work)
    return redirect(url_for("job", job_id=job_id))


@app.route("/jobs")
@login_required
def jobs():
    with db() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("jobs.html", jobs=rows)


@app.route("/jobs/<int:job_id>")
@login_required
def job(job_id):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    return render_template("job.html", j=row)


@app.route("/jobs/<int:job_id>/output")
@login_required
def job_output(job_id):
    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        abort(400)
    if offset < 0:
        abort(400)
    # Return only new text. The browser uses this Python character offset
    # rather than JavaScript's UTF-16 length, including for emoji in logs.
    with closing(db()) as c:
        row = c.execute(
            "SELECT status, finished, output FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    output = row["output"] or ""
    reset = offset > len(output)
    response = jsonify(status=row["status"], finished=row["finished"],
                       output=output if reset else output[offset:],
                       offset=len(output), reset=reset)
    response.headers["Cache-Control"] = "no-store"
    return response


SECRET_SETTINGS = ("ts_authkey", "syno_pass", "smtp_pass")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        for k in DEFAULT_SETTINGS:
            if k in request.form:
                v = request.form[k].strip()
                if k in SECRET_SETTINGS:
                    if v == "":
                        continue            # empty = keep the saved secret
                    if v.lower() == "clear":
                        v = ""              # the word 'clear' removes it
                if k == "ts_authkey" and v and not v.startswith("tskey-"):
                    # reject junk (e.g. browser autofill injecting a username)
                    flash("Tailscale key NOT saved: a valid key starts with "
                          "'tskey-' but the field contained something else "
                          "(probably autofill). The previously saved key was kept.")
                    continue
                set_setting(k, v)
        flash("Settings saved. They apply to newly linked servers; use the "
              "push panel on the home page to update existing servers.")
        return redirect(url_for("settings"))
    vals = {k: get_setting(k) for k in DEFAULT_SETTINGS}
    # never render secrets back into the page — show a saved-flag instead
    saved = {k: bool(vals[k]) for k in SECRET_SETTINGS}
    ts_invalid = bool(vals["ts_authkey"]) and not vals["ts_authkey"].startswith("tskey-")
    ts_hint = vals["ts_authkey"]  # shown in full on the settings page
    for k in SECRET_SETTINGS:
        vals[k] = ""
    ts_note, ts_urgent = "", False
    if vals["ts_expiry"]:
        try:
            d = datetime.strptime(vals["ts_expiry"], "%Y-%m-%d").date()
            days = (d - datetime.now().date()).days
            if days < 0:
                ts_note, ts_urgent = "⚠ key EXPIRED %d day(s) ago — generate a new one" % -days, True
            elif days <= 14:
                ts_note, ts_urgent = "⚠ key expires in %d day(s) (%s)" % (days, vals["ts_expiry"]), True
            else:
                ts_note = "key expires %s (in %d days)" % (vals["ts_expiry"], days)
        except ValueError:
            pass
    pub = ""
    try:
        pub = dashboard_pubkey()
    except OSError:
        pass
    return render_template("settings.html", s=vals, pubkey=pub, saved=saved,
                           ts_note=ts_note, ts_urgent=ts_urgent,
                           ts_invalid=ts_invalid, ts_hint=ts_hint)


# ------------------------------------------------- snapshot browse/download --
SNAP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")
RELPATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def nas_connect():
    pw = get_setting("syno_pass")
    if not pw:
        raise RuntimeError("save the NAS backup user password in Settings "
                           "to enable browsing/downloading snapshots")
    return sshops.connect(get_setting("syno_host"), get_setting("syno_port"),
                          get_setting("syno_user"), password=pw)


def server_hostname(row):
    client = connect_server(row)
    rc, out = sshops.run(client, "hostname", timeout=20)
    client.close()
    name = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if rc != 0 or not name:
        raise RuntimeError("could not read the server's hostname")
    return name


def classify_site(arch, parent, root=""):
    """Show the domain for both old path-named and new domain-named archives."""
    base = arch[:-len(".tar.gz")] if arch.endswith(".tar.gz") else arch
    info = {"arch": arch, "label": base, "url": "", "path": ""}
    if parent:
        mang = parent.strip("/").replace("/", "_")
        top = base[len(mang) + 1:] if base.startswith(mang + "_") else ""
        if root:
            full = root
        elif top:
            full = "%s/%s" % (parent.rstrip("/"), top)
        elif re.fullmatch(r"/home/[^/]+/web/[^/]+", parent):
            full = parent.rstrip("/") + "/public_html"
        elif re.fullmatch(r"/home/[^/]+/htdocs", parent):
            full = parent.rstrip("/") + "/" + base
        else:
            full = parent
        info["path"] = full
        m = re.match(r"^/home/([^/]+)/web/([^/]+)/public_html$", full)   # HestiaCP
        if m:
            info["label"], info["url"] = m.group(2), m.group(2)
            info["user"] = m.group(1)
            return info
        m = re.match(r"^/home/([^/]+)/htdocs/([^/]+)$", full)            # CloudPanel
        if m:
            info["label"], info["url"] = m.group(2), m.group(2)
            info["user"] = m.group(1)
            return info
        info["label"] = full
    # Pre-PATHS snapshots can still have a friendly domain label, but restoring
    # requires the original parent map rather than guessing a live location.
    if not parent:
        m = re.fullmatch(r"home_(.+)_web_([^_]+)_public_html", base)
        if not m:
            m = re.fullmatch(r"home_(.+)_htdocs_([^_]+)", base)
        if m:
            info.update(label=m.group(2), url=m.group(2), user=m.group(1))
    return info


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


ARCHIVE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*\.tar\.gz")
ARCHIVE_CACHE = OrderedDict()
ARCHIVE_CACHE_LOCK = threading.Lock()


def snapshot_maps(nas, base):
    maps = []
    for name in ("PATHS.txt", "ROOTS.txt"):
        rc, out = sshops.run(nas, "cat %s 2>/dev/null" % sshops.shq(base + "/files/" + name), timeout=30)
        mapping = {}
        if rc == 0:
            for line in out.splitlines():
                if "\t" in line:
                    arch, path = line.split("\t", 1)
                    mapping[arch] = path
        maps.append(mapping)
    return maps


def archive_data(row, snap, arch):
    """Read indexes; build legacy indexes on the NAS when Python is available."""
    helper = require_archive_helper()
    key = (row["id"], row["host"], get_setting("syno_host"), get_setting("syno_path"), snap, arch)
    with ARCHIVE_CACHE_LOCK:
        cached = ARCHIVE_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            ARCHIVE_CACHE.move_to_end(key)
            return cached[1]
    host = server_hostname(row)
    base = "%s/%s/%s" % (get_setting("syno_path").rstrip("/"), host, snap)
    nas = nas_connect()
    try:
        parents, roots = snapshot_maps(nas, base)
        remote = base + "/files/" + arch
        index_path = remote[:-7] + ".index.json"
        rc, out = sshops.run(nas, "cat %s 2>/dev/null" % sshops.shq(index_path), timeout=60)
        index = None
        if rc == 0:
            try:
                stored = json.loads(out)
                if stored.get("version") == 1:
                    index = helper.complete_index(stored["entries"])
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # Missing/damaged indexes can be rebuilt from the archive.
        if index is None:
            # Older snapshots can be large. Scan them on the NAS and transfer
            # only the resulting metadata instead of the whole compressed file.
            # NASes without Python or write access retain the streaming fallback.
            rc, _ = sshops.run(nas, "command -v python3 >/dev/null 2>&1", timeout=30)
            if rc == 0:
                with open(helper.__file__, encoding="utf-8") as source:
                    code = source.read()
                cmd = "python3 -c %s index-cache %s" % (sshops.shq(code), sshops.shq(remote))
                rc, _ = sshops.run(nas, cmd, timeout=600)
                if rc == 0:
                    rc, out = sshops.run(nas, "cat %s 2>/dev/null" % sshops.shq(index_path), timeout=60)
                    if rc == 0:
                        try:
                            stored = json.loads(out)
                            if stored.get("version") == 1:
                                index = helper.complete_index(stored["entries"])
                        except (ValueError, KeyError, TypeError, AttributeError):
                            pass
        if index is None:
            chan = nas.get_transport().open_session()
            chan.settimeout(600)
            chan.exec_command("cat -- %s" % sshops.shq(remote))
            try:
                with chan.makefile("rb") as stream:
                    index = helper.read_index(stream)
                while chan.recv(65536):
                    pass
                if chan.recv_exit_status() != 0:
                    raise RuntimeError("Could not read this website archive")
            finally:
                chan.close()
        parent = parents.get(arch, "")
        actual_root = parent.rstrip("/") + "/" + index["root"] if parent else ""
        if roots.get(arch) and roots[arch] != actual_root:
            raise ValueError("Snapshot root mapping does not match the archive")
        site = classify_site(arch, parent, actual_root)
        data = {"index": index, "site": site, "parent": parent}
    finally:
        nas.close()
    with ARCHIVE_CACHE_LOCK:
        ARCHIVE_CACHE[key] = (time.monotonic(), data)
        ARCHIVE_CACHE.move_to_end(key)
        while len(ARCHIVE_CACHE) > 6:
            ARCHIVE_CACHE.popitem(last=False)
    return data


def validate_archive_request(snap, arch):
    if not SNAP_RE.fullmatch(snap) or not ARCHIVE_RE.fullmatch(arch):
        abort(400)


def restore_csrf():
    token = session.setdefault("restore_csrf", secrets.token_hex(32))
    if request.method == "POST" and not secrets.compare_digest(token, request.form.get("csrf", "")):
        abort(400, "Restore form expired. Reload the file browser.")
    return token


@app.route("/servers/<int:server_id>/snapshots/<snap>/files/<arch>")
@login_required
def snapshot_files(server_id, snap, arch):
    validate_archive_request(snap, arch)
    return render_template("snapshot_files.html", s=server_row(server_id), snap=snap,
                           arch=arch, csrf=restore_csrf())


@app.route("/servers/<int:server_id>/snapshots/<snap>/file-index")
@login_required
def snapshot_file_index(server_id, snap):
    arch = request.args.get("archive", "")
    validate_archive_request(snap, arch)
    try:
        data = archive_data(server_row(server_id), snap, arch)
        return jsonify(index=data["index"], site=data["site"], restorable=bool(data["parent"]))
    except Exception as exc:
        return jsonify(error="Could not read the backup files: %s" % exc), 502


def selected_restore_work(row, snap, arch, selection):
    def work(append):
        client = connect_server(row)
        folder = "/tmp/wb-selection-" + secrets.token_hex(12)
        created = False
        try:
            rc, _ = sshops.run(client, "command -v python3 >/dev/null && test -f /usr/local/lib/webbackup/archiveops.py", timeout=30)
            if rc != 0:
                raise RuntimeError("Open this server's page and click Update backup agent, then retry the restore.")
            rc, out = sshops.run(client, "mkdir -m 700 -- %s" % sshops.shq(folder), timeout=30)
            if rc != 0:
                raise RuntimeError(out)
            created = True
            path = folder + "/selection.json"
            sshops.sftp_put_data(client, json.dumps(selection), path, 0o600)
            rc, _ = sshops.run(client, "/usr/local/bin/webbackup restore-items %s %s %s"
                              % (sshops.shq(snap), sshops.shq(arch), sshops.shq(path)), timeout=7200, append=append)
            return rc == 0
        finally:
            try:
                if created:
                    sshops.run(client, "rm -rf -- %s" % sshops.shq(folder), timeout=30)
            finally:
                client.close()
    return work


@app.route("/servers/<int:server_id>/snapshots/<snap>/restore-selected", methods=["POST"])
@login_required
def snapshot_restore_selected(server_id, snap):
    csrf = restore_csrf()
    row = server_row(server_id)
    signer = URLSafeTimedSerializer(app.secret_key, salt="selected-restore")
    token = request.form.get("restore_token", "")
    if token:
        try:
            reviewed = signer.loads(token, max_age=1200)
        except (BadSignature, SignatureExpired):
            abort(400, "Restore review expired. Select the files again.")
        if reviewed["server"] != server_id or reviewed["snapshot"] != snap:
            abort(400)
        arch, selection = reviewed["archive"], reviewed["selection"]
    else:
        arch, selection = request.form.get("archive", ""), request.form.getlist("paths")
    validate_archive_request(snap, arch)
    try:
        data = archive_data(row, snap, arch)
        if not data["parent"]:
            raise ValueError("This snapshot has no original path map. Download it for a manual restore.")
        chosen = require_archive_helper().selected_entries(data["index"], selection)
    except Exception as exc:
        flash(str(exc))
        return redirect(url_for("snapshot_files", server_id=server_id, snap=snap, arch=arch))
    if token:
        job_id = start_job(server_id, row["name"], "restore selected files: %s" % data["site"]["label"],
                           selected_restore_work(row, snap, arch, selection))
        return redirect(url_for("job", job_id=job_id))
    token = signer.dumps({"server": server_id, "snapshot": snap, "archive": arch, "selection": selection})
    files = [e for e in chosen if e["type"] == "file"]
    return render_template("restore_review.html", s=row, snap=snap, arch=arch, site=data["site"],
                           selection=selection, files=files, total_size=human_bytes(sum(f["size"] for f in files)),
                           token=token, csrf=csrf)


@app.route("/servers/<int:server_id>/snapshots/<snap>")
@login_required
def snapshot_browse(server_id, snap):
    row = server_row(server_id)
    if not SNAP_RE.match(snap):
        abort(400)
    files, sites, dbs, error = [], [], [], ""
    nas = None
    try:
        host = server_hostname(row)
        nas = nas_connect()
        base = "%s/%s/%s" % (get_setting("syno_path").rstrip("/"), host, snap)
        # plain SSH (no SFTP service needed on the Synology)
        script = ("cd %s && find . -type f | sed 's|^\\./||' | "
                  "while IFS= read -r f; do "
                  "printf '%%s\\t%%s\\n' \"$(wc -c < \"$f\" | tr -d ' ')\" \"$f\"; done"
                  % sshops.shq(base))
        rc, out = sshops.run(nas, script, timeout=120)
        if rc != 0:
            raise RuntimeError(out.strip() or "could not list the snapshot")
        for line in out.splitlines():
            if "\t" not in line:
                continue
            size, path = line.split("\t", 1)
            entry = {"path": path, "size": human_bytes(int(size or 0))}
            files.append(entry)
            if (path.startswith("files/") and path.endswith(".tar.gz")):
                sites.append(path[len("files/"):])
            # older snapshots: loose per-database dumps
            if (path.startswith("databases/mysql_") and path.endswith(".sql.gz")):
                dbs.append(path[len("databases/mysql_"):-len(".sql.gz")])
        # databases inside the bundle
        rc, out = sshops.run(nas, "tar -tf %s 2>/dev/null"
                             % sshops.shq(base + "/databases/mysql_all_databases.tar"),
                             timeout=60)
        if rc == 0:
            for m in out.split():
                if m.startswith("mysql_") and m.endswith(".sql.gz"):
                    dbs.append(m[len("mysql_"):-len(".sql.gz")])
        # path map -> friendly site names (user + domain)
        paths_map, roots_map = snapshot_maps(nas, base)
        named, sys_sites = [], []
        for arch in sorted(set(sites)):
            info = classify_site(arch, paths_map.get(arch, ""), roots_map.get(arch, ""))
            (named if info["url"] else sys_sites).append(info)
        named.sort(key=lambda i: i["label"].lower())
        files.sort(key=lambda f: f["path"])
        others = [f for f in files
                  if not f["path"].startswith(("files/", "databases/mysql_"))
                  or f["path"] == "databases/mysql_all_databases.tar"]
    except Exception as exc:
        error = str(exc)
        named, sys_sites, others = [], [], []
    finally:
        if nas is not None:
            nas.close()
    return render_template("snapshot.html", s=row, snap=snap, files=files,
                           others=others, sites=named, sys_sites=sys_sites,
                           dbs=sorted(set(dbs)), error=error)


@app.route("/servers/<int:server_id>/snapshots/<snap>/dbdump/<dbname>")
@login_required
def snapshot_db_download(server_id, snap, dbname):
    """Download one database dump — from a loose file or extracted on the
    fly out of the mysql_all_databases.tar bundle."""
    row = server_row(server_id)
    if not SNAP_RE.fullmatch(snap) or not re.fullmatch(r"[A-Za-z0-9_\-]+", dbname):
        abort(400)
    host = server_hostname(row)
    nas = nas_connect()
    base = "%s/%s/%s" % (get_setting("syno_path").rstrip("/"), host, snap)
    loose = "%s/databases/mysql_%s.sql.gz" % (base, dbname)
    bundle = "%s/databases/mysql_all_databases.tar" % base
    member = "mysql_%s.sql.gz" % dbname
    size = None
    rc, out = sshops.run(nas, "wc -c < %s" % sshops.shq(loose), timeout=30)
    if rc == 0:
        size = int(out.strip() or 0)
        cmd = "cat %s" % sshops.shq(loose)
    else:
        rc, out = sshops.run(nas, "tar -tvf %s %s 2>/dev/null"
                             % (sshops.shq(bundle), sshops.shq(member)), timeout=60)
        if rc != 0 or not out.strip():
            nas.close()
            abort(404)
        try:
            size = int(out.split()[2])
        except (IndexError, ValueError):
            size = None
        cmd = "tar -xOf %s %s" % (sshops.shq(bundle), sshops.shq(member))
    chan = nas.get_transport().open_session()
    chan.exec_command(cmd)

    def gen():
        try:
            while True:
                chunk = chan.recv(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            chan.close()
            nas.close()

    headers = {"Content-Disposition": 'attachment; filename="%s"' % member}
    if size:
        headers["Content-Length"] = str(size)
    return Response(gen(), mimetype="application/octet-stream", headers=headers)


@app.route("/servers/<int:server_id>/snapshots/<snap>/download")
@login_required
def snapshot_download(server_id, snap):
    row = server_row(server_id)
    fpath = request.args.get("f", "")
    if not SNAP_RE.match(snap) or not RELPATH_RE.match(fpath) or ".." in fpath:
        abort(400)
    host = server_hostname(row)
    nas = nas_connect()
    remote = "%s/%s/%s/%s" % (get_setting("syno_path").rstrip("/"), host, snap, fpath)
    rc, out = sshops.run(nas, "wc -c < %s" % sshops.shq(remote), timeout=30)
    if rc != 0:
        nas.close()
        abort(404)
    size = int(out.strip() or 0)
    # stream over plain SSH exec — no SFTP service required on the NAS
    chan = nas.get_transport().open_session()
    chan.exec_command("cat %s" % sshops.shq(remote))

    def gen():
        try:
            while True:
                chunk = chan.recv(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            chan.close()
            nas.close()

    fname = os.path.basename(fpath)
    return Response(gen(), mimetype="application/octet-stream", headers={
        "Content-Disposition": 'attachment; filename="%s"' % fname,
        "Content-Length": str(size)})


# ---------------------------------------------------------- panel updates ---
UPDATE_LOCK = threading.Lock()


def apply_update_from(root):
    if not UPDATE_LOCK.acquire(blocking=False):
        raise RuntimeError("A dashboard update is already in progress.")
    try:
        _apply_update_from(root)
    finally:
        UPDATE_LOCK.release()


def _apply_update_from(root):
    """Copy panel + agent files from an extracted/checked-out project root.
    Raises on problems; config/db/keys are never touched."""
    app_dir = CFG.get("APP_DIR") or os.path.dirname(os.path.abspath(__file__))
    dsrc = os.path.join(root, "dashboard")
    if os.path.realpath(dsrc) == os.path.realpath(app_dir):
        raise ValueError("The project folder must be separate from the installed dashboard.")
    required = ("dashboard/app.py", "dashboard/sshops.py", "dashboard/requirements.txt",
                "dashboard/templates/base.html", "dashboard/templates/_ui.html",
                "dashboard/templates/_schedule.html", "dashboard/templates/snapshot_files.html",
                "dashboard/templates/restore_review.html", "dashboard/static/style.css",
                "dashboard/templates/job.html", "dashboard/static/job-output.js",
                "dashboard/static/dashboard.js", "dashboard/static/snapshot-files.js",
                "dashboard/static/snapshot-files.css", "agent/webbackup", "agent/install.sh",
                "agent/archiveops.py")
    missing = [name for name in required if not os.path.isfile(os.path.join(root, name))]
    if missing:
        raise ValueError("Incomplete project upload. Missing: " + ", ".join(missing))
    # Reject invalid Python before replacing any installed files.
    for name in ("dashboard/app.py", "dashboard/sshops.py", "agent/archiveops.py"):
        path = os.path.join(root, name)
        with open(path, "rb") as source:
            compile(source.read(), path, "exec")
    pip = os.path.join(app_dir, "venv", "bin", "pip")
    if os.path.isfile(pip):
        subprocess.run([pip, "install", "-q", "-r", os.path.join(dsrc, "requirements.txt")],
                       timeout=300, check=True)
    asrc = os.path.join(root, "agent")
    payload = [(os.path.join(dsrc, name), os.path.join(app_dir, name))
               for name in ("sshops.py", "requirements.txt", "templates", "static")]
    payload += [(os.path.join(asrc, name), os.path.join(CFG["AGENT_DIR"], name))
                for name in ("webbackup", "install.sh", "archiveops.py")]
    # The entry point is installed last, after its helper and assets.
    payload.append((os.path.join(dsrc, "app.py"), os.path.join(app_dir, "app.py")))
    token = secrets.token_hex(12)
    staged, backups, installed = [], {}, []

    def remove_path(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.unlink(path)

    try:
        # Stage alongside each destination so every rename stays on the same
        # filesystem, including installations with a separate AGENT_DIR.
        for src, dst in payload:
            parent = os.path.dirname(dst)
            os.makedirs(parent, exist_ok=True)
            new = os.path.join(parent, ".wbupdate-new-%s-%s" % (token, os.path.basename(dst)))
            staged.append((new, dst))
            if os.path.isdir(src):
                shutil.copytree(src, new)
            else:
                shutil.copy2(src, new)
                if os.path.dirname(src) == asrc:
                    os.chmod(new, 0o755)
        for new, dst in staged:
            if os.path.lexists(dst):
                old = os.path.join(os.path.dirname(dst), ".wbupdate-old-%s-%s" % (token, os.path.basename(dst)))
                os.replace(dst, old)
                backups[dst] = old
            os.replace(new, dst)
            installed.append(dst)
    except Exception:
        # Restore the previous complete installation if publishing any file
        # fails. Config, database, and keys are outside the update payload.
        for _, dst in reversed(staged):
            if dst in installed:
                remove_path(dst)
            if dst in backups:
                os.replace(backups[dst], dst)
        raise
    else:
        for old in backups.values():
            try:
                remove_path(old)
            except OSError:
                app.logger.warning("Update installed; previous files remain at %s", old)
    finally:
        for new, _ in staged:
            remove_path(new)


def restart_and_confirm(extra=""):
    if not os.environ.get("WBD_NO_RESTART"):
        subprocess.Popen(["bash", "-c", "sleep 1; systemctl restart webbackup-dashboard"],
                         start_new_session=True)
    return ("<!doctype html><meta http-equiv='refresh' content='7;url=/'>"
            "<body style='font-family:system-ui;padding:48px;background:#f4f6f9'>"
            "<h2>✔ Update installed — the panel is restarting…</h2>"
            + ("<pre style='background:#17212b;color:#d9e2ec;padding:14px;"
               "border-radius:8px'>%s</pre>" % extra if extra else "") +
            "<p>This page reloads automatically in a few seconds. "
            "If it doesn't, <a href='/'>click here</a> and hard-refresh (Ctrl+F5).</p>"
            "</body>")


@app.route("/update/local", methods=["POST"])
@login_required
def update_from_local():
    """One click: apply the project files already uploaded to this server
    (via PhpStorm/SFTP/git — however they got there), then restart."""
    folder = request.form.get("repo_dir", "").strip() or get_setting("repo_dir")
    if folder:
        set_setting("repo_dir", folder)
    if not folder or not os.path.isdir(folder):
        flash("Set the project folder first (where you upload the files on "
              "this server).")
        return redirect(url_for("settings"))
    try:
        apply_update_from(folder)
    except Exception as exc:
        flash("Update failed, nothing was restarted: %s" % exc)
        return redirect(url_for("settings"))
    return restart_and_confirm("Applied files from: " + folder)


@app.route("/update", methods=["POST"])
@login_required
def update_panel():
    """Apply an uploaded LinuxWebsitesBackup bundle: replace panel + agent
    files (config/db/keys untouched) and restart the service."""
    f = request.files.get("package")
    if not f or not (f.filename or "").endswith((".tar.gz", ".tgz")):
        flash("Please upload the LinuxWebsitesBackup.tar.gz bundle.")
        return redirect(url_for("settings"))
    tmp = tempfile.mkdtemp(prefix="wbupdate-")
    try:
        pkg = os.path.join(tmp, "pkg.tar.gz")
        f.save(pkg)
        with tarfile.open(pkg) as tar:
            for m in tar.getmembers():
                parts = m.name.split("/")
                if m.name.startswith("/") or ".." in parts:
                    raise ValueError("unsafe path in archive: " + m.name)
                if m.issym() or m.islnk():
                    raise ValueError("links not allowed in archive: " + m.name)
            tar.extractall(tmp)
        # locate the project root (bundle may or may not have a top folder)
        root = None
        for cand in [tmp] + [os.path.join(tmp, d) for d in sorted(os.listdir(tmp))]:
            if os.path.isfile(os.path.join(cand, "dashboard", "app.py")):
                root = cand
                break
        if not root:
            raise ValueError("this file does not contain dashboard/app.py — "
                             "upload the full LinuxWebsitesBackup.tar.gz")
        apply_update_from(root)
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        flash("Update failed, nothing was restarted: %s" % exc)
        return redirect(url_for("settings"))
    shutil.rmtree(tmp, ignore_errors=True)
    return restart_and_confirm()


# ------------------------------------------------------------------- main ---
init_db()

if not os.environ.get("WBD_NO_MONITOR"):
    threading.Thread(target=monitor_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host=CFG["HOST"], port=int(CFG["PORT"]), threaded=True)
