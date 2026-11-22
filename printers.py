"""'Connect' for the receipt printer, used by the Printer row in the CafePOS window (gui.py).

Plug a USB thermal printer (e.g. EZO, 58 mm) into the counter PC and press Connect:
  1. finds the USB printer that is plugged in right now (or a paired Bluetooth printer if there is none)
  2. if Windows has no printer for it yet, installs one with Windows' built-in "Generic / Text Only" driver
     (one admin prompt) - ESC/POS receipt printers need no other driver, the POS sends them RAW bytes
  3. makes it the Windows default printer
  4. points the POS at it, so every Print / Bill & print / day report from any tablet prints on it straight away
"""
import os, subprocess, tempfile, time
from pathlib import Path

import app

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DRIVER = "Generic / Text Only"
WPP_KEY = r"SOFTWARE\Policies\Microsoft\Windows NT\Printers\WPP"

INSTALL_PS1 = r"""
param([string]$Name, [string]$Port, [string]$Log)
Start-Transcript -Path $Log -Force | Out-Null
try {
    if (-not (Get-PrinterDriver -Name '%(driver)s' -ErrorAction SilentlyContinue)) {
        try { Add-PrinterDriver -Name '%(driver)s' -ErrorAction Stop }
        catch {
            $inf = Get-ChildItem "$env:windir\System32\DriverStore\FileRepository\prnge001.inf_*\prnge001.inf" | Select-Object -First 1
            if (-not $inf) { throw "Windows' generic printer driver (prnge001.inf) is missing" }
            Add-PrinterDriver -Name '%(driver)s' -InfPath $inf.FullName -ErrorAction Stop
        }
    }
    if (-not (Get-Printer -Name $Name -ErrorAction SilentlyContinue)) {
        Add-Printer -Name $Name -DriverName '%(driver)s' -PortName $Port -ErrorAction Stop
    }
    "OK"
} catch { "ERROR: $($_.Exception.Message)" }
Stop-Transcript | Out-Null
""" % {"driver": DRIVER}


class ConnectError(Exception):
    pass


def protected_print_mode():
    """Windows 11 'Windows protected print mode' blocks every printer driver except Microsoft's modern ones,
    so a receipt printer can't be installed while it's on."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WPP_KEY)
        return bool(winreg.QueryValueEx(k, "WindowsProtectedPrintMode")[0])
    except (ImportError, OSError):
        return False


def plugged_usb():
    """USB printer ports with a device plugged in right now: [("USB001", "micro-printer"), ...]"""
    return [(port, u["desc"]) for port, u in sorted(app.usb_printer_ports().items()) if u["present"]]


def install_queue(name, port, log):
    """Add a Windows printer for a USB port (needs admin: shows one UAC prompt)."""
    if protected_print_mode():
        raise ConnectError("Windows protected print mode is on, so Windows refuses the receipt printer's driver. "
                           "Turn it off: Settings > Bluetooth & devices > Printers & scanners > "
                           "'Windows protected print mode' = Off, then press Connect again.")
    tmp = Path(tempfile.mkdtemp(prefix="cafepos-printer-"))
    script, out = tmp / "install.ps1", tmp / "install.log"
    script.write_text(INSTALL_PS1, encoding="utf-8-sig")
    log.info("Installing '%s' on %s - approve the Windows admin prompt", name, port)
    args = (f"-NoProfile -ExecutionPolicy Bypass -File \"{script}\" -Name \"{name}\" -Port \"{port}\" -Log \"{out}\"")
    ps = ("$p = Start-Process powershell -Verb RunAs -WindowStyle Hidden -PassThru -Wait -ArgumentList '"
          + args.replace("'", "''") + "'; exit $p.ExitCode")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                       creationflags=NOWIN, timeout=300)
    text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    if "OK" not in text.splitlines():
        err = next((l for l in text.splitlines() if l.startswith("ERROR:")), None)
        if not text and r.returncode:
            raise ConnectError("The admin prompt was cancelled - press Connect again and choose Yes.")
        raise ConnectError((err or r.stderr.strip() or "install failed")[:200])
    for _ in range(10):  # the spooler takes a moment to list it
        if any(p["name"] == name for p in app.windows_printers()):
            return
        time.sleep(0.5)
    raise ConnectError(f"'{name}' was installed but Windows doesn't list it yet - press Connect again.")


def save_choice(target):
    con = app.db()
    try:
        with con:
            con.execute("UPDATE settings SET value = ? WHERE key = 'printer'", (target,))
    finally:
        con.close()


def connect(log):
    """Find, install if needed, make default and select the receipt printer. Returns a short description."""
    usb = plugged_usb()
    if usb:
        port, desc = usb[0]
        queue = next((p for p in app.windows_printers() if p["port"] == port), None)
        if queue:
            name = queue["name"]
        else:
            name = "Receipt Printer" + ("" if len(usb) == 1 else f" {port}")
            install_queue(name, port, log)
        app.set_default_printer(name)
        save_choice(app.WIN_PREFIX + name)
        log.info("Printer connected: %s on %s (%s) - now the Windows default and the POS prints here", name, port, desc or "USB")
        return name

    # no USB printer: a paired Bluetooth printer still works over its COM port
    bt = [p for p in app.com_ports() if p["bluetooth"] and p["name"]]
    pick = next((p for p in bt if "print" in p["name"].lower() or "pos" in p["name"].lower()), None)
    if pick:
        save_choice(pick["port"])
        log.info("Printer connected: %s on %s (Bluetooth) - the POS prints here", pick["name"], pick["port"])
        return f"{pick['name']} ({pick['port']})"
    raise ConnectError("No printer found. Plug the printer's USB cable into this PC, switch it on, "
                       "wait a few seconds and press Connect again.")


def test_print(log):
    con = app.db()
    try:
        target = app.settings(con).get("printer") or ""
        if not target:
            raise ConnectError("No printer chosen yet - press Connect first.")
        app.send_to_printer(target, app.test_ticket(con))
    finally:
        con.close()
    log.info("Test page printed on %s", app.printer_label(target))


def current():
    con = app.db()
    try:
        return app.settings(con).get("printer") or ""
    finally:
        con.close()
