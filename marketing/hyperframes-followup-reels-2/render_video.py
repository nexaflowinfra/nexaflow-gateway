from pathlib import Path
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".video_deps"))

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio.v2 as imageio
import numpy as np


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "nexaflow-followup-reels-2-bilingual.mp4"
PREVIEW = ROOT / "nexaflow-followup-reels-2-preview.png"
CONTACT_SHEET = ROOT / "nexaflow-followup-reels-2-contact-sheet.png"

W, H = 1080, 1920
FPS = 30
DURATION = 20.0

BG = (3, 5, 4)
TEXT = (248, 250, 247)
MUTED = (185, 179, 165)
SOFT = (218, 208, 190)
TEAL = (45, 212, 191)
GOLD = (243, 199, 106)
RED = (255, 107, 107)
PANEL = (12, 15, 14)
PANEL2 = (6, 8, 8)
LINE = (54, 60, 58)


def font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for item in candidates:
        p = Path(item)
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


F_BRAND = font(36, True)
F_EYEBROW = font(24, True)
F_H1 = font(78, True)
F_H2 = font(64, True)
F_SUB = font(34, False)
F_CARD = font(32, True)
F_BODY = font(26, False)
F_ZH = font(36, True)
F_EN = font(27, True)
F_URL = font(30, True)


