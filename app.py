"""Restaurant POS server (dine-in / takeaway / delivery, KOT, GST billing, daily reports).
Run: python app.py      Self-check: python app.py test      Windows control window: python gui.py
Only needs Python 3 - no packages. All data lives in pos.db: back it up."""
import base64, csv, hashlib, hmac, json, logging, os, re, secrets, socket, sqlite3, sys, threading, time, unicodedata, webbrowser
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
FROZEN = getattr(sys, "frozen", False)           # running inside CafePOS.exe
RES = Path(getattr(sys, "_MEIPASS", HERE))       # index.html + menu.csv (unpacked from the exe)
# exe keeps data in the user's profile so moving/updating the exe never loses bills
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or HERE) / "CafePOS" if FROZEN else HERE
# Cloud hosting sets these: POS_DB must point at a persistent disk, or data is lost on every redeploy.
DB = Path(os.environ.get("POS_DB") or DATA_DIR / "pos.db")
PORT = int(os.environ.get("PORT") or 8000)
log = logging.getLogger("pos")
AFTER_CLOSE_DAY = None  # set by the CafePOS window (gui.py) to back up after "Close day"
TYPES = ("dine_in", "takeaway", "delivery")
PAYMENTS = ("Cash", "UPI", "Card")
DEFAULT_SETTINGS = {"name": "Guns & Roses Café", "address": "Kondapur, Hyderabad", "phone": "", "gstin": "", "fssai": "",
                    "tax_pct": "5", "tables": "8", "footer": "Thank you! Visit again.",
                    "printer": "", "paper": "58",  # printer: "" = tablet's print dialog, else COM port of a thermal printer
                    "pin": os.environ.get("POS_PIN") or "1234",  # staff PIN; set POS_PIN when hosting so a fresh site never has 1234
                    "owner_pin": os.environ.get("POS_OWNER_PIN") or ""}  # blank = the staff PIN also has owner rights
SECRET_SETTINGS = ("pin", "owner_pin", "pin_key")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
    price INTEGER NOT NULL CHECK(price >= 0), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, type TEXT NOT NULL, table_no INTEGER,
    customer TEXT, phone TEXT, address TEXT, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'billed', 'cancelled')),
    bill_no INTEGER UNIQUE, day_no INTEGER, billed TEXT, day TEXT, payment TEXT, subtotal INTEGER, discount_pct TEXT,
    discount INTEGER, tax_pct TEXT, cgst INTEGER, sgst INTEGER, round_off INTEGER, total INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_order_per_table ON orders(table_no) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS orders_day ON orders(day);
CREATE TABLE IF NOT EXISTS kots(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
    created TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'ready')));
CREATE TABLE IF NOT EXISTS lines(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
    kot_id INTEGER NOT NULL REFERENCES kots, item_id INTEGER, name TEXT NOT NULL, price INTEGER NOT NULL,
    qty INTEGER NOT NULL CHECK(qty > 0));
CREATE INDEX IF NOT EXISTS lines_order ON lines(order_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
-- one row per payment on a bill: split payments (Cash 500 + UPI rest) or a table split between people
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
    method TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0));
CREATE INDEX IF NOT EXISTS payments_order ON payments(order_id);
CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders, at TEXT NOT NULL,
    day TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0), method TEXT NOT NULL, reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS refunds_day ON refunds(day);
-- who did what: cancels, removed items, voids, refunds, discounts, menu/settings changes, day close
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, role TEXT NOT NULL,
    device TEXT, ip TEXT, action TEXT NOT NULL, order_id INTEGER, amount INTEGER, reason TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS audit_day ON audit(day);
-- named sign-ins: up to 3 owners + 5 staff, each with its own PIN, link (/profile=<slug>) and permissions
CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner', 'staff')), pin_hash TEXT NOT NULL, perms TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS profiles_slug ON profiles(slug) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS profiles_pin ON profiles(pin_hash) WHERE active = 1;
"""


def db(path=DB):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init(con):
    con.execute("PRAGMA journal_mode=WAL")  # kitchen/floor polling reads don't block billing writes
    with con:
        con.executescript(SCHEMA)
        if "day_no" not in {r[1] for r in con.execute("PRAGMA table_info(orders)")}:
            con.execute("ALTER TABLE orders ADD COLUMN day_no INTEGER")  # databases from before daily bill numbers
        cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
        for c in ("voided", "void_reason"):  # voided bills keep their number (GST) but leave the totals
            if c not in cols:
                con.execute(f"ALTER TABLE orders ADD COLUMN {c} TEXT")
        kcols = {r[1] for r in con.execute("PRAGMA table_info(kots)")}
        if "client_id" not in kcols:
            con.execute("ALTER TABLE kots ADD COLUMN client_id TEXT")  # tablet's id for a KOT, so offline resends don't double up
        if "ready_at" not in kcols:
            con.execute("ALTER TABLE kots ADD COLUMN ready_at TEXT")  # kitchen's "recently ready" list (undo a mis-tap)
        if "who" not in {r[1] for r in con.execute("PRAGMA table_info(audit)")}:
            con.execute("ALTER TABLE audit ADD COLUMN who TEXT")  # profile name
        # secret for PIN hashes, made once per database (profile PINs are never stored as typed)
        con.execute("INSERT OR IGNORE INTO settings VALUES('pin_key', ?)", (secrets.token_hex(16),))
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS kots_client ON kots(client_id) WHERE client_id IS NOT NULL")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS bill_no_per_day ON orders(day, day_no)")
        # bills from before split payments: one payment row for the whole total
        con.execute("INSERT INTO payments(order_id, method, amount) SELECT id, payment, total FROM orders o "
                    "WHERE status = 'billed' AND payment IS NOT NULL AND total > 0 "
                    "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)")
        con.executemany("INSERT OR IGNORE INTO settings VALUES(?, ?)", DEFAULT_SETTINGS.items())
        # Business day: bills go to this date until staff press "Close day", so reopening the app or
        # billing after midnight never splits a day. Existing databases continue their latest billed day.
        con.execute("INSERT OR IGNORE INTO settings VALUES('business_day', "
                    "COALESCE((SELECT MAX(day) FROM orders WHERE status = 'billed'), ?))", (date.today().isoformat(),))
        if not con.execute("SELECT 1 FROM items").fetchone():  # first run: load the starter menu
            with open(RES / "menu.csv", encoding="utf-8") as f:
                con.executemany("INSERT INTO items(category, name, price) VALUES(?, ?, ?)",
                                [(r["category"], r["name"], paise(r["price"])) for r in csv.DictReader(f)])


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---- money: integer paise everywhere, half-up rounding ----
def paise(x):
    return int((Decimal(str(x)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def rs(p):
    return f"₹{p / 100:,.2f}"


def pct(amount, p):
    return int((Decimal(amount) * Decimal(str(p)) / 100).quantize(Decimal(1), ROUND_HALF_UP))


def totals(lines, discount_pct, tax_pct, discount_amt=0):
    """lines: [(price, qty)]. Discount is a % or a flat amount in paise. GST on the discounted amount,
    split equally into CGST + SGST. No round-off: the total is charged to the paisa (round_off stays 0;
    bills from older versions keep theirs)."""
    subtotal = sum(p * q for p, q in lines)
    discount = discount_amt or pct(subtotal, discount_pct)
    cgst = sgst = pct(subtotal - discount, Decimal(str(tax_pct)) / 2)
    return subtotal, discount, cgst, sgst, 0, subtotal - discount + cgst + sgst


def check_pct(v):
    v = str(v or 0)
    if not 0 <= Decimal(v) <= 100:
        raise ValueError("percentage must be 0-100")
    return v


def settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT * FROM settings")}


def business_day(con):
    return con.execute("SELECT value FROM settings WHERE key = 'business_day'").fetchone()[0]


def bill_label(day, n):
    """Printed invoice number: restarts at 1 each business day, prefixed DDMMYY so it stays unique for the
    financial year as GST requires (e.g. 131126-1)."""
    return f"{day[8:10]}{day[5:7]}{day[2:4]}-{n}"


def order_out(con, r):
    o = dict(r)
    if o.get("bill_no"):
        o["bill_label"] = bill_label(o["day"], o["day_no"]) if o.get("day_no") else str(o["bill_no"])  # old bills
    o["lines"] = [dict(l) for l in con.execute(
        "SELECT l.id, l.name, l.price, l.qty, l.kot_id, k.status AS kot_status FROM lines l "
        "JOIN kots k ON k.id = l.kot_id WHERE l.order_id = ? ORDER BY l.id", (r["id"],))]
    o["amount"] = sum(l["price"] * l["qty"] for l in o["lines"])
    o["pending"] = len({l["kot_id"] for l in o["lines"] if l["kot_status"] == "new"})
    if o["status"] == "billed":
        o["payments"] = [dict(p) for p in con.execute("SELECT method, amount FROM payments WHERE order_id = ? ORDER BY id", (r["id"],))]
        o["refunded"] = con.execute("SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE order_id = ?", (r["id"],)).fetchone()[0]
    return o


# ---- profiles, permissions and the audit trail ----
PERMS = {  # what a profile may do - the owner ticks these per profile on the Profiles screen
    "orders": "Take orders and send them to the kitchen",
    "bill": "Bill and print",
    "kitchen": "Kitchen screen (mark ready, undo)",
    "remove_item": "Remove items already sent to the kitchen",
    "cancel": "Cancel whole orders",
    "discount": "Give discounts",
    "void_refund": "Void and refund bills",
    "reports": "Reports, audit log and reprints",
    "dashboard": "Dashboard",
    "menu": "Edit the menu",
    "settings": "Settings, printer and backup download",
    "close_day": "Close the day",
    "profiles": "Create and edit profiles (owners only)",
}
STAFF_DEFAULT = ["orders", "bill", "kitchen", "remove_item"]
MAX_PROFILES = {"owner": 3, "staff": 5}
LEGACY_STAFF = {"orders", "bill", "kitchen", "remove_item"}  # old staff PIN, until profiles are set up


def pin_hash(s, pin):
    return hmac.new(s["pin_key"].encode(), str(pin).encode(), hashlib.sha256).hexdigest()


def profile_perms(r):
    p = set(json.loads(r["perms"])) & set(PERMS)
    return p | {"profiles"} if r["role"] == "owner" else p - {"profiles"}  # an owner can always manage profiles


def profiles_on(con):
    """Once an owner profile exists, only profile PINs sign in (the old shared PINs stop working)."""
    return bool(con.execute("SELECT 1 FROM profiles WHERE active = 1 AND role = 'owner'").fetchone())


def who_for(con, s, given, slug=None):
    """Who a PIN signs in as: {role, name, slug, perms} or None. On a profile link (/profile=<slug>)
    the PIN must be that profile's."""
    if given:
        r = con.execute("SELECT * FROM profiles WHERE pin_hash = ? AND active = 1", (pin_hash(s, given),)).fetchone()
        if r:
            if slug and slug != r["slug"]:
                return None
            return {"role": r["role"], "name": r["name"], "slug": r["slug"], "perms": profile_perms(r)}
    if slug or profiles_on(con):
        return None
    g = (given or "").encode()  # before profiles: the old owner/staff PINs, exactly as in v3
    if s.get("owner_pin") and hmac.compare_digest(g, s["owner_pin"].encode()):
        return {"role": "owner", "name": "Owner", "slug": None, "perms": set(PERMS)}
    if hmac.compare_digest(g, s["pin"].encode()):
        if s.get("owner_pin"):
            return {"role": "staff", "name": "Staff", "slug": None, "perms": set(LEGACY_STAFF)}
        return {"role": "owner", "name": "Owner", "slug": None, "perms": set(PERMS)}
    return None


