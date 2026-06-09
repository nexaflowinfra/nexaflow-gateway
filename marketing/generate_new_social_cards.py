from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".video_deps"))

from PIL import Image, ImageDraw, ImageFilter, ImageFont


OUT_DIR = Path(__file__).resolve().parent
W = H = 1080

INK = (248, 248, 244)
MUTED = (172, 178, 172)
GOLD = (245, 198, 94)
TEAL = (35, 226, 214)
BLACK = (3, 5, 5)
PANEL = (10, 13, 13)
LINE = (48, 56, 52)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()


F_LOGO = font(42, True)
F_H1 = font(76, True)
F_H1_SMALL = font(64, True)
F_H2 = font(34, True)
F_BODY = font(30)
F_BODY_B = font(30, True)
F_SMALL = font(23)
F_CHIP = font(25, True)


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0]


def ascii_word(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9+/#&._-]*$", token))


def mixed_tokens(text: str) -> list[str]:
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return re.findall(r"[A-Za-z0-9][A-Za-z0-9+/#&._-]*|[^\s]", text)
    return text.split()


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    tokens = mixed_tokens(text)
    mixed = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    lines: list[str] = []
    line = ""
    previous = ""
    for token in tokens:
        if not mixed:
            trial = f"{line} {token}".strip()
        elif line and ascii_word(previous) and ascii_word(token):
            trial = f"{line} {token}"
        else:
            trial = f"{line}{token}"
        if text_width(draw, trial, fnt) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = token
        previous = token
    if line:
        lines.append(line)
    return lines


def draw_wrapped(draw: ImageDraw.ImageDraw, xy, text: str, fnt, fill, max_width: int, line_gap=8):
    x, y = xy
    for line in wrap(draw, text, fnt, max_width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + line_gap
    return y


def gradient_background() -> Image.Image:
    img = Image.new("RGB", (W, H), BLACK)
    px = img.load()
    for y in range(H):
        for x in range(W):
            t = x / W
            v = y / H
            teal = max(0, 1 - math.hypot((x - 150) / 620, (y - 420) / 620))
            gold = max(0, 1 - math.hypot((x - 970) / 600, (y - 250) / 600))
            base = int(4 + 6 * v)
            px[x, y] = (
                min(255, base + int(18 * gold)),
                min(255, base + int(22 * teal) + int(12 * gold)),
                min(255, base + int(20 * teal)),
            )
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for i in range(-W, W, 42):
        alpha = 22 if i % 84 == 0 else 12
        d.line((i, H, i + W, 0), fill=(255, 255, 255, alpha), width=1)
    d.rectangle((0, 0, W, H), outline=(38, 42, 39, 255), width=2)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def rounded_panel(draw: ImageDraw.ImageDraw, box, radius=28, fill=PANEL, outline=LINE, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def brand(draw: ImageDraw.ImageDraw):
    x, y = 58, 54
    draw.rounded_rectangle((x, y, x + 54, y + 54), radius=15, fill=(238, 238, 232), outline=(70, 82, 76), width=1)
    draw.arc((x + 8, y + 12, x + 50, y + 50), 205, 344, fill=TEAL, width=6)
    draw.arc((x + 4, y + 3, x + 48, y + 45), 25, 154, fill=GOLD, width=6)
    draw.text((x + 72, y + 1), "Nexa", font=F_LOGO, fill=INK)
    draw.text((x + 174, y + 1), "Flow", font=F_LOGO, fill=GOLD)


def chip(draw: ImageDraw.ImageDraw, box, text: str, fill=(12, 17, 17), outline=LINE, text_fill=INK):
    draw.rounded_rectangle(box, radius=999, fill=fill, outline=outline, width=2)
    tw = text_width(draw, text, F_CHIP)
    draw.text((box[0] + (box[2] - box[0] - tw) / 2, box[1] + 13), text, font=F_CHIP, fill=text_fill)


def button(draw: ImageDraw.ImageDraw, box, text: str):
    draw.rounded_rectangle(box, radius=18, fill=GOLD, outline=(255, 232, 175), width=2)
    tw = text_width(draw, text, F_BODY_B)
    draw.text((box[0] + (box[2] - box[0] - tw) / 2, box[1] + 18), text, font=F_BODY_B, fill=(0, 0, 0))


def card_1():
    img = gradient_background()
    d = ImageDraw.Draw(img)
    brand(d)
    chip(d, (742, 56, 1018, 114), "30-day trial", fill=(24, 18, 6), outline=(96, 75, 30), text_fill=GOLD)

    d.text((64, 186), "30 天免费试用", font=F_H1, fill=INK)
    d.text((64, 276), "AI WhatsApp 询盘助手", font=F_H1_SMALL, fill=GOLD)
    draw_wrapped(d, (68, 366), "一个链接收询盘，智能整理，WhatsApp 跟进。", F_BODY, MUTED, 900, 10)

    rounded_panel(d, (64, 520, 1016, 804), radius=28, fill=(8, 12, 12), outline=(45, 61, 56))
    items = [
        ("01", "客户提交询问", "姓名、电话、需求、同意资料用途"),
        ("02", "智能整理", "判断报价、预约、普通询问和优先级"),
        ("03", "商家快速跟进", "在 private inbox 里查看，再用 WhatsApp 回复"),
    ]
    y = 554
    for num, title, body in items:
        d.ellipse((92, y + 2, 144, y + 54), fill=GOLD)
        d.text((107, y + 14), num, font=F_SMALL, fill=(0, 0, 0))
        d.text((166, y), title, font=F_BODY_B, fill=INK)
        d.text((166, y + 40), body, font=F_SMALL, fill=MUTED)
        y += 82

    button(d, (64, 870, 430, 940), "申请 30 天试用")
    d.text((458, 886), "Built for renovation, beauty, repair, cleaning, tuition.", font=F_SMALL, fill=MUTED)
    img.save(OUT_DIR / "nexaflow-social-trial-30days.png", quality=95)


def card_2():
    img = gradient_background()
    d = ImageDraw.Draw(img)
    brand(d)
    chip(d, (760, 56, 1018, 114), "No website needed", fill=(7, 22, 21), outline=(28, 92, 88), text_fill=TEAL)

    d.text((64, 178), "没有网站，", font=F_H1, fill=INK)
    d.text((64, 268), "也可以收客户询盘", font=F_H1_SMALL, fill=GOLD)
    draw_wrapped(d, (68, 362), "一个链接可放到 WhatsApp、FB、IG 或 Google Business Profile。", F_BODY, MUTED, 920, 10)

    rounded_panel(d, (70, 525, 1010, 742), radius=32, fill=(8, 12, 12), outline=(45, 61, 56))
    x_positions = [138, 368, 598, 828]
    labels = ["WhatsApp", "Facebook", "Instagram", "Link"]
    colors = [TEAL, (90, 132, 255), (245, 116, 178), GOLD]
    for i, (x, label, col) in enumerate(zip(x_positions, labels, colors)):
        d.rounded_rectangle((x - 78, 575, x + 78, 660), radius=22, fill=(13, 17, 17), outline=col, width=2)
        d.text((x - text_width(d, label, F_SMALL) / 2, 604), label, font=F_SMALL, fill=INK)
        if i < 3:
            d.line((x + 88, 617, x_positions[-1] - 94, 617), fill=(92, 98, 94), width=2)
            d.polygon([(x_positions[-1] - 94, 617), (x_positions[-1] - 110, 607), (x_positions[-1] - 110, 627)], fill=(92, 98, 94))

    rounded_panel(d, (166, 790, 914, 912), radius=28, fill=(11, 15, 15), outline=(78, 68, 40))
    d.text((206, 820), "客户点链接 -> 填需求 -> 商家 inbox 收到", font=F_BODY_B, fill=INK)
    d.text((206, 865), "Customer opens link -> submits enquiry -> merchant follows up.", font=F_SMALL, fill=MUTED)
    img.save(OUT_DIR / "nexaflow-social-no-website-needed.png", quality=95)


def card_3():
    img = gradient_background()
    d = ImageDraw.Draw(img)
    brand(d)
    chip(d, (780, 56, 1018, 114), "Follow-up", fill=(24, 18, 6), outline=(96, 75, 30), text_fill=GOLD)

    d.text((64, 178), "客户问了，", font=F_H1, fill=INK)
    d.text((64, 268), "不要再漏跟进", font=F_H1_SMALL, fill=GOLD)
    draw_wrapped(d, (68, 362), "把 WhatsApp、FB、IG 询盘集中到 private inbox，让商家知道该回谁。", F_BODY, MUTED, 900, 10)

    rounded_panel(d, (64, 512, 1016, 835), radius=30, fill=(8, 12, 12), outline=(45, 61, 56))
    stages = [
        ("新询盘", "New lead", TEAL),
        ("智能整理", "Intent + priority", GOLD),
        ("WhatsApp 跟进", "Reply faster", (255, 255, 255)),
    ]
    y = 560
    for idx, (zh, en, col) in enumerate(stages, start=1):
        d.rounded_rectangle((104, y, 198, y + 58), radius=18, fill=(14, 18, 18), outline=col, width=2)
        d.text((134, y + 12), f"{idx}", font=F_BODY_B, fill=col)
        d.text((230, y - 2), zh, font=F_BODY_B, fill=INK)
        d.text((230, y + 39), en, font=F_SMALL, fill=MUTED)
        if idx < len(stages):
            d.line((151, y + 72, 151, y + 94), fill=(78, 86, 82), width=3)
        y += 94

    button(d, (64, 890, 412, 960), "DM 申请试用")
    draw_wrapped(d, (442, 898), "For WhatsApp-based local service businesses.", F_SMALL, MUTED, 560, 6)
    img.save(OUT_DIR / "nexaflow-social-followup-inbox.png", quality=95)


def main():
    card_1()
    card_2()
    card_3()
    print("Generated:")
    print(OUT_DIR / "nexaflow-social-trial-30days.png")
    print(OUT_DIR / "nexaflow-social-no-website-needed.png")
    print(OUT_DIR / "nexaflow-social-followup-inbox.png")


if __name__ == "__main__":
    main()
