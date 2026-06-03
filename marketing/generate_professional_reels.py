from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = ROOT / ".video_deps"
if DEPS.exists():
    sys.path.insert(0, str(DEPS))

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


W, H = 1080, 1920
FPS = 20
MARKETING = ROOT / "marketing"
OUT = MARKETING / "nexaflow-enquiry-professional-reels.mp4"
PREVIEW = MARKETING / "nexaflow-enquiry-professional-reels-preview.png"
PRODUCT_URL = "api.nexaflowinfra.com/ai-enquiry"

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_ARIAL_BOLD = "C:/Windows/Fonts/arialbd.ttf"


SCENES = [
    {
        "duration": 4.2,
        "kicker": "FOR LOCAL SERVICE MERCHANTS",
        "title": "WhatsApp 询盘太乱？",
        "body": "客户一多，报价、预约、follow-up 很容易漏。",
        "bullets": ["消息分散", "回复变慢", "客户跑掉"],
        "accent": "teal",
    },
    {
        "duration": 4.8,
        "kicker": "ONE LINK",
        "title": "给客户一个询盘链接",
        "body": "客户填写名字、电话、需求和同意条款。没有网站也可以用。",
        "bullets": ["Share link", "Collect details", "Get consent"],
        "accent": "gold",
    },
    {
        "duration": 4.8,
        "kicker": "AI ORGANIZES THE LEAD",
        "title": "AI 帮你整理重点",
        "body": "NexaFlow 判断客户想要什么、急不急、下一步该怎么回复。",
        "bullets": ["服务类型", "优先级", "回复草稿"],
        "accent": "teal",
    },
    {
        "duration": 5.0,
        "kicker": "PRIVATE MERCHANT INBOX",
        "title": "老板只看一个 Inbox",
        "body": "新询盘、已报价、跟进中、已成交，全都清楚记录。",
        "bullets": ["New", "Quoted", "Follow-up", "Won"],
        "accent": "gold",
    },
    {
        "duration": 5.2,
        "kicker": "START SIMPLE",
        "title": "先试 1 个月",
        "body": "适合装修、美容、维修、清洁、补习、顾问等服务型商家。",
        "bullets": ["更快回复", "减少漏单", "保护客户资料"],
        "accent": "teal",
    },
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(FONT_ARIAL_BOLD, size)


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in text:
        test = current + token
        if text_size(draw, test, fnt)[0] <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = token
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
    line_gap: int,
) -> int:
    x, y = xy
    for line in text.split("\n"):
        for wrapped in wrap_text(draw, line, fnt, max_width):
            draw.text((x, y), wrapped, font=fnt, fill=fill)
            y += text_size(draw, wrapped, fnt)[1] + line_gap
    return y


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return 1 - (1 - value) ** 3


def fit_cover(img: Image.Image, size: tuple[int, int], zoom: float = 1.0) -> Image.Image:
    img = img.convert("RGB")
    sw, sh = size
    scale = max(sw / img.width, sh / img.height) * zoom
    nw, nh = int(img.width * scale), int(img.height * scale)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - sw) // 2
    top = (nh - sh) // 2
    return resized.crop((left, top, left + sw, top + sh))


