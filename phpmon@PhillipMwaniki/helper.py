#!/usr/bin/env python3
"""Status and service control for the PHP Monitor applet.

Usage:
  helper.py status            print a JSON snapshot of PHP, services and sites
  helper.py restart <unit>    restart one service (start/stop work the same way)
  helper.py repair            restart Valet services that failed (e.g. at boot)

Service control goes through `systemctl`; the bundled polkit rule lets your
user manage just these dev services without a password prompt.
"""
import json
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
VALET_DIR = os.path.join(HOME, ".config", "valet")
VALET_CONFIG = os.path.join(VALET_DIR, "config.json")

# Order matters for repair: PHP-FPM must be up before nginx proxies to it
VALET_UNITS = ["php{v}-fpm", "nginx", "dnsmasq"]
EXTRA_UNITS = ["mysql", "mariadb", "postgresql", "redis-server", "memcached", "mailpit", "meilisearch"]
LABELS = {
    "nginx": "Nginx", "dnsmasq": "DNSMasq", "mysql": "MySQL", "mariadb": "MariaDB",
    "postgresql": "PostgreSQL", "redis-server": "Redis", "memcached": "Memcached",
    "mailpit": "Mailpit", "meilisearch": "Meilisearch",
}
LIMIT_KEYS = ["memory_limit", "upload_max_filesize", "post_max_size", "max_execution_time"]
ALLOWED_VERBS = {"start", "stop", "restart"}


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def valet_config():
    """Only the fields we need; never pass on credentials stored in the file."""
    try:
        with open(VALET_CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {"tld": "test", "paths": []}
    return {"tld": cfg.get("domain") or cfg.get("tld") or "test", "paths": cfg.get("paths", [])}


def php_version():
    rc, out, _ = run(["php", "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION.'|'.PHP_VERSION;"])
    if rc != 0 or "|" not in out:
        return None, None
    short, full = out.split("|", 1)
    return short, full


def installed_php_versions():
    found = []
    for name in sorted(os.listdir("/usr/bin")):
        m = re.fullmatch(r"php(\d+\.\d+)", name)
        if m:
            found.append(m.group(1))
    return found


def existing_units(names):
    rc, out, _ = run(["systemctl", "list-unit-files", "--no-pager", "--plain", "--no-legend",
                      *[n + ".service" for n in names]])
    present = {line.split()[0][:-len(".service")] for line in out.splitlines() if line.strip()}
    return [n for n in names if n in present]


def unit_states(names):
    if not names:
        return {}
    rc, out, _ = run(["systemctl", "show", "--no-pager", "--property=Id,ActiveState,SubState",
                      *[n + ".service" for n in names]])
    states, cur = {}, {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if "Id" in cur:
                states[cur["Id"][:-len(".service")]] = cur.get("ActiveState", "unknown")
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    return states


def fpm_limits(version):
    rc, out, _ = run([f"php-fpm{version}", "-i"])
    limits, ini = {}, None
    for line in out.splitlines():
        k, _, rest = line.partition(" => ")
        if k in LIMIT_KEYS:
            limits[k] = rest.split(" => ")[0]
        elif k == "Loaded Configuration File":
            ini = rest
    return limits, ini


def sites(cfg):
    tld = cfg["tld"]
    certs = os.path.join(VALET_DIR, "Certificates")
    secured = set()
    if os.path.isdir(certs):
        secured = {f[:-4] for f in os.listdir(certs) if f.endswith(".crt")}

    found = {}
    for parked in cfg["paths"]:
        if not os.path.isdir(parked):
            continue
        for d in os.listdir(parked):
            path = os.path.join(parked, d)
            if not d.startswith(".") and os.path.isdir(path):
                found[d] = {"path": path, "linked": False}
    links = os.path.join(VALET_DIR, "Sites")
    if os.path.isdir(links):
        for d in os.listdir(links):
            found[d] = {"path": os.path.realpath(os.path.join(links, d)), "linked": True}

    # Valet proxies (`valet proxy`) only exist as nginx configs, with no folder
    nginx_dir = os.path.join(VALET_DIR, "Nginx")
    if os.path.isdir(nginx_dir):
        for host in os.listdir(nginx_dir):
            name = host[:-len(tld) - 1] if host.endswith("." + tld) else host
            if name not in found:
                found[name] = {"path": None, "linked": False, "proxy": True}

    out = []
    for name, info in found.items():
        host = f"{name}.{tld}"
        out.append({
            "name": name,
            "host": host,
            "path": info["path"],
            "linked": info["linked"],
            "proxy": info.get("proxy", False),
            "secure": host in secured,
            "url": ("https://" if host in secured else "http://") + host,
        })
    out.sort(key=lambda s: s["name"].lower())
    return out


def status():
    cfg = valet_config()
    short, full = php_version()
    valet_units = [u.format(v=short) for u in VALET_UNITS] if short else ["nginx", "dnsmasq"]
    valet_units = existing_units(valet_units)
    extra_units = existing_units(EXTRA_UNITS)
    states = unit_states(valet_units + extra_units)
    limits, fpm_ini = fpm_limits(short) if short else ({}, None)

    def svc(name, valet):
        label = LABELS.get(name) or (f"PHP {short} FPM" if name.endswith("-fpm") else name)
        return {"unit": name, "label": label, "state": states.get(name, "unknown"), "valet": valet}

    services = [svc(n, True) for n in valet_units] + [svc(n, False) for n in extra_units]
    return {
        "php": {"version": short, "full": full, "installed": installed_php_versions()},
        "services": services,
        "valet_ok": all(s["state"] == "active" for s in services if s["valet"]),
        "limits": limits,
        "paths": {
            "fpm_ini": fpm_ini,
            "cli_ini": f"/etc/php/{short}/cli/php.ini" if short else None,
            "valet_dir": VALET_DIR,
            "valet_nginx": os.path.join(VALET_DIR, "Nginx"),
            "valet_logs": os.path.join(VALET_DIR, "Log"),
            "nginx_error_log": os.path.join(VALET_DIR, "Log", "nginx-error.log"),
        },
        "tld": cfg["tld"],
        "sites": sites(cfg),
    }


def control(verb, unit):
    if verb not in ALLOWED_VERBS:
        raise SystemExit(f"unsupported action {verb!r}")
    allowed = set(EXTRA_UNITS) | {"nginx", "dnsmasq"}
    if unit not in allowed and not re.fullmatch(r"php\d+\.\d+-fpm", unit):
        raise SystemExit(f"{unit!r} is not a service this applet manages")
    rc, _, err = run(["systemctl", verb, unit + ".service"], timeout=60)
    if rc != 0:
        raise SystemExit(err or f"systemctl {verb} {unit} failed")


def repair():
    """Restart Valet services that aren't running, in dependency order."""
    snap = status()
    fixed = []
    for s in snap["services"]:
        if s["valet"] and s["state"] != "active":
            run(["systemctl", "reset-failed", s["unit"] + ".service"])
            control("restart", s["unit"])
            fixed.append(s["unit"])
    print(json.dumps({"repaired": fixed}))


def main(argv):
    if len(argv) >= 2 and argv[1] == "status":
        print(json.dumps(status()))
    elif len(argv) == 3 and argv[1] in ALLOWED_VERBS:
        control(argv[1], argv[2])
    elif len(argv) == 2 and argv[1] == "repair":
        repair()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
