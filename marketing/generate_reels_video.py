from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALT_VIDEO_DEPS = ROOT / ".video_deps"
if ALT_VIDEO_DEPS.exists():
    sys.path.insert(0, str(ALT_VIDEO_DEPS))
else:
    VIDEO_DEPS = ROOT / "tools" / "video_deps"
    if VIDEO_DEPS.exists():
        sys.path.insert(0, str(VIDEO_DEPS))

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


W, H = 1080, 1920
OUT = ROOT / "marketing" / "nexaflow-enquiry-reels.mp4"
SOURCE_PREVIEW = ROOT / "marketing" / "nexaflow-enquiry-reels-source-preview.png"
MARKETING = ROOT / "marketing"

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_ARIAL_BOLD = "C:/Windows/Fonts/arialbd.ttf"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(FONT_ARIAL_BOLD, size)


def fit_cover(img: Image.Image, size: tuple[int, int], zoom: float = 1.0) -> Image.Image:
    img = img.convert("RGB")
    sw, sh = size
    scale = max(sw / img.width, sh / img.height) * zoom
    nw, nh = int(img.width * scale), int(img.height * scale)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - sw) // 2
    top = (nh - sh) // 2
    return resized.crop((left, top, left + sw, top + sh))


FPS = 20
_GRADIENT: Image.Image | None = None
_BG_CACHE: dict[int, Image.Image] = {}


def gradient_layer(strength: float = 0.78) -> Image.Image:
    global _GRADIENT
    if _GRADIENT is not None:
        return _GRADIENT
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    teal = (18 * (1 - x) * (1 - y * 0.35)).astype(np.uint8)
    gold = (20 * x * (1 - y * 0.2)).astype(np.uint8)
    red = teal
    green = gold
    blue = np.full((H, W), 10, dtype=np.uint8)
    alpha = np.minimum(235, 255 * (strength * 0.72 + y * 0.18)).astype(np.uint8)
    rgba = np.dstack([
        red,
        green,
        blue,
        np.broadcast_to(alpha, (H, W)),
    ])
    _GRADIENT = Image.fromarray(rgba, "RGBA")
    return _GRADIENT


def overlay_gradient(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    return Image.alpha_composite(img, gradient_layer())


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        current = ""
        for ch in para:
            test = current + ch
            if text_size(draw, test, fnt)[0] <= max_width or not current:
                current = test
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


def draw_multiline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fnt: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    max_width: int,
    line_gap: int = 12,
) -> int:
    x, y = xy
    for line in wrap_text(draw, text, fnt, max_width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += text_size(draw, line, fnt)[1] + line_gap
    return y


def rounded_rect(draw: ImageDraw.ImageDraw, box, radius=34, fill=(8, 10, 10, 210), outline=(255, 255, 255, 32), width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def draw_brand(draw: ImageDraw.ImageDraw, canvas: Image.Image) -> None:
    logo = Image.open(MARKETING / "nexaflow-social-avatar-icon.png").convert("RGBA")
    logo = fit_cover(logo, (58, 58)).convert("RGBA")
    logo.putalpha(255)
    canvas.alpha_composite(logo, (70, 72))
    draw.text((148, 78), "Nexa", font=font(37, True), fill=(255, 255, 255, 255))
    draw.text((248, 78), "Flow", font=font(37, True), fill=(243, 199, 106, 255))


def draw_wave(draw: ImageDraw.ImageDraw, t: float) -> None:
    for i in range(9):
        y = 1450 + i * 34
        points = []
        for x in range(-60, W + 80, 18):
            amp = 26 + i * 3
            yy = y + math.sin((x / 115) + t * 4.0 + i * 0.45) * amp
            points.append((x, yy))
        color = (45, 212, 191, max(35, 118 - i * 8)) if i % 2 == 0 else (243, 199, 106, max(32, 95 - i * 7))
        draw.line(points, fill=color, width=2)


def progress_bar(draw: ImageDraw.ImageDraw, t_abs: float, total: float) -> None:
    margin = 70
    y = H - 68
    draw.rounded_rectangle((margin, y, W - margin, y + 8), radius=4, fill=(255, 255, 255, 35))
    draw.rounded_rectangle((margin, y, margin + int((W - margin * 2) * min(1, t_abs / total)), y + 8), radius=4, fill=(243, 199, 106, 220))


def scene_bg(scene_idx: int, local_t: float, duration: float) -> Image.Image:
    if scene_idx in _BG_CACHE:
        return _BG_CACHE[scene_idx].copy()
    source = MARKETING / f"nexaflow-carousel-{min(scene_idx + 1, 5):02d}.png"
    img = Image.open(source).convert("RGB")
    bg = fit_cover(img, (W, H), zoom=1.3)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=5))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)
    bg = ImageEnhance.Contrast(bg).enhance(1.05)
    _BG_CACHE[scene_idx] = overlay_gradient(bg)
    return _BG_CACHE[scene_idx].copy()


