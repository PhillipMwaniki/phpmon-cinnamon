#!/usr/bin/env python3
"""Status and service control for the PHP Monitor applet.

Usage:
  helper.py status            print a JSON snapshot of PHP, services and sites
  helper.py restart <unit>    restart one service (start/stop work the same way)
  helper.py repair            restart Valet services that failed (e.g. at boot)
  helper.py ext <ver> <name> on|off   enable/disable a PHP extension (asks for password)
  helper.py xdebug-mode <ver> <mode>  set xdebug.mode for PHP-FPM and CLI (asks for password)

Service control goes through `systemctl`; the bundled polkit rule lets your
user manage just these dev services without a password prompt.
"""
import json
import os
import re
import subprocess
import sys
import time

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

VALET_PHPFPM_SRC = os.path.join(HOME, ".config", "composer", "vendor", "genesisweb",
                                "valet-linux-plus", "cli", "Valet", "PhpFpm.php")
CACHE_DIR = os.path.join(HOME, ".cache", "phpmon-cinnamon")
APT_CACHE_SECONDS = 6 * 3600
# Extensions worth offering to install when the PPA has them
INSTALLABLE = ["xdebug", "pcov", "redis", "imagick", "pgsql", "sqlite3", "memcached", "mongodb",
               "soap", "ldap", "bz2", "gmp", "apcu", "igbinary", "msgpack", "yaml", "swoole",
               "amqp", "xhprof"]
XDEBUG_MODES = ["off", "develop", "debug", "develop,debug", "coverage", "profile", "trace"]
XDEBUG_CONF = "99-phpmon-xdebug.ini"
VERSION_RE = re.compile(r"\d+\.\d+")
EXT_RE = re.compile(r"[a-z0-9_]+")


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
        return {"tld": "test", "paths": [], "php_version": None}
    return {"tld": cfg.get("domain") or cfg.get("tld") or "test", "paths": cfg.get("paths", []),
            "php_version": cfg.get("php_version")}


def php_version():
    rc, out, _ = run(["php", "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION.'|'.PHP_VERSION;"])
    if rc != 0 or "|" not in out:
        return None, None
    short, full = out.split("|", 1)
    return short, full


def fpm_full_version(version):
    rc, out, _ = run([f"php-fpm{version}", "-v"])
    m = re.match(r"PHP (\S+)", out)
    return m.group(1) if rc == 0 and m else None


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
        for host in (h for h in os.listdir(nginx_dir) if not h.startswith(".")):
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


def supported_versions():
    """PHP versions `valet use` accepts, read from the installed Valet."""
    try:
        with open(VALET_PHPFPM_SRC) as f:
            m = re.search(r"SUPPORTED_PHP_VERSIONS\s*=\s*\[([^\]]*)\]", f.read())
        if m:
            return re.findall(r"'(\d+\.\d+)'", m.group(1))
    except OSError:
        pass
    return ["8.2", "8.3", "8.4"]


def enabled_in(version, sapi):
    conf = f"/etc/php/{version}/{sapi}/conf.d"
    if not os.path.isdir(conf):
        return set()
    return {re.sub(r"^\d+-", "", f)[:-4] for f in os.listdir(conf) if f.endswith(".ini")}


def extensions(version):
    mods = f"/etc/php/{version}/mods-available"
    if not os.path.isdir(mods):
        return []
    fpm, cli = enabled_in(version, "fpm"), enabled_in(version, "cli")
    names = sorted(f[:-4] for f in os.listdir(mods) if f.endswith(".ini"))
    return [{"name": n, "fpm": n in fpm, "cli": n in cli} for n in names]


