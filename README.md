# LinuxWebsitesBackup

Self-hosted backup system for Linux web servers → **Synology NAS**, with a
central **web dashboard** that controls all your servers from one place.

```
┌────────────┐   SSH    ┌──────────────┐   rsync/SSH   ┌──────────────┐
│  Dashboard │ ───────► │ Web server 1 │ ────────────► │              │
│  (browser  │ ───────► │ Web server 2 │ ────────────► │ Synology NAS │
│   control) │ ───────► │ Web server N │ ────────────► │  WebBackups/ │
└────────────┘          └──────────────┘               └──────────────┘
```

**Supported server setups (auto-detected, no configuration needed):**

| Setup | Websites found in | Extras backed up |
|-------|------------------|------------------|
| Plain nginx / Apache | roots from the web-server config + `/var/www`, `/srv/www`, `/home/*/public_html`, XAMPP `htdocs` | web server + PHP configs, SSL certs, crontabs |
| **HestiaCP** | `/home/<user>/web/<domain>/public_html` (one archive per site) | HestiaCP panel config & user data (`/usr/local/hestia/conf`, `data`, `ssl`) |
| **CloudPanel** | `/home/<site-user>/htdocs/<domain>` (one archive per site) | CloudPanel's own database (`/home/clp/htdocs/app/data`) |

MySQL/MariaDB and PostgreSQL databases are dumped per-database on all setups
(both panels use standard MySQL — on CloudPanel, root credentials from
`/root/.my.cnf` are picked up automatically).

**Two components:**

| Component | Folder | Runs on | What it does |
|-----------|--------|---------|--------------|
| **Agent** (`webbackup`) | `agent/` | Every web server | Auto-detects websites, MySQL/MariaDB + PostgreSQL databases and server configs, archives them, ships snapshots to the NAS, rotates old ones. Works standalone too (CLI + admin menu). |
| **Dashboard** | `dashboard/` | One server you choose | Web panel: link servers with one password prompt (SSH keys created & installed automatically, agent deployed & configured automatically), run backups/restores with a click, change schedules & retention and push them to selected servers, watch live job logs. |

## Quick start

### 0. Synology NAS — one-time (~5 min)

1. Control Panel → **User & Group** → create user `vpsbackup` (not admin)
2. Control Panel → **Shared Folder** → create `WebBackups`, give `vpsbackup` Read/Write
3. Control Panel → **Terminal & SNMP** → enable **SSH**
4. Control Panel → **File Services → rsync** → enable **rsync service**
5. Control Panel → **User & Group → Advanced** → enable **User Home** service
6. Reachability for remote VPSes: **DDNS + router port-forward** to the NAS SSH
   port, or install **Tailscale** on NAS + servers (recommended, no open ports).
   QuickConnect does *not* work for rsync/SSH.
7. Recommended: Control Panel → Security → Protection → enable **Auto Block**

> DSM note: if key login fails for the backup user, SSH to the NAS as admin once and run
> `chmod 755 /var/services/homes/vpsbackup; chmod 700 /var/services/homes/vpsbackup/.ssh; chmod 600 /var/services/homes/vpsbackup/.ssh/authorized_keys`

### 1. Install the dashboard (on your control server)

```sh
git clone https://github.com/YOURNAME/LinuxWebsitesBackup.git
cd LinuxWebsitesBackup/dashboard
sudo bash install.sh
```

The installer sets up Python (if missing), a private virtualenv, the
dashboard's SSH key, your admin login, and a systemd service. Then open
`http://your-server:8800`.

### 2. In the browser

1. **Settings** → enter the NAS address, port, backup user and folder + your
   default schedule/retention → Save
2. **Servers → + Add a server** — two ways:

   **Option A (recommended): one command on the server.** The Add-server page
   shows a ready-made command like

   ```sh
   curl -fsSL http://PANEL:8800/enroll.sh | sudo bash -s -- http://PANEL:8800 <token>
   ```

   Paste it on any web server: it downloads the agent *from your panel*
   (servers don't need git or Python), installs it, registers the server with
   the panel and authorizes the panel's SSH key. The server appears on the
   Servers page as **enrolled** — open it, click **Complete setup**, enter the
   NAS user's password once (not stored), and the panel configures the NAS
   target, creates & authorizes the server's NAS key, sets the schedule and
   runs a live test.

   **Option B: link with a root password.** Enter the server's IP + root
   password in the form (used once, never stored) and the panel does all of
   the above itself, including the agent install.
3. Done — the server backs up on schedule. Repeat step 2 for each server.

### Everyday use

| Where | What you can do |
|-------|-----------------|
| Servers page | See all servers with last-backup status at a glance; select servers → **push schedule/retention** to all of them at once |
| Server page | **Backup now** · **Test NAS connection** · **Refresh status** · change this server's schedule · browse snapshots · **Restore** (downloads a snapshot to the server with instructions — never overwrites) · edit the server's config file directly |
| Jobs | Live output of every backup/restore/link with success/failure |
| Settings | NAS connection + defaults for new servers |

### Using the agent standalone (no dashboard)

```sh
cd LinuxWebsitesBackup/agent
sudo bash install.sh     # runs an interactive setup wizard
webbackup menu           # admin menu on the server itself
```

See `agent/` — the agent is fully usable by itself; the dashboard just drives
the same commands over SSH.

## Snapshot layout on the NAS

```
WebBackups/<server-hostname>/<YYYY-MM-DD_HHMM>/
├── files/       one tar.gz per web root (e.g. var_www.tar.gz)
├── databases/   mysql_<db>.sql.gz, pgsql_<db>.sql.gz
├── configs/     etc-configs.tar.gz, packages.txt, root-crontab.txt
└── manifest.txt
```

Every snapshot is self-contained; retention (e.g. 7 recent + 4 weekly +
6 monthly) is applied automatically after each successful upload.

## Security notes

- The dashboard can run commands on your servers — treat it like a master
  key. Firewall its port to your own IP and/or put it behind an HTTPS
  reverse proxy (nginx + Let's Encrypt).
- Server and NAS passwords are used once during linking and never stored;
  afterwards everything is SSH-key based.
- Restores never overwrite anything automatically.

## Requirements

- **Servers:** any systemd-based Linux with bash (Debian/Ubuntu, RHEL/Alma/
  Rocky; Alpine works for the agent). Root SSH access.
- **Dashboard server:** same — Python 3.8+ (installed automatically).
- **NAS:** Synology DSM 6/7 with SSH + rsync enabled.
