from pathlib import Path
import sys
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".video_deps"))

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio.v2 as imageio
import numpy as np


ROOT = Path(__file__).resolve().parent
MARKETING = ROOT.parent
OUT = ROOT / "nexaflow-product-guide-bilingual.mp4"
PREVIEW = ROOT / "nexaflow-product-guide-bilingual-preview.png"
CONTACT_SHEET = ROOT / "nexaflow-product-guide-bilingual-contact-sheet.png"

W, H = 1080, 1920
FPS = 18
SOURCE_DURATION = 72.0
DURATION = 52.0

BG = (3, 5, 4)
TEXT = (248, 250, 247)
MUTED = (185, 179, 165)
SOFT = (218, 208, 189)
TEAL = (45, 212, 191)
GOLD = (243, 199, 106)
GREEN = (34, 197, 94)
RED = (255, 107, 107)
PANEL = (12, 15, 14)
PANEL2 = (6, 8, 8)
LINE = (55, 62, 59)


def font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for item in candidates:
        path = Path(item)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


F_BRAND = font(36, True)
F_EYE = font(24, True)
F_H1 = font(70, True)
F_H2 = font(54, True)
F_H3 = font(31, True)
F_BODY = font(26, False)
F_BODY_B = font(26, True)
F_SMALL = font(21, False)
F_BTN = font(25, True)
F_ZH = font(34, True)
F_EN = font(25, True)
F_URL = font(25, True)
BASE_BG_CACHE = None
LOGO_CACHE = None


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ease(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def alpha_between(t, start, end, fade=0.45):
    if t < start or t > end:
        return 0.0
    if t < start + fade:
        return ease((t - start) / fade)
    if t > end - fade:
        return ease((end - t) / fade)
    return 1.0


def lerp(a, b, x):
    return a + (b - a) * clamp(x)


def wrap(draw, text, fnt, max_width):
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        lines = []
        line = ""
        for ch in text:
            trial = line + ch
            if draw.textbbox((0, 0), trial, font=fnt)[2] <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = ch
        if line:
            lines.append(line)
        return lines
    words = text.split()
    lines = []
    line = ""
    for word in words:
        trial = word if not line else f"{line} {word}"
        if draw.textbbox((0, 0), trial, font=fnt)[2] <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw_multiline(draw, xy, text, fnt, fill, max_width, line_gap=10):
    x, y = xy
    for line in wrap(draw, text, fnt, max_width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + line_gap
    return y


def draw_text_fit(draw, xy, text, fnt, fill, max_width):
    if draw.textbbox((0, 0), text, font=fnt)[2] <= max_width:
        draw.text(xy, text, font=fnt, fill=fill)
        return
    draw_multiline(draw, xy, text, fnt, fill, max_width, 5)


def typed_value(value, progress):
    progress = clamp(progress)
    count = int(round(len(value) * progress))
    return value[:count]


def draw_field(draw, box, label, value, alpha, progress=1.0, active=False):
    x1, y1, x2, y2 = box
    a = int(255 * alpha)
    draw.text((x1, y1), label, font=F_SMALL, fill=(*MUTED, a))
    outline = GOLD if active else LINE
    rounded(draw, (x1, y1 + 34, x2, y1 + 94), 16, (255, 255, 255, int(12 * alpha)), (*outline, a), 2 if active else 1)
    visible = typed_value(value, progress)
    draw.text((x1 + 20, y1 + 50), visible, font=F_BODY_B, fill=(*TEXT, a))
    if active and int(progress * 10) % 2 == 0:
        cx = x1 + 24 + draw.textbbox((0, 0), visible, font=F_BODY_B)[2]
        draw.line((cx, y1 + 49, cx, y1 + 78), fill=(*GOLD, a), width=2)


def click_state(t, click_times, window=0.22):
    return max((max(0.0, 1.0 - abs(t - item) / window) for item in click_times), default=0.0)


def add_glow(img, x, y, color, radius, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius // 2)))


def base_background():
    global BASE_BG_CACHE
    if BASE_BG_CACHE is not None:
        return BASE_BG_CACHE.copy()
    img = Image.new("RGBA", (W, H), (*BG, 255))
    # Clean premium background: glows only, no decorative arcs or wave lines.
    add_glow(img, 135, 250, TEAL, 360, 42)
    add_glow(img, 930, 285, GOLD, 360, 40)
    add_glow(img, 540, 1490, TEAL, 430, 18)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade, "RGBA")
    sd.rectangle((0, 0, W, H), fill=(0, 0, 0, 42))
    sd.rectangle((0, 1540, W, H), fill=(0, 0, 0, 68))
    img.alpha_composite(shade)
    BASE_BG_CACHE = img
    return img.copy()


def draw_background(t):
    img = base_background()
    return img


def load_logo(size=58):
    global LOGO_CACHE
    if LOGO_CACHE is not None:
        return LOGO_CACHE.copy()
    source = MARKETING / "nexaflow-social-avatar-icon.png"
    if not source.exists():
        source = MARKETING / "nexaflow-logo-mono.png"
    logo = Image.open(source).convert("RGBA")
    side = min(logo.width, logo.height)
    left = (logo.width - side) // 2
    top = (logo.height - side) // 2
    logo = logo.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=14, fill=255)
    logo.putalpha(mask)
    LOGO_CACHE = logo
    return logo.copy()


def draw_brand(img, alpha=1.0):
    d = ImageDraw.Draw(img, "RGBA")
    a = int(255 * alpha)
    logo = load_logo()
    if alpha < 1:
        logo.putalpha(logo.getchannel("A").point(lambda p: int(p * alpha)))
    img.alpha_composite(logo, (64, 55))
    d.text((136, 61), "Nexa", font=F_BRAND, fill=(*TEXT, a))
    nx = d.textbbox((136, 61), "Nexa", font=F_BRAND)[2]
    d.text((nx, 61), "Flow", font=F_BRAND, fill=(*GOLD, a))


def rounded(draw, box, radius=28, fill=None, outline=None, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def browser(layer, x, y, w, h, url, alpha):
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    rounded(d, (x, y, x + w, y + h), 34, (*PANEL2, int(244 * alpha)), (*LINE, a), 2)
    d.rounded_rectangle((x, y, x + w, y + 76), radius=34, fill=(255, 255, 255, int(10 * alpha)))
    d.rectangle((x, y + 42, x + w, y + 76), fill=(255, 255, 255, int(10 * alpha)))
    for i in range(3):
        d.ellipse((x + 28 + i * 24, y + 31, x + 42 + i * 24, y + 45), fill=(255, 255, 255, int(72 * alpha)))
    rounded(d, (x + 116, y + 18, x + w - 26, y + 58), 20, (0, 0, 0, int(120 * alpha)), (*LINE, int(180 * alpha)), 1)
    d.text((x + 136, y + 24), url, font=F_SMALL, fill=(*MUTED, a))
    return (x + 36, y + 112, x + w - 36, y + h - 36)


def button(draw, xy, text, alpha=1.0, primary=True, width=None):
    x, y = xy
    tw = draw.textbbox((0, 0), text, font=F_BTN)[2]
    w = width or tw + 58
    h = 66
    fill = (*GOLD, int(255 * alpha)) if primary else (255, 255, 255, int(14 * alpha))
    outline = None if primary else (*LINE, int(255 * alpha))
    rounded(draw, (x, y, x + w, y + h), 19, fill, outline, 2)
    color = (5, 5, 5, int(255 * alpha)) if primary else (*TEXT, int(255 * alpha))
    draw.text((x + 28, y + 17), text, font=F_BTN, fill=color)
    return (x, y, x + w, y + h)


def card(draw, box, alpha=1.0, accent=None):
    fill = (*PANEL, int(232 * alpha))
    outline = (*(accent or LINE), int(150 * alpha))
    rounded(draw, box, 26, fill, outline, 2)


def draw_subtitle(img, zh, en, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    rounded(d, (64, 1660, 1016, 1868), 26, (3, 5, 5, int(228 * alpha)), (255, 255, 255, int(42 * alpha)), 2)
    next_y = draw_multiline(d, (94, 1686), zh, F_ZH, (*TEXT, a), 890, 7)
    draw_multiline(d, (94, next_y + 10), en, F_EN, (*SOFT, a), 890, 7)
    img.alpha_composite(layer)


def draw_cursor(img, x, y, click=0.0, alpha=1.0):
    if alpha <= 0:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    if click > 0:
        r = 15 + int(20 * click)
        d.ellipse((x - r, y - r, x + r, y + r), outline=(*GOLD, int(200 * click * alpha)), width=4)
        d.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(*GOLD, int(125 * click * alpha)))
    shadow = [(x + 2, y + 4), (x + 28, y + 29), (x + 13, y + 33), (x + 6, y + 49)]
    pointer = [(x, y), (x + 26, y + 26), (x + 10, y + 31), (x + 4, y + 47)]
    d.polygon(shadow, fill=(0, 0, 0, int(170 * alpha)))
    d.polygon(pointer, fill=(*TEXT, a), outline=(*GOLD, int(180 * alpha)))
    img.alpha_composite(layer)


def scene_title(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    y = 420 - int(24 * (1 - alpha))
    d.text((64, y), "2-MINUTE MERCHANT GUIDE", font=F_EYE, fill=(*GOLD, a))
    y += 86
    d.text((64, y), "一个 enquiry link.", font=F_H1, fill=(*TEXT, a))
    y += 86
    d.text((64, y), "一个 private inbox.", font=F_H1, fill=(*GOLD, a))
    y += 116
    draw_multiline(d, (64, y), "For local service merchants who receive enquiries from WhatsApp, Facebook, Instagram, calls, and referrals.", F_BODY, (*SOFT, a), 880)
    button(d, (64, y + 160), "Start with Enquiry", alpha, True, 310)
    button(d, (396, y + 160), "Merchant Login", alpha, False, 280)
    img.alpha_composite(layer)


def scene_product_page(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    px, py, pw, ph = 64, 300, 952, 1170
    x1, y1, x2, y2 = browser(layer, px, py, pw, ph, "api.nexaflowinfra.com/ai-enquiry", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "NEXAFLOW AI BUSINESS ECOSYSTEM", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 74), "One link to your", font=F_H2, fill=(*TEXT, a))
    d.text((x1, y1 + 136), "business tools.", font=F_H2, fill=(*TEXT, a))
    draw_multiline(d, (x1, y1 + 225), "Choose your country, language, and start with the Enquiry product first.", F_BODY, (*MUTED, a), 760)
    card(d, (x1, y1 + 350, x1 + 398, y1 + 500), alpha, TEAL)
    d.text((x1 + 28, y1 + 384), "Singapore", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 28, y1 + 432), "SGD pricing", font=F_BODY, fill=(*MUTED, a))
    card(d, (x1 + 424, y1 + 350, x1 + 822, y1 + 500), alpha, GOLD)
    d.text((x1 + 452, y1 + 384), "Malaysia", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 452, y1 + 432), "MYR pricing", font=F_BODY, fill=(*MUTED, a))
    button(d, (x1, y1 + 575), "Start with Enquiry", alpha, True, 330)
    button(d, (x1 + 358, y1 + 575), "View Pricing", alpha, False, 255)
    img.alpha_composite(layer)


def scene_trial_form(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    x1, y1, x2, y2 = browser(layer, 64, 285, 952, 1160, "/start-trial", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "STEP 1", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 70), "Submit business details", font=F_H2, fill=(*TEXT, a))
    fields = [
        ("Business name", "Apex Renovation"),
        ("WhatsApp number", "+65 9123 4567"),
        ("Service type", "Renovation / Cleaning / Tuition"),
        ("Monthly enquiries", "10-30"),
    ]
    yy = y1 + 175
    local = t - 17.5
    for index, (label, value) in enumerate(fields):
        start = 1.0 + index * 1.25
        progress = clamp((local - start) / 0.8)
        active = start <= local <= start + 0.9
        draw_field(d, (x1, yy, x2, yy + 94), label, value, alpha, progress, active)
        yy += 120
    rounded(d, (x1, yy + 8, x2, yy + 78), 18, (34, 197, 94, int(34 * alpha)), (34, 197, 94, int(150 * alpha)), 2)
    consent_text = "PDPA consent: customer data used for enquiry follow-up only"
    check_on = local > 6.0
    d.rounded_rectangle((x1 + 22, yy + 29, x1 + 48, yy + 55), radius=7, outline=(*GREEN, a), width=2, fill=(*GREEN, int(120 * alpha)) if check_on else (0, 0, 0, 0))
    if check_on:
        d.line((x1 + 28, yy + 42, x1 + 35, yy + 50, x1 + 45, yy + 35), fill=(*TEXT, a), width=3)
    d.text((x1 + 62, yy + 25), consent_text, font=F_SMALL, fill=(*TEXT, a))
    btn = button(d, (x1, yy + 125), "Submit trial request", alpha, True, 360)
    if 7.0 <= local <= 7.6:
        d.rounded_rectangle(btn, radius=19, outline=(*TEXT, int(210 * alpha)), width=4)
    if local > 7.6:
        rounded(d, (x1 + 385, yy + 128, x2, yy + 190), 18, (34, 197, 94, int(36 * alpha)), (34, 197, 94, int(150 * alpha)), 2)
        d.text((x1 + 408, yy + 145), "Saved securely", font=F_SMALL, fill=(*TEXT, a))
    img.alpha_composite(layer)


def scene_load_inbox(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    x1, y1, x2, y2 = browser(layer, 64, 302, 952, 1125, "/inbox/your-business", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "STEP 2", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 70), "Load your private inbox", font=F_H2, fill=(*TEXT, a))
    local = t - 26.5
    card(d, (x1, y1 + 185, x2, y1 + 350), alpha, TEAL)
    d.text((x1 + 28, y1 + 220), "Business access key", font=F_H3, fill=(*TEXT, a))
    draw_field(
        d,
        (x1 + 28, y1 + 270, x2 - 28, y1 + 364),
        "Paste your private key once to unlock leads.",
        "nf_live_merchant_key",
        alpha,
        clamp((local - 1.3) / 1.2),
        1.3 <= local <= 2.7,
    )
    load_btn = button(d, (x1, y1 + 420), "Load Leads", alpha, True, 230)
    button(d, (x1 + 258, y1 + 420), "Export CSV", alpha, False, 230)
    if 3.0 <= local <= 3.5:
        d.rounded_rectangle(load_btn, radius=19, outline=(*TEXT, int(210 * alpha)), width=4)
    card(d, (x1, y1 + 540, x2, y1 + 760), alpha, GOLD)
    d.text((x1 + 28, y1 + 575), "Access is protected", font=F_H3, fill=(*TEXT, a))
    if local > 3.6:
        d.text((x1 + 28, y1 + 625), "Inbox loaded: 3 enquiries", font=F_BODY_B, fill=(*GOLD, a))
        draw_multiline(d, (x1 + 28, y1 + 674), "Only the merchant with the private key can view them.", F_BODY, (*MUTED, a), 700)
    else:
        draw_multiline(d, (x1 + 28, y1 + 625), "The form is public. The inbox needs the business key.", F_BODY, (*MUTED, a), 700)
    img.alpha_composite(layer)


def scene_daily_workspace(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    x1, y1, x2, y2 = browser(layer, 48, 246, 984, 1300, "Merchant daily workspace", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "STEP 3", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 68), "Know what to do today", font=F_H2, fill=(*TEXT, a))
    card(d, (x1, y1 + 165, x1 + 412, y1 + 360), alpha, TEAL)
    d.text((x1 + 26, y1 + 198), "Today's focus", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 26, y1 + 250), "Reply to hot leads", font=F_BODY_B, fill=(*GOLD, a))
    draw_multiline(d, (x1 + 26, y1 + 288), "Use the WhatsApp draft, then mark status.", F_SMALL, (*MUTED, a), 340)
    card(d, (x1 + 436, y1 + 165, x2, y1 + 360), alpha, GOLD)
    d.text((x1 + 462, y1 + 198), "Setup status", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 462, y1 + 250), "83% ready", font=F_BODY_B, fill=(*GOLD, a))
    draw_multiline(d, (x1 + 462, y1 + 288), "Submit one test enquiry before sharing.", F_SMALL, (*MUTED, a), 380)
    card(d, (x1, y1 + 392, x2, y1 + 630), alpha, GOLD)
    d.text((x1 + 28, y1 + 426), "Main customer link", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 28, y1 + 478), "Copy one link. Share it anywhere.", font=F_BODY, fill=(*MUTED, a))
    button(d, (x1 + 28, y1 + 535), "Copy Main Link", alpha, True, 285)
    img.alpha_composite(layer)