def apt_available(version):
    """Package names the configured repos offer for this PHP version (cached)."""
    path = os.path.join(CACHE_DIR, f"apt-php{version}.json")
    try:
        if time.time() - os.path.getmtime(path) < APT_CACHE_SECONDS:
            with open(path) as f:
                return set(json.load(f))
    except (OSError, ValueError):
        pass
    rc, out, _ = run(["apt-cache", "pkgnames", f"php{version}-"], timeout=20)
    names = sorted(out.split()) if rc == 0 else []
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(names, f)
    return set(names)


def installed_packages(version):
    rc, out, _ = run(["dpkg-query", "-W", "-f", "${Package} ${db:Status-Status}\n", f"php{version}-*"])
    return {line.split()[0] for line in out.splitlines() if line.endswith(" installed")}


def installable(version):
    avail, have = apt_available(version), installed_packages(version)
    return [e for e in INSTALLABLE if f"php{version}-{e}" in avail and f"php{version}-{e}" not in have]


def xdebug_info(version):
    if not os.path.exists(f"/etc/php/{version}/mods-available/xdebug.ini"):
        return {"installed": False, "mode": None}
    rc, out, _ = run([f"php-fpm{version}", "-i"])
    m = re.search(r"^xdebug\.mode => (\S+)", out, re.M)
    return {"installed": True, "mode": m.group(1) if m else None}


def status():
    cfg = valet_config()
    cli_short, cli_full = php_version()
    # Sites use the PHP-FPM version Valet is set to; the `php` command can differ
    short = cfg["php_version"] or cli_short
    full = fpm_full_version(short) if short else None
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
        "php": {"version": short, "full": full or cli_full, "cli_version": cli_short,
                "cli_full": cli_full, "installed": installed_php_versions()},
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
        "supported_versions": supported_versions(),
        "extensions": extensions(short) if short else [],
        "installable": installable(short) if short else [],
        "xdebug": xdebug_info(short) if short else {"installed": False, "mode": None},
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


def check_version(version):
    if not VERSION_RE.fullmatch(version) or not os.path.isdir(f"/etc/php/{version}"):
        raise SystemExit(f"PHP {version!r} isn't installed")


def restart_fpm(version):
    if unit_states([f"php{version}-fpm"]).get(f"php{version}-fpm") == "active":
        control("restart", f"php{version}-fpm")


def set_extension(version, name, on):
    check_version(version)
    if not EXT_RE.fullmatch(name) or not os.path.exists(f"/etc/php/{version}/mods-available/{name}.ini"):
        raise SystemExit(f"extension {name!r} isn't available for PHP {version}")
    tool = "/usr/sbin/phpenmod" if on else "/usr/sbin/phpdismod"
    rc, _, err = run(["pkexec", tool, "-v", version, name], timeout=120)
    if rc != 0:
        raise SystemExit(err or "cancelled")
    restart_fpm(version)


def set_xdebug_mode(version, mode):
    check_version(version)
    if mode not in XDEBUG_MODES:
        raise SystemExit(f"unsupported Xdebug mode {mode!r}")
    targets = [f"/etc/php/{version}/{sapi}/conf.d/{XDEBUG_CONF}" for sapi in ("fpm", "cli")]
    content = f"; Managed by phpmon-cinnamon\nxdebug.mode = {mode}\n"
    try:
        r = subprocess.run(["pkexec", "/usr/bin/tee", *targets], input=content, text=True,
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(str(e))
    if r.returncode != 0:
        raise SystemExit(r.stderr.strip() or "cancelled")
    restart_fpm(version)


def main(argv):
    if len(argv) >= 2 and argv[1] == "status":
        print(json.dumps(status()))
    elif len(argv) == 3 and argv[1] in ALLOWED_VERBS:
        control(argv[1], argv[2])
    elif len(argv) == 2 and argv[1] == "repair":
        repair()
    elif len(argv) == 5 and argv[1] == "ext" and argv[4] in ("on", "off"):
        set_extension(argv[2], argv[3], argv[4] == "on")
    elif len(argv) == 4 and argv[1] == "xdebug-mode":
        set_xdebug_mode(argv[2], argv[3])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
