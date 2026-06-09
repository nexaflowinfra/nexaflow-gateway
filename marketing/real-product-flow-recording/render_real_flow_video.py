from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".video_deps"))

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


BASE = Path(__file__).resolve().parent
FRAMES = BASE / "frames"
OUT = BASE / "nexaflow-real-product-flow.mp4"
SHEET = BASE / "nexaflow-real-product-flow-contact-sheet.png"

W, H = 1080, 1920
FPS = 24
PHONE_W, PHONE_H = 614, 1328
PHONE_X, PHONE_Y = 233, 126

BG = (3, 5, 5)
TEXT = (248, 248, 244)
MUTED = (174, 178, 172)
GOLD = (245, 198, 94)
TEAL = (35, 226, 214)
PANEL = (8, 11, 11)


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


F_LOGO = font(44, True)
F_STEP = font(25, True)
F_CAP_ZH = font(37, True)
F_CAP_EN = font(26)
F_SMALL = font(22)
F_BADGE = font(21, True)


SCENES = [
    {
        "file": "01-product-hero.png",
        "title": "Step 1 - Open Enquiry",
        "zh": "打开产品页，点 Try Demo 或 Merchant Login。",
        "en": "Open the product page. Try Demo or Merchant Login.",
        "cursor": [(0.15, 246, 430), (0.55, 74, 594), (0.82, 74, 594)],
        "click": (0.62, 74, 594),
        "duration": 4.6,
    },
    {
        "file": "08-merchant-login-filled.png",
        "title": "Step 2 - Open Inbox",
        "zh": "输入 slug 和 access key。key 只给商家。",
        "en": "Enter slug and access key. Keep the key private.",
        "cursor": [(0.18, 232, 424), (0.56, 88, 496), (0.84, 88, 496)],
        "click": (0.68, 88, 496),
        "duration": 4.8,
    },
    {
        "file": "11-copy-main-link.png",
        "title": "Step 3 - Share Link",
        "zh": "复制客户链接，发到 WhatsApp、FB、IG 或网站。",
        "en": "Copy the customer link and share it anywhere.",
        "cursor": [(0.18, 112, 252), (0.58, 204, 320), (0.84, 204, 320)],
        "click": (0.7, 204, 320),
        "duration": 4.8,
    },
    {
        "file": "14-customer-enquiry-form.png",
        "title": "Step 4 - Customer Sends Enquiry",
        "zh": "客户打开链接，选择需求并提交。",
        "en": "Customers open the link, choose a need, and submit.",
        "cursor": [(0.18, 244, 236), (0.55, 84, 331), (0.84, 84, 331)],
        "click": (0.64, 84, 331),
        "duration": 4.8,
    },
    {
        "file": "06-demo-result.png",
        "title": "Step 5 - AI Sorts It",
        "zh": "AI 判断意向、优先级和跟进方向。",
        "en": "AI identifies intent, priority, and next action.",
        "duration": 4.8,
    },
    {
        "file": "09-merchant-inbox.png",
        "title": "Step 6 - Follow Up",
        "zh": "商家回到 inbox，用 WhatsApp 跟进。",
        "en": "Return to the inbox and follow up on WhatsApp.",
        "duration": 4.8,
    },
    {
        "file": "13-lead-contacted.png",
        "title": "Step 7 - Update Status",
        "zh": "回复后更新状态，避免漏单。",
        "en": "Update the lead status so no enquiry gets lost.",
        "cursor": [(0.18, 306, 546), (0.58, 310, 422), (0.84, 310, 422)],
        "click": (0.7, 310, 422),
        "duration": 4.6,
    },
]


def ease(x: float) -> float:
    x = max(0, min(1, x))
    return x * x * (3 - 2 * x)


def cursor_opacity(p: float) -> int:
    fade_in = ease(min(1, max(0, (p - 0.06) / 0.18)))
    fade_out = ease(min(1, max(0, (0.98 - p) / 0.18)))
    return int(230 * min(fade_in, fade_out))


def draw_wrapped(draw: ImageDraw.ImageDraw, xy, text: str, fnt, fill, max_width: int, line_gap=8):
    x, y = xy
    words = list(text) if any("\u4e00" <= ch <= "\u9fff" for ch in text) else text.split()
    lines = []
    line = ""
    for word in words:
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
            trial = f"{line}{word}"
        else:
            trial = f"{line} {word}".strip()
        if draw.textbbox((0, 0), trial, font=fnt)[2] <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + line_gap
    return y


def load_phone(path: Path) -> Image.Image:
    im = Image.open(path).convert("RGB")
    return im.resize((PHONE_W, PHONE_H), Image.Resampling.LANCZOS)


def cursor_xy(points, p: float):
    if not points:
        return None
    if len(points) == 1:
        return points[0][1], points[0][2]
    before = points[0]
    after = points[-1]
    for idx in range(len(points) - 1):
        if points[idx][0] <= p <= points[idx + 1][0]:
            before = points[idx]
            after = points[idx + 1]
            break
    if after[0] == before[0]:
        return after[1], after[2]
    local = max(0, min(1, (p - before[0]) / (after[0] - before[0])))
    local = ease(local)
    return before[1] + (after[1] - before[1]) * local, before[2] + (after[2] - before[2]) * local


