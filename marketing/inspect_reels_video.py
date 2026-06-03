from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = ROOT / ".video_deps"
if DEPS.exists():
    sys.path.insert(0, str(DEPS))

import imageio.v2 as imageio
from PIL import Image

VIDEO = ROOT / "marketing" / "nexaflow-enquiry-reels.mp4"
PREVIEW = ROOT / "marketing" / "nexaflow-enquiry-reels-preview.png"

reader = imageio.get_reader(VIDEO)
meta = reader.get_meta_data()
fps = meta.get("fps", 20)
duration = meta.get("duration", 0)

thumbs = []
for second in [0, 5, 10, 15, 21]:
    frame = reader.get_data(int(second * fps))
    thumbs.append(Image.fromarray(frame).resize((216, 384), Image.Resampling.LANCZOS))

sheet = Image.new("RGB", (216 * len(thumbs), 384), (0, 0, 0))
for index, thumb in enumerate(thumbs):
    sheet.paste(thumb, (216 * index, 0))
sheet.save(PREVIEW)

print(f"size={meta.get('size')} fps={fps} duration={duration}")
print(f"preview={PREVIEW}")