def make_base(t_abs: float) -> Image.Image:
    source = MARKETING / "nexaflow-brand-final.png"
    if not source.exists():
        source = MARKETING / "nexaflow-social-cover-photo.png"
    bg = fit_cover(Image.open(source), (W, H), zoom=1.55)
    bg = bg.filter(ImageFilter.GaussianBlur(8))
    bg = ImageEnhance.Brightness(bg).enhance(0.34)
    bg = ImageEnhance.Contrast(bg).enhance(1.12).convert("RGBA")

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pixels = np.zeros((H, W, 4), dtype=np.uint8)
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    pixels[:, :, 0] = (8 + 22 * x).astype(np.uint8)
    pixels[:, :, 1] = (10 + 48 * (1 - x) + 24 * x).astype(np.uint8)
    pixels[:, :, 2] = (14 + 42 * (1 - x)).astype(np.uint8)
    pixels[:, :, 3] = (178 + 48 * y).astype(np.uint8)
    overlay = Image.fromarray(pixels, "RGBA")
    bg = Image.alpha_composite(bg, overlay)

    decor = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(decor)
    for i in range(13):
        points = []
        y0 = 1320 + i * 34
        for px in range(-80, W + 90, 18):
            wave = math.sin(px / 116 + t_abs * 3.3 + i * 0.5) * (28 + i * 3)
            points.append((px, y0 + wave))
        color = (49, 216, 205, max(28, 112 - i * 7)) if i % 2 == 0 else (244, 201, 110, max(28, 100 - i * 7))
        draw.line(points, fill=color, width=2)
    for i in range(52):
        px = int((i * 91 + t_abs * (30 + i % 5 * 6)) % (W + 160) - 80)
        py = int(260 + ((i * 137) % 1000) + math.sin(t_abs * 1.4 + i) * 14)
        color = (49, 216, 205, 45) if i % 2 == 0 else (244, 201, 110, 42)
        r = 1 + (i % 3)
        draw.ellipse((px - r, py - r, px + r, py + r), fill=color)
    return Image.alpha_composite(bg, decor)


def draw_brand(draw: ImageDraw.ImageDraw, canvas: Image.Image) -> None:
    logo_path = MARKETING / "nexaflow-social-avatar-icon.png"
    if logo_path.exists():
        logo = fit_cover(Image.open(logo_path), (76, 76)).convert("RGBA")
        canvas.alpha_composite(logo, (70, 72))
        x = 166
    else:
        x = 70
    draw.text((x, 79), "Nexa", font=font(39, True), fill=(255, 255, 255, 255))
    draw.text((x + 106, 79), "Flow", font=font(39, True), fill=(244, 201, 110, 255))
    draw.rounded_rectangle((756, 76, 1008, 130), radius=27, outline=(244, 201, 110, 140), width=2)
    draw.text((789, 90), "AI Enquiry", font=font(25, True), fill=(255, 239, 199, 245))


def draw_phone_card(draw: ImageDraw.ImageDraw, scene: dict[str, object], local_t: float) -> None:
    rise = int((1 - ease((local_t - 0.45) / 0.72)) * 60)
    alpha = int(235 * ease((local_t - 0.45) / 0.72))
    left, top, right, bottom = 84, 1060 + rise, 996, 1452 + rise
    draw.rounded_rectangle((left, top, right, bottom), radius=38, fill=(3, 5, 8, alpha), outline=(255, 255, 255, 56), width=2)
    accent_color = (49, 216, 205, alpha) if scene["accent"] == "teal" else (244, 201, 110, alpha)
    draw.text((left + 52, top + 40), "NexaFlow Inbox", font=font(31, True), fill=(255, 255, 255, alpha))
    draw.text((right - 250, top + 46), "3 leads today", font=font(22, True), fill=accent_color)
    draw.line((left + 52, top + 100, right - 52, top + 100), fill=(255, 255, 255, int(alpha * 0.14)), width=2)

    scene_idx = SCENES.index(scene)
    rows = [
        [("01", "客户询盘", "名字、电话、需求集中收进来"), ("02", "避免漏单", "每个客户都有清楚状态")],
        [("01", "分享链接", "客户不用下载 app"), ("02", "进入 Inbox", "老板只看一个地方")],
        [("01", "AI 整理", "判断服务类型和优先级"), ("02", "准备回复", "WhatsApp 草稿更快发出")],
        [("01", "新询盘", "今天新客户清楚显示"), ("02", "跟进中", "不会沉在聊天记录里")],
        [("01", "试用 1 个月", "先确认真的帮到生意"), ("02", "保护资料", "客户数据只用于服务跟进")],
    ][min(scene_idx, 4)]

    for idx, (number, title, detail) in enumerate(rows):
        y = top + 138 + idx * 92
        if idx > 0:
            draw.line((left + 58, y - 19, right - 58, y - 19), fill=(255, 255, 255, int(alpha * 0.13)), width=1)
        draw.ellipse((left + 70, y + 5, left + 92, y + 27), fill=accent_color)
        draw.text((left + 116, y - 1), f"{number}  {title}", font=font(25, True), fill=(255, 255, 255, alpha))
        draw.text((left + 116, y + 34), detail, font=font(19, True), fill=(214, 211, 201, int(alpha * 0.92)))

    if scene["accent"] == "teal":
        caption = "Next: WhatsApp reply"
        color = (49, 216, 205, alpha)
    else:
        caption = "Lead saved clearly"
        color = (244, 201, 110, alpha)
    draw.text((left + 62, bottom - 76), caption, font=font(25, True), fill=(255, 245, 222, alpha))