SCENES = [
    {
        "duration": 4.2,
        "eyebrow": "本地服务型商家",
        "title": "WhatsApp 询盘太乱？",
        "subtitle": "客户一多，follow-up 就容易漏。",
        "en": "Too many scattered enquiries? Follow-ups get missed.",
        "cards": ["WhatsApp", "Facebook", "Instagram DM"],
    },
    {
        "duration": 5.0,
        "eyebrow": "常见问题",
        "title": "不是没有客户，\n是回复和跟进太分散。",
        "subtitle": "问价、预约、报价、服务内容，都散在不同聊天里。",
        "en": "Messages spread across chats become missed revenue.",
        "cards": ["客户信息分散", "老板忙起来忘记回", "机会流失"],
    },
    {
        "duration": 5.0,
        "eyebrow": "NexaFlow Enquiry",
        "title": "一个链接，\n一个私人 inbox。",
        "subtitle": "客户填写询盘，商家在同一个地方看到所有 leads。",
        "en": "One enquiry link. One private inbox.",
        "cards": ["客户填写询盘", "进入私人 inbox", "记录跟进状态"],
    },
    {
        "duration": 5.2,
        "eyebrow": "AI 协助回复",
        "title": "AI 准备 WhatsApp\n回复草稿。",
        "subtitle": "让商家更快回复客户，也更容易继续 follow-up。",
        "en": "AI prepares WhatsApp reply drafts so merchants respond faster.",
        "cards": ["分类重点", "准备回复", "减少漏单"],
    },
    {
        "duration": 5.6,
        "eyebrow": "开放 trial",
        "title": "先试 1 个月。\n真的帮到你，再继续用。",
        "subtitle": "适合装修、美容、清洁、维修、补习和顾问服务。",
        "en": "Built for local service merchants. DM us for a simple demo.",
        "cards": ["1 个月 trial", "保护客户资料", "可扩展 CRM / Billing"],
    },
]


def draw_frame(scene_idx: int, local_t: float, scene_duration: float, t_abs: float, total: float) -> Image.Image:
    scene = SCENES[scene_idx]
    img = scene_bg(scene_idx, local_t, scene_duration)
    draw = ImageDraw.Draw(img)
    draw_wave(draw, t_abs)
    draw_brand(draw, img)

    fade = min(1.0, local_t / 0.55, (scene_duration - local_t) / 0.55)
    y_shift = int((1 - fade) * 28)
    alpha = int(255 * max(0, min(1, fade)))

    x = 72
    y = 430 + y_shift
    draw.text((x, y), scene["eyebrow"].upper(), font=font(25, True), fill=(243, 199, 106, alpha))
    y += 76
    y = draw_multiline(draw, (x, y), scene["title"], font(67, True), (248, 250, 247, alpha), 900, 18)
    y += 30
    y = draw_multiline(draw, (x, y), scene["subtitle"], font(34, True), (225, 214, 193, alpha), 890, 14)
    y += 18
    draw_multiline(draw, (x, y), scene["en"], font(26, False), (185, 179, 165, alpha), 890, 10)

    card_top = 1110
    for idx, text in enumerate(scene["cards"]):
        delay = idx * 0.15
        card_fade = min(1, max(0, (local_t - 0.7 - delay) / 0.45))
        ca = int(210 * card_fade)
        cy = card_top + idx * 128 + int((1 - card_fade) * 24)
        rounded_rect(draw, (72, cy, W - 72, cy + 96), radius=28, fill=(8, 10, 10, ca), outline=(255, 255, 255, int(42 * card_fade)))
        badge_fill = (243, 199, 106, int(255 * card_fade)) if idx == 0 else (45, 212, 191, int(220 * card_fade))
        draw.rounded_rectangle((100, cy + 24, 148, cy + 72), radius=14, fill=(255, 255, 255, int(30 * card_fade)), outline=badge_fill, width=2)
        draw.text((116, cy + 30), str(idx + 1), font=font(24, True), fill=(255, 255, 255, int(240 * card_fade)))
        draw.text((174, cy + 27), text, font=font(31, True), fill=(248, 250, 247, int(250 * card_fade)))

    if scene_idx == len(SCENES) - 1:
        draw.rounded_rectangle((72, 1584, 540, 1668), radius=42, fill=(243, 199, 106, 240))
        draw.text((112, 1604), "DM / WhatsApp 我们试用", font=font(30, True), fill=(5, 5, 5, 255))

    draw.text((W - 332, H - 140), "nexaflowinfra.com", font=font(29, True), fill=(225, 214, 193, 225))
    progress_bar(draw, t_abs, total)
    return img.convert("RGB")


def main() -> None:
    total = sum(scene["duration"] for scene in SCENES)
    frame_count = int(total * FPS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    preview_times = [0, 5, 10, 15, 21]
    thumbs = []
    for t_abs in preview_times:
        acc = 0.0
        scene_idx = 0
        local_t = t_abs
        for idx, scene in enumerate(SCENES):
            if t_abs < acc + scene["duration"]:
                scene_idx = idx
                local_t = t_abs - acc
                break
            acc += scene["duration"]
        thumbs.append(draw_frame(scene_idx, local_t, SCENES[scene_idx]["duration"], t_abs, total).resize((216, 384), Image.Resampling.LANCZOS))
    sheet = Image.new("RGB", (216 * len(thumbs), 384), (0, 0, 0))
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, (216 * index, 0))
    sheet.save(SOURCE_PREVIEW)

    with imageio.get_writer(
        OUT,
        format="FFMPEG",
        fps=FPS,
        codec="libx264",
        quality=8,
        macro_block_size=1,
        output_params=["-pix_fmt", "yuv420p", "-vf", "scale=1080:1920"],
        ffmpeg_log_level="error",
    ) as writer:
        for frame_i in range(frame_count):
            t_abs = frame_i / FPS
            acc = 0.0
            scene_idx = 0
            local_t = t_abs
            for idx, scene in enumerate(SCENES):
                if t_abs < acc + scene["duration"]:
                    scene_idx = idx
                    local_t = t_abs - acc
                    break
                acc += scene["duration"]
            frame = draw_frame(scene_idx, local_t, SCENES[scene_idx]["duration"], t_abs, total)
            writer.append_data(np.asarray(frame))

    print(f"created {OUT}")


if __name__ == "__main__":
    main()
