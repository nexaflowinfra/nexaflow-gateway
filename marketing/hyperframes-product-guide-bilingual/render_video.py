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


def add_glow(img, x, y, color, radius, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius // 2)))


def base_background():
    global BASE_BG_CACHE
    if BASE_BG_CACHE is not None:
        return BASE_BG_CACHE.copy()
    source = MARKETING / "nexaflow-brand-final.png"
    if source.exists():
        bg = Image.open(source).convert("RGB")
        scale = max(W / bg.width, H / bg.height) * 1.5
        resized = bg.resize((int(bg.width * scale), int(bg.height * scale)), Image.Resampling.LANCZOS)
        left = (resized.width - W) // 2
        top = (resized.height - H) // 2
        img = resized.crop((left, top, left + W, top + H)).convert("RGBA").filter(ImageFilter.GaussianBlur(12))
        img.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, 196)))
    else:
        img = Image.new("RGBA", (W, H), (*BG, 255))
    add_glow(img, 150, 230, TEAL, 340, 44)
    add_glow(img, 930, 300, GOLD, 330, 42)
    add_glow(img, 520, 1590, TEAL, 420, 20)
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(8):
        y = 1330 + i * 42
        d.arc((-120, y - 360, 1220, y + 420), 190, 345, fill=(*TEAL, 20), width=2)
        d.arc((-80, y - 300, 1260, y + 480), 195, 350, fill=(*GOLD, 18), width=2)
    BASE_BG_CACHE = img
    return img.copy()


def draw_background(t):
    img = base_background()
    return img


def draw_brand(img, alpha=1.0):
    d = ImageDraw.Draw(img, "RGBA")
    a = int(255 * alpha)
    d.rounded_rectangle((64, 58, 112, 106), radius=13, fill=(*TEAL, a))
    d.rounded_rectangle((70, 61, 116, 107), radius=13, fill=(*GOLD, a))
    d.text((132, 61), "Nexa", font=F_BRAND, fill=(*TEXT, a))
    nx = d.textbbox((132, 61), "Nexa", font=F_BRAND)[2]
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
    rounded(d, (64, 1674, 1016, 1852), 26, (3, 5, 5, int(222 * alpha)), (255, 255, 255, int(36 * alpha)), 2)
    draw_multiline(d, (94, 1702), zh, F_ZH, (*TEXT, a), 890, 8)
    draw_multiline(d, (94, 1762), en, F_EN, (*SOFT, a), 890, 8)
    img.alpha_composite(layer)


def draw_cursor(img, x, y, click=0.0, alpha=1.0):
    if alpha <= 0:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    r = 23 + int(18 * click)
    d.ellipse((x - r, y - r, x + r, y + r), outline=(*GOLD, int(170 * alpha)), width=4)
    d.polygon([(x - 12, y - 16), (x + 26, y + 18), (x + 4, y + 24), (x - 4, y + 48)], fill=(*TEXT, a), outline=(0, 0, 0, a))
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
    for label, value in fields:
        d.text((x1, yy), label, font=F_SMALL, fill=(*MUTED, a))
        rounded(d, (x1, yy + 34, x2, yy + 94), 16, (255, 255, 255, int(12 * alpha)), (*LINE, a), 1)
        d.text((x1 + 20, yy + 50), value, font=F_BODY_B, fill=(*TEXT, a))
        yy += 120
    rounded(d, (x1, yy + 8, x2, yy + 78), 18, (34, 197, 94, int(34 * alpha)), (34, 197, 94, int(150 * alpha)), 2)
    d.text((x1 + 22, yy + 25), "PDPA consent: customer data used for enquiry follow-up only", font=F_SMALL, fill=(*TEXT, a))
    button(d, (x1, yy + 125), "Submit trial request", alpha, True, 360)
    img.alpha_composite(layer)