def draw_cursor(base: Image.Image, x: float, y: float, click=False, phase=0.0, opacity=230):
    sx = PHONE_X + int(x * PHONE_W / 390)
    sy = PHONE_Y + int(y * PHONE_H / 844)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    opacity = max(0, min(255, opacity))
    if click:
        pulse = 1 - max(0, min(1, phase))
        r1 = 26 + int(34 * (1 - pulse))
        r2 = 18 + int(18 * (1 - pulse))
        ring_alpha = int(opacity * 0.55 * pulse)
        if ring_alpha > 0:
            draw.ellipse((sx - r1, sy - r1, sx + r1, sy + r1), outline=(*GOLD, ring_alpha), width=4)
            draw.ellipse((sx - r2, sy - r2, sx + r2, sy + r2), outline=(*TEAL, int(ring_alpha * 0.55)), width=2)
    shadow_pts = [(sx + 4, sy + 5), (sx + 8, sy + 57), (sx + 22, sy + 43), (sx + 36, sy + 77), (sx + 52, sy + 69), (sx + 37, sy + 36), (sx + 60, sy + 36)]
    draw.polygon(shadow_pts, fill=(0, 0, 0, int(opacity * 0.42)))
    pts = [(sx, sy), (sx + 4, sy + 52), (sx + 18, sy + 38), (sx + 32, sy + 72), (sx + 48, sy + 64), (sx + 33, sy + 31), (sx + 56, sy + 31)]
    draw.polygon(pts, fill=(255, 255, 250, opacity), outline=(18, 18, 18, opacity))
    base.alpha_composite(overlay)


def frame_for(scene, local_t: float) -> Image.Image:
    p = min(1, max(0, local_t / scene["duration"]))
    canvas = Image.new("RGB", (W, H), BG)
    bg = Image.new("RGB", (W, H), BG)
    bd = ImageDraw.Draw(bg)
    bd.rectangle((0, 0, W, 150), fill=(10, 12, 12))
    bd.rectangle((0, 1550, W, H), fill=(6, 8, 8))
    bd.ellipse((-190, 280, 420, 980), fill=(0, 36, 32))
    bd.ellipse((650, 180, 1280, 900), fill=(36, 26, 5))
    bd.ellipse((260, 1120, 820, 1760), fill=(0, 26, 24))
    bg = bg.filter(ImageFilter.GaussianBlur(58))
    canvas = Image.blend(canvas, bg, 0.68)

    phone = load_phone(FRAMES / scene["file"])
    phone_layer = Image.new("RGBA", (PHONE_W + 24, PHONE_H + 24), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (PHONE_W, PHONE_H), (0, 0, 0, 190))
    phone_layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)), (12, 12))
    phone_layer.alpha_composite(phone.convert("RGBA"), (0, 0))
    canvas.paste(phone_layer.convert("RGB"), (PHONE_X, PHONE_Y))

    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((44, 34, 276, 90), radius=26, fill=PANEL, outline=(46, 58, 52), width=1)
    d.text((74, 44), "Nexa", font=F_LOGO, fill=TEXT)
    d.text((174, 44), "Flow", font=F_LOGO, fill=GOLD)
    d.rounded_rectangle((750, 38, 1036, 88), radius=25, fill=PANEL, outline=(63, 55, 37), width=1)
    d.text((790, 51), "Real product walkthrough", font=F_BADGE, fill=GOLD)
    d.rounded_rectangle((PHONE_X - 4, PHONE_Y - 4, PHONE_X + PHONE_W + 4, PHONE_Y + PHONE_H + 4), radius=28, outline=(52, 63, 55), width=2)

    d.rounded_rectangle((44, 1648, 1036, 1858), radius=28, fill=PANEL, outline=(45, 52, 47), width=2)
    d.text((78, 1676), scene["title"], font=F_STEP, fill=GOLD)
    d.text((78, 1720), scene["zh"], font=F_CAP_ZH, fill=TEXT)
    draw_wrapped(d, (78, 1776), scene["en"], F_CAP_EN, MUTED, 910)

    pos = cursor_xy(scene.get("cursor", []), p) if scene.get("click") else None
    if pos:
        click_phase = 0
        is_click = False
        if scene.get("click"):
            ct, cx, cy = scene["click"]
            if abs(p - ct) < 0.16:
                is_click = True
                click_phase = min(1, abs(p - ct) / 0.16)
        canvas_rgba = canvas.convert("RGBA")
        draw_cursor(canvas_rgba, pos[0], pos[1], click=is_click, phase=click_phase, opacity=cursor_opacity(p))
        canvas = canvas_rgba.convert("RGB")
    return canvas


def render_video():
    writer = imageio.get_writer(
        OUT,
        fps=FPS,
        codec="libx264",
        quality=8,
        pixelformat="yuv420p",
        macro_block_size=1,
    )
    try:
        for scene in SCENES:
            total = int(scene["duration"] * FPS)
            for idx in range(total):
                frame = frame_for(scene, idx / FPS)
                writer.append_data(np.asarray(frame))
    finally:
        writer.close()


def render_sheet():
    thumbs = []
    for scene in SCENES:
        im = frame_for(scene, scene["duration"] * 0.55)
        thumbs.append(im.resize((270, 480), Image.Resampling.LANCZOS))
    cols = 3
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (270 * cols, 480 * rows), (12, 12, 12))
    for idx, thumb in enumerate(thumbs):
        x = (idx % cols) * 270
        y = (idx // cols) * 480
        sheet.paste(thumb, (x, y))
    sheet.save(SHEET, quality=95)


if __name__ == "__main__":
    render_sheet()
    render_video()
    print(OUT)
    print(SHEET)
