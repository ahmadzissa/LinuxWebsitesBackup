#!/usr/bin/env python3
"""webbackup dashboard — central control panel for webbackup servers.

Runs as a small Flask service. Talks to linked servers over SSH (paramiko)
using its own key, deploys/configures the webbackup agent, triggers backups
and restores, and pushes schedule/retention settings to selected servers.
"""
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

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

app = Flask(__name__)
app.secret_key = CFG["SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # update bundles
# honor X-Forwarded-Proto/Host from the reverse proxy (CloudPanel nginx), so
# generated URLs (enroll command) correctly say https:// and the right host
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DEFAULT_SETTINGS = {
    "syno_host": "", "syno_port": "22", "syno_user": "",
    "syno_path": "/volume1/WebBackups",
    "syno_pass": "",
    "default_cron": "30 3 * * *",
    "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "6",
    "ts_authkey": "",
    "ts_expiry": "",
    "repo_dir": "",
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

STAGING_DIR="/var/backups/webbackup"
BWLIMIT="0"
SCHEDULE_CRON="{default_cron}"
NOTIFY_WEBHOOK=""
NOTIFY_EMAIL=""
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
        status = "no-backup-yet"
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
    if name not in ("webbackup", "install.sh"):
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
        row = c.execute("SELECT id FROM servers WHERE host=?", (ip,)).fetchone()
        if row:
            c.execute("UPDATE servers SET name=?, port=?, status='enrolled' WHERE id=?",
                      (hostname or ip, port, row["id"]))
        else:
            c.execute("INSERT INTO servers(name,host,port,ssh_user,created,status) "
                      "VALUES(?,?,?,?,?,'enrolled')",
                      (hostname or ip, ip, port, "root", now()))
    try:
        return dashboard_pubkey() + "\n"
    except OSError:
        return "panel has no SSH key (run the installer)", 500


def configure_server_work(row, nas_pass):
    """Finish setup for an enrolled server: config, NAS key, schedule, test."""
    server_id = row["id"]

    def work(append):
        append("[1/4] Connecting with the panel's SSH key ...")
        client = connect_server(row)
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
        sshops.sftp_put_file(client, os.path.join(CFG["AGENT_DIR"], "webbackup"),
                             "/tmp/wb-agent/webbackup", 0o755)
        sshops.sftp_put_file(client, os.path.join(CFG["AGENT_DIR"], "install.sh"),
                             "/tmp/wb-agent/install.sh", 0o755)
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
    snapshots, conf, error = [], "", ""
    try:
        client = connect_server(row)
        rc, out = sshops.run(client, "/usr/local/bin/webbackup snapshots", timeout=40)
        if rc == 0:
            snapshots = [s for s in out.split() if re.fullmatch(
                r"\d{4}-\d{2}-\d{2}_\d{4}", s)]
        conf = sshops.read_file(client, "/etc/webbackup/webbackup.conf") or ""
        client.close()
    except Exception as exc:
        error = str(exc)
    return render_template("server.html", s=row, jobs=jobs,
                           snapshots=list(reversed(snapshots)), conf=conf, error=error)


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

    if action == "complete":
        nas_pass = request.form.get("nas_password", "") or get_setting("syno_pass")
        job_id = start_job(server_id, row["name"], "complete setup",
                           configure_server_work(row, nas_pass))
    elif action == "backup":
        job_id = start_job(server_id, row["name"], "backup",
                           make("/usr/local/bin/webbackup run --cron && echo BACKUP-OK"))
    elif action == "test":
        job_id = start_job(server_id, row["name"], "test NAS connection",
                           make("/usr/local/bin/webbackup test", 120, False))
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
        cron = request.form.get("cron", "").strip()
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
        conf_text = request.form.get("conf", "")
        def work(append):
            client = connect_server(row)
            sshops.sftp_put_data(client, conf_text, "/etc/webbackup/webbackup.conf", 0o600)
            append("Configuration saved.")
            client.close()
            return True
        job_id = start_job(server_id, row["name"], "save config", work)
    elif action == "unlink":
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


@app.route("/push", methods=["POST"])
@login_required
def push():
    """Push schedule and/or retention settings to the selected servers."""
    ids = [int(x) for x in request.form.getlist("server_ids")]
    if not ids:
        flash("No servers selected.")
        return redirect(url_for("index"))

    cron = request.form.get("cron", "").strip()
    push_sched = bool(cron) and request.form.get("push_schedule")
    push_ret = request.form.get("push_retention")
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


SECRET_SETTINGS = ("ts_authkey", "syno_pass")


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
                set_setting(k, v)
        flash("Settings saved. They apply to newly linked servers; use the "
              "push panel on the home page to update existing servers.")
        return redirect(url_for("settings"))
    vals = {k: get_setting(k) for k in DEFAULT_SETTINGS}
    # never render secrets back into the page — show a saved-flag instead
    saved = {k: bool(vals[k]) for k in SECRET_SETTINGS}
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
                           ts_note=ts_note, ts_urgent=ts_urgent)


# ---------------------------------------------------------- panel updates ---
def apply_update_from(root):
    """Copy panel + agent files from an extracted/checked-out project root.
    Raises on problems; config/db/keys are never touched."""
    if not os.path.isfile(os.path.join(root, "dashboard", "app.py")):
        raise ValueError("no dashboard/app.py found in " + root)
    app_dir = CFG.get("APP_DIR") or os.path.dirname(os.path.abspath(__file__))
    dsrc = os.path.join(root, "dashboard")
    for name in ("app.py", "sshops.py", "requirements.txt"):
        src = os.path.join(dsrc, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(app_dir, name))
    for dname in ("templates", "static"):
        src = os.path.join(dsrc, dname)
        if os.path.isdir(src):
            dst = os.path.join(app_dir, dname)
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
    asrc = os.path.join(root, "agent")
    if os.path.isdir(asrc):
        os.makedirs(CFG["AGENT_DIR"], exist_ok=True)
        for name in ("webbackup", "install.sh"):
            src = os.path.join(asrc, name)
            if os.path.isfile(src):
                dst = os.path.join(CFG["AGENT_DIR"], name)
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
    pip = os.path.join(app_dir, "venv", "bin", "pip")
    req = os.path.join(app_dir, "requirements.txt")
    if os.path.isfile(pip) and os.path.isfile(req):
        subprocess.run([pip, "install", "-q", "-r", req], timeout=300, check=False)


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

if __name__ == "__main__":
    app.run(host=CFG["HOST"], port=int(CFG["PORT"]), threaded=True)