class Forbidden(Exception):
    pass


def can(d, perm):
    w = d.get("_who")
    return w is None or perm in w.get("perms", ())  # no _who = called from code/tests


def need_perm(d, perm, what="This"):
    if not can(d, perm):
        raise Forbidden(f"{what} needs a profile that may: {PERMS[perm].lower()}")


def need_reason(d):
    r = str(d.get("reason") or "").strip()[:200]
    if len(r) < 3:
        raise ValueError("please type a reason (at least 3 letters)")
    return r


def audit(con, d, action, order_id=None, amount=None, reason=None, detail=None, day=None):
    w = d.get("_who") or {}
    if w.get("for") and w.get("for") != w.get("slug"):  # approved on someone else's tablet
        detail = f"{detail + '; ' if detail else ''}approved for {w['for']}"
    con.execute("INSERT INTO audit(at, day, role, who, device, ip, action, order_id, amount, reason, detail) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now(), day or business_day(con), w.get("role", "system"), w.get("name"), w.get("device"), w.get("ip"),
                 action, order_id, amount, reason, detail))
    log.warning("AUDIT %s by %s%s%s%s%s", action, w.get("name") or w.get("role", "system"),
                f" on {w['device']}" if w.get("device") else "", f", order #{order_id}" if order_id else "",
                f", {rs(amount)}" if amount else "", f": {reason}" if reason else "")


def get_order(con, oid):
    return order_out(con, con.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone())


# ---- GET routes (con, query) ----
def state(con, q):
    s = settings(con)
    w = q.get("_who") or {"role": "owner", "name": "Owner", "slug": None, "perms": set(PERMS)}
    return {"settings": {k: v for k, v in s.items() if k not in SECRET_SETTINGS},
            "role": w["role"], "owner_pin_set": bool(s.get("owner_pin")), "profiles_on": profiles_on(con),
            "me": {"name": w["name"], "role": w["role"], "slug": w["slug"], "perms": sorted(w["perms"])},
            "items": [dict(r) for r in con.execute(
                "SELECT id, name, category, price FROM items WHERE active = 1 ORDER BY id")],  # menu.csv order
            "orders": [order_out(con, r) for r in con.execute("SELECT * FROM orders WHERE status = 'open' ORDER BY id")]}


def kitchen(con, q):
    def tickets(where, order, limit=""):
        out = []
        for k in con.execute(
                "SELECT k.id, k.created, k.ready_at, k.note, k.order_id, o.type, o.table_no, o.customer FROM kots k "
                f"JOIN orders o ON o.id = k.order_id WHERE {where} AND o.status != 'cancelled' ORDER BY {order} {limit}"):
            lines = [dict(l) for l in con.execute("SELECT name, qty FROM lines WHERE kot_id = ? ORDER BY id", (k["id"],))]
            if lines:  # every line removed -> nothing left to cook
                out.append({**dict(k), "lines": lines})
        return out
    return {"kots": tickets("k.status = 'new'", "k.id"),
            "recent": tickets("k.status = 'ready'", "COALESCE(k.ready_at, k.created) DESC, k.id DESC", "LIMIT 10")}


def report(con, q):
    day = q.get("day", [date.today().isoformat()])[0]
    every = [order_out(con, r) for r in con.execute(
        "SELECT * FROM orders WHERE status = 'billed' AND day = ? ORDER BY COALESCE(day_no, bill_no) DESC", (day,))]
    bills = [b for b in every if not b.get("voided")]  # voided bills keep their number but don't count
    pay, typ, items = {}, {}, {}
    for b in bills:
        for p in b["payments"]:
            pay[p["method"]] = pay.get(p["method"], 0) + p["amount"]
        typ[b["type"]] = typ.get(b["type"], 0) + b["total"]
        for l in b["lines"]:
            qty, amt = items.get(l["name"], (0, 0))
            items[l["name"]] = (qty + l["qty"], amt + l["qty"] * l["price"])
    refunds = [dict(r) for r in con.execute("SELECT * FROM refunds WHERE day = ? ORDER BY id", (day,))]
    for r in refunds:  # by payment = what should be in the drawer / bank after refunds
        pay[r["method"]] = pay.get(r["method"], 0) - r["amount"]
    sums = {k: sum(b[k] for b in bills) for k in ("subtotal", "discount", "cgst", "sgst", "round_off", "total")}
    refunded = sum(r["amount"] for r in refunds)
    voided = [b for b in every if b.get("voided")]
    return {"day": day, "open": day == business_day(con), "count": len(bills),
            "sums": sums, "refunds": refunded, "net": sums["total"] - refunded,
            "voided": {"count": len(voided), "total": sum(b["total"] for b in voided)},
            "by_payment": pay, "by_type": typ,
            "items": sorted(([n, q, a] for n, (q, a) in items.items()), key=lambda x: -x[2]),
            "cancelled": con.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled' "
                                     "AND COALESCE(day, substr(created, 1, 10)) = ?", (day,)).fetchone()[0],
            "bills": every}


def audit_log(con, q):
    day = q.get("day", [business_day(con)])[0]
    return [dict(r) for r in con.execute("SELECT * FROM audit WHERE day = ? ORDER BY id DESC", (day,))]


