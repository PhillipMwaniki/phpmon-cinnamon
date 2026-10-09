#!/bin/sh
# Lets one user start/stop/restart the local dev services managed by the
# PHP Monitor applet without a password prompt. Nothing else is allowed.
#
#   pkexec sh polkit/install.sh      (or: sudo sh polkit/install.sh)
#   pkexec sh polkit/install.sh --uninstall
set -eu

RULE=/etc/polkit-1/rules.d/50-phpmon-cinnamon.rules

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root: pkexec sh $0" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$RULE"
    echo "Removed $RULE"
    exit 0
fi

# The user who ran pkexec/sudo, not root
if [ -n "${PKEXEC_UID:-}" ]; then
    TARGET_USER=$(id -nu "$PKEXEC_UID")
elif [ -n "${SUDO_USER:-}" ]; then
    TARGET_USER=$SUDO_USER
else
    echo "Could not tell which user to allow; run via pkexec or sudo." >&2
    exit 1
fi

cat > "$RULE" <<EOF
// Installed by phpmon-cinnamon for user "$TARGET_USER".
// Allows start/stop/restart of local dev services without a password.
polkit.addRule(function (action, subject) {
    if (action.id !== "org.freedesktop.systemd1.manage-units" || subject.user !== "$TARGET_USER") {
        return polkit.Result.NOT_HANDLED;
    }
    var unit = action.lookup("unit") || "";
    var verb = action.lookup("verb") || "";
    var units = ["nginx.service", "dnsmasq.service", "mysql.service", "mariadb.service",
                 "postgresql.service", "redis-server.service", "memcached.service",
                 "mailpit.service", "meilisearch.service"];
    var unitOk = units.indexOf(unit) !== -1 || /^php[0-9]+\.[0-9]+-fpm\.service$/.test(unit);
    var verbOk = ["start", "stop", "restart", "reset-failed"].indexOf(verb) !== -1;
    return unitOk && verbOk ? polkit.Result.YES : polkit.Result.NOT_HANDLED;
});
EOF
chmod 644 "$RULE"
echo "Installed $RULE for user $TARGET_USER"