def scene_pipeline(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    x1, y1, x2, y2 = browser(layer, 64, 290, 952, 1165, "Lead pipeline", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "STEP 4", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 70), "Reply and track every lead", font=F_H2, fill=(*TEXT, a))
    rows = [
        ("New lead", "Hot", "WhatsApp"),
        ("Customer asks for quote", "Reply draft", "Follow-up today"),
        ("Quoted customer", "Set value", "Won / Lost"),
    ]
    yy = y1 + 182
    for name, status, action in rows:
        card(d, (x1, yy, x2, yy + 118), alpha, TEAL if "Hot" in status else None)
        d.text((x1 + 24, yy + 28), name, font=F_BODY_B, fill=(*TEXT, a))
        d.text((x1 + 446, yy + 28), status, font=F_BODY, fill=(*GOLD, a))
        d.text((x1 + 628, yy + 28), action, font=F_BODY, fill=(*MUTED, a))
        yy += 142
    button(d, (x1, yy + 25), "Contacted", alpha, False, 210)
    button(d, (x1 + 230, yy + 25), "Quoted", alpha, False, 185)
    button(d, (x1 + 435, yy + 25), "Won", alpha, True, 150)
    img.alpha_composite(layer)


def scene_security(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    x1, y1, x2, y2 = browser(layer, 64, 286, 952, 1170, "Data protection", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "BUILT FOR TRUST", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 70), "Customer data stays protected", font=F_H2, fill=(*TEXT, a))
    boxes = [
        ("Consent", "Customers accept privacy notice before submitting.", TEAL),
        ("Private inbox", "Business access key protects merchant leads.", GOLD),
        ("Retention", "Old enquiries can be cleaned by policy.", GREEN),
        ("Audit log", "Data actions are logged for review.", TEAL),
    ]
    for i, (title, body, color) in enumerate(boxes):
        col = i % 2
        row = i // 2
        bx = x1 + col * 425
        by = y1 + 190 + row * 235
        card(d, (bx, by, bx + 394, by + 190), alpha, color)
        d.text((bx + 26, by + 28), title, font=F_H3, fill=(*TEXT, a))
        draw_multiline(d, (bx + 26, by + 82), body, F_BODY, (*MUTED, a), 320)
    rounded(d, (x1, y1 + 710, x2, y1 + 830), 26, (34, 197, 94, int(28 * alpha)), (34, 197, 94, int(150 * alpha)), 2)
    d.text((x1 + 26, y1 + 745), "Merchant confidence point", font=F_BODY_B, fill=(*TEXT, a))
    draw_multiline(d, (x1 + 26, y1 + 786), "Use customer data only for replies, quotations, appointments, and service follow-up.", F_SMALL, (*SOFT, a), 790, 4)
    img.alpha_composite(layer)


