"""Generate icon.ico - a falling note above piano keys. Run once; the .ico is
committed alongside, so building the exe does not require Pillow."""

import os
from PIL import Image, ImageDraw

BG = (14, 15, 22)
WHITE = (242, 243, 247)
BLACK = (22, 24, 32)
NOTE = (94, 205, 255)
NOTE2 = (150, 130, 255)
ACCENT = (94, 205, 255)

S = 256          # master size, downsampled into the .ico
img = Image.new("RGBA", (S, S), BG)
d = ImageDraw.Draw(img)

kb_top = int(S * 0.62)
kb_h = S - kb_top
n_white = 7
ww = S / n_white

# falling notes, aligned to the key columns they land on
for col, (top, bot, col_rgb) in {
    1: (0.10, 0.50, NOTE),
    3: (0.22, 0.58, NOTE2),
    5: (0.04, 0.38, NOTE),
}.items():
    x0 = col * ww + ww * 0.18
    x1 = (col + 1) * ww - ww * 0.18
    d.rounded_rectangle([x0, top * S, x1, bot * S], radius=int(ww * 0.22),
                        fill=col_rgb)

# the strike line the notes land on
d.rectangle([0, kb_top - int(S * 0.018), S, kb_top], fill=ACCENT)

# white keys
for i in range(n_white):
    x0 = int(round(i * ww))
    x1 = int(round((i + 1) * ww))
    d.rectangle([x0, kb_top, x1 - 1, S], fill=WHITE, outline=(168, 174, 190))

# black keys, centred on the boundary between their neighbouring whites
bw = ww * 0.58
for i in (0, 1, 3, 4, 5):
    bx = (i + 1) * ww
    d.rounded_rectangle([bx - bw / 2, kb_top, bx + bw / 2, kb_top + kb_h * 0.62],
                        radius=int(bw * 0.18), fill=BLACK)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("wrote", out)