def dashboard(con, q):
    """Owner's overview: today, top items, busy hours, payment mix (over a period) and this week vs last week."""
    today = date.fromisoformat(business_day(con))
    days = {"today": 1, "7": 7, "30": 30}.get(q.get("period", ["7"])[0], 7)
    start = (today - timedelta(days=days - 1)).isoformat()
    live = "status = 'billed' AND voided IS NULL"

    def daily(a, b):  # {day: net sales} for business days a..b, refunds taken off the day they were given
        out = {r[0]: r[1] for r in con.execute(f"SELECT day, SUM(total) FROM orders WHERE {live} AND day BETWEEN ? AND ? "
                                               "GROUP BY day", (a, b))}
        for d, amt in con.execute("SELECT day, SUM(amount) FROM refunds WHERE day BETWEEN ? AND ? GROUP BY day", (a, b)):
            out[d] = out.get(d, 0) - amt
        return out

    t = today.isoformat()
    tb = con.execute(f"SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE {live} AND day = ?", (t,)).fetchone()
    net_today = daily(t, t).get(t, 0)
    items = [list(r) for r in con.execute(
        f"SELECT l.name, SUM(l.qty), SUM(l.qty * l.price) FROM lines l JOIN orders o ON o.id = l.order_id "
        f"WHERE o.{live.replace(' AND ', ' AND o.')} AND o.day BETWEEN ? AND ? GROUP BY l.name "
        "ORDER BY SUM(l.qty) DESC, SUM(l.qty * l.price) DESC LIMIT 10", (start, t))]
    hours = {r[0]: {"bills": r[1], "sales": r[2]} for r in con.execute(
        f"SELECT CAST(substr(billed, 12, 2) AS INTEGER), COUNT(*), SUM(total) FROM orders WHERE {live} "
        "AND day BETWEEN ? AND ? GROUP BY 1", (start, t))}
    pay = {}
    for m, a in con.execute(f"SELECT p.method, SUM(p.amount) FROM payments p JOIN orders o ON o.id = p.order_id "
                            f"WHERE o.{live.replace(' AND ', ' AND o.')} AND o.day BETWEEN ? AND ? GROUP BY 1", (start, t)):
        pay[m] = pay.get(m, 0) + a
    for m, a in con.execute("SELECT method, SUM(amount) FROM refunds WHERE day BETWEEN ? AND ? GROUP BY 1", (start, t)):
        pay[m] = pay.get(m, 0) - a
    mon = today - timedelta(days=today.weekday())
    this_w = daily(mon.isoformat(), (mon + timedelta(days=6)).isoformat())
    last_w = daily((mon - timedelta(days=7)).isoformat(), (mon - timedelta(days=1)).isoformat())
    week = [{"day": (mon + timedelta(days=i)).isoformat(),
             "this": this_w.get((mon + timedelta(days=i)).isoformat(), 0) if mon + timedelta(days=i) <= today else None,
             "last": last_w.get((mon + timedelta(days=i - 7)).isoformat(), 0)} for i in range(7)]
    return {"day": t, "period": days, "today": {"bills": tb[0], "net": net_today, "avg": tb[1] // tb[0] if tb[0] else 0},
            "top_items": items, "hours": hours, "payments": pay, "week": week,
            "week_so_far": sum(w["this"] or 0 for w in week),
            "last_week_same_days": sum(w["last"] for w in week if w["this"] is not None),
            "last_week_total": sum(w["last"] for w in week)}


# ---- POST routes (con, body) ----
def save_item(con, d):
    name, cat, price = str(d["iname"]).strip(), str(d.get("icat") or "").strip() or "Other", paise(d["iprice"])
    if not name or price < 0:
        raise ValueError("name required, price must be >= 0")
    if d.get("iid"):
        con.execute("UPDATE items SET name = ?, category = ?, price = ? WHERE id = ?", (name, cat, price, int(d["iid"])))
    else:
        con.execute("INSERT INTO items(name, category, price) VALUES(?, ?, ?)", (name, cat, price))
    audit(con, d, "menu_save", amount=price, detail=f"{name} ({cat})" + (" - changed" if d.get("iid") else " - added"))
    return {"ok": True}


def delete_item(con, d):
    # ponytail: soft delete; past bills keep their own name/price snapshot anyway
    r = con.execute("SELECT name, price FROM items WHERE id = ?", (int(d["id"]),)).fetchone()
    con.execute("UPDATE items SET active = 0 WHERE id = ?", (int(d["id"]),))
    audit(con, d, "menu_delete", amount=r and r["price"], detail=r and r["name"])
    return {"ok": True}


def save_settings(con, d):
    if "tax_pct" in d:
        check_pct(d["tax_pct"])
    if "tables" in d and not 1 <= int(d["tables"]) <= 50:
        raise ValueError("tables must be 1-50")
    for k in SECRET_SETTINGS:
        if d.get(k) and not (str(d[k]).isdigit() and len(str(d[k])) >= 6):
            raise ValueError("PIN must be 6+ digits")
    old = settings(con)
    staff, owner = str(d.get("pin") or old["pin"]), str(d.get("owner_pin") or old.get("owner_pin") or "")
    if owner and owner == staff:
        raise ValueError("the owner PIN must be different from the staff PIN")
    if d.get("printer") and not PORT_RE.fullmatch(str(d["printer"])):  # only COM ports, never a file path
        raise ValueError("printer must be a COM port like COM5")
    if "paper" in d and d["paper"] not in ("58", "80"):
        raise ValueError("paper must be 58 or 80 mm")
    changed = []
    for k in DEFAULT_SETTINGS:
        if k in d and not (k in SECRET_SETTINGS and not d[k]):  # blank PIN field = keep current
            v = str(d[k]).strip()
            if v != (old.get(k) or ""):
                changed.append(k)
            con.execute("UPDATE settings SET value = ? WHERE key = ?", (v, k))
    if changed:  # names only - PINs are never written to the log
        audit(con, d, "settings", detail=", ".join(changed))
    return {"ok": True}


def new_order(con, d):
    typ, table = d.get("type"), None
    if typ not in TYPES:
        raise ValueError("bad order type")
    if typ == "dine_in":
        table = int(d["table_no"])
        if not 1 <= table <= int(settings(con)["tables"]):
            raise ValueError("bad table number")
    cust = [str(d.get(k) or "").strip()[:300] for k in ("customer", "phone", "address")]
    if typ == "delivery" and not (cust[1] and cust[2]):
        raise ValueError("delivery needs phone and address")
    cur = con.execute("INSERT INTO orders(type, table_no, customer, phone, address, created) VALUES(?, ?, ?, ?, ?, ?)",
                      (typ, table, *cust, now()))
    log.info("Order #%s opened: %s", cur.lastrowid, f"Table {table}" if table else typ.title())
    return {"id": cur.lastrowid}


def send_kot(con, d):
    """Send items to the kitchen. With `order` (and no open order_id) the order is found or created here, so a tablet
    that was offline can resend safely: same client_id = same KOT, and a table that got an order meanwhile is joined."""
    cid = str(d.get("client_id") or "")[:64] or None
    if cid:
        r = con.execute("SELECT id, order_id FROM kots WHERE client_id = ?", (cid,)).fetchone()
        if r:
            return {"id": r["id"], "order_id": r["order_id"], "duplicate": True}
    oid = int(d["order_id"]) if d.get("order_id") else None
    if oid and d.get("order") and not con.execute("SELECT 1 FROM orders WHERE id = ? AND status = 'open'", (oid,)).fetchone():
        oid = None  # billed/cancelled while this tablet was offline: start again from the draft
    if not oid and d.get("order"):
        o = d["order"]
        ex = o.get("type") == "dine_in" and con.execute(
            "SELECT id FROM orders WHERE status = 'open' AND type = 'dine_in' AND table_no = ?", (int(o["table_no"]),)).fetchone()
        oid = ex["id"] if ex else new_order(con, o)["id"]
    if not oid or not con.execute("SELECT 1 FROM orders WHERE id = ? AND status = 'open'", (oid,)).fetchone():
        raise ValueError("order is not open")
    lines = []
    for l in d.get("lines") or []:
        qty = int(l["qty"])
        if not 1 <= qty <= 999:
            raise ValueError("invalid quantity")
        if l.get("id") is None:  # custom item typed at the counter: billed and printed, never added to the menu
            name, price = str(l.get("name") or "").strip()[:60], paise(l["price"])
            if not name or price < 0:
                raise ValueError("custom item needs a name and a price")
            lines.append((None, name, price, qty))
            continue
        r = con.execute("SELECT id, name, price FROM items WHERE id = ? AND active = 1", (int(l["id"]),)).fetchone()
        if not r:
            raise ValueError("invalid item")
        lines.append((r["id"], r["name"], r["price"], qty))
    if not lines:
        raise ValueError("no items")
    kid = con.execute("INSERT INTO kots(order_id, created, note, client_id) VALUES(?, ?, ?, ?)",
                      (oid, now(), str(d.get("note") or "").strip()[:200], cid)).lastrowid
    con.executemany("INSERT INTO lines(order_id, kot_id, item_id, name, price, qty) VALUES(?, ?, ?, ?, ?, ?)",
                    [(oid, kid, *l) for l in lines])
    log.info("KOT %s -> kitchen for order #%s: %s", kid, oid,
             ", ".join(f"{q}x {n}" + ("" if i else f" (custom, {rs(p)})") for i, n, p, q in lines))
    return {"id": kid, "order_id": oid}


def remove_line(con, d):
    """Take an item that was already sent to the kitchen off an open order (a void): owner PIN + reason, logged."""
    reason = need_reason(d)
    l = con.execute("SELECT l.* FROM lines l JOIN orders o ON o.id = l.order_id WHERE l.id = ? AND o.status = 'open'",
                    (int(d["line_id"]),)).fetchone()
    if not l:
        raise ValueError("item not found or order already billed")
    con.execute("DELETE FROM lines WHERE id = ?", (l["id"],))
    audit(con, d, "remove_item", l["order_id"], l["price"] * l["qty"], reason, f"{l['qty']} x {l['name']}")
    return {"ok": True}


def cancel_order(con, d):
    reason = need_reason(d)
    o = get_order(con, int(d["order_id"])) if con.execute("SELECT 1 FROM orders WHERE id = ?", (int(d["order_id"]),)).fetchone() else None
    if not o or not con.execute("UPDATE orders SET status = 'cancelled', day = ? WHERE id = ? AND status = 'open'",
                                (business_day(con), o["id"])).rowcount:
        raise ValueError("order is not open")
    audit(con, d, "cancel_order", o["id"], o["amount"], reason,
          ", ".join(f"{l['qty']} x {l['name']}" for l in o["lines"]) or "no items")
    return {"ok": True}


def kot_ready(con, d):
    con.execute("UPDATE kots SET status = 'ready', ready_at = ? WHERE id = ?", (now(), int(d["id"])))
    log.info("KOT %s ready", d["id"])
    return {"ok": True}


def kot_undo(con, d):
    """Kitchen tapped "ready" by mistake: the ticket goes back to cooking."""
    if not con.execute("UPDATE kots SET status = 'new', ready_at = NULL WHERE id = ? AND status = 'ready'",
                       (int(d["id"]),)).rowcount:
        raise ValueError("ticket not found or already back in the kitchen")
    log.info("KOT %s back to cooking (by %s)", d["id"], (d.get("_who") or {}).get("name", "?"))
    return {"ok": True}


def list_profiles(con, q):
    rows = con.execute("SELECT id, name, slug, role, perms, created FROM profiles WHERE active = 1 ORDER BY role, id")
    return {"profiles": [{**dict(r), "perms": sorted(profile_perms(r))} for r in rows],
            "perms": PERMS, "staff_default": STAFF_DEFAULT, "max": MAX_PROFILES}


def save_profile(con, d):
    """Create or change a profile. Owners only. PINs are unique, 6+ digits, and stored hashed."""
    s, pid = settings(con), int(d["id"]) if d.get("id") else None
    name = re.sub(r"\s+", " ", str(d.get("name") or "")).strip()[:30]
    role = d.get("role")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", name or "-"):
        raise ValueError("name: letters and numbers only (e.g. Sai)")
    if role not in MAX_PROFILES:
        raise ValueError("pick owner or staff")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + "_" + role
    perms = sorted((set(d.get("perms") or []) & set(PERMS)) | ({"profiles"} if role == "owner" else set()))
    if role == "staff":
        perms = [p for p in perms if p != "profiles"]
    pin = str(d.get("pin") or "")
    if pin and not (pin.isdigit() and 6 <= len(pin) <= 12):
        raise ValueError("PIN must be 6 to 12 digits")
    if not pid and not pin:
        raise ValueError("a new profile needs a PIN")
    old = con.execute("SELECT * FROM profiles WHERE id = ? AND active = 1", (pid,)).fetchone() if pid else None
    if pid and not old:
        raise ValueError("profile not found - refresh")
    count = con.execute("SELECT COUNT(*) FROM profiles WHERE active = 1 AND role = ? AND id != ?", (role, pid or 0)).fetchone()[0]
    if count >= MAX_PROFILES[role]:
        raise ValueError(f"at most {MAX_PROFILES[role]} {role} profiles")
    if old and old["role"] == "owner" and role != "owner" and not con.execute(
            "SELECT 1 FROM profiles WHERE active = 1 AND role = 'owner' AND id != ?", (pid,)).fetchone():
        raise ValueError("keep at least one owner profile")
    if con.execute("SELECT 1 FROM profiles WHERE active = 1 AND slug = ? AND id != ?", (slug, pid or 0)).fetchone():
        raise ValueError(f"there is already a {role} called {name}")
    if pin and con.execute("SELECT 1 FROM profiles WHERE active = 1 AND pin_hash = ? AND id != ?",
                           (pin_hash(s, pin), pid or 0)).fetchone():
        raise ValueError("that PIN is already used by another profile - pick a different one")
    if old:
        con.execute("UPDATE profiles SET name = ?, slug = ?, role = ?, perms = ?" + (", pin_hash = ?" if pin else "")
                    + " WHERE id = ?", (name, slug, role, json.dumps(perms), *([pin_hash(s, pin)] if pin else []), pid))
    else:
        pid = con.execute("INSERT INTO profiles(name, slug, role, pin_hash, perms, created) VALUES(?, ?, ?, ?, ?, ?)",
                          (name, slug, role, pin_hash(s, pin), json.dumps(perms), now())).lastrowid
    audit(con, d, "profile_save", detail=f"{name} ({role})" + (" - changed" if old else " - created")
          + (", new PIN" if pin and old else "") + ": " + ", ".join(perms))
    return {"ok": True, "id": pid, "slug": slug}


def delete_profile(con, d):
    r = con.execute("SELECT * FROM profiles WHERE id = ? AND active = 1", (int(d["id"]),)).fetchone()
    if not r:
        raise ValueError("profile not found - refresh")
    if r["slug"] == (d.get("_who") or {}).get("slug"):
        raise ValueError("you can't delete the profile you are signed in with")
    others = con.execute("SELECT COUNT(*) FROM profiles WHERE active = 1 AND id != ?", (r["id"],)).fetchone()[0]
    if r["role"] == "owner" and others and not con.execute(
            "SELECT 1 FROM profiles WHERE active = 1 AND role = 'owner' AND id != ?", (r["id"],)).fetchone():
        raise ValueError("keep at least one owner profile")
    con.execute("UPDATE profiles SET active = 0 WHERE id = ?", (r["id"],))  # kept for the audit history
    audit(con, d, "profile_delete", detail=f"{r['name']} ({r['role']})")
    return {"ok": True}


def parse_payments(d, total):
    """[(method, paise)] adding up to the bill. One `payment` = the whole bill; `payments` = split
    (e.g. Cash 500 + UPI the rest, or one row per person when a table splits the bill)."""
    if not d.get("payments"):
        if d.get("payment") not in PAYMENTS:
            raise ValueError("pick a payment mode")
        return [(d["payment"], total)] if total else []
    out = []
    for p in d["payments"][:20]:
        m, a = p.get("method"), paise(p.get("amount") or 0)
        if m not in PAYMENTS or a <= 0:
            raise ValueError("each payment needs a mode and an amount above 0")
        out.append((m, a))
    if sum(a for _, a in out) != total:
        raise ValueError(f"payments add up to {rs(sum(a for _, a in out))} but the bill is {rs(total)}")
    return out


def bill_order(con, d):
    """Bill an order. `line_ids` bills only those items (split by items): they move to their own bill and the
    rest stays open on the table. Discounts need the owner PIN and are logged."""
    oid = int(d["order_id"])
    o = con.execute("SELECT * FROM orders WHERE id = ? AND status = 'open'", (oid,)).fetchone()
    if not o:
        raise ValueError("order is not open")
    all_ids = [r[0] for r in con.execute("SELECT id FROM lines WHERE order_id = ?", (oid,))]
    if not all_ids:
        raise ValueError("order has no items")
    pick = sorted({int(x) for x in d.get("line_ids") or []})
    if pick and not set(pick) <= set(all_ids):
        raise ValueError("some items are not on this order any more - refresh")
    split = bool(pick) and len(pick) < len(all_ids)
    ids = pick if split else all_ids
    lines = [(r["price"], r["qty"]) for r in con.execute(
        f"SELECT price, qty FROM lines WHERE id IN ({','.join('?' * len(ids))})", ids)]
    tax_pct, disc = settings(con)["tax_pct"], check_pct(d.get("discount_pct"))
    amt = paise(d.get("discount_amt") or 0)  # flat ₹ discount; when used, discount_pct stays "0"
    if amt:
        disc = "0"
        if not 0 < amt <= sum(p * q for p, q in lines):
            raise ValueError("rupee discount must be more than 0 and not more than the bill")
    if amt or Decimal(disc):
        need_perm(d, "discount", "A discount")
    sub, dis, cgst, sgst, ro, total = totals(lines, disc, tax_pct, amt)
    pays = parse_payments(d, total)
    payment = "+".join(dict.fromkeys(m for m, _ in pays)) or "Cash"  # "Cash", or "Cash+UPI" for a split
    if split:  # everything checked - the part being paid becomes its own order (table_no NULL for a moment)
        part = con.execute("INSERT INTO orders(type, table_no, customer, phone, address, created) VALUES(?, NULL, ?, ?, ?, ?)",
                           (o["type"], o["customer"], o["phone"], o["address"], o["created"])).lastrowid
        con.execute(f"UPDATE lines SET order_id = ? WHERE id IN ({','.join('?' * len(pick))})", (part, *pick))
        log.info("Split bill: %d of %d items of order #%s billed separately as #%s", len(pick), len(all_ids), oid, part)
        oid = part
    t, day = now(), business_day(con)
    # numbers assigned inside the UPDATE so two tablets billing at once can't get the same one;
    # bill_no = lifetime sequence (internal), day_no = 1, 2, 3... restarting every business day
    if not con.execute(
            "UPDATE orders SET status = 'billed', bill_no = (SELECT COALESCE(MAX(bill_no), 0) + 1 FROM orders), "
            "day_no = (SELECT COALESCE(MAX(day_no), 0) + 1 FROM orders WHERE day = ?), "
            "billed = ?, day = ?, payment = ?, subtotal = ?, discount_pct = ?, discount = ?, tax_pct = ?, "
            "cgst = ?, sgst = ?, round_off = ?, total = ? WHERE id = ? AND status = 'open'",
            (day, t, day, payment, sub, disc, dis, tax_pct, cgst, sgst, ro, total, oid)).rowcount:
        raise ValueError("order is not open")
    con.execute("UPDATE orders SET table_no = ? WHERE id = ?", (o["table_no"], oid))  # split part: back on its table
    con.executemany("INSERT INTO payments(order_id, method, amount) VALUES(?, ?, ?)", [(oid, m, a) for m, a in pays])
    b = get_order(con, oid)
    if dis:
        audit(con, d, "discount", oid, dis, d.get("reason") or None,
              f"bill {b['bill_label']}: " + (f"{disc}% off" if Decimal(disc) else f"{rs(dis)} off") + f" {rs(sub)}")
    log.info("BILL %s: %s paid by %s (order #%s)", b["bill_label"], rs(total),
             " + ".join(f"{m} {rs(a)}" for m, a in pays) or "-", oid)
    return b


def void_bill(con, d):
    """Cancel a printed bill (wrong items, wrong table...). It keeps its number for GST but leaves the totals.
    With reopen, the same items come back as an open order to correct and bill again."""
    reason = need_reason(d)
    r = con.execute("SELECT * FROM orders WHERE id = ? AND status = 'billed' AND voided IS NULL", (int(d["order_id"]),)).fetchone()
    if not r:
        raise ValueError("bill not found or already voided")
    b = order_out(con, r)
    if b["refunded"]:
        raise ValueError("this bill already has a refund - it can't be voided")
    con.execute("UPDATE orders SET voided = ?, void_reason = ? WHERE id = ?", (now(), reason, r["id"]))
    new = None
    if d.get("reopen"):  # table already has another open order -> IntegrityError, and nothing is changed
        new = con.execute("INSERT INTO orders(type, table_no, customer, phone, address, created) VALUES(?, ?, ?, ?, ?, ?)",
                          (r["type"], r["table_no"], r["customer"], r["phone"], r["address"], now())).lastrowid
        kid = con.execute("INSERT INTO kots(order_id, created, note, status) VALUES(?, ?, ?, 'ready')",
                          (new, now(), f"Reopened from bill {b['bill_label']}")).lastrowid  # not sent to the kitchen again
        con.executemany("INSERT INTO lines(order_id, kot_id, item_id, name, price, qty) SELECT ?, ?, item_id, name, price, qty "
                        "FROM lines WHERE order_id = ? AND id = ?", [(new, kid, r["id"], l["id"]) for l in b["lines"]])
    audit(con, d, "void_bill", r["id"], r["total"], reason,
          f"bill {b['bill_label']}" + (f", reopened as order #{new}" if new else ""))
    return {"ok": True, "reopened": new}


def refund_bill(con, d):
    """Give money back on a bill (full or part). Counted on today's business day, by payment mode."""
    reason, amount, method = need_reason(d), paise(d.get("amount") or 0), d.get("method")
    if method not in PAYMENTS:
        raise ValueError("pick how the money is given back")
    r = con.execute("SELECT * FROM orders WHERE id = ? AND status = 'billed' AND voided IS NULL", (int(d["order_id"]),)).fetchone()
    if not r:
        raise ValueError("bill not found or voided")
    left = r["total"] - con.execute("SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE order_id = ?", (r["id"],)).fetchone()[0]
    if not 0 < amount <= left:
        raise ValueError(f"refund must be more than 0 and at most {rs(left)}")
    con.execute("INSERT INTO refunds(order_id, at, day, amount, method, reason) VALUES(?, ?, ?, ?, ?, ?)",
                (r["id"], now(), business_day(con), amount, method, reason))
    audit(con, d, "refund", r["id"], amount, reason,
          f"bill {bill_label(r['day'], r['day_no']) if r['day_no'] else r['bill_no']}, by {method}")
    return {"ok": True}


def close_day(con, d):
    """End-of-day: lock in the daily report and move billing to the next business day."""
    day = business_day(con)
    if d.get("day") != day:
        raise ValueError("this day is already closed - refresh the page")
    still_open = con.execute("SELECT COUNT(*) FROM orders WHERE status = 'open'").fetchone()[0]
    if still_open:
        raise ValueError(f"{still_open} order(s) still open - bill or cancel them before closing the day")
    nxt = max(date.fromisoformat(day) + timedelta(days=1), date.today()).isoformat()  # forgot to close? skip to today
    # conditional UPDATE: if two tablets press Close at once, only one wins
    if not con.execute("UPDATE settings SET value = ? WHERE key = 'business_day' AND value = ?", (nxt, day)).rowcount:
        raise ValueError("this day is already closed - refresh the page")
    r = report(con, {"day": [day]})
    audit(con, d, "close_day", amount=r["net"], detail=f"{day}: {r['count']} bills, next day {nxt}", day=day)
    log.info("DAY CLOSED %s: %d bills, net %s. Next business day: %s", day, r["count"], rs(r["net"]), nxt)
    if AFTER_CLOSE_DAY:  # e.g. Google Drive backup; a few seconds later, once this transaction is saved
        threading.Timer(5, AFTER_CLOSE_DAY).start()
    return {"report": r, "next": nxt}


def backup(con, q):
    """Consistent snapshot of the whole database, safe to take while the café is billing."""
    snap = sqlite3.connect(":memory:")
    con.backup(snap)
    data = snap.serialize()
    snap.close()
    log.info("Backup downloaded (%d KB)", len(data) // 1024)
    return data


# ---------------- direct printing: ESC/POS thermal printer on a COM port (Bluetooth or USB) ----------------
PORT_RE = re.compile(r"COM\d{1,3}")
PRINT_LOCK = threading.Lock()


class PrinterError(Exception):
    pass


def printer_ports(con=None, q=None):
    """COM ports Windows knows about; Bluetooth printers show up as \\Device\\BthModemN."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except (ImportError, OSError):
        return {"ports": []}  # not Windows, or no COM ports
    ports, i = [], 0
    while True:
        try:
            dev, port, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        ports.append({"port": port, "bluetooth": "Bth" in dev, "name": bt_names(winreg).get(port, "")})
        i += 1
    # an "incoming" Bluetooth port (no device behind it) can never be a printer
    ports = [p for p in ports if not (p["bluetooth"] and p["name"] == "incoming")]
    return {"ports": sorted(ports, key=lambda p: int(p["port"][3:]) if p["port"][3:].isdigit() else 0)}


def bt_names(winreg):
    """COM port -> paired device name, so the list says "COM6 EZO Chota" instead of a bare COM6."""
    hk, out = winreg.HKEY_LOCAL_MACHINE, {}
    try:
        enum = winreg.OpenKey(hk, r"SYSTEM\CurrentControlSet\Enum\BTHENUM")
    except OSError:
        return out
    for i in range(1024):
        try:
            svc = winreg.EnumKey(enum, i)
        except OSError:
            break
        if not svc.upper().startswith("{00001101"):  # serial port profile only
            continue
        try:
            sk = winreg.OpenKey(enum, svc)
            for j in range(64):
                inst = winreg.EnumKey(sk, j)
                try:
                    port = winreg.QueryValueEx(winreg.OpenKey(sk, inst + r"\Device Parameters"), "PortName")[0]
                except OSError:
                    continue
                addr = inst.split("&")[-1].split("_")[0].lower()
                if addr == "000000000000":
                    out[port] = "incoming"
                    continue
                try:
                    raw = winreg.QueryValueEx(winreg.OpenKey(
                        hk, r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices" + "\\" + addr), "Name")[0]
                    out[port] = bytes(raw).split(b"\0")[0].decode("utf-8", "ignore")
                except OSError:
                    pass
        except OSError:
            continue
    return out


def amt(p):
    return ("-" if p < 0 else "") + f"{abs(p) / 100:,.2f}"


class Ticket:
    """Plain-text receipt for 58 mm (32 columns) or 80 mm (48 columns) ESC/POS printers."""

    def __init__(self, width):
        self.w, self.out = width, bytearray(b"\x1b@")  # ESC @ = reset printer

    def line(self, s="", bold=False, center=False, big=False):
        s = unicodedata.normalize("NFKD", str(s).replace("₹", "Rs.").replace("−", "-").replace("×", "x"))
        s = s.encode("ascii", "ignore")  # printer fonts are ASCII; "Café" -> "Cafe"
        big = big and len(s) <= self.w // 2  # double size only if it still fits on one line
        self.out += b"\x1ba" + (b"\x01" if center else b"\x00") + b"\x1bE" + (b"\x01" if bold else b"\x00")
        self.out += b"\x1d!" + (b"\x11" if big else b"\x00") + s + b"\n"
        return self

    def row(self, left, right="", bold=False):
        left, right = str(left), str(right)
        if len(left) + len(right) + 1 > self.w:  # too long: right-hand value on its own line
            return self.line(left, bold).line(right.rjust(self.w), bold)
        return self.line(left + " " * (self.w - len(left) - len(right)) + right, bold)

    def rule(self):
        return self.line("-" * self.w)

    def end(self, cut):
        self.out += b"\x1bd\x04" + (b"\x1dV\x01" if cut else b"")  # feed 4 lines (+ cut on 80 mm cutters)
        return bytes(self.out)


def header(t, s):
    t.line(s["name"], bold=True, center=True, big=True)
    for k, label in (("address", ""), ("phone", "Ph: "), ("gstin", "GSTIN: "), ("fssai", "FSSAI: ")):
        if s.get(k):
            t.line(label + s[k], center=True)
    return t


def bill_ticket(b, s, w):
    t = header(Ticket(w), s).line("TAX INVOICE", bold=True, center=True)
    if b.get("voided"):
        t.line("*** VOID - NOT PAYABLE ***", bold=True, center=True)
    t.rule()
    t.row("Bill No:", b["bill_label"]).row("Date:", datetime.fromisoformat(b["billed"]).strftime("%d/%m/%y %I:%M %p"))
    t.row({"dine_in": f"Dine-in, Table {b['table_no']}", "takeaway": "Takeaway", "delivery": "Delivery"}[b["type"]],
          "Paid: " + b["payment"])
    if b.get("customer") or b.get("phone"):
        t.line(f"{b.get('customer') or ''} {b.get('phone') or ''}".strip())
    if b.get("address"):
        t.line(b["address"])
    t.rule()
    for l in b["lines"]:
        t.line(l["name"]).row(f"  {l['qty']} x {amt(l['price'])}", amt(l["qty"] * l["price"]))
    t.rule().row("Subtotal", amt(b["subtotal"]))
    if b["discount"]:
        t.row(f"Discount ({b['discount_pct']}%)" if Decimal(b["discount_pct"] or 0) else "Discount", amt(-b["discount"]))
    h = Decimal(b["tax_pct"]) / 2
    t.row(f"CGST @ {h}%", amt(b["cgst"])).row(f"SGST @ {h}%", amt(b["sgst"]))
    if b["round_off"]:
        t.row("Round off", amt(b["round_off"]))
    t.rule().row("TOTAL Rs.", amt(b["total"]), bold=True).rule()
    if len(b.get("payments") or []) > 1:  # split payment: show each part
        for p in b["payments"]:
            t.row(f"  Paid {p['method']}", amt(p["amount"]))
        t.rule()
    if s.get("footer"):
        t.line(s["footer"], center=True)
    return t.end(cut=w >= 48)


def day_ticket(r, s, w):
    sm, h = r["sums"], Decimal(s["tax_pct"]) / 2
    t = header(Ticket(w), s).line("DAILY SALES REPORT", bold=True, center=True)
    t.line(date.fromisoformat(r["day"]).strftime("%a %d %b %Y"), center=True)
    t.line(("Day still open" if r["open"] else "Day closed") + " - printed " + datetime.now().strftime("%d/%m %I:%M %p"), center=True)
    t.rule().row("Bills", r["count"]).row("Cancelled orders", r["cancelled"]).rule()
    t.row("Gross", amt(sm["subtotal"])).row("Discounts", amt(-sm["discount"]))
    t.row(f"CGST @ {h}%", amt(sm["cgst"])).row(f"SGST @ {h}%", amt(sm["sgst"]))
    if sm["round_off"]:  # only bills from before v4 have a round-off
        t.row("Round off", amt(sm["round_off"]))
    if r.get("voided", {}).get("count"):
        t.row(f"Voided bills ({r['voided']['count']})", amt(r["voided"]["total"]))
    if r.get("refunds"):
        t.row("Bills total", amt(sm["total"])).row("Refunds", amt(-r["refunds"]))
    t.rule().row("NET SALES Rs.", amt(r.get("net", sm["total"])), bold=True).rule().line("By payment", bold=True)
    for k, v in r["by_payment"].items():
        t.row(k, amt(v))
    t.rule().line("By order type", bold=True)
    for k, v in r["by_type"].items():
        t.row({"dine_in": "Dine-in", "takeaway": "Takeaway", "delivery": "Delivery"}[k], amt(v))
    t.rule().line("Items sold", bold=True)
    for n, q, a in r["items"]:
        t.row(f"{q} x {n}", amt(a))
    return t.rule().line("--- end of report ---", center=True).end(cut=w >= 48)


def send_to_printer(port, data, timeout=25):
    """Write raw ESC/POS bytes to the printer's COM port; Bluetooth connects on open, so allow some seconds."""
    err = []

    def run():
        try:
            with open(rf"\\.\{port}", "wb", buffering=0) as f:
                f.write(data)
        except OSError as e:
            err.append(e)
    with PRINT_LOCK:  # one print job at a time
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout)
    if th.is_alive():
        raise PrinterError(f"The printer on {port} did not answer. Is it switched on, charged and near the PC?")
    if err:
        raise PrinterError(f"Could not print on {port}: {err[0].strerror or err[0]}. Is the printer on and paired?")


def print_job(con, d):
    s = settings(con)
    port, w = s.get("printer") or "", 32 if s.get("paper", "58") == "58" else 48
    phone = d.get("phone")  # the phone prints it itself (RawBT app); just hand back the ESC/POS bytes
    if not phone and not PORT_RE.fullmatch(port):
        raise ValueError("no printer chosen - pick one in Settings")
    if d.get("bill"):
        r = con.execute("SELECT * FROM orders WHERE id = ? AND status = 'billed'", (int(d["bill"]),)).fetchone()
        if not r:
            raise ValueError("bill not found")
        b = order_out(con, r)
        data, what = bill_ticket(b, s, w), f"bill {b['bill_label']}"
    elif d.get("day"):
        if not (can(d, "reports") or can(d, "close_day")):
            raise Forbidden("The day report needs a profile that may see reports")
        data, what = day_ticket(report(con, {"day": [str(d["day"])]}), s, w), f"day report {d['day']}"
    elif d.get("test"):
        t = header(Ticket(w), s).line("Printer test", bold=True, center=True).rule()
        data, what = t.row("Paper", f"{s.get('paper')} mm").row("Port", port or "phone").row("Sample", amt(12345)).rule().end(w >= 48), "test page"
    else:
        raise ValueError("nothing to print")
    if phone:
        return {"escpos": base64.b64encode(data).decode()}
    send_to_printer(port, data)
    log.info("Printed %s on %s", what, port)
    return {"ok": True}


GET = {"/api/state": state, "/api/kitchen": kitchen, "/api/report": report, "/api/backup": backup,
       "/api/printers": printer_ports, "/api/audit": audit_log, "/api/dashboard": dashboard, "/api/profiles": list_profiles}
POST = {"/api/items": save_item, "/api/items/delete": delete_item, "/api/settings": save_settings,
        "/api/orders/new": new_order, "/api/orders/kot": send_kot, "/api/orders/remove_line": remove_line,
        "/api/orders/cancel": cancel_order, "/api/orders/bill": bill_order, "/api/kots/ready": kot_ready,
        "/api/day/close": close_day, "/api/print": print_job, "/api/bills/void": void_bill, "/api/bills/refund": refund_bill,
        "/api/kots/undo": kot_undo, "/api/profiles/save": save_profile, "/api/profiles/delete": delete_profile}
# what each request needs (discounts and the day report are checked inside bill_order / print_job)
ROUTE_PERMS = {"/api/kitchen": "kitchen", "/api/report": "reports", "/api/audit": "reports", "/api/backup": "settings",
               "/api/printers": "settings", "/api/dashboard": "dashboard", "/api/items": "menu", "/api/items/delete": "menu",
               "/api/settings": "settings", "/api/orders/new": "orders", "/api/orders/kot": "orders",
               "/api/orders/remove_line": "remove_item", "/api/orders/cancel": "cancel", "/api/orders/bill": "bill",
               "/api/kots/ready": "kitchen", "/api/kots/undo": "kitchen", "/api/day/close": "close_day",
               "/api/bills/void": "void_refund", "/api/bills/refund": "void_refund", "/api/profiles": "profiles",
               "/api/profiles/save": "profiles", "/api/profiles/delete": "profiles"}


# ponytail: one global lockout, so guessing a 6-digit PIN online takes months; an attacker can also keep
# staff locked out - switch to per-IP lockout if that ever happens.
LOCK = {"fails": 0, "until": 0.0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # tablets poll every few seconds; keep the console readable
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if ctype == "application/octet-stream":
            self.send_header("Content-Disposition", f'attachment; filename="pos-backup-{date.today()}.db"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path.startswith("/profile="):  # each profile has its own link to save
            return self.send((RES / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        self.api(GET.get(u.path), parse_qs(u.query))

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send({"error": "bad request"}, 400)
        if not isinstance(data, dict):
            return self.send({"error": "bad request"}, 400)
        self.api(POST.get(self.path), data)

    def api(self, route, arg):
        if not route:
            return self.send({"error": "not found"}, 404)
        if time.time() < LOCK["until"]:
            return self.send({"error": "Too many wrong PINs. Wait a minute and try again."}, 429)
        con = db()
        try:
            slug = self.headers.get("X-Profile", "")[:60] or None
            who = who_for(con, settings(con), self.headers.get("X-Pin", ""), slug)
            if not who:
                LOCK["fails"] += 1
                if self.headers.get("X-Pin"):  # a blank PIN is just a tablet's first visit
                    # behind Tailscale Funnel every visitor is 127.0.0.1; the proxy passes the real one along
                    log.warning("Wrong PIN from %s", self.headers.get("X-Forwarded-For") or self.client_address[0])
                if LOCK["fails"] >= 5:  # 5 wrong in a row -> everyone locked out for 60 s
                    LOCK.update(fails=0, until=time.time() + 60)
                    log.warning("5 wrong PINs in a row - POS locked for 60 seconds")
                time.sleep(1)
                return self.send({"error": "Wrong PIN"}, 401)
            LOCK["fails"] = 0
            path = urlparse(self.path).path
            perm = ROUTE_PERMS.get(path)
            if perm and perm not in who["perms"]:
                return self.send({"error": f"{who['name']} can't do this ({PERMS[perm].lower()}). "
                                           "Ask an owner to approve it or to change your profile.", "perm": perm}, 403)
            arg["_who"] = {**who, "device": self.headers.get("X-Device", "")[:40] or None,
                           "for": self.headers.get("X-For", "")[:60] or None,
                           "ip": (self.headers.get("X-Forwarded-For") or self.client_address[0])[:60]}
            with con:  # one transaction per request, rolled back on any error
                out = route(con, arg)
            self.send(out, ctype="application/octet-stream" if isinstance(out, bytes) else "application/json")
        except Forbidden as e:
            self.send({"error": f"{e} - ask an owner to approve it.", "perm": True}, 403)
        except PrinterError as e:
            log.error("Print failed: %s", e)
            self.send({"error": str(e)}, 503)
        except sqlite3.IntegrityError as e:
            self.send({"error": "That PIN or name is already used." if "profiles" in str(e)
                       else "That table already has an open order. Refresh and try again."}, 409)
        except (ValueError, KeyError, TypeError, InvalidOperation) as e:
            log.warning("Rejected %s: %s", self.path, e)
            self.send({"error": f"Invalid input: {e}"}, 400)
        except Exception:  # never let one bad request kill the server silently (exe has no console)
            log.exception("Error handling %s", self.path)
            self.send({"error": "Server error - see the POS log window."}, 500)
        finally:
            con.close()


def lan_ips():
    # ponytail: lists every address (Wi-Fi, Ethernet, VPN...) since stdlib can't tell which adapter is Wi-Fi
    ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254."))) or ["THIS-PC-IP"]


def selftest():
    two = [(10000, 2), (4550, 1)]
    assert totals(two, 0, 5) == (24550, 0, 614, 614, 0, 25778)        # 613.75 -> 614 each, no round-off
    assert totals(two, 10, 5) == (24550, 2455, 552, 552, 0, 23199)    # GST on discounted amount
    assert totals(two, 0, 18) == (24550, 0, 2210, 2210, 0, 28970)
    assert totals([(4949, 1)], 0, 0) == (4949, 0, 0, 0, 0, 4949)
    assert totals(two, 0, 5, 5000) == (24550, 5000, 489, 489, 0, 20528)  # flat ₹50 off, GST on the rest
    assert paise("249.50") == 24950 and paise("0.005") == 1

    con = db(":memory:")
    init(con)
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 100  # menu.csv loaded
    with con:
        save_item(con, {"iname": "Paneer Tikka", "icat": "Starters", "iprice": "249.50"})
        pid = con.execute("SELECT id FROM items WHERE name = 'Paneer Tikka'").fetchone()[0]
        oid = new_order(con, {"type": "dine_in", "table_no": 1})["id"]
        try:
            new_order(con, {"type": "dine_in", "table_no": 1})
            raise AssertionError("table double-booked")
        except sqlite3.IntegrityError:
            pass
        send_kot(con, {"order_id": oid, "lines": [{"id": pid, "qty": 2}], "note": "less spicy"})
        k = kitchen(con, {})["kots"][0]
        assert k["lines"] == [{"name": "Paneer Tikka", "qty": 2}] and k["note"] == "less spicy"
        b = bill_order(con, {"order_id": oid, "payment": "UPI", "discount_pct": 0})
        assert (b["bill_no"], b["subtotal"], b["cgst"], b["round_off"], b["total"]) == (1, 49900, 1248, 0, 52396)
        assert b["day_no"] == 1 and b["bill_label"] == bill_label(b["day"], 1)
        try:
            bill_order(con, {"order_id": oid, "payment": "UPI"})
            raise AssertionError("billed twice")
        except ValueError:
            pass
        assert b["day"] == business_day(con) == date.today().isoformat()
        o2 = new_order(con, {"type": "dine_in", "table_no": 1})["id"]  # table is free again after billing
        r = report(con, {"day": [b["day"]]})
        assert r["count"] == 1 and r["by_payment"] == {"UPI": 52396} and r["items"] == [["Paneer Tikka", 2, 49900]]
        assert r["open"]
        try:
            close_day(con, {"day": b["day"]})
            raise AssertionError("closed a day with an open order")
        except ValueError:
            pass
        items_before = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        send_kot(con, {"order_id": o2, "lines": [{"id": pid, "qty": 1}, {"name": "Birthday Cake", "price": "350", "qty": 1}]})
        assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == items_before  # custom item not added to menu
        try:
            bill_order(con, {"order_id": o2, "payment": "Cash", "discount_amt": "1000"})  # more than the bill
            raise AssertionError("discount bigger than the bill")
        except ValueError:
            pass
        b2 = bill_order(con, {"order_id": o2, "payment": "Cash", "discount_amt": "50"})
        assert b2["day_no"] == 2  # same day -> 2
        assert [l["name"] for l in b2["lines"]] == ["Paneer Tikka", "Birthday Cake"]
        assert (b2["subtotal"], b2["discount"], b2["discount_pct"]) == (24950 + 35000, 5000, "0")
        o2 = new_order(con, {"type": "takeaway"})["id"]
        try:
            cancel_order(con, {"order_id": o2})
            raise AssertionError("cancelled without a reason")
        except ValueError:
            pass
        cancel_order(con, {"order_id": o2, "reason": "customer left"})
        closed = close_day(con, {"day": b["day"]})
        assert closed["report"]["count"] == 2 and closed["report"]["cancelled"] == 1
        assert business_day(con) == closed["next"] == (date.today() + timedelta(days=1)).isoformat()
        try:
            close_day(con, {"day": b["day"]})  # second tablet pressing Close on the same day
            raise AssertionError("closed the same day twice")
        except ValueError:
            pass
        assert not report(con, {"day": [b["day"]]})["open"]
        o3 = new_order(con, {"type": "takeaway"})["id"]  # next business day: numbering starts again at 1
        send_kot(con, {"order_id": o3, "lines": [{"id": pid, "qty": 1}]})
        b3 = bill_order(con, {"order_id": o3, "payment": "Cash"})
        assert (b3["day_no"], b3["bill_no"]) == (1, 3) and b3["bill_label"] != b["bill_label"]
    # printed receipt fits a 58 mm roll (32 columns) and carries the right totals
    s = settings(con)
    for w in (32, 48):
        tk = bill_ticket(b2, s, w)
        txt = re.sub(rb"\x1b[aE@d].?|\x1d[!V].?", b"", tk).decode()  # strip ESC/POS control codes
        assert all(len(x) <= w for x in txt.splitlines()), [x for x in txt.splitlines() if len(x) > w]
        assert "TAX INVOICE" in txt and "Birthday Cake" in txt and "Guns & Roses Cafe" in txt
        assert amt(b2["total"]) in txt and "Discount" in txt
    assert b"NET SALES" in day_ticket(report(con, {"day": [b2["day"]]}), s, 32)
    # phone printing (RawBT) needs no COM port and gets the same receipt bytes back
    assert base64.b64decode(print_job(con, {"bill": b2["id"], "phone": True})["escpos"]) == bill_ticket(b2, settings(con), 32)
    for bad in ("C:\\pos.db", "..\\x", "LPT1", "COM1;x"):
        try:
            save_settings(con, {"printer": bad})
            raise AssertionError(f"accepted printer {bad!r}")
        except ValueError:
            pass
    # ---- v3: owner/staff PINs, audit trail, split bills & payments, voids, refunds, offline resends, dashboard ----
    staff = {"_who": {"role": "staff", "name": "Staff", "slug": None, "device": "T1", "perms": set(LEGACY_STAFF)}}
    owner = {"_who": {"role": "owner", "name": "Owner", "slug": None, "device": "Counter", "perms": set(PERMS)}}
    with con:
        save_settings(con, {**owner, "owner_pin": "987654"})
        s = settings(con)
        role = lambda pin: (who_for(con, s, pin) or {}).get("role")
        assert role("987654") == "owner" and role("1234") == "staff" and role("1") is None
        try:
            save_settings(con, {"pin": "555555", "owner_pin": "555555"})
            raise AssertionError("owner PIN same as staff PIN")
        except ValueError:
            pass
        assert "owner_pin" not in state(con, staff)["settings"] and state(con, staff)["role"] == "staff"
        # offline resend: the same client_id twice makes one KOT; a draft for a busy table joins its order
        k1 = send_kot(con, {**staff, "client_id": "tab1-1", "order": {"type": "dine_in", "table_no": 2},
                            "lines": [{"id": pid, "qty": 3}]})
        again = send_kot(con, {**staff, "client_id": "tab1-1", "order": {"type": "dine_in", "table_no": 2},
                               "lines": [{"id": pid, "qty": 3}]})
        assert again["duplicate"] and again["id"] == k1["id"]
        k2 = send_kot(con, {**staff, "client_id": "tab2-1", "order": {"type": "dine_in", "table_no": 2},
                            "lines": [{"name": "Water", "price": "20", "qty": 3}]})
        oid4 = k2["order_id"]
        assert oid4 == k1["order_id"] and len(get_order(con, oid4)["lines"]) == 2
        try:
            bill_order(con, {**staff, "order_id": oid4, "payment": "Cash", "discount_pct": 10})
            raise AssertionError("staff gave a discount")
        except Forbidden:
            pass
        # split by items + split payment: Paneer paid now (Cash 500 + UPI rest), Water stays on table 2
        paneer = [l["id"] for l in get_order(con, oid4)["lines"] if l["name"] == "Paneer Tikka"]
        try:
            bill_order(con, {**staff, "order_id": oid4, "line_ids": paneer, "payments": [{"method": "Cash", "amount": "1"}]})
            raise AssertionError("payments don't add up")
        except ValueError:
            pass
        part = bill_order(con, {**staff, "order_id": oid4, "line_ids": paneer,
                                "payments": [{"method": "Cash", "amount": "500"}, {"method": "UPI", "amount": "285.92"}]})
        assert part["total"] == 78592 and part["table_no"] == 2 and part["payment"] == "Cash+UPI"
        assert part["payments"] == [{"method": "Cash", "amount": 50000}, {"method": "UPI", "amount": 28592}]
        rest = get_order(con, oid4)
        assert rest["status"] == "open" and [l["name"] for l in rest["lines"]] == ["Water"]
        tk = re.sub(rb"\x1b[aE@d].?|\x1d[!V].?", b"", bill_ticket(part, settings(con), 32)).decode()
        assert "Paid Cash" in tk and "Paid UPI" in tk
        # the rest split equally between 3 people
        w = bill_order(con, {**staff, "order_id": oid4, "payments": [{"method": "UPI", "amount": "21"}] * 3})
        assert w["total"] == 6300 and len(w["payments"]) == 3
        k3 = send_kot(con, {**staff, "order": {"type": "takeaway"}, "lines": [{"id": pid, "qty": 1}]})
        bill_order(con, {**owner, "order_id": k3["order_id"], "payment": "Card", "discount_pct": 10})
        # void a bill and reopen it to correct; voided bills leave the totals; refunds need a reason
        try:
            void_bill(con, {**owner, "order_id": w["id"]})
            raise AssertionError("void without a reason")
        except ValueError:
            pass
        v = void_bill(con, {**owner, "order_id": w["id"], "reason": "wrong table", "reopen": True})
        ro_ = get_order(con, v["reopened"])
        assert ro_["status"] == "open" and ro_["table_no"] == 2 and [(l["name"], l["qty"]) for l in ro_["lines"]] == [("Water", 3)]
        assert not any("Reopened" in (k["note"] or "") for k in kitchen(con, {})["kots"])  # not cooked twice
        assert "VOID" in bill_ticket(get_order(con, w["id"]), settings(con), 32).decode("ascii", "ignore")
        try:
            void_bill(con, {**owner, "order_id": w["id"], "reason": "again"})
            raise AssertionError("voided twice")
        except ValueError:
            pass
        remove_line(con, {**owner, "line_id": ro_["lines"][0]["id"], "reason": "not wanted"})
        refund_bill(con, {**owner, "order_id": part["id"], "amount": "100", "method": "Cash", "reason": "cold food"})
        try:
            refund_bill(con, {**owner, "order_id": part["id"], "amount": "700", "method": "Cash", "reason": "too much"})
            raise AssertionError("refunded more than the bill")
        except ValueError:
            pass
        r = report(con, {"day": [business_day(con)]})
        assert r["voided"] == {"count": 1, "total": 6300} and r["refunds"] == 10000 and r["net"] == r["sums"]["total"] - 10000
        assert r["by_payment"]["UPI"] == 28592  # the voided bill's UPI is not counted
        acts = {a["action"] for a in audit_log(con, {})}
        assert {"settings", "discount", "void_bill", "refund", "remove_item"} <= acts, acts
        assert all(a["role"] for a in audit_log(con, {}))
        dsh = dashboard(con, {"period": ["today"]})
        assert dsh["payments"] == r["by_payment"] and dsh["today"]["net"] == r["net"] and dsh["today"]["bills"] == r["count"]
        assert dsh["top_items"][0][0] == "Paneer Tikka" and len(dsh["week"]) == 7 and dsh["hours"]
        assert b"Refunds" in day_ticket(r, settings(con), 32)
    # ---- v4: profiles (3 owners / 5 staff), per-profile permissions and links, kitchen undo ----
    with con:
        s = settings(con)
        assert not profiles_on(con) and who_for(con, s, "987654")["name"] == "Owner"  # old PINs until profiles exist
        tej = save_profile(con, {**owner, "name": "Tej", "role": "staff", "pin": "111111", "perms": ["orders", "remove_item", "profiles"]})
        assert tej["slug"] == "tej_staff" and not profiles_on(con)  # a staff profile alone doesn't switch over
        sai = save_profile(con, {**owner, "name": "Sai", "role": "owner", "pin": "222222", "perms": ["reports"]})
        assert sai["slug"] == "sai_owner" and profiles_on(con)
        assert who_for(con, s, "987654") is None and who_for(con, s, "1234") is None  # old shared PINs stop
        t = who_for(con, s, "111111")
        assert t["name"] == "Tej" and t["perms"] == {"orders", "remove_item"}  # staff never get "profiles"
        assert who_for(con, s, "222222")["perms"] == {"reports", "profiles"}   # owners always can manage profiles
        assert who_for(con, s, "111111", "tej_staff") and who_for(con, s, "111111", "sai_owner") is None  # link = that profile
        for bad in ({"name": "Tej", "role": "staff", "pin": "333333"},   # same name + role
                    {"name": "Ravi", "role": "staff", "pin": "111111"},  # PIN in use
                    {"name": "Ravi", "role": "staff", "pin": "12"},      # too short
                    {"name": "Ravi", "role": "staff"},                   # no PIN
                    {"name": "<b>", "role": "staff", "pin": "444444"}):
            try:
                save_profile(con, {**owner, **bad})
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass
        for i in range(2):
            save_profile(con, {**owner, "name": f"Own{i}", "role": "owner", "pin": f"55555{i}"})
        try:
            save_profile(con, {**owner, "name": "Own3", "role": "owner", "pin": "555559"})
            raise AssertionError("4th owner")
        except ValueError:
            pass
        for i in range(4):
            save_profile(con, {**owner, "name": f"Waiter{i}", "role": "staff", "pin": f"66666{i}"})
        try:
            save_profile(con, {**owner, "name": "Waiter9", "role": "staff", "pin": "666669"})
            raise AssertionError("6th staff")
        except ValueError:
            pass
        lst = list_profiles(con, {})
        assert len(lst["profiles"]) == 8 and all("pin_hash" not in x for x in lst["profiles"])
        me = {"_who": {**who_for(con, s, "222222"), "device": "Counter"}}
        try:
            delete_profile(con, {**me, "id": sai["id"]})
            raise AssertionError("deleted own profile")
        except ValueError:
            pass
        delete_profile(con, {**me, "id": tej["id"]})
        assert who_for(con, s, "111111") is None
        save_profile(con, {**me, "id": sai["id"], "name": "Sai", "role": "owner", "pin": "", "perms": list(PERMS)})
        assert who_for(con, s, "222222")["perms"] == set(PERMS)  # blank PIN keeps the old one
        # staff profile removes a cooking item with its own PIN; the audit log names them
        w0 = {"_who": {**who_for(con, s, "666660"), "device": "Floor"}}
        k = send_kot(con, {**w0, "order": {"type": "takeaway"}, "lines": [{"id": pid, "qty": 2}]})
        line = get_order(con, k["order_id"])["lines"][0]
        remove_line(con, {**w0, "line_id": line["id"], "reason": "customer changed mind"})
        a = audit_log(con, {})[0]
        assert (a["action"], a["who"], a["role"], a["device"]) == ("remove_item", "Waiter0", "staff", "Floor"), a
        # kitchen: last 10 ready tickets, and undo a mis-tap
        k2 = send_kot(con, {**w0, "order": {"type": "takeaway"}, "lines": [{"id": pid, "qty": 1}]})
        kot_ready(con, {"id": k2["id"]})
        kd = kitchen(con, {})
        assert kd["recent"][0]["id"] == k2["id"] and kd["recent"][0]["ready_at"] and len(kd["recent"]) <= 10
        assert all(x["id"] != k2["id"] for x in kd["kots"])
        kot_undo(con, {"id": k2["id"]})
        assert any(x["id"] == k2["id"] for x in kitchen(con, {})["kots"])
        st = state(con, me)
        assert st["me"]["name"] == "Sai" and st["me"]["slug"] == "sai_owner" and st["profiles_on"]
        assert "pin_key" not in st["settings"]
    snap = sqlite3.connect(":memory:")
    if hasattr(snap, "deserialize"):  # Python 3.11+
        snap.deserialize(backup(con, {}))
        assert snap.execute("SELECT total FROM orders WHERE status = 'billed' ORDER BY id").fetchone()[0] == 52396  # backup holds the bill
    print("ok")


def make_server(port=PORT):
    """Open (or create) the database and return a ready, not-yet-serving HTTP server."""
    if FROZEN:
        DB.parent.mkdir(parents=True, exist_ok=True)
    if not DB.parent.is_dir():  # e.g. POS_DB=/data/pos.db but no volume mounted at /data
        raise SystemExit(f"Data folder {DB.parent} does not exist. On Railway: add a Volume with mount path {DB.parent}.")
    con = db()
    init(con)
    con.close()
    ThreadingHTTPServer.allow_reuse_address = False  # on Windows, True lets a 2nd copy silently share the port
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        sys.exit(selftest())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    server = make_server()
    print(f"Data file: {DB}", flush=True)
    print(f"POS running.\n  This PC:  http://localhost:{PORT}\n  Tablets (same Wi-Fi) - try these:")
    for ip in lan_ips():
        print(f"            http://{ip}:{PORT}")
    print("  If a VPN is on, its address won't work from tablets - use the other one.\n"
          "  Default staff PIN is 1234 - change it in Settings.  Close this window to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()
