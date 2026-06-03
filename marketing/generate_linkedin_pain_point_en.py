from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".video_deps"))

from PIL import Image, ImageDraw, ImageFont, ImageFilter


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "nexaflow-linkedin-pain-point-en.png"

W, H = 1080, 1350
BG = (3, 5, 5)
TEXT = (248, 250, 247)
MUTED = (190, 184, 172)
SOFT = (224, 216, 201)
TEAL = (45, 212, 191)
GOLD = (243, 199, 106)
PANEL = (14, 17, 17)
LINE = (52, 57, 55)


def font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for item in candidates:
        p = Path(item)
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


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


def rounded_panel(draw, box, radius=28, fill=PANEL, outline=LINE, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    x1, y1, x2, y2 = box
    draw.line((x1 + 30, y1, x2 - 30, y1), fill=(72, 72, 64), width=1)


def add_glow(base, xy, color, radius=220, alpha=120):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    x, y = xy
    gd.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
    base.alpha_composite(glow)


img = Image.new("RGBA", (W, H), BG + (255,))
add_glow(img, (120, 160), TEAL, 270, 80)
add_glow(img, (990, 260), GOLD, 280, 78)
add_glow(img, (520, 1200), TEAL, 330, 44)
draw = ImageDraw.Draw(img)

grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
gd = ImageDraw.Draw(grid)
for x in range(0, W, 96):
    gd.line((x, 0, x, H), fill=(255, 255, 255, 14), width=1)
for y in range(0, H, 96):
    gd.line((0, y, W, y), fill=(255, 255, 255, 9), width=1)
grid = grid.filter(ImageFilter.GaussianBlur(0.35))
img.alpha_composite(grid)

for i in range(16):
    y = 1040 + i * 15
    draw.arc((-240, y - 220, 1320, y + 300), 190, 345, fill=(*TEAL, 58 - i * 2), width=2)
    draw.arc((-120, y - 160, 1380, y + 260), 205, 360, fill=(*GOLD, 50 - i * 2), width=2)

brand_font = font(32, True)
small_font = font(20, True)
eyebrow_font = font(20, True)
h_font = font(64, True)
h2_font = font(38, True)
body_font = font(28, False)
body_bold = font(30, True)
footer_font = font(24, True)

draw.rounded_rectangle((64, 56, 108, 100), radius=12, fill=TEAL)
draw.rounded_rectangle((70, 58, 112, 102), radius=12, fill=GOLD)
draw.text((132, 62), "NexaFlow", font=brand_font, fill=TEXT)
draw.text((64, 154), "THE COMMON PROBLEM", font=eyebrow_font, fill=GOLD)

y = 205
for line in ["Not a customer problem.", "It's a follow-up problem."]:
    draw.text((64, y), line, font=h_font, fill=TEXT)
    y += 76

lead = "Many service businesses get enquiries every day, but the messages are scattered across WhatsApp, Facebook, Instagram, and calls."
y += 24
for line in wrap(draw, lead, body_font, 915):
    draw.text((64, y), line, font=body_font, fill=SOFT)
    y += 40

cards = [
    ("WA", "Customer messages are scattered", "WhatsApp, Messenger, Instagram DM, calls, and notes all live in different places."),
    ("?", "Busy owners forget to reply", "A customer asks for price, timing, or service details, but there is no clear follow-up record."),
    ("$", "Slow replies lose revenue", "Reply one step too late, and the customer may already choose another provider."),
]

y = 525
for icon, title, desc in cards:
    rounded_panel(draw, (64, y, 1016, y + 166), radius=26)
    draw.rounded_rectangle((94, y + 42, 178, y + 126), radius=22, fill=(255, 255, 255, 16), outline=(255, 255, 255, 34), width=2)
    iw = draw.textbbox((0, 0), icon, font=body_bold)
    draw.text((136 - (iw[2] - iw[0]) / 2, y + 66), icon, font=body_bold, fill=TEAL if icon != "$" else GOLD)
    draw.text((208, y + 35), title, font=body_bold, fill=TEXT)
    dy = y + 82
    for line in wrap(draw, desc, body_font, 745):
        draw.text((208, dy), line, font=body_font, fill=MUTED)
        dy += 36
    y += 192

rounded_panel(draw, (64, 1128, 1016, 1234), radius=28, fill=(7, 9, 9), outline=(96, 76, 38), width=2)
draw.text((94, 1156), "NexaFlow Enquiry", font=h2_font, fill=GOLD)
solution = "Collect enquiries, organize follow-ups, and prepare faster WhatsApp replies."
draw.text((94, 1203), solution, font=footer_font, fill=SOFT)

draw.text((64, 1272), "Built for local service merchants", font=footer_font, fill=MUTED)
draw.rounded_rectangle((760, 1250, 1016, 1306), radius=28, fill=(245, 216, 153))
draw.text((802, 1263), "Start 1-month trial", font=footer_font, fill=(5, 5, 5))

img.convert("RGB").save(OUT, quality=95)
print(OUT)