def ease_out(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def ease_in_out(x):
    x = max(0.0, min(1.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def alpha_between(t, start, end, fade=0.35):
    if t < start or t > end:
        return 0.0
    if t < start + fade:
        return ease_out((t - start) / fade)
    if t > end - fade:
        return ease_out((end - t) / fade)
    return 1.0


def wrap(draw, text, fnt, max_width):
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


def add_glow(img, x, y, color, radius, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    img.alpha_composite(layer)


def fit_cover(img, size, zoom=1.0):
    img = img.convert("RGB")
    sw, sh = size
    scale = max(sw / img.width, sh / img.height) * zoom
    nw, nh = int(img.width * scale), int(img.height * scale)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - sw) // 2
    top = (nh - sh) // 2
    return resized.crop((left, top, left + sw, top + sh))


def draw_background(t):
    source = ROOT.parent / "nexaflow-brand-final.png"
    if not source.exists():
        source = ROOT.parent / "nexaflow-social-cover-photo.png"
    if source.exists():
        img = fit_cover(Image.open(source), (W, H), zoom=1.52).convert("RGBA")
        img = img.filter(ImageFilter.GaussianBlur(10))
        dim = Image.new("RGBA", (W, H), (0, 0, 0, 184))
        img.alpha_composite(dim)
    else:
        img = Image.new("RGBA", (W, H), (*BG, 255))
    add_glow(img, 145, 200, TEAL, 300, 42)
    add_glow(img, 960, 280, GOLD, 300, 42)
    add_glow(img, 520, 1560, TEAL, 380, 22)
    d = ImageDraw.Draw(img, "RGBA")

    offset = int((t * 22) % 38)
    for i in range(13):
        y = 1365 + i * 34 + offset
        d.arc((-320, y - 290, 1400, y + 410), 190, 350, fill=(*TEAL, max(8, 54 - i * 3)), width=2)
        d.arc((-220, y - 230, 1480, y + 360), 200, 358, fill=(*GOLD, max(7, 44 - i * 3)), width=2)
    return img


def draw_brand(img):
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((64, 58, 112, 106), radius=13, fill=(*TEAL, 255))
    d.rounded_rectangle((70, 61, 116, 107), radius=13, fill=(*GOLD, 255))
    d.text((132, 61), "Nexa", font=F_BRAND, fill=TEXT)
    nx = d.textbbox((132, 61), "Nexa", font=F_BRAND)[2]
    d.text((nx, 61), "Flow", font=F_BRAND, fill=GOLD)


def draw_multiline(draw, xy, text, fnt, fill, max_width, line_gap=12):
    x, y = xy
    for line in wrap(draw, text, fnt, max_width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + line_gap
    return y


def draw_subtitle(img, zh, en, alpha):
    if alpha <= 0:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    box = (64, 1662, 1016, 1846)
    d.rounded_rectangle(box, radius=24, fill=(3, 5, 5, int(215 * alpha)), outline=(255, 255, 255, int(34 * alpha)), width=2)
    d.text((92, 1690), zh, font=F_ZH, fill=(*TEXT, a))
    d.text((92, 1744), en, font=F_EN, fill=(*SOFT, a))
    img.alpha_composite(layer)


def draw_scene_1(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    y = 505 - int(28 * (1 - alpha))
    d.text((64, y), "FOR SERVICE MERCHANTS", font=F_EYEBROW, fill=(*GOLD, a))
    y += 76
    d.text((64, y), "不是没有客户。", font=F_H1, fill=(*TEXT, a))
    y += 92
    d.text((64, y), "是 ", font=F_H1, fill=(*TEXT, a))
    x = d.textbbox((64, y), "是 ", font=F_H1)[2]
    d.text((x, y), "follow-up", font=F_H1, fill=(*GOLD, a))
    x = d.textbbox((x, y), "follow-up", font=F_H1)[2]
    d.text((x, y), " 漏了。", font=F_H1, fill=(*TEXT, a))
    y += 120
    sub = "WhatsApp, Facebook, Instagram, calls. Customer messages are scattered, and busy owners forget to reply."
    draw_multiline(d, (64, y), sub, F_SUB, (*SOFT, a), 900)
    img.alpha_composite(layer)


def draw_chat(layer, x, y, icon, title, body, alpha, accent=TEAL):
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    d.rounded_rectangle((x, y, x + 952, y + 154), radius=28, fill=(*PANEL, int(246 * alpha)), outline=(*LINE, int(255 * alpha)), width=2)
    d.rounded_rectangle((x + 28, y + 38, x + 106, y + 116), radius=22, fill=(255, 255, 255, int(18 * alpha)), outline=(255,255,255,int(36*alpha)), width=2)
    bbox = d.textbbox((0, 0), icon, font=F_CARD)
    d.text((x + 67 - (bbox[2] - bbox[0]) / 2, y + 58), icon, font=F_CARD, fill=(*accent, a))
    d.text((x + 132, y + 36), title, font=F_CARD, fill=(*TEXT, a))
    draw_multiline(d, (x + 132, y + 84), body, F_BODY, (*MUTED, a), 730, 6)


def draw_scene_2(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    y = 430
    d.text((64, y), "DAILY ENQUIRY CHAOS", font=F_EYEBROW, fill=(*GOLD, a))
    y += 76
    d.text((64, y), "一天里面，客户从", font=F_H2, fill=(*TEXT, a))
    y += 78
    d.text((64, y), "不同地方来。", font=F_H2, fill=(*TEXT, a))
    base = 730
    draw_chat(layer, 64, base, "WA", "“Can quote today?”", "客户问价，但没有统一记录。", alpha)
    draw_chat(layer, 64, base + 188, "IG", "“Any slot this week?”", "预约问题散在 inbox 和 DM。", alpha, GOLD)
    draw_chat(layer, 64, base + 376, "☎", "“Call me back later.”", "一忙起来，follow-up 就容易漏。", alpha, RED)
    img.alpha_composite(layer)


def draw_scene_3(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    y = 565
    d.text((64, y), "THE COST", font=F_EYEBROW, fill=(*GOLD, a))
    y += 80
    d.text((64, y), "回复慢一步，", font=F_H1, fill=(*TEXT, a))
    y += 94
    d.text((64, y), "客户可能已经找了", font=F_H1, fill=(*TEXT, a))
    y += 94
    d.text((64, y), "另一家。", font=F_H1, fill=(*RED, a))
    y += 140
    sub = "This is not always a complex CRM problem. Most merchants need a simple place to see every enquiry."
    draw_multiline(d, (64, y), sub, F_SUB, (*SOFT, a), 910)
    img.alpha_composite(layer)


def draw_scene_4(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    y = 350
    d.text((64, y), "NEXAFLOW ENQUIRY", font=F_EYEBROW, fill=(*GOLD, a))
    y += 76
    d.text((64, y), "一个 enquiry link。", font=F_H2, fill=(*TEXT, a))
    y += 78
    d.text((64, y), "一个 private inbox。", font=F_H2, fill=(*TEXT, a))
    box = (64, 655, 1016, 1330)
    d.rounded_rectangle(box, radius=34, fill=(*PANEL2, int(242 * alpha)), outline=(255,255,255,int(35*alpha)), width=2)
    d.text((98, 690), "Merchant Inbox", font=F_CARD, fill=(*TEXT, a))
    d.rounded_rectangle((720, 682, 974, 730), radius=24, fill=(*GOLD, int(44 * alpha)), outline=(*GOLD, int(110 * alpha)), width=2)
    d.text((748, 692), "AI reply draft", font=F_BODY, fill=(*GOLD, a))
    rows = [
        ("New lead: Renovation quote", "Status: New · Priority: Hot · Follow-up: Today"),
        ("Customer asks for appointment", "WhatsApp reply prepared. Merchant checks and sends."),
        ("Every lead stays visible", "No more lost enquiries inside chat history."),
    ]
    ry = 780
    for title, body in rows:
        d.rounded_rectangle((98, ry, 982, ry + 132), radius=22, fill=(8,10,10,int(244*alpha)), outline=(255,255,255,int(28*alpha)), width=1)
        d.text((128, ry + 24), title, font=font(28, True), fill=(*TEXT, a))
        d.text((128, ry + 70), body, font=font(23, False), fill=(*MUTED, a))
        ry += 162
    img.alpha_composite(layer)


def draw_scene_5(img, t, alpha):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    a = int(255 * alpha)
    box = (64, 500, 1016, 1335)
    d.rounded_rectangle(box, radius=36, fill=(*PANEL2, int(232 * alpha)), outline=(*GOLD, int(92 * alpha)), width=2)
    d.text((106, 550), "TRIAL NOW OPEN", font=F_EYEBROW, fill=(*GOLD, a))
    y = 625
    d.text((106, y), "给本地服务型商家的", font=F_H2, fill=(*TEXT, a))
    y += 80
    d.text((106, y), "AI WhatsApp 询盘助手。", font=F_H2, fill=(*TEXT, a))
    y += 125
    sub = "Suitable for renovation, beauty, cleaning, repair, tuition, and other WhatsApp-first service businesses."
    draw_multiline(d, (106, y), sub, F_SUB, (*SOFT, a), 820)
    d.rounded_rectangle((106, 1038, 540, 1110), radius=36, fill=(*GOLD, a))
    d.text((140, 1053), "1-month trial available", font=font(26, True), fill=(5, 5, 5, a))
    d.text((106, 1182), "api.nexaflowinfra.com/start-trial", font=F_URL, fill=(*GOLD, a))
    img.alpha_composite(layer)


SCENES = [
    (0.0, 3.8, draw_scene_1, "不是没有客户，是 follow-up 漏了。", "You are not missing leads. You are missing follow-ups."),
    (3.8, 8.0, draw_scene_2, "客户从 WhatsApp、IG、FB 和电话进来。", "Customers come from WhatsApp, Instagram, Facebook, and calls."),
    (8.0, 11.8, draw_scene_3, "回复慢一步，生意可能就流失。", "Reply too late, and the customer may choose someone else."),
    (11.8, 16.0, draw_scene_4, "NexaFlow 把询盘和跟进集中在一个 inbox。", "NexaFlow keeps enquiries and follow-ups in one inbox."),
    (16.0, 20.0, draw_scene_5, "现在开放 1 个月试用。", "1-month trial now open for local service businesses."),
]


def render_frame(t):
    img = draw_background(t)
    draw_brand(img)
    for start, end, draw_fn, zh, en in SCENES:
        alpha = alpha_between(t, start, end, 0.42)
        if alpha > 0:
            draw_fn(img, t, alpha)
            draw_subtitle(img, zh, en, alpha)
    return img.convert("RGB")


def main():
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    render_frame(12.9).save(PREVIEW, quality=95)
    samples = [1.4, 5.2, 9.2, 13.1, 17.2]
    thumbs = [render_frame(t).resize((216, 384), Image.Resampling.LANCZOS) for t in samples]
    sheet = Image.new("RGB", (216 * len(thumbs), 384), (0, 0, 0))
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, (216 * index, 0))
    sheet.save(CONTACT_SHEET, quality=95)
    total = int(DURATION * FPS)
    writer = imageio.get_writer(
        OUT,
        fps=FPS,
        codec="libx264",
        quality=8,
        macro_block_size=1,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    )
    try:
        for i in range(total):
            writer.append_data(np.asarray(render_frame(i / FPS)))
    finally:
        writer.close()
    print(OUT)
    print(PREVIEW)
    print(CONTACT_SHEET)


if __name__ == "__main__":
    main()
