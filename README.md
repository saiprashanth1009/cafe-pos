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
| Kitchen | Kitchen tablet | Live list of KOTs (refreshes every 5 s). A ticket turns red after 15 min. Tap **Mark ready**. The last 10 ready tickets stay below with **Back to cooking**, in case of a mis-tap. |
| Bill & print | Counter | Pick Cash / UPI / Card, set an optional discount %, and print. GST is split into CGST + SGST. There is no round-off: the total is charged to the paisa (e.g. ₹124.96). |
| Close day | Owner, at closing time | Reports → **Close day & print report**. Bills keep going to the same business day (even after midnight or after restarting the app) until you press this. It prints the daily report and moves billing to the next day. It's blocked while any order is still open. |
| Printer on a phone | Owner, once per phone | Android only. Install **RawBT** from the Play Store, pair the printer (e.g. EZO, 58 mm) inside RawBT and print its test page. In the POS on that phone: **⚙ (top right) → tick "Print from this phone"**, then **Print a test page**. Paper width is in Settings. Bills and day reports then open RawBT and print. iPhones can't do this. |
| Printer on the counter PC | Owner, once | Pair the Bluetooth receipt printer (e.g. EZO Chota, 58 mm) with the counter PC in Windows Bluetooth settings. Then **Settings → Receipt printer** = its COM port, **Paper width** = 58 mm, Save, and **Print a test page**. Bills and day reports then print straight away from any tablet. |
| Custom item | Waiters / counter | On the order screen, **+ Custom item**: type a name and price for a one-off dish. It goes to the kitchen, the bill and the receipt, but is never added to the menu. |
| Discounts | Counter + owner | In the bill box, choose **%** or **₹**. GST is charged on the amount after the discount. On a staff tablet the owner types the owner PIN and a reason. |
| Profiles | Owner | **Profiles** tab (owners only): up to **3 owners and 5 staff**. Each has a name, their own 6-12 digit PIN, their own link (e.g. `https://<your-link>/profile=sai_owner`) and tick boxes for what they may do: take orders, bill, kitchen, remove cooking items, cancel orders, discounts, void/refund, reports, dashboard, menu, settings, close day. Owners can always manage profiles. Nothing is created for you. Until the first **owner** profile exists, the old shared PINs keep working; after that only profile PINs sign in. |
| Profile links | Everyone | Each person opens and saves **their own link** (Profiles → Copy link). Signing in on the main page with a profile PIN jumps to that person's link. The PIN is remembered per link, so two people on one device or in two tabs never mix. **⚙ → Sign out** to hand the device to someone else. |
| Approvals | - | If a profile may not do something (e.g. a discount), the box asks for the PIN of someone who may. The audit log then shows who approved it and for whom. |
| Split bill | Counter | In the bill box: **Split by items** (tick what this person pays; the rest stays on the table), **+ Split payment** (e.g. ₹500 cash, the rest fills in as UPI), or **÷2 / ÷3 / ÷4** (one bill, equal shares, each with its own payment mode). |
| Void / refund | Owner | Reports → **Void** (keeps the bill number for GST, leaves the totals, prints as VOID; can reopen the items to correct and bill again) or **Refund** (full or part, by Cash/UPI/Card). Both need a reason. |
| Audit log | Owner | Reports → **Audit log**: every cancelled order, removed item, discount, void, refund, menu/settings change and day close, with the time, owner/staff, the tablet's name (⚙ → Tablet name) and the reason. |
| Offline tablets | - | If Wi-Fi drops, **Send to kitchen** still works: the KOT waits on the tablet (a red bar says so, even after a reload) and goes to the kitchen by itself when the connection is back, never twice. Billing needs the connection. |
| Dashboard | Owner | Today's sales, bills and average bill; this week vs last week by day; top items, busiest hours and Cash vs UPI vs Card for today / 7 / 30 days. |
| Start with Windows | Owner, once | In the CafePOS window tick **Start with Windows**: the POS opens by itself when the laptop restarts and signs in. |
| Bill numbers | - | Restart at 1 every business day. The printed invoice number carries the date (e.g. `131126-1` = bill 1 on 13 Nov 2026) so it stays unique for the financial year, as GST requires. |
| PIN prompts | - | Saving/deleting menu items, saving settings and Close day each ask for the owner PIN again, so a stray tap can't change them. |
| Reports | Owner | For any date: net sales (after refunds), GST, discounts, cancelled orders, voided bills, refunds, totals by payment and by order type, item-wise sales, bill reprints and the audit log. |