def scene_final(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    y = 430
    d.text((64, y), "READY TO TRY", font=F_EYE, fill=(*GOLD, a))
    y += 82
    d.text((64, y), "Start with Enquiry.", font=F_H1, fill=(*TEXT, a))
    y += 88
    d.text((64, y), "Add CRM, Billing,", font=F_H1, fill=(*GOLD, a))
    y += 88
    d.text((64, y), "Inventory later.", font=F_H1, fill=(*GOLD, a))
    y += 122
    draw_multiline(d, (64, y), "One simple workflow first. One AI business ecosystem as your business grows.", F_BODY, (*SOFT, a), 850)
    button(d, (64, y + 155), "Start at api.nexaflowinfra.com", alpha, True, 560)
    img.alpha_composite(layer)


SCENES = [
    (0, 8.5, scene_title, "NexaFlow 是给服务型商家的 AI WhatsApp 询盘助手。", "NexaFlow is an AI WhatsApp enquiry assistant for service merchants."),
    (8.5, 17.5, scene_product_page, "第一步：打开产品页，选择语言和新加坡或马来西亚。", "Step one: open the product page and choose language plus Singapore or Malaysia."),
    (17.5, 26.5, scene_trial_form, "填写商家资料和 WhatsApp，确认同意隐私与 PDPA 说明。", "Submit business details, WhatsApp, and consent to the privacy notice."),
    (26.5, 35.5, scene_load_inbox, "开通后，商家用 private access key 打开自己的 inbox。", "After setup, the merchant opens the private inbox with a business access key."),
    (35.5, 45.0, scene_daily_workspace, "每天只看三个重点：今天该回谁、setup 状态、复制客户链接。", "Each day, check today's focus, setup status, and copy the customer link."),
    (45.0, 54.5, scene_pipeline, "客户进来后，用 WhatsApp 草稿回复，再标记已联系、已报价、成交或未成交。", "When a lead comes in, use the WhatsApp draft and update contacted, quoted, won, or lost."),
    (54.5, 64.0, scene_security, "安全方面，我们有 consent、private inbox、retention 和 audit log。", "For trust, NexaFlow uses consent, private inbox access, retention, and audit logs."),
    (64.0, 72.0, scene_final, "先从 Enquiry 开始，之后再加 CRM、Billing、Inventory 和 Automation。", "Start with Enquiry first, then add CRM, Billing, Inventory, and Automation later."),
]


CURSOR_ACTIONS = [
    (10.2, 754, 840, "Malaysia"),
    (15.2, 384, 1024, "Start with Enquiry"),
    (18.4, 864, 636, "Business name"),
    (19.8, 864, 756, "WhatsApp number"),
    (21.1, 864, 876, "Service type"),
    (22.5, 864, 996, "Monthly enquiries"),
    (24.4, 132, 1094, "PDPA consent"),
    (25.7, 432, 1210, "Submit trial request"),
    (28.2, 865, 748, "Private key"),
    (31.0, 310, 868, "Load Leads"),
    (40.2, 366, 928, "Copy Main Link"),
    (50.0, 666, 1068, "Won"),
    (60.0, 754, 890, "Audit log"),
    (69.0, 592, 998, "Final link"),
]


def cursor_at(t):
    for action_t, x, y, _label in CURSOR_ACTIONS:
        distance = abs(t - action_t)
        if distance <= 0.85:
            alpha = ease(1 - distance / 0.85)
            click = max(0.0, 1.0 - distance / 0.24)
            return x, y, click, alpha
    return 0, 0, 0.0, 0.0


def render_frame(t):
    img = draw_background(t)
    draw_brand(img)
    for start, end, fn, zh, en in SCENES:
        a = alpha_between(t, start, end, 0.55)
        if a > 0:
            fn(img, t, a)
            draw_subtitle(img, zh, en, a)
    x, y, click, cursor_alpha = cursor_at(t)
    draw_cursor(img, int(x), int(y), click, alpha=cursor_alpha)
    return img.convert("RGB")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    render_frame(39.5).save(PREVIEW, quality=95)
    samples = [10.2, 15.2, 22.0, 25.7, 31.0, 40.2, 50.0, 60.0, 69.0]
    thumbs = [render_frame(t).resize((270, 480), Image.Resampling.LANCZOS) for t in samples]
    sheet = Image.new("RGB", (270 * 3, 480 * 3), (0, 0, 0))
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((index % 3) * 270, (index // 3) * 480))
    sheet.save(CONTACT_SHEET, quality=95)
    if "--preview-only" in sys.argv:
        print(PREVIEW)
        print(CONTACT_SHEET)
        return

    writer = imageio.get_writer(
        OUT,
        fps=FPS,
        codec="libx264",
        quality=8,
        macro_block_size=1,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    )
    try:
        for i in range(int(DURATION * FPS)):
            output_t = i / FPS
            source_t = output_t * (SOURCE_DURATION / DURATION)
            writer.append_data(np.asarray(render_frame(source_t)))
    finally:
        writer.close()
    print(OUT)
    print(PREVIEW)
    print(CONTACT_SHEET)


if __name__ == "__main__":
    main()
