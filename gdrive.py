"""Google Drive backup for CafePOS - Python standard library only.

Setup (once): make an OAuth client of type "Desktop app" in Google Cloud (Drive API on), paste its Client ID and
secret in CafePOS -> Google Drive -> Setup, press Connect and pick the Google account. CafePOS only gets the
"drive.file" permission: it can see and change only the files it made itself (the "CafePOS Backups" folder).

Backups: a consistent copy of the database (+ optionally logs and the menu as CSV), uploaded to that folder; the
newest `keep` of each kind are kept. Nothing secret about the café is sent anywhere else."""
import base64, csv, hashlib, io, json, os, secrets, sqlite3, tempfile, threading, time, urllib.error, urllib.parse
import urllib.request, webbrowser, zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import app

CONF = app.DATA_DIR / "gdrive.json"
SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "CafePOS Backups"
DEFAULTS = {"client_id": "", "client_secret": "", "refresh_token": "", "email": "", "folder_id": "",
            "files": {"db": True, "logs": False, "menu": True}, "every_hours": 24, "after_close": True, "keep": 30,
            "last_ok": "", "last_error": ""}
KINDS = {"db": "Sales database (bills, menu, settings, audit log)", "logs": "Log files (last 14 days)",
         "menu": "Menu as a spreadsheet (CSV)"}
LOCK = threading.Lock()  # one backup at a time


class DriveError(Exception):
    pass


def load():
    try:
        c = json.loads(CONF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        c = {}
    return {**DEFAULTS, **c, "files": {**DEFAULTS["files"], **c.get("files", {})}}


def save(c):
    CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=1), encoding="utf-8")
    os.replace(tmp, CONF)


def _req(url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())
            msg = msg.get("error_description") or (msg.get("error") or {}).get("message") or msg.get("error") or msg
        except ValueError:
            msg = e.reason
        raise DriveError(f"Google said: {msg} (HTTP {e.code})") from None
    except urllib.error.URLError as e:
        raise DriveError(f"No internet connection to Google ({e.reason})") from None


def _form(url, fields):
    return _req(url, urllib.parse.urlencode(fields).encode(), {"Content-Type": "application/x-www-form-urlencoded"})


# ---------------- sign in (OAuth "installed app" flow with PKCE, answer comes back to 127.0.0.1) ----------------
def connect(c, open_browser=webbrowser.open, wait=300):
    """Opens Google's sign-in page in the browser; the person picks the account. Saves the refresh token."""
    if not (c.get("client_id") and c.get("client_secret")):
        raise DriveError("Paste the Client ID and Client secret first (Setup)")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(16), {}

    class Back(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got
            body = (f"<h2>{'CafePOS is connected to Google Drive.' if ok else 'Google sign-in was cancelled.'}</h2>"
                    "<p>You can close this tab and go back to the CafePOS window.</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Back)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    open_browser(AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent select_account", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    srv.timeout, end = 1, time.time() + wait
    try:
        while not got and time.time() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    if "code" not in got:
        raise DriveError("Google sign-in was not finished" + (f" ({got['error']})" if got.get("error") else " in time"))
    tok = _form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c["client_secret"], "code": got["code"],
                            "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"})
    if not tok.get("refresh_token"):
        raise DriveError("Google did not give a lasting sign-in - press Connect again")
    c.update(refresh_token=tok["refresh_token"], folder_id="", last_error="")
    c["email"] = _req(f"{API}/about?fields=user(emailAddress)",
                      headers={"Authorization": "Bearer " + tok["access_token"]}).get("user", {}).get("emailAddress", "")
    save(c)
    return c["email"]


def disconnect(c):
    if c.get("refresh_token"):
        try:  # also tell Google, so the access is gone from the Google account's list too
            _form("https://oauth2.googleapis.com/revoke", {"token": c["refresh_token"]})
        except DriveError:
            pass
    c.update(refresh_token="", email="", folder_id="")
    save(c)


def _token(c):
    if not c.get("refresh_token"):
        raise DriveError("Not connected - press Connect and pick the Google account")
    try:
        return _form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c["client_secret"],
                                 "refresh_token": c["refresh_token"], "grant_type": "refresh_token"})["access_token"]
    except DriveError as e:
        if "invalid_grant" in str(e) or "expired" in str(e).lower():
            raise DriveError("The Google sign-in has expired or was removed - press Connect again") from None
        raise