def scene_load_inbox(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    x1, y1, x2, y2 = browser(layer, 64, 302, 952, 1125, "/inbox/your-business", alpha)
    a = int(255 * alpha)
    d.text((x1, y1), "STEP 2", font=F_EYE, fill=(*GOLD, a))
    d.text((x1, y1 + 70), "Load your private inbox", font=F_H2, fill=(*TEXT, a))
    card(d, (x1, y1 + 185, x2, y1 + 350), alpha, TEAL)
    d.text((x1 + 28, y1 + 220), "Business access key", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 28, y1 + 276), "Paste your private key once to unlock leads.", font=F_BODY, fill=(*MUTED, a))
    button(d, (x1, y1 + 420), "Load Leads", alpha, True, 230)
    button(d, (x1 + 258, y1 + 420), "Export CSV", alpha, False, 230)
    card(d, (x1, y1 + 540, x2, y1 + 740), alpha, GOLD)
    d.text((x1 + 28, y1 + 575), "Access is protected", font=F_H3, fill=(*TEXT, a))
    draw_multiline(d, (x1 + 28, y1 + 625), "The public enquiry form is open, but the merchant inbox requires the business key.", F_BODY, (*MUTED, a), 760)
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
    card(d, (x1, y1 + 392, x2, y1 + 590), alpha, GOLD)
    d.text((x1 + 28, y1 + 426), "Main customer link", font=F_H3, fill=(*TEXT, a))
    d.text((x1 + 28, y1 + 478), "Copy one link. Share it anywhere.", font=F_BODY, fill=(*MUTED, a))
    button(d, (x1 + 28, y1 + 526), "Copy Main Link", alpha, True, 285)
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
    d.text((x1 + 26, y1 + 786), "Use customer data only for enquiries, quotations, appointments, and service follow-up.", font=F_SMALL, fill=(*SOFT, a))
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
    button(d, (64, y + 155), "api.nexaflowinfra.com/ai-enquiry", alpha, True, 560)
    img.alpha_composite(layer)


SCENES = [
    (0, 8.5, scene_title, "NexaFlow 是给服务型商家的 AI WhatsApp 询盘助手。", "NexaFlow is an AI WhatsApp enquiry assistant for service merchants."),
    (8.5, 17.5, scene_product_page, "第一步：打开产品页，选择语言和新加坡或马来西亚。", "Step one: open the product page and choose language plus Singapore or Malaysia."),
    (17.5, 26.5, scene_trial_form, "填写商家资料和 WhatsApp，确认同意隐私与 PDPA 说明。", "Submit business details, WhatsApp, and consent to the privacy notice."),
    (26.5, 35.5, scene_load_inbox, "开通后，商家用 private access key 打开自己的 inbox。", "After setup, the merchant opens the private inbox with a business access key."),
    (35.5, 45.0, scene_daily_workspace, "每天只看三个重点：今天该回谁、setup 状态、复制客户链接。", "Each day, check today's focus, setup status, and copy the customer link."),
    (45.0, 54.5, scene_pipeline, "客户进来后，用 WhatsApp 草稿回复，再标记 contacted、quoted、won 或 lost。", "When a lead comes in, use the WhatsApp draft and update contacted, quoted, won, or lost."),
    (54.5, 64.0, scene_security, "安全方面，我们有 consent、private inbox、retention 和 audit log。", "For trust, NexaFlow uses consent, private inbox access, retention, and audit logs."),
    (64.0, 72.0, scene_final, "先从 Enquiry 开始，之后再加 CRM、Billing、Inventory 和 Automation。", "Start with Enquiry first, then add CRM, Billing, Inventory, and Automation later."),
]


def cursor_at(t):
    points = [
        (0, (850, 1500, 0)),
        (9.4, (760, 1085, 0)),
        (12.5, (300, 1085, 1)),
        (18.6, (720, 1210, 0)),
        (22.5, (300, 1245, 1)),
        (27.5, (500, 920, 0)),
        (31.0, (225, 1095, 1)),
        (36.8, (350, 1046, 0)),
        (40.5, (230, 1110, 1)),
        (46.3, (782, 960, 0)),
        (50.0, (715, 1200, 1)),
        (56.0, (280, 740, 0)),
        (60.0, (715, 965, 1)),
        (65.5, (365, 1190, 0)),
        (69.0, (350, 1328, 1)),
    ]
    current = points[0]
    for nxt in points[1:]:
        if t < nxt[0]:
            span = max(0.001, nxt[0] - current[0])
            p = ease((t - current[0]) / span)
            x = lerp(current[1][0], nxt[1][0], p)
            y = lerp(current[1][1], nxt[1][1], p)
            click = nxt[1][2] * max(0, 1 - abs(p - 1) * 8)
            return x, y, click
        current = nxt
    return current[1]


def render_frame(t):
    img = draw_background(t)
    draw_brand(img)
    for start, end, fn, zh, en in SCENES:
        a = alpha_between(t, start, end, 0.55)
        if a > 0:
            fn(img, t, a)
            draw_subtitle(img, zh, en, a)
    x, y, click = cursor_at(t)
    draw_cursor(img, int(x), int(y), click, alpha=1.0 if t < 70 else max(0, (72 - t) / 2))
    return img.convert("RGB")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    render_frame(39.5).save(PREVIEW, quality=95)
    samples = [3, 12, 22, 31, 40, 49, 59, 68]
    thumbs = [render_frame(t).resize((135, 240), Image.Resampling.LANCZOS) for t in samples]
    sheet = Image.new("RGB", (135 * len(thumbs), 240), (0, 0, 0))
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, (135 * index, 0))
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