Printing uses the tablet's normal print dialog: AirPrint on iPad, the print service on Android.
Any 80 mm thermal printer that the tablet can see works.

## Development plan
**Phase 1: done (this build)**
- Orders for 8 tables (set the count in Settings), takeaway, and delivery with customer name, phone and address
- KOT flow with a kitchen display, kitchen notes, and "cooking/ready" status on every table
- GST tax invoice: GSTIN, FSSAI, CGST/SGST, sequential bill numbers
- Daily sales report and reprints
- Staff PIN, and the full café menu loaded from the PDF

**Phase 2: once you've picked your hardware**
- Direct thermal printing (ESC/POS over LAN) so bills and KOTs print without a dialog. Needs the printer model.
- Printed KOT in the kitchen, as an alternative to the kitchen tablet
- Daily automatic backup of `pos.db` (to a USB stick or Google Drive)
- Bill numbers that restart each financial year (e.g. `GR/26-27/0001`)

**v4: done**
- Profiles (3 owners, 5 staff) with per-person PIN, link and permissions; staff can remove cooking items (logged by name)
- Kitchen: last 10 ready tickets with "Back to cooking"; no round-off on bills; Google Drive backup

**v3: done**
- Owner and staff PINs, void/refund with a reason, audit log, split bills and split payments, offline KOTs, dashboard, Start with Windows

**Phase 3: after a few weeks of real use**
- Month and GST-period reports with CSV export for your accountant
- UPI QR code on the bill, and a packing charge for takeaway/delivery (for now, add it as a menu item)
- Swiggy/Zomato order entry, and a customer phone book for repeat deliveries

## Checks
`python app.py test` runs the GST maths and a full order → KOT → bill → report flow against a throwaway database.

## Google Drive backup
In the CafePOS window: **Google Drive → Setup…**. Pick what to back up (sales database, logs, menu as CSV), how often
(every 6 / 12 hours or once a day, plus right after **Close day**) and how many to keep. **Backup now** runs one
straight away. Backups go to a folder called **CafePOS Backups** in the Google account you connect.

CafePOS only asks Google for the `drive.file` permission: it can see and change **only the files it made itself**,
not the rest of your Drive.

**One-time setup (about 10 minutes, free):** Google requires every app that uploads to Drive to be registered.
1. Open <https://console.cloud.google.com> with the café's Google account and **create a project** (e.g. "CafePOS Backup").
2. **APIs & Services → Library**, search **Google Drive API**, press **Enable**.
3. **Google Auth Platform** (or **OAuth consent screen**) → **Get started**. App name "CafePOS Backup", your email
   as the support and contact email, Audience **External**, then **Create**.
4. **Audience → Publish app** (set it to **In production**). Otherwise Google signs CafePOS out every 7 days.
5. **Clients → Create client**, Application type **Desktop app**, name "CafePOS", then **Create**. Copy the
   **Client ID** and **Client secret**.
6. In CafePOS: **Google Drive → Setup…**, paste both, then **Connect Google account**. Pick the account and press
   **Allow** (if Google shows a "Google hasn't verified this app" page, choose **Continue**: it is your own app).
   Then **Save & back up now**.

To restore: download a `cafepos-db-….db` file from Drive, close CafePOS, and put it in place of
`%LOCALAPPDATA%\CafePOS\pos.db` (Database → Open folder). Keep the old file somewhere until you're sure.

## Updates
Every push to `main` builds a new `CafePOS.exe` on the GitHub release **latest**, with a build number. The **Updates** row in the CafePOS window:
- **Auto-update** (on by default): checks GitHub every 30 minutes and installs a new build by itself once no orders are open.
- **Check now / Update now**: check right away, or install a build that was found.

An update downloads the new exe, checks its SHA-256, swaps it in place of the running one and restarts (about 10-20 s; tablets reconnect and the public link stays on). Bills, menu, settings and logs live in `%LOCALAPPDATA%\CafePOS`, outside the exe, so nothing is lost and nothing needs reinstalling.

## License
MIT. See [LICENSE](LICENSE). Originally created by [muchkurisrivardhan](https://github.com/muchkurisrivardhan/cafe-pos).
