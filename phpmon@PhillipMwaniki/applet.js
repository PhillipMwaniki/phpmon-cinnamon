const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const Main = imports.ui.main;
const Util = imports.misc.util;
const Mainloop = imports.mainloop;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const St = imports.gi.St;

const ICON = "application-x-php";
const BAD_STYLE = "color: #fd4663;";
const DOT_COLOR = { active: "#4cd07d", failed: "#fd4663" };
const STATE_TEXT = {
    active: "running", inactive: "stopped", failed: "failed",
    activating: "starting", deactivating: "stopping", reloading: "reloading",
};

class PhpMonitorApplet extends Applet.TextIconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this.metadata = metadata;
        this.helper = GLib.build_filenamev([metadata.path, "helper.py"]);
        this.status = null;
        this.busy = false;
        this.timerId = 0;
        this.repairedOnce = false;

        this.settings = new Settings.AppletSettings(this, metadata.uuid, instanceId);
        this.settings.bind("refresh-interval", "refreshInterval", () => this._schedule());
        this.settings.bind("auto-repair", "autoRepair");
        this.settings.bind("editor-command", "editorCommand");

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);
        this.menu.connect("open-state-changed", (m, open) => {
            if (open) this._refresh();
        });

        this.set_applet_icon_name(ICON);
        this.set_applet_label("PHP");
        this.set_applet_tooltip("PHP Monitor: loading…");
        this._buildMenu();
        this._refresh();
        this._schedule();
    }

    // ---- helper process ----------------------------------------------------

    _runHelper(args, callback) {
        let proc;
        try {
            proc = Gio.Subprocess.new(["python3", this.helper, ...args],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            callback(false, "", e.message);
            return;
        }
        proc.communicate_utf8_async(null, null, (p, res) => {
            let out = "", err = "";
            try {
                [, out, err] = p.communicate_utf8_finish(res);
            } catch (e) {
                err = e.message;
            }
            callback(p.get_successful(), out || "", (err || "").trim());
        });
    }

    _refresh() {
        if (this.busy) return;
        this.busy = true;
        this._runHelper(["status"], (ok, out, err) => {
            this.busy = false;
            if (!ok) {
                global.logError("phpmon: status failed: " + err);
                return;
            }
            try {
                this.status = JSON.parse(out);
            } catch (e) {
                global.logError("phpmon: bad status JSON: " + e.message);
                return;
            }
            this._updatePanel();
            this._buildMenu();
            this._maybeAutoRepair();
        });
    }

    _schedule() {
        if (this.timerId) Mainloop.source_remove(this.timerId);
        let secs = Math.max(5, this.refreshInterval || 15);
        this.timerId = Mainloop.timeout_add_seconds(secs, () => {
            if (!this.menu.isOpen) this._refresh();
            return true;
        });
    }

    _control(verb, svc) {
        this._runHelper([verb, svc.unit], (ok, out, err) => {
            if (!ok) Main.notify("PHP Monitor", `Couldn't ${verb} ${svc.label}: ${err || "unknown error"}`);
            this._refresh();
        });
    }

    _repair(automatic) {
        this._runHelper(["repair"], (ok, out, err) => {
            if (!ok) {
                Main.notify("PHP Monitor", "Repair failed: " + (err || "unknown error"));
            } else {
                let fixed = [];
                try {
                    fixed = JSON.parse(out).repaired || [];
                } catch (e) { /* nothing restarted */ }
                if (fixed.length)
                    Main.notify("PHP Monitor", (automatic ? "Valet services were down after login. " : "") +
                        "Restarted: " + fixed.join(", "));
            }
            this._refresh();
        });
    }

    // Home is only mounted after login, so services that start at boot can fail.
    _maybeAutoRepair() {
        if (this.repairedOnce || !this.autoRepair || !this.status) return;
        this.repairedOnce = true;
        if (!this.status.valet_ok) this._repair(true);
    }

    // ---- panel -------------------------------------------------------------

    _updatePanel() {
        let s = this.status;
        let version = s.php.version || "?";
        this.set_applet_label(version);
        let down = s.services.filter(x => x.valet && x.state !== "active").map(x => x.label);
        if (down.length) {
            this._applet_label.set_style(BAD_STYLE);
            this.set_applet_tooltip(`PHP ${s.php.full || version}: not running: ${down.join(", ")}`);
        } else {
            this._applet_label.set_style(null);
            this.set_applet_tooltip(`PHP ${s.php.full || version}: all Valet services running`);
        }
    }

    on_applet_clicked() {
        this.menu.toggle();
    }

    // ---- menu --------------------------------------------------------------

    _heading(text) {
        let item = new PopupMenu.PopupMenuItem(text, { reactive: false });
        item.label.set_style("font-weight: bold; opacity: 0.75;");
        return item;
    }

    _info(text) {
        let item = new PopupMenu.PopupMenuItem(text, { reactive: false });
        item.label.set_style("opacity: 0.8;");
        return item;
    }

    _action(menu, text, icon, cb) {
        let item = icon ? new PopupMenu.PopupIconMenuItem(text, icon, 1) : new PopupMenu.PopupMenuItem(text);
        item.connect("activate", cb);
        menu.addMenuItem(item);
        return item;
    }

    _open(target) {
        if (target) Util.spawn(["xdg-open", target]);
    }

    _openInEditor(path) {
        let cmd = (this.editorCommand || "code").trim().split(/\s+/);
        try {
            Util.spawn([...cmd, path]);
        } catch (e) {
            this._open(path);
        }
    }

    _buildMenu() {
        let m = this.menu;
        m.removeAll();
        let s = this.status;
        if (!s) {
            m.addMenuItem(this._info("Loading…"));
            return;
        }

        // PHP + overall health
        m.addMenuItem(this._heading(`PHP ${s.php.full || "not found"}`));
        if (s.valet_ok) {
            m.addMenuItem(this._info("✓ All Valet services are running"));
        } else {
            let down = s.services.filter(x => x.valet && x.state !== "active").map(x => x.label);
            m.addMenuItem(this._info("⚠ Not running: " + down.join(", ")));
            this._action(m, "Repair Valet services", "emblem-synchronizing-symbolic", () => this._repair(false));
        }

        // Services: click to restart (or start when stopped)
        m.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        m.addMenuItem(this._heading("Services"));
        for (let svc of s.services) {
            let running = svc.state === "active";
            let item = new PopupMenu.PopupMenuItem(svc.label);
            let color = DOT_COLOR[svc.state] || "#9a9ab0";
            let state = new St.Label({ text: "● " + (STATE_TEXT[svc.state] || svc.state), style: `color: ${color};` });
            item.addActor(state, { align: St.Align.END });
            item.connect("activate", () => this._control(running ? "restart" : "start", svc));
            m.addMenuItem(item);
        }
        this._action(m, "Restart Valet services", "view-refresh-symbolic", () => {
            for (let svc of s.services.filter(x => x.valet)) this._control("restart", svc);
        });

        // Sites
        m.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        let featured = s.sites.filter(x => x.secure || x.proxy || x.linked);
        m.addMenuItem(this._heading(`Sites (.${s.tld})`));
        for (let site of featured) {
            let tags = [site.secure ? "🔒" : "", site.proxy ? "proxy" : "", site.linked ? "linked" : ""]
                .filter(Boolean).join(" ");
            this._action(m, `${site.host}  ${tags}`, null, () => this._open(site.url));
        }
        let browse = new PopupMenu.PopupSubMenuMenuItem(`Open site in browser (${s.sites.length})`);
        for (let site of s.sites)
            this._action(browse.menu, site.host + (site.secure ? "  🔒" : ""), null, () => this._open(site.url));
        m.addMenuItem(browse);
        let edit = new PopupMenu.PopupSubMenuMenuItem("Open project in editor");
        for (let site of s.sites.filter(x => x.path))
            this._action(edit.menu, site.name, null, () => this._openInEditor(site.path));
        m.addMenuItem(edit);

        // PHP limits (as seen by PHP-FPM, i.e. your sites)
        m.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        m.addMenuItem(this._heading("Limits (PHP-FPM)"));
        let l = s.limits;
        if (l.memory_limit) {
            m.addMenuItem(this._info(`Memory ${l.memory_limit}   ·   Upload ${l.upload_max_filesize}   ·   POST ${l.post_max_size}`));
            m.addMenuItem(this._info(`Max execution time ${l.max_execution_time === "0" ? "unlimited" : l.max_execution_time + "s"}`));
        } else {
            m.addMenuItem(this._info("Unavailable"));
        }

        // Config files and logs
        let cfg = new PopupMenu.PopupSubMenuMenuItem("Config & logs");
        let p = s.paths;
        if (p.fpm_ini) this._action(cfg.menu, "php.ini (PHP-FPM, used by sites)", "text-x-generic-symbolic", () => this._open(p.fpm_ini));
        if (p.cli_ini) this._action(cfg.menu, "php.ini (command line)", "text-x-generic-symbolic", () => this._open(p.cli_ini));
        this._action(cfg.menu, "Nginx error log", "text-x-generic-symbolic", () => this._open(p.nginx_error_log));
        this._action(cfg.menu, "Valet logs folder", "folder-symbolic", () => this._open(p.valet_logs));
        this._action(cfg.menu, "Valet Nginx configs", "folder-symbolic", () => this._open(p.valet_nginx));
        this._action(cfg.menu, "Valet folder", "folder-symbolic", () => this._open(p.valet_dir));
        m.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        m.addMenuItem(cfg);
        this._action(m, "Refresh", "view-refresh-symbolic", () => this._refresh());
    }

    on_applet_removed_from_panel() {
        if (this.timerId) Mainloop.source_remove(this.timerId);
        this.settings.finalize();
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new PhpMonitorApplet(metadata, orientation, panelHeight, instanceId);
}