# ---------------- what gets backed up ----------------
def db_snapshot():
    """Consistent copy of pos.db, safe while the café is billing."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src, dst = sqlite3.connect(app.DB, timeout=10), sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


def logs_zip(days=14):
    buf, cut = io.BytesIO(), time.time() - days * 86400
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted((app.DB.parent / "logs").glob("*.log")):
            if p.stat().st_mtime >= cut:
                z.write(p, p.name)
    return buf.getvalue()


def menu_csv():
    con = app.db()
    try:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["category", "name", "price"])
        for r in con.execute("SELECT category, name, price FROM items WHERE active = 1 ORDER BY id"):
            w.writerow([r["category"], r["name"], f"{r['price'] / 100:.2f}"])
        return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel shows ₹ and é properly
    finally:
        con.close()


def build(c, stamp):
    out = []
    if c["files"].get("db"):
        out.append((f"cafepos-db-{stamp}.db", db_snapshot(), "application/x-sqlite3"))
    if c["files"].get("logs"):
        out.append((f"cafepos-logs-{stamp}.zip", logs_zip(), "application/zip"))
    if c["files"].get("menu"):
        out.append((f"cafepos-menu-{stamp}.csv", menu_csv(), "text/csv"))
    return out


# ---------------- Drive ----------------
def _folder(c, auth):
    if c.get("folder_id"):
        try:
            f = _req(f"{API}/files/{c['folder_id']}?fields=id,trashed", headers=auth)
            if not f.get("trashed"):
                return c["folder_id"]
        except DriveError:
            pass
    q = f"name = '{FOLDER}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    found = _req(f"{API}/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}), headers=auth).get("files")
    fid = found[0]["id"] if found else _req(
        f"{API}/files?fields=id", json.dumps({"name": FOLDER, "mimeType": "application/vnd.google-apps.folder"}).encode(),
        {**auth, "Content-Type": "application/json"})["id"]
    c["folder_id"] = fid
    return fid


def _upload(auth, folder, name, data, mime):
    b = "cafepos" + secrets.token_hex(8)
    meta = json.dumps({"name": name, "parents": [folder]}).encode()
    body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta
            + f"\r\n--{b}\r\nContent-Type: {mime}\r\n\r\n".encode() + data + f"\r\n--{b}--\r\n".encode())
    return _req(UPLOAD + "?uploadType=multipart&fields=id,name,size", body,
                {**auth, "Content-Type": f"multipart/related; boundary={b}"}, timeout=300)


def _prune(auth, folder, keep):
    q = f"'{folder}' in parents and trashed = false"
    files = _req(f"{API}/files?" + urllib.parse.urlencode(
        {"q": q, "orderBy": "createdTime desc", "fields": "files(id,name)", "pageSize": 1000}), headers=auth).get("files", [])
    seen, gone = {}, 0
    for f in files:  # newest first; keep `keep` of each kind (db / logs / menu)
        kind = f["name"].split("-")[1] if f["name"].startswith("cafepos-") else "other"
        seen[kind] = seen.get(kind, 0) + 1
        if kind != "other" and seen[kind] > keep:
            _req(f"{API}/files/{f['id']}", headers=auth, method="DELETE")
            gone += 1
    return gone


def backup_now(c=None, log=None):
    """Upload the chosen files now. Returns a short summary; raises DriveError with a plain-English message."""
    with LOCK:
        c = c or load()
        try:
            if not any(c["files"].values()):
                raise DriveError("No files are ticked in Setup")
            auth = {"Authorization": "Bearer " + _token(c)}
            folder = _folder(c, auth)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            done = []
            for name, data, mime in build(c, stamp):
                _upload(auth, folder, name, data, mime)
                done.append(f"{name} ({max(1, len(data) // 1024)} KB)")
            gone = _prune(auth, folder, int(c.get("keep") or 30))
            c.update(last_ok=datetime.now().isoformat(timespec="seconds"), last_error="")
            save(c)
            msg = f"Backed up to Google Drive ({c.get('email') or 'connected account'}): " + ", ".join(done)
            if gone:
                msg += f"; removed {gone} old backup(s)"
            if log:
                log.info(msg)
            return msg
        except Exception as e:
            c["last_error"] = str(e)[:300]
            save(c)
            raise


def due(c, now=None):
    """Automatic backup due? (every N hours since the last good one)"""
    hours = int(c.get("every_hours") or 0)
    if not (hours and c.get("refresh_token")):
        return False
    if not c.get("last_ok"):
        return True
    return (now or datetime.now()) - datetime.fromisoformat(c["last_ok"]) >= timedelta(hours=hours)
