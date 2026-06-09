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

BG = (5, 7, 7)
TEXT = (248, 248, 244)
MUTED = (174, 178, 172)
GOLD = (245, 198, 94)
TEAL = (35, 226, 214)


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


SCENES = [
    {
        "file": "01-product-hero.png",
        "title": "真实产品页",
        "zh": "商家先打开 NexaFlow 产品页。",
        "en": "Open the real NexaFlow product page.",
        "cursor": [(0.18, 258, 180), (0.54, 178, 180), (0.84, 178, 180)],
        "click": (0.62, 178, 180),
        "duration": 3.8,
    },
    {
        "file": "02-product-singapore.png",
        "title": "选择市场",
        "zh": "选择新加坡或马来西亚，对应本地价格。",
        "en": "Choose Singapore or Malaysia for local pricing.",
        "cursor": [(0.18, 54, 180), (0.56, 94, 180), (0.84, 94, 180)],
        "click": (0.64, 94, 180),
        "duration": 4.2,
    },
    {
        "file": "03-product-chinese.png",
        "title": "中英切换",
        "zh": "页面支持英文和中文，比较适合本地商家。",
        "en": "English and Chinese are both supported.",
        "duration": 4.2,
    },
    {
        "file": "04-demo-form.png",
        "title": "客户提交询盘",
        "zh": "客户填写名字、电话、需求，并同意资料用途。",
        "en": "Customers submit contact details, message, and consent.",
        "cursor": [(0.18, 280, 650), (0.58, 78, 745), (0.84, 78, 745)],
        "click": (0.66, 78, 745),
        "duration": 4.4,
    },
    {
        "file": "06-demo-result.png",
        "title": "AI 自动整理",
        "zh": "系统会判断意向、优先级，并准备回复方向。",
        "en": "AI classifies intent, priority, and reply direction.",
        "duration": 4.5,
    },
    {
        "file": "07-merchant-login.png",
        "title": "商家后台入口",
        "zh": "后台需要 business slug 和私密 access key。",
        "en": "The inbox is protected by slug and private access key.",
        "duration": 4.3,
    },
    {
        "file": "08-merchant-login-filled.png",
        "title": "安全登录",
        "zh": "真实 key 不放进公开链接，也不要发给客户。",
        "en": "The real key is not placed in public links.",
        "cursor": [(0.18, 230, 424), (0.56, 88, 496), (0.84, 88, 496)],
        "click": (0.68, 88, 496),
        "duration": 4.4,
    },
    {
        "file": "09-merchant-inbox.png",
        "title": "Private inbox",
        "zh": "商家进入私人 inbox，看今天该跟进谁。",
        "en": "The merchant sees today's follow-up priorities.",
        "duration": 4.4,
    },
    {
        "file": "11-copy-main-link.png",
        "title": "复制客户链接",
        "zh": "一个 enquiry link 可以放在 WhatsApp、FB、IG 或网站。",
        "en": "One enquiry link can be shared anywhere.",
        "cursor": [(0.18, 112, 356), (0.58, 212, 424), (0.84, 212, 424)],
        "click": (0.7, 212, 424),
        "duration": 4.6,
    },
    {
        "file": "12-lead-pipeline.png",
        "title": "Lead pipeline",
        "zh": "所有客户询盘都会进入同一个跟进流程。",
        "en": "Every lead stays visible in one workflow.",
        "duration": 4.3,
    },
    {
        "file": "13-lead-contacted.png",
        "title": "更新状态",
        "zh": "跟进后可把 lead 标记为 contacted 或 quoted。",
        "en": "After replying, update the lead status.",
        "cursor": [(0.18, 306, 546), (0.58, 310, 422), (0.84, 310, 422)],
        "click": (0.7, 310, 422),
        "duration": 4.4,
    },
    {
        "file": "14-customer-enquiry-form.png",
        "title": "客户看到的页面",
        "zh": "没有网站的商家，也可以直接分享这个询盘页。",
        "en": "Merchants without a website can still use this link.",
        "duration": 4.2,
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
    words = text.split()
    lines = []
    line = ""
    for word in words:
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
    bg = Image.new("RGB", (W, H), (4, 8, 8))
    bd = ImageDraw.Draw(bg)
    for i in range(0, W, 28):
        alpha = int(28 + 22 * (i / W))
        color = (0, alpha, alpha) if i < W // 2 else (alpha, int(alpha * 0.78), 0)
        bd.line((i, H, i + 620, 0), fill=color, width=1)
    bg = bg.filter(ImageFilter.GaussianBlur(1.4))
    canvas = Image.blend(canvas, bg, 0.32)

    phone = load_phone(FRAMES / scene["file"])
    phone_layer = Image.new("RGBA", (PHONE_W + 24, PHONE_H + 24), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (PHONE_W, PHONE_H), (0, 0, 0, 190))
    phone_layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)), (12, 12))
    phone_layer.alpha_composite(phone.convert("RGBA"), (0, 0))
    canvas.paste(phone_layer.convert("RGB"), (PHONE_X, PHONE_Y))

    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((44, 34, 260, 90), radius=26, fill=(7, 10, 10), outline=(46, 58, 52), width=1)
    d.text((74, 44), "Nexa", font=F_LOGO, fill=TEXT)
    d.text((174, 44), "Flow", font=F_LOGO, fill=GOLD)
    d.rounded_rectangle((PHONE_X - 4, PHONE_Y - 4, PHONE_X + PHONE_W + 4, PHONE_Y + PHONE_H + 4), radius=28, outline=(52, 63, 55), width=2)

    d.rounded_rectangle((44, 1648, 1036, 1858), radius=34, fill=(8, 11, 11), outline=(45, 52, 47), width=2)
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
    sheet = Image.new("RGB", (270 * 3, 480 * 4), (12, 12, 12))
    for idx, thumb in enumerate(thumbs):
        x = (idx % 3) * 270
        y = (idx // 3) * 480
        sheet.paste(thumb, (x, y))
    sheet.save(SHEET, quality=95)


if __name__ == "__main__":
    render_sheet()
    render_video()
    print(OUT)
    print(SHEET)
