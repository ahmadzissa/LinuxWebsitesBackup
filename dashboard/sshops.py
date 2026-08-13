"""SSH operations for the webbackup dashboard (paramiko)."""
import os
import re
import socket

import paramiko

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean(text: str) -> str:
    """Strip ANSI color codes from remote command output."""
    return ANSI_RE.sub("", text or "")


def connect(host, port=22, user="root", password=None, keyfile=None, timeout=15):
    """Open an SSH connection using either a password or a private key."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=host, port=int(port), username=user,
        timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
        allow_agent=False, look_for_keys=False,
    )
    if password is not None:
        kwargs["password"] = password
    if keyfile:
        kwargs["key_filename"] = keyfile
    client.connect(**kwargs)
    return client


def run(client, cmd, timeout=3600, append=None):
    """Run a command; return (exit_code, combined_output). Optionally stream
    output lines into append() as they arrive."""
    transport = client.get_transport()
    chan = transport.open_session()
    chan.settimeout(timeout)
    chan.set_combine_stderr(True)
    chan.exec_command(cmd)
    chunks = []
    try:
        while True:
            data = chan.recv(4096)
            if not data:
                break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            if append:
                append(clean(text), raw=True)
    except socket.timeout:
        chunks.append("\n[timeout after %ss]\n" % timeout)
    rc = chan.recv_exit_status()
    chan.close()
    return rc, clean("".join(chunks))


def sftp_put_data(client, data, remote_path, mode=0o644):
    """Write bytes/str to a remote file via SFTP."""
    if isinstance(data, str):
        data = data.encode()
    sftp = client.open_sftp()
    try:
        with sftp.file(remote_path, "wb") as f:
            f.write(data)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def sftp_put_file(client, local_path, remote_path, mode=0o644):
    sftp = client.open_sftp()
    try:
        sftp.put(local_path, remote_path)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def read_file(client, remote_path):
    rc, out = run(client, "cat %s 2>/dev/null" % shq(remote_path), timeout=30)
    return out if rc == 0 else None


def shq(s):
    """Shell-quote a string."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def install_authorized_key(client, pubkey):
    """Idempotently add a public key to the connected user's authorized_keys."""
    pub = pubkey.strip()
    cmd = (
        "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        "grep -qxF {k} ~/.ssh/authorized_keys || echo {k} >> ~/.ssh/authorized_keys; "
        "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; chmod 755 \"$HOME\" 2>/dev/null; true"
    ).format(k=shq(pub))
    return run(client, cmd, timeout=30)


def ensure_server_keypair(client, key_path="/root/.ssh/webbackup_ed25519"):
    """Make sure the server has its own NAS key; return its public key."""
    cmd = (
        "mkdir -p $(dirname {p}) && "
        "[ -f {p} ] || ssh-keygen -t ed25519 -N '' -C webbackup@$(hostname) -f {p} -q; "
        "cat {p}.pub"
    ).format(p=shq(key_path))
    rc, out = run(client, cmd, timeout=30)
    if rc != 0 or "ssh-" not in out:
        raise RuntimeError("Could not create/read server SSH key: " + out)
    for line in out.splitlines():
        if line.startswith(("ssh-", "ecdsa-")):
            return line.strip()
    raise RuntimeError("No public key found in output: " + out)
