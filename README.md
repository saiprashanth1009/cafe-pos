# Guns & Roses Café POS

Billing, kitchen orders (KOT) and daily reports for dine-in, takeaway and delivery.
One Windows PC at the counter runs the server, and staff use any iPad or Android tablet's browser over the café Wi-Fi.

## Run it
1. Install Python 3 on the counter PC (python.org; tick "Add to PATH").
2. Double-click `start.bat`. It prints the address for tablets, e.g. `http://192.168.1.20:8000`.
3. Windows Firewall will ask about Python. Allow it on **Private** networks.
4. On each tablet, open that address, then use Share → **Add to Home Screen** to get an app icon.
5. The staff PIN is `1234`. **Change it in Settings on day one.** Also fill in your GSTIN, FSSAI licence number and phone there.

All data is in `pos.db`. Copy that file somewhere safe every day.
`menu.csv` only loads on the very first run. After that, edit the menu in the app.

## One-file Windows app: CafePOS.exe (recommended for the café PC)
Download `CafePOS.exe` from the repo's **Releases → latest** page (it's rebuilt automatically on every push), put it on the Desktop, and double-click it.
The first time, Windows may say "Windows protected your PC": click **More info → Run anyway**.

The window then:
- starts the POS and its SQLite database (`%LOCALAPPDATA%\CafePOS\pos.db`, with daily log files in `logs\` next to it)
- installs Tailscale if it's missing (approve the Windows prompt), starts it, and opens the sign-in page if needed
- turns on the public link (Tailscale Funnel). The first time on a new tailnet, approve **Enable Funnel** in the page that opens
- shows the public link, the Wi-Fi link and the database location with Open/Copy buttons, plus a live log of every order, KOT, bill, cancellation, menu change, wrong PIN and error
- checks everything every 20 seconds and repairs Tailscale or the public link if they drop

Closing the window stops the POS and turns the public link off.

## Online version (Railway)
The private GitHub repo deploys itself: every push to `main` updates the website in a couple of minutes.
1. railway.com → **Login with GitHub** → **New Project** → **Deploy from GitHub repo** → `cafe-pos` (allow Railway access to it).
2. Open the service → **Variables**: add `POS_DB` = `/data/pos.db` and `POS_PIN` = your 6-digit staff PIN.
3. Right-click the service (or ⌘K) → **Add Volume** → mount path `/data`. **Without this, every update wipes all bills.**
4. **Settings → Networking → Generate Domain**. That's your link.

`POS_PIN` only sets the PIN when the site first starts. After that, change it in the app's Settings.
Back up data: in the app, **Settings → Download backup of all data**, at least weekly. To restore one, rename it `pos.db` and run the app from it.

## How it works
| Screen | Who | What |
|---|---|---|
| Tables | Waiters / counter | Tap a table (or + Takeaway / + Delivery), tap items, add a kitchen note, then **Send to kitchen**. |
| Kitchen | Kitchen tablet | Live list of KOTs (refreshes every 5 s). A ticket turns red after 15 min. Tap **Mark ready**. |
| Bill & print | Counter | Pick Cash / UPI / Card, set an optional discount %, and print. GST is split into CGST + SGST and the total is rounded to the nearest ₹1. |
| Close day | Owner, at closing time | Reports → **Close day & print report**. Bills keep going to the same business day (even after midnight or after restarting the app) until you press this. It prints the daily report and moves billing to the next day. It's blocked while any order is still open. |
| Printer on a phone | Owner, once per phone | Android only. Install **RawBT** from the Play Store, pair the printer (e.g. EZO, 58 mm) inside RawBT and print its test page. In the POS on that phone: **Settings → tick "Print from this phone"**, **Paper width** = 58 mm, **Print a test page**. Bills and day reports then open RawBT and print. iPhones can't do this. |
| Printer on the counter PC | Owner, once | Pair the Bluetooth receipt printer (e.g. EZO Chota, 58 mm) with the counter PC in Windows Bluetooth settings. Then **Settings → Receipt printer** = its COM port, **Paper width** = 58 mm, Save, and **Print a test page**. Bills and day reports then print straight away from any tablet. |
| Custom item | Waiters / counter | On the order screen, **+ Custom item**: type a name and price for a one-off dish. It goes to the kitchen, the bill and the receipt, but is never added to the menu. |
| Discounts | Counter | In the bill box, choose **%** or **₹**. GST is charged on the amount after the discount. |
| Bill numbers | - | Restart at 1 every business day. The printed invoice number carries the date (e.g. `131126-1` = bill 1 on 13 Nov 2026) so it stays unique for the financial year, as GST requires. |
| PIN prompts | - | Saving/deleting menu items, saving settings and Close day each ask for the staff PIN, so a stray tap can't change them. |
| Reports | Owner | For any date: net sales, GST, discounts, cancelled orders, totals by payment and by order type, item-wise sales, and bill reprints. |

Printing uses the tablet's normal print dialog: AirPrint on iPad, the print service on Android.
Any 80 mm thermal printer that the tablet can see works.

## Development plan
**Phase 1: done (this build)**
- Orders for 8 tables (set the count in Settings), takeaway, and delivery with customer name, phone and address
- KOT flow with a kitchen display, kitchen notes, and "cooking/ready" status on every table
- GST tax invoice: GSTIN, FSSAI, CGST/SGST, round-off, sequential bill numbers
- Daily sales report and reprints
- Staff PIN, and the full café menu loaded from the PDF

**Phase 2: once you've picked your hardware**
- Direct thermal printing (ESC/POS over LAN) so bills and KOTs print without a dialog. Needs the printer model.
- Printed KOT in the kitchen, as an alternative to the kitchen tablet
- Daily automatic backup of `pos.db` (to a USB stick or Google Drive)
- Bill numbers that restart each financial year (e.g. `GR/26-27/0001`)

**Phase 3: after a few weeks of real use**
- Void/refund of printed bills with a reason (an audit trail for the owner)
- Separate PINs for owner and staff (staff can't edit the menu or see reports)
- Month and GST-period reports with CSV export for your accountant
- UPI QR code on the bill, and a packing charge for takeaway/delivery (for now, add it as a menu item)
- Swiggy/Zomato order entry, and a customer phone book for repeat deliveries

## Checks
`python app.py test` runs the GST and rounding maths and a full order → KOT → bill → report flow against a throwaway database.

## License
MIT. See [LICENSE](LICENSE). Originally created by [muchkurisrivardhan](https://github.com/muchkurisrivardhan/cafe-pos).
