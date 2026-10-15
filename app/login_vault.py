"""Saved logins for employer job sites, for accounts that already exist.

The owner types them on the local board, never in a chat. The password stays in this Mac's Keychain,
and only an index of site and username sits on disk. It goes out in one place: to the paired
extension, for the exact site it was saved under. No model, log or event ever sees it.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import config as cfgmod

_root = cfgmod.ROOT / "data" / "agent_lab"
_lock = threading.RLock()
_SECURITY = "/usr/bin/security"
# Job-site accounts only, on sites the extension runs on. A bank, a mailbox or a single sign-on
# account such as Google or Apple can never be saved here.
JOB_SITES = ("myworkdayjobs.com", "smartrecruiters.com", "icims.com", "avature.net",
             "successfactors.com", "sapsf.com", "dayforcehcm.com", "amazon.jobs", "jobs.siemens.com")


def _name(cfg: dict) -> str:
    return re.sub(r"\W+", "_", cfgmod.workspace_name(cfg)).strip("_").lower()


def _index(cfg: dict) -> Path:
    return Path(_root) / f"logins.{_name(cfg)}.json"


def _service(cfg: dict) -> str:
    return f"jobscan-login.{_name(cfg)}"


def site_host(site: str) -> str:
    """The exact host a login belongs to. Refuses anything that is not an https job site."""
    site = (site or "").strip()
    parsed = urlparse(site if "://" in site else "https://" + site)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("网址要写成 https 开头的招聘网站地址。")
    if not any(host == name or host.endswith("." + name) for name in JOB_SITES):
        raise ValueError("这里只存招聘网站的账号：" + "、".join(JOB_SITES) + "。")
    return host


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _keychain_write(service: str, account: str, password: str) -> None:
    # Through stdin, because a command-line argument is visible to every process on the Mac.
    command = f"add-generic-password -U -s {_quote(service)} -a {_quote(account)} -w {_quote(password)}\n"
    done = subprocess.run([_SECURITY, "-i"], input=command, text=True, capture_output=True, timeout=20)
    if done.returncode != 0 or "error" in done.stderr.lower():
        raise RuntimeError("钥匙串保存失败。")   # The tool's own text could echo what it was given.


def _keychain_read(service: str, account: str) -> str | None:
    done = subprocess.run([_SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"],
                          capture_output=True, text=True, timeout=20)
    return done.stdout.rstrip("\n") if done.returncode == 0 else None


def _keychain_delete(service: str, account: str) -> None:
    subprocess.run([_SECURITY, "delete-generic-password", "-s", service, "-a", account], capture_output=True, timeout=20)


def _read_index(cfg: dict) -> dict:
    try:
        return json.loads(_index(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_index(cfg: dict, data: dict) -> None:
    path = _index(cfg)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def listing(cfg: dict) -> list[dict]:
    return [{"host": host, "username": item["username"], "saved_at": item.get("saved_at")}
            for host, item in sorted(_read_index(cfg).items())]


def save(cfg: dict, site: str, username: str, password: str) -> str:
    host, username = site_host(site), (username or "").strip()
    if not username or not password:
        raise ValueError("账号和密码都要填。")
    with _lock:
        _keychain_write(_service(cfg), host, password)
        data = _read_index(cfg)
        data[host] = {"username": username, "saved_at": time.time()}
        _write_index(cfg, data)
    return host


def remove(cfg: dict, host: str) -> None:
    with _lock:
        data = _read_index(cfg)
        if host not in data:
            raise KeyError(host)
        _keychain_delete(_service(cfg), host)
        del data[host]
        _write_index(cfg, data)


def username_for(cfg: dict, host: str) -> str | None:
    return (_read_index(cfg).get(host) or {}).get("username")


def fill_for(cfg: dict, host: str) -> dict | None:
    """Only the exact host it was saved under gets it: a look-alike site has a different name."""
    username = username_for(cfg, host)
    if not username:
        return None
    password = _keychain_read(_service(cfg), host)
    return {"username": username, "password": password} if password else None