def draw_frame(scene_idx: int, local_t: float, t_abs: float, total: float) -> Image.Image:
    scene = SCENES[scene_idx]
    img = make_base(t_abs)
    draw = ImageDraw.Draw(img)
    draw_brand(draw, img)

    fade = min(1.0, local_t / 0.45, (scene["duration"] - local_t) / 0.45)
    alpha = int(255 * max(0.0, min(1.0, fade)))
    slide = int((1 - ease(local_t / 0.8)) * 58)
    x = 72 - slide
    y = 330
    accent = (49, 216, 205, alpha) if scene["accent"] == "teal" else (244, 201, 110, alpha)

    draw.text((x, y), str(scene["kicker"]), font=font(25, True), fill=accent)
    y += 80
    y = draw_multiline(draw, (x, y), str(scene["title"]), font(76, True), (250, 250, 248, alpha), 908, 14)
    y += 30
    draw_multiline(draw, (x, y), str(scene["body"]), font(35, True), (221, 217, 205, alpha), 900, 15)

    draw_phone_card(draw, scene, local_t)

    if scene_idx == len(SCENES) - 1:
        pulse = 1 + 0.04 * math.sin(t_abs * 6.8)
        cx, cy = 540, 1608
        bw, bh = int(768 * pulse), int(96 * pulse)
        box = (cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2)
        draw.rounded_rectangle(box, radius=48, fill=(244, 201, 110, 245))
        draw.text((214, 1578), "DM / WhatsApp 申请试用", font=font(38, True), fill=(4, 6, 8, 255))

    draw.text((72, H - 132), PRODUCT_URL, font=font(30, True), fill=(255, 239, 199, 230))
    draw.rounded_rectangle((72, H - 72, W - 72, H - 62), radius=5, fill=(255, 255, 255, 34))
    progress_w = int((W - 144) * min(1.0, t_abs / total))
    draw.rounded_rectangle((72, H - 72, 72 + progress_w, H - 62), radius=5, fill=(244, 201, 110, 230))
    return img.convert("RGB")


def scene_at(t_abs: float) -> tuple[int, float]:
    acc = 0.0
    for idx, scene in enumerate(SCENES):
        duration = float(scene["duration"])
        if t_abs < acc + duration:
            return idx, t_abs - acc
        acc += duration
    return len(SCENES) - 1, float(SCENES[-1]["duration"])


def main() -> None:
    total = sum(float(scene["duration"]) for scene in SCENES)
    frame_count = int(total * FPS)
    OUT.parent.mkdir(parents=True, exist_ok=True)

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
            scene_idx, local_t = scene_at(t_abs)
            frame = draw_frame(scene_idx, local_t, t_abs, total)
            writer.append_data(np.asarray(frame))

    thumbs = []
    for t_abs in [0.3, 5.0, 10.0, 15.0, 21.0]:
        scene_idx, local_t = scene_at(t_abs)
        thumb = draw_frame(scene_idx, local_t, t_abs, total).resize((216, 384), Image.Resampling.LANCZOS)
        thumbs.append(thumb)
    sheet = Image.new("RGB", (216 * len(thumbs), 384), (0, 0, 0))
    for idx, thumb in enumerate(thumbs):
        sheet.paste(thumb, (idx * 216, 0))
    sheet.save(PREVIEW)

    print(f"created={OUT}")
    print(f"preview={PREVIEW}")
    print(f"duration={total:.1f}s fps={FPS} size={W}x{H}")


if __name__ == "__main__":
    main()
