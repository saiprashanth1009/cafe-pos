"""CafePOS.exe control window: runs the POS + SQLite database, keeps Tailscale connected,
publishes the public link with Tailscale Funnel, and shows a live log.
Dev run: python gui.py      Exe build: see .github/workflows/build-exe.yml"""
import json, logging, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request, webbrowser
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import messagebox, ttk

import app
import gdrive
import updater

PORT = app.PORT
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # don't flash a console for every tailscale call
TS_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale"
log = logging.getLogger("pos")
COLORS = {"green": "#17794f", "amber": "#c98a00", "red": "#b3122b", "grey": "#9a958c"}


# ---------------- Tailscale helpers ----------------
def ts_exe():
    p = TS_DIR / "tailscale.exe"
    return str(p) if p.exists() else shutil.which("tailscale")


def ts(*args, timeout=30):
    r = subprocess.run([ts_exe(), *args], capture_output=True, text=True, timeout=timeout,
                       creationflags=NOWIN, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def running(image):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], capture_output=True, text=True,
                         creationflags=NOWIN).stdout
    return image.lower() in out.lower()


class Window:
    def __init__(self, root):
        self.root, self.q, self.server = root, queue.Queue(), None
        self.closing, self.opened_urls, self.helper = False, set(), None  # helper: running tailscale login/funnel
        self.dns_fixed = False
        self.update_info, self.installing, self.waiting_logged = None, False, False
        self.auto_on = updater.load_prefs()["auto"] and updater.can_self_update()
        root.title("Guns & Roses Café POS")
        root.geometry("940x660")
        root.minsize(680, 440)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(root, padding=(14, 12, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Guns & Roses Café POS", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(top, text="Keep this window open while the café is running.", foreground=COLORS["grey"]).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        top.columnconfigure(2, weight=1)

        self.rows = {}
        specs = [("pos", "POS server", [("Open POS", lambda: self.open_url(f"http://localhost:{PORT}"))]),
                 ("pub", "Public link", [("Open", lambda: self.open_url(self.rows["pub"]["var"].get())),
                                         ("Copy", lambda: self.copy("pub"))]),
                 ("lan", "Same Wi-Fi", [("Copy", lambda: self.copy("lan"))]),
                 ("ts", "Tailscale", [("Sign in", None)]),  # action button, shown only when Tailscale needs you
                 ("db", "Database", [("Open folder", lambda: os.startfile(app.DB.parent))]),
                 ("gd", "Google Drive", [("Backup now", self.gdrive_now), ("Setup…", self.gdrive_setup)]),
                 ("upd", "Updates", [("Check now", self.update_button)])]
        for i, (key, title, buttons) in enumerate(specs, start=2):
            dot = tk.Label(top, text="●", fg=COLORS["grey"], font=("Segoe UI", 13))
            dot.grid(row=i, column=0, padx=(0, 6))
            ttk.Label(top, text=title, width=12).grid(row=i, column=1, sticky="w")
            var = tk.StringVar(value="…")
            ttk.Entry(top, textvariable=var, state="readonly").grid(row=i, column=2, sticky="ew", padx=6, pady=3)
            btns = []
            for j, (label, cmd) in enumerate(buttons):
                b = ttk.Button(top, text=label, command=cmd)
                b.grid(row=i, column=3 + j, padx=2)
                btns.append(b)
            self.rows[key] = {"dot": dot, "var": var, "btns": btns, "row": i}
        self.rows["ts"]["btns"][0].grid_remove()
        self.auto_var = tk.BooleanVar(value=self.auto_on)
        auto = ttk.Checkbutton(top, text="Auto-update", variable=self.auto_var, command=self.toggle_auto)
        auto.grid(row=self.rows["upd"]["row"], column=4, padx=2, sticky="w")
        self.start_var = tk.BooleanVar(value=updater.can_self_update() and updater.autostart_enabled())
        start = ttk.Checkbutton(top, text="Start with Windows", variable=self.start_var, command=self.toggle_autostart)
        start.grid(row=self.rows["upd"]["row"] + 1, column=2, sticky="w", padx=6, pady=(2, 0))
        if not updater.can_self_update():
            start.state(["disabled"])
        mine = updater.current()
        if not updater.can_self_update():
            auto.state(["disabled"])
            self.rows["upd"]["var"].set("Running from source (python gui.py) - update with: git pull")
        else:
            self.rows["upd"]["var"].set(f"Build {mine['build']} ({mine['sha']}) - checking for updates…")

        ttk.Label(root, text="Live log", font=("Segoe UI", 10, "bold"), padding=(14, 6, 0, 2)).pack(anchor="w")
        box = ttk.Frame(root, padding=(14, 0, 14, 14))
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, font=("Consolas", 10), wrap="word", state="disabled", bg="#1c1a17", fg="#e8e4dc",
                            insertbackground="#e8e4dc", relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("warn", foreground="#f2b84b")
        self.text.tag_configure("err", foreground="#ff6b6b")
        root.after(150, self.pump)

    # ---- thread-safe UI updates (tkinter may only be touched from the main thread) ----
    def set(self, key, color, text):
        self.q.put(("set", key, color, text))

    def need_action(self, label, url):
        """Show the Tailscale button (Sign in / Enable Funnel) and open the page once."""
        self.q.put(("action", label, url))
        if url not in self.opened_urls:
            self.opened_urls.add(url)
            webbrowser.open(url)

    def pump(self):
        while not self.q.empty():
            msg = self.q.get()
            if msg[0] == "log":
                _, level, line = msg
                self.text.configure(state="normal")
                self.text.insert("end", line + "\n", "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else "")
                if int(self.text.index("end-1c").split(".")[0]) > 3000:  # keep the window light; full log is on disk
                    self.text.delete("1.0", "500.0")
                self.text.configure(state="disabled")
                self.text.see("end")
            elif msg[0] == "set":
                _, key, color, text = msg
                self.rows[key]["dot"].configure(fg=COLORS[color])
                self.rows[key]["var"].set(text)
            elif msg[0] == "action":
                _, label, url = msg
                b = self.rows["ts"]["btns"][0]
                if url:
                    b.configure(text=label, command=lambda u=url: webbrowser.open(u))
                    b.grid()
                else:
                    b.grid_remove()
            elif msg[0] == "call":
                msg[1]()
            elif msg[0] == "updbtn":
                self.rows["upd"]["btns"][0].configure(text=msg[1])
            elif msg[0] == "restart":
                self.restart_into_update()
                return
        self.root.after(150, self.pump)

    def open_url(self, url):
        if url.startswith("http"):
            webbrowser.open(url)

    def copy(self, key):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.rows[key]["var"].get())
        log.info("Copied: %s", self.rows[key]["var"].get())

    # ---------------- POS server ----------------
    def start_server(self):
        # right after an update the previous copy may need a moment to let go of the port
        tries = 15 if os.environ.get("CAFEPOS_UPDATED_FROM") else 1
        for attempt in range(tries):
            try:
                self.server = app.make_server(PORT)
                break
            except OSError as e:
                err = e
                if attempt + 1 < tries:
                    time.sleep(1)
        else:
            self.set("pos", "red", f"Port {PORT} is busy - is the POS already open in another window?")
            log.error("Could not start the POS on port %s: %s", PORT, err)
            messagebox.showerror("POS already running?",
                                 f"Port {PORT} is already in use.\nClose the other POS window (or the old black window) and open this again.")
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.set("pos", "green", f"Running - http://localhost:{PORT}")
        self.set("db", "green", str(app.DB))
        ips = [ip for ip in app.lan_ips() if not ip.startswith("100.")]  # 100.x is Tailscale's own address
        self.set("lan", "green", "   ".join(f"http://{ip}:{PORT}" for ip in ips))
        log.info("POS started on port %s. Database: %s", PORT, app.DB)
        return True

    def pos_alive(self):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5).status == 200
        except OSError:
            return False

    # ---------------- Tailscale: install, connect, publish ----------------
    def run_helper(self, args, label):
        """Run a tailscale command that may wait for the user (login / funnel); stream its output to the log."""
        if self.helper and self.helper.poll() is None:
            return  # still waiting for the user on the previous one
        p = subprocess.Popen([ts_exe(), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             creationflags=NOWIN, encoding="utf-8", errors="replace")
        self.helper = p

        def read():
            for line in p.stdout:
                if line.strip():
                    log.info("Tailscale: %s", line.strip())
                    m = re.search(r"https://login\.tailscale\.com/\S+", line)
                    if m:
                        self.need_action(label, m.group(0))
        threading.Thread(target=read, daemon=True).start()

    def ensure_tailscale(self):
        if not ts_exe():
            self.set("ts", "amber", "Installing Tailscale (approve the Windows prompt)…")
            log.info("Tailscale not found - installing it with winget (one time, needs internet)")
            winget = shutil.which("winget")
            if not winget:
                self.set("ts", "red", "Install Tailscale from tailscale.com/download, then reopen this app")
                self.need_action("Download Tailscale", "https://tailscale.com/download/windows")
                return
            r = subprocess.run([winget, "install", "-e", "--id", "Tailscale.Tailscale", "--accept-package-agreements",
                                "--accept-source-agreements"], capture_output=True, text=True, creationflags=NOWIN,
                               encoding="utf-8", errors="replace", timeout=900)
            log.info("winget: %s", (r.stdout.strip().splitlines() or ["(no output)"])[-1])
            if not ts_exe():
                self.set("ts", "red", "Tailscale install failed - install it from tailscale.com/download")
                self.need_action("Download Tailscale", "https://tailscale.com/download/windows")
                return

        # On Windows the Tailscale engine only connects while its tray app is running.
        if not running("tailscale-ipn.exe") and (TS_DIR / "tailscale-ipn.exe").exists():
            log.info("Starting the Tailscale tray app")
            subprocess.Popen([str(TS_DIR / "tailscale-ipn.exe")], creationflags=NOWIN)
            time.sleep(5)

        code, out, err = ts("status", "--json")
        try:
            st = json.loads(out)
        except ValueError:
            self.set("ts", "amber", f"Starting… ({err or out or 'no answer yet'})"[:120])
            return
        state = st.get("BackendState")
        if state == "NeedsLogin":
            self.set("ts", "amber", "Sign in to Tailscale - a browser page has opened")
            if st.get("AuthURL"):
                self.need_action("Sign in", st["AuthURL"])
            else:
                self.run_helper(["login"], "Sign in")
            return
        if state == "Stopped":
            log.info("Tailscale was switched off - reconnecting")
            ts("up", timeout=60)
            return
        if state != "Running":
            self.set("ts", "amber", f"Starting… ({state})")
            return

        me = st.get("Self", {})
        self.set("ts", "green", f"Connected as {me.get('HostName', '?')} ({', '.join(me.get('TailscaleIPs', [])[:1])})")
        if not self.dns_fixed:
            # Tailscale DNS made websites stop loading on some PCs (seen with VPNs); the POS doesn't need it.
            ts("set", "--accept-dns=false")
            self.dns_fixed = True

        public = "https://" + me.get("DNSName", "").rstrip(".")
        _, cfg, _ = ts("funnel", "status", "--json")
        if f"127.0.0.1:{PORT}" in cfg and "AllowFunnel" in cfg:
            if self.rows["pub"]["var"].get() != public:
                log.info("Public link is live: %s (first visit can take ~30 s while the certificate is issued)", public)
            self.q.put(("action", "", None))
            self.set("pub", "green", public)
        else:
            self.set("pub", "amber", "Turning on the public link… (approve 'Enable Funnel' if a page opens)")
            self.run_helper(["funnel", "--bg", str(PORT)], "Enable Funnel")

    def watchdog(self):
        """Every 20 s: check the POS answers and Tailscale + public link are up; repair what isn't."""
        while not self.closing:
            if self.server and not self.pos_alive():
                self.set("pos", "red", "Not responding - restart this app")
                log.error("POS server is not responding")
            try:
                self.ensure_tailscale()
            except Exception as e:  # keep the POS running even if Tailscale misbehaves
                log.exception("Tailscale check failed: %s", e)
                self.set("ts", "red", f"Problem: {e}"[:120])
            for _ in range(20):
                if self.closing:
                    return
                time.sleep(1)

    # ---------------- Updates from GitHub ----------------
    def toggle_auto(self):
        self.auto_on = self.auto_var.get()
        updater.save_prefs({"auto": self.auto_on})
        log.info("Auto-update %s", "on - new versions install by themselves when no orders are open" if self.auto_on else "off")

    # ---------------- Google Drive backup ----------------
    def gdrive_status(self):
        c = gdrive.load()
        if not c["refresh_token"]:
            self.set("gd", "grey", "Not set up - press Setup… to connect a Google account" if not c["client_id"]
                     else "Not connected - press Setup… → Connect Google account")
            return
        auto = f"every {c['every_hours']} h" if c["every_hours"] else "automatic off"
        last = (f"last backup {datetime.fromisoformat(c['last_ok']).strftime('%d %b %H:%M')}" if c["last_ok"]
                else "no backup yet")
        if c["last_error"]:
            self.set("gd", "red", f"{c['email']} - backup failed: {c['last_error']}"[:160])
        else:
            self.set("gd", "green", f"{c['email']} - {last}, {auto}" + (", and after Close day" if c["after_close"] else ""))

    def gdrive_now(self, quiet=False):
        def run():
            self.set("gd", "amber", "Backing up to Google Drive…")
            try:
                gdrive.backup_now(log=log)
            except Exception as e:
                log.error("Google Drive backup failed: %s", e)
            self.gdrive_status()
        threading.Thread(target=run, daemon=True).start()

    def gdrive_loop(self):
        """Automatic backups: checks every minute whether one is due (Setup → Automatic backup)."""
        self.wait(30)
        while not self.closing:
            try:
                if gdrive.due(gdrive.load()) and not gdrive.LOCK.locked():
                    log.info("Automatic Google Drive backup")
                    self.gdrive_now()
            except Exception as e:
                log.error("Google Drive check failed: %s", e)
            self.wait(60)

    def after_close_day(self):
        c = gdrive.load()
        if c["refresh_token"] and c["after_close"]:
            log.info("Day closed - backing up to Google Drive")
            self.gdrive_now()

    def gdrive_setup(self):
        c = gdrive.load()
        w = tk.Toplevel(self.root)
        w.title("Google Drive backup")
        w.transient(self.root)
        w.resizable(False, False)
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        r = 0

        def head(text):
            nonlocal r
            ttk.Label(f, text=text, font=("Segoe UI", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 2))
            r += 1

        head("1. Google sign-in app (one time)")
        ttk.Label(f, foreground=COLORS["grey"], wraplength=520, justify="left",
                  text="Google needs a free 'Desktop app' sign-in made in your Google Cloud account. Steps are in the "
                       "README under 'Google Drive backup'. Paste its Client ID and Client secret here.").grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        cid, sec = tk.StringVar(value=c["client_id"]), tk.StringVar(value=c["client_secret"])
        for label, var, show in (("Client ID", cid, ""), ("Client secret", sec, "•")):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(f, textvariable=var, width=58, show=show).grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
            r += 1
        ttk.Button(f, text="Open setup steps", command=lambda: webbrowser.open(
            f"https://github.com/{updater.REPO}#google-drive-backup")).grid(row=r, column=1, sticky="w", pady=2)
        r += 1

        head("2. Google account")
        acct = tk.StringVar(value=c["email"] or "Not connected")
        ttk.Label(f, textvariable=acct).grid(row=r, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(f)
        btns.grid(row=r, column=2, sticky="e")
        r += 1

        def keep_fields():
            c.update(client_id=cid.get().strip(), client_secret=sec.get().strip())

        def do_connect():
            keep_fields()
            gdrive.save(c)
            acct.set("Waiting for Google sign-in in the browser… pick the café's Google account")

            def run():
                try:
                    email = gdrive.connect(c)
                    log.info("Google Drive connected: %s", email)
                    self.q.put(("call", lambda: acct.set(f"Connected: {email}")))
                except Exception as e:
                    log.error("Google sign-in failed: %s", e)
                    self.q.put(("call", lambda: acct.set(f"Not connected - {e}"[:90])))
                self.gdrive_status()
            threading.Thread(target=run, daemon=True).start()

        def do_disconnect():
            if messagebox.askyesno("Disconnect Google Drive?", "Stop backing up to this Google account?\n"
                                   "Backups already in Drive stay there.", parent=w):
                gdrive.disconnect(c)
                acct.set("Not connected")
                log.info("Google Drive disconnected")
                self.gdrive_status()
        ttk.Button(btns, text="Connect Google account", command=do_connect).pack(side="left", padx=2)
        ttk.Button(btns, text="Disconnect", command=do_disconnect).pack(side="left", padx=2)

        head("3. What to back up")
        picks = {}
        for k, label in gdrive.KINDS.items():
            picks[k] = tk.BooleanVar(value=c["files"].get(k, False))
            ttk.Checkbutton(f, text=label, variable=picks[k]).grid(row=r, column=0, columnspan=3, sticky="w")
            r += 1

        head("4. Automatic backup")
        every = {"Off": 0, "Every 6 hours": 6, "Every 12 hours": 12, "Once a day": 24}
        when = tk.StringVar(value=next((k for k, v in every.items() if v == c["every_hours"]), "Once a day"))
        ttk.Combobox(f, textvariable=when, values=list(every), state="readonly", width=18).grid(row=r, column=0, sticky="w")
        after = tk.BooleanVar(value=c["after_close"])
        ttk.Checkbutton(f, text="Also right after Close day", variable=after).grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        keep = tk.StringVar(value=str(c["keep"]))
        kf = ttk.Frame(f)
        kf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(kf, text="Keep the newest").pack(side="left")
        ttk.Spinbox(kf, from_=3, to=365, textvariable=keep, width=5).pack(side="left", padx=4)
        ttk.Label(kf, text="backups of each kind in the 'CafePOS Backups' folder (older ones are deleted)").pack(side="left")
        r += 1

        def do_save(then_backup=False):
            keep_fields()
            c["files"] = {k: v.get() for k, v in picks.items()}
            c["every_hours"], c["after_close"] = every[when.get()], after.get()
            try:
                c["keep"] = max(3, min(365, int(keep.get())))
            except ValueError:
                c["keep"] = 30
            fresh = gdrive.load()  # don't overwrite a sign-in that finished meanwhile
            c.update(refresh_token=fresh["refresh_token"], email=fresh["email"], folder_id=fresh["folder_id"])
            gdrive.save(c)
            log.info("Google Drive backup settings saved")
            self.gdrive_status()
            w.destroy()
            if then_backup:
                self.gdrive_now()
        bar = ttk.Frame(f)
        bar.grid(row=r, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(bar, text="Save & back up now", command=lambda: do_save(True)).pack(side="right", padx=2)
        ttk.Button(bar, text="Save", command=do_save).pack(side="right", padx=2)

    def toggle_autostart(self):
        try:
            updater.set_autostart(self.start_var.get())
            log.info("Start with Windows %s", "on - the POS opens by itself when this PC signs in" if self.start_var.get() else "off")
        except OSError as e:
            self.start_var.set(not self.start_var.get())
            log.error("Could not change Start with Windows: %s", e)

    def update_button(self):
        """'Check now', or 'Update now' once a newer build has been found."""
        info = self.update_info
        if info and updater.newer(info) and updater.can_self_update():
            msg = (f"Install build {info['build']} now?\n\nThe POS restarts (about 10-20 seconds) and tablets reconnect "
                   "by themselves. Bills, menu and settings are kept.")
            n = updater.open_orders()
            if n:
                msg += f"\n\n{n} open order(s) are saved and will still be there after the restart."
            if messagebox.askyesno("Update Cafe POS", msg):
                threading.Thread(target=self.install_update, daemon=True).start()
        else:
            threading.Thread(target=self.check_update, args=(True,), daemon=True).start()

    def check_update(self, manual=False):
        mine = updater.current()
        try:
            info = updater.latest()
        except Exception as e:
            self.set("upd", "amber", f"Build {mine['build']} ({mine['sha']}) - could not check for updates ({e})"[:140])
            if manual:
                log.warning("Update check failed: %s", e)
            return None
        self.update_info = info
        when = time.strftime("%H:%M")
        if not updater.newer(info, mine):
            self.set("upd", "green", f"Build {mine['build']} ({mine['sha']}) - up to date, checked {when}")
            self.q.put(("updbtn", "Check now"))
            if manual:
                log.info("Up to date (build %s)", mine["build"])
        elif not updater.can_self_update():
            self.set("upd", "amber", f"Build {info['build']} is on GitHub - running from source, update with: git pull")
        else:
            self.set("upd", "amber", f"New version: build {info['build']} - {info.get('message', '')}"[:140])
            self.q.put(("updbtn", "Update now"))
            if manual or not self.auto_on:
                log.info("Update available: build %s (%s). Click 'Update now' to install.", info["build"], info.get("message", ""))
        return info

    def update_loop(self):
        """Check GitHub soon after start, then every 30 min; with Auto-update on, install when no orders are open."""
        self.wait(60)
        while not self.closing:
            info = self.check_update()
            pause = 30 * 60
            if info and updater.newer(info) and self.auto_on and updater.can_self_update():
                n = updater.open_orders()
                if n:
                    if not self.waiting_logged:
                        log.info("Build %s will install automatically once all %s open order(s) are billed", info["build"], n)
                        self.waiting_logged = True
                    pause = 5 * 60
                else:
                    log.info("Auto-update: installing build %s", info["build"])
                    self.install_update()
                    if self.installing:
                        return
            self.wait(pause)

    def wait(self, seconds):
        for _ in range(seconds):
            if self.closing:
                return
            time.sleep(1)

    def install_update(self):
        if self.installing:
            return
        self.installing = True
        info = self.update_info
        try:
            self.set("upd", "amber", f"Downloading build {info['build']}…")
            updater.download(info, progress=lambda p: self.set("upd", "amber", f"Downloading build {info['build']}… {p}%"))
            log.info("Build %s downloaded and verified - restarting into it", info["build"])
            self.q.put(("restart", info))
        except Exception as e:
            log.error("Update failed: %s", e)
            self.set("upd", "red", f"Update failed: {e}"[:140])
            self.installing = False

    def restart_into_update(self):
        """Main thread: stop the server, swap in the new exe, start it and close this copy. Funnel stays on."""
        self.closing = True
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        try:
            updater.swap(updater.NEW)
        except OSError as e:
            log.error("Could not replace CafePOS.exe (%s) - restarting the current version", e)
        updater.relaunch(updater.current()["build"])
        self.root.destroy()

    def on_close(self):
        if not messagebox.askyesno("Close the POS?", "Closing stops the POS on every tablet and turns off the public link.\n\nClose now?"):
            return
        self.closing = True
        log.info("Shutting down…")
        if ts_exe():
            try:  # don't leave a public link pointing at a stopped POS
                ts("funnel", "--https=443", "off", timeout=15)
            except Exception:
                pass
        if self.server:
            self.server.shutdown()
        self.root.destroy()


class UIHandler(logging.Handler):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def emit(self, record):
        self.win.q.put(("log", record.levelno, self.format(record)))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    win = Window(root)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    ui = UIHandler(win)
    ui.setFormatter(fmt)
    logs = app.DB.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    disk = logging.FileHandler(logs / f"pos-{date.today()}.log", encoding="utf-8")
    disk.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(ui)
    log.addHandler(disk)
    if updater.can_self_update():
        updater.cleanup()
    if os.environ.get("CAFEPOS_UPDATED_FROM"):
        log.info("Updated from build %s to build %s - your data is unchanged", os.environ["CAFEPOS_UPDATED_FROM"],
                 updater.current()["build"])
    started = win.start_server()
    os.environ.pop("CAFEPOS_UPDATED_FROM", None)  # don't hand it on to the next restart
    if started:
        threading.Thread(target=win.watchdog, daemon=True).start()
        threading.Thread(target=win.update_loop, daemon=True).start()
        threading.Thread(target=win.gdrive_loop, daemon=True).start()
        app.AFTER_CLOSE_DAY = win.after_close_day
    win.gdrive_status()
    root.mainloop()


if __name__ == "__main__":
    main()
