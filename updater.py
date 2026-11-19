"""Self-update for CafePOS.exe from this repo's GitHub "latest" release (built by .github/workflows/build-exe.yml).

Every build stamps build.json (build number + commit) into the exe and uploads version.json next to CafePOS.exe
on the release. The app compares the two; to update it downloads the new exe, checks its SHA-256, swaps it in
(Windows allows renaming a running exe) and restarts. The database, logs and settings live in %LOCALAPPDATA%\\CafePOS,
outside the exe, so an update never touches them."""
import hashlib, json, os, subprocess, sys, urllib.request
from pathlib import Path

import app

REPO = os.environ.get("POS_UPDATE_REPO") or "saiprashanth1009/cafe-pos"
BASE = f"https://github.com/{REPO}/releases/download/latest"
PREFS = app.DATA_DIR / "update.json"
EXE = Path(sys.executable)
NEW, OLD = EXE.with_name(EXE.stem + ".new.exe"), EXE.with_name(EXE.stem + ".old.exe")


def current():
    """This copy's build: {"build": 12, "sha": "abc1234"}; build 0 = running from source or an exe from before updates."""
    try:
        return json.loads((app.RES / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"build": 0, "sha": "source" if not app.FROZEN else "old"}


def can_self_update():
    return bool(app.FROZEN)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "CafePOS-updater", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest():
    """What's published on GitHub: {"build", "sha", "size", "sha256", "message"}."""
    with _get(f"{BASE}/version.json") as r:
        return json.loads(r.read().decode("utf-8"))


def newer(info, mine=None):
    return int(info.get("build", 0)) > int((mine or current()).get("build", 0))


def load_prefs():
    try:
        return {"auto": True, **json.loads(PREFS.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return {"auto": True}


def save_prefs(prefs):
    PREFS.parent.mkdir(parents=True, exist_ok=True)
    PREFS.write_text(json.dumps(prefs), encoding="utf-8")


def open_orders():
    """Tables/takeaways not billed yet. They survive a restart (they're in pos.db), but auto-update waits for none."""
    con = app.db()
    try:
        return con.execute("SELECT COUNT(*) FROM orders WHERE status = 'open'").fetchone()[0]
    finally:
        con.close()


def download(info, progress=lambda pct: None, dest=None):
    """Download the new exe next to the current one and verify it. Returns its path."""
    dest = Path(dest or NEW)
    h, got = hashlib.sha256(), 0
    try:
        with _get(f"{BASE}/CafePOS.exe", timeout=60) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if info.get("size"):
                    progress(min(99, got * 100 // info["size"]))
        if info.get("size") and got != info["size"]:
            raise ValueError(f"download incomplete ({got} of {info['size']} bytes)")
        if info.get("sha256") and h.hexdigest() != info["sha256"].lower():
            raise ValueError("download is damaged (checksum mismatch) - try again in a few minutes")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest


def swap(new, exe=None):
    """Put the new exe in place of the running one; keeps the old one as CafePOS.old.exe (deleted on next start)."""
    exe = Path(exe or EXE)
    old = exe.with_name(exe.stem + ".old.exe")
    old.unlink(missing_ok=True)
    os.replace(exe, old)          # allowed while it runs
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)      # put things back as they were
        raise


def relaunch(from_build):
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1", CAFEPOS_UPDATED_FROM=str(from_build))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(EXE)], env=env, close_fds=True, creationflags=flags, cwd=str(EXE.parent))


RUN_KEY, RUN_NAME = r"Software\Microsoft\Windows\CurrentVersion\Run", "CafePOS"


def autostart_enabled():
    """True when Windows opens this exe at sign-in (current user's Run key - no admin needed)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0].strip('"').lower() == str(EXE).lower()
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:  # same path after updates (the exe is swapped in place), so this stays valid
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{EXE}"')
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def cleanup():
    """After an update: remove the previous exe and any half-finished download."""
    for p in (OLD, NEW):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass  # still locked for a moment - next start gets it
