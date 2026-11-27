"""Bills and day reports as pictures: PNG / JPEG / PDF for viewing and saving, and a black-and-white raster for
the receipt printer, all with the café logo faded into the background. Needs Pillow (bundled in CafePOS.exe);
without it the POS still works and just prints text receipts."""
import io, sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

RES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
LOGO = RES / "logo.png"          # artwork on a transparent background
COLOUR_STRENGTH, MONO_STRENGTH = 0.48, 0.80   # how dark the faded logo is (approved on the sample receipt)
FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/TTF")]
FONT_FILES = {False: ["arial.ttf", "DejaVuSans.ttf"], True: ["arialbd.ttf", "DejaVuSans-Bold.ttf"]}


@lru_cache(maxsize=32)
def font(size, bold=False):
    for d in FONT_DIRS:
        for f in FONT_FILES[bold]:
            if (d / f).exists():
                return ImageFont.truetype(str(d / f), size)
    return ImageFont.load_default(size)


@lru_cache(maxsize=8)
def watermark(size, mono):
    """The logo washed out towards white paper, size x size pixels (None if there is no logo file)."""
    if not LOGO.exists():
        return None
    art = Image.open(LOGO).convert("RGBA").resize((size, size), Image.LANCZOS)
    alpha = art.getchannel("A").point(lambda v: int(v * (MONO_STRENGTH if mono else COLOUR_STRENGTH)))
    rgb = ImageOps.grayscale(art.convert("RGB")).convert("RGB") if mono else art.convert("RGB")
    paper = Image.new("RGB", (size, size), "white")
    paper.paste(rgb, mask=alpha)
    return paper


class Canvas:
    """Same calls as the ESC/POS Ticket (line / row / rule), drawn as a picture."""
    rupee = "₹"

    def __init__(self, width=640, size=24, mono=False, logo=True):
        self.w, self.size, self.mono, self.logo, self.ops = width, size, mono, logo, []
        self.pad = max(12, width // 19)

    def line(self, s="", bold=False, center=False, big=False):
        self.ops.append(("line", str(s), bold, center, big))
        return self

    def row(self, left, right="", bold=False):
        self.ops.append(("row", str(left), str(right), bold))
        return self

    def rule(self):
        self.ops.append(("rule",))
        return self

    def _wrap(self, s, f, width):
        words, out, cur = s.split(" "), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if cur and f.getlength(t) > width:
                out.append(cur)
                cur = w
            else:
                cur = t
        return out + [cur]

    def image(self):
        sz, w, pad = self.size, self.w, self.pad
        gap = int(sz * 0.45)
        draw_list, y = [], int(sz * 1.2)
        for op in self.ops:  # lay out first, so the height is known before drawing
            if op[0] == "line":
                _, s, bold, center, big = op
                f = font(int(sz * (1.55 if big else 1)), bold or big)
                for part in self._wrap(s, f, w - 2 * pad):
                    draw_list.append(("text", (w / 2 if center else pad, y), part, f, "ma" if center else "la"))
                    y += int(f.size * 1.2) + gap // 2
            elif op[0] == "row":
                _, l, r, bold = op
                f = font(int(sz * (1.3 if bold else 1)), bold)
                indent = pad + (sz if l.startswith("  ") else 0)
                l = l.strip()
                if f.getlength(l) + f.getlength(r) + sz > w - 2 * pad:  # too long: value on its own line
                    draw_list.append(("text", (indent, y), l, f, "la"))
                    y += int(f.size * 1.2)
                    draw_list.append(("text", (w - pad, y), r, f, "ra"))
                else:
                    draw_list.append(("text", (indent, y), l, f, "la"))
                    draw_list.append(("text", (w - pad, y), r, f, "ra"))
                y += int(f.size * 1.2) + gap // 2
            else:
                y += gap
                draw_list.append(("rule", y))
                y += gap + 2
        h = y + int(sz * 1.2)
        img = Image.new("RGB", (w, h), "white")
        if self.logo:
            side = int(min(w * 0.82, h * 0.9))
            mark = watermark(side, self.mono)
            if mark:
                img.paste(mark, ((w - side) // 2, (h - side) // 2 + int(sz * 1.5)))
        d = ImageDraw.Draw(img)
        for it in draw_list:
            if it[0] == "text":
                _, xy, s, f, anchor = it
                d.text(xy, s, font=f, fill="black", anchor=anchor)
            else:
                d.line((pad, it[1], w - pad, it[1]), fill="black", width=max(1, sz // 12))
        return img


def encode(img, fmt):
    """PNG / JPEG / PDF bytes of a picture."""
    fmt = fmt.lower()
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, "PNG", optimize=True)
    elif fmt in ("jpg", "jpeg"):
        img.save(buf, "JPEG", quality=92)
    elif fmt == "pdf":
        img.save(buf, "PDF", resolution=203)  # 640 px wide at 203 dpi = an 80 mm receipt
    else:
        raise ValueError("format must be pdf, png or jpeg")
    return buf.getvalue()


def escpos_raster(img, cut):
    """Black-and-white picture -> ESC/POS 'GS v 0' raster bytes (any ESC/POS thermal printer)."""
    bw = img.convert("L").convert("1")  # dithers the grey logo, keeps the text solid black
    w, h = bw.size
    wb = (w + 7) // 8
    data = ImageOps.invert(bw.convert("L")).convert("1").tobytes()  # Pillow: 1 = white; printer: 1 = dot
    out = bytearray(b"\x1b@")
    for top in range(0, h, 256):  # some printers only take short bands
        rows = min(256, h - top)
        out += b"\x1dv0\x00" + bytes([wb & 255, wb >> 8, rows & 255, rows >> 8]) + data[top * wb:(top + rows) * wb]
    out += b"\x1bd\x04" + (b"\x1dV\x01" if cut else b"")
    return bytes(out)
