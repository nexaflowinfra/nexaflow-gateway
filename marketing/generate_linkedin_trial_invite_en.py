from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".video_deps"))

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "nexaflow-linkedin-trial-invite-en.png"
BRAND = ROOT / "nexaflow-brand-final.png"

W, H = 1080, 1350
TEXT = (248, 250, 247)
MUTED = (190, 184, 172)
SOFT = (224, 216, 201)
TEAL = (45, 212, 191)
GOLD = (243, 199, 106)
PANEL = (8, 10, 10)


def font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for item in candidates:
        path = Path(item)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def fit_cover(img, size, zoom=1.0):
    img = img.convert("RGB")
    sw, sh = size
    scale = max(sw / img.width, sh / img.height) * zoom
    nw, nh = int(img.width * scale), int(img.height * scale)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - sw) // 2
    top = (nh - sh) // 2
    return resized.crop((left, top, left + sw, top + sh))


def wrap(draw, text, fnt, width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = word if not current else current + " " + word
        if draw.textbbox((0, 0), test, font=fnt)[2] <= width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


if BRAND.exists():
    img = fit_cover(Image.open(BRAND), (W, H), zoom=1.24).convert("RGBA")
    img = img.filter(ImageFilter.GaussianBlur(7))
    img = ImageEnhance.Brightness(img).enhance(0.44)
    img = ImageEnhance.Contrast(img).enhance(1.08)
else:
    img = Image.new("RGBA", (W, H), (3, 5, 4, 255))

overlay = Image.new("RGBA", (W, H), (0, 0, 0, 128))
img.alpha_composite(overlay)
draw = ImageDraw.Draw(img, "RGBA")

draw.rounded_rectangle((64, 58, 108, 102), radius=12, fill=TEAL)
draw.rounded_rectangle((70, 62, 112, 106), radius=12, fill=GOLD)
draw.text((132, 62), "Nexa", font=font(32, True), fill=TEXT)
nx = draw.textbbox((132, 62), "Nexa", font=font(32, True))[2]
draw.text((nx, 62), "Flow", font=font(32, True), fill=GOLD)

draw.rounded_rectangle((64, 160, 408, 210), radius=25, fill=(8, 10, 10, 224), outline=(243, 199, 106, 130), width=2)
draw.text((88, 174), "1-MONTH TRIAL OPEN", font=font(18, True), fill=GOLD)

y = 275
headline = ["Turn scattered enquiries", "into one simple inbox."]
for line in headline:
    draw.text((64, y), line, font=font(62, True), fill=TEXT)
    y += 72

body = "Built for local service businesses that receive customer enquiries from WhatsApp, Facebook, Instagram, calls, and referrals."
y += 18
for line in wrap(draw, body, font(28, False), 880):
    draw.text((64, y), line, font=font(28, False), fill=SOFT)
    y += 40

panel = (64, 620, 1016, 1014)
draw.rounded_rectangle(panel, radius=34, fill=(*PANEL, 224), outline=(255, 255, 255, 34), width=2)
draw.text((104, 664), "NexaFlow Enquiry helps merchants:", font=font(30, True), fill=TEXT)

items = [
    ("01", "Collect enquiries from one link"),
    ("02", "Keep follow-ups organized"),
    ("03", "Prepare faster WhatsApp replies"),
]
y = 735
for number, text in items:
    draw.ellipse((104, y, 154, y + 50), fill=GOLD)
    draw.text((119, y + 9), number, font=font(18, True), fill=(5, 5, 5))
    draw.text((180, y + 7), text, font=font(29, True), fill=TEXT)
    y += 82

draw.rounded_rectangle((64, 1068, 620, 1142), radius=37, fill=GOLD)
draw.text((104, 1088), "Apply for selected trial", font=font(26, True), fill=(5, 5, 5))

draw.text((64, 1206), "Best fit: renovation, beauty, cleaning, repair, tuition, local services", font=font(23, True), fill=MUTED)
draw.text((64, 1252), "api.nexaflowinfra.com/start-trial", font=font(27, True), fill=GOLD)

img.convert("RGB").save(OUT, quality=95)
print(OUT)
