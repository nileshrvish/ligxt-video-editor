"""Generate the bundled stickers (stickers/*.png, transparent, 512 px): simple shapes drawn here with Pillow,
so they carry no third-party rights. Released under CC0 1.0 (stickers/LICENSE.md).

Run:  .venv\\Scripts\\python.exe scripts\\make_stickers.py
"""
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "stickers"
S = 512
K = 4  # drawn at 4x, downsampled (clean edges)


def canvas():
    return Image.new("RGBA", (S * K, S * K), (0, 0, 0, 0))


def finish(img, name, label, shadow=True):
    img = img.resize((S, S), Image.LANCZOS)
    if shadow:
        a = img.split()[3].filter(ImageFilter.GaussianBlur(10))
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sh.putalpha(a.point(lambda v: int(v * 0.45)))
        base = Image.new("RGBA", img.size, (0, 0, 0, 0))
        base.alpha_composite(sh, (0, 8))
        base.alpha_composite(img)
        img = base
    img.save(OUT / f"{name}.png")
    return {"id": name, "name": label}


def P(v):
    return v * K


def pin():
    im = canvas()
    d = ImageDraw.Draw(im)
    cx, top, r = P(256), P(70), P(150)
    d.ellipse([cx - r, top, cx + r, top + 2 * r], fill=(235, 64, 52, 255))
    d.polygon([(cx - r * 0.82, top + r * 1.55), (cx + r * 0.82, top + r * 1.55), (cx, P(470))], fill=(235, 64, 52, 255))
    d.ellipse([cx - r * 0.42, top + r * 0.58, cx + r * 0.42, top + r * 1.42], fill=(255, 255, 255, 255))
    return finish(im, "pin", "Map pin")


def heart():
    im = canvas()
    d = ImageDraw.Draw(im)
    pts = []
    for i in range(400):
        t = 2 * math.pi * i / 400
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((P(256) + x * P(13.5), P(240) + y * P(13.5)))
    d.polygon(pts, fill=(255, 59, 92, 255))
    return finish(im, "heart", "Heart")


def star(name="star", label="Star", color=(255, 196, 0, 255), n=5, inner=0.45):
    im = canvas()
    d = ImageDraw.Draw(im)
    pts = []
    for i in range(2 * n):
        r = P(220) if i % 2 == 0 else P(220) * inner
        a = -math.pi / 2 + math.pi * i / n
        pts.append((P(256) + r * math.cos(a), P(266) + r * math.sin(a)))
    d.polygon(pts, fill=color)
    return finish(im, name, label)


def sparkle():
    im = canvas()
    d = ImageDraw.Draw(im)
    for cx, cy, R in ((256, 256, 200), (400, 110, 70), (120, 400, 55)):
        pts = []
        for i in range(8):
            r = P(R) if i % 2 == 0 else P(R) * 0.18
            a = -math.pi / 2 + math.pi * i / 4
            pts.append((P(cx) + r * math.cos(a), P(cy) + r * math.sin(a)))
        d.polygon(pts, fill=(255, 255, 255, 255))
    return finish(im, "sparkle", "Sparkle")


def arrow():
    im = canvas()
    d = ImageDraw.Draw(im)
    d.rectangle([P(200), P(40), P(312), P(300)], fill=(255, 255, 255, 255))
    d.polygon([(P(90), P(270)), (P(422), P(270)), (P(256), P(480))], fill=(255, 255, 255, 255))
    return finish(im, "arrow", "Arrow (points down)")


def ring():
    im = canvas()
    d = ImageDraw.Draw(im)
    d.ellipse([P(40), P(40), P(472), P(472)], outline=(255, 230, 60, 255), width=P(26))
    return finish(im, "ring", "Highlight ring")


def flame():
    im = canvas()
    d = ImageDraw.Draw(im)

    def blob(scale, color, dy=0):
        pts = []
        for i in range(400):  # a teardrop with its point at the top
            t = 2 * math.pi * i / 400
            x = math.sin(t) * abs(math.sin(t / 2)) ** 1.3 * 0.78
            y = -math.cos(t)
            pts.append((P(256) + x * P(220) * scale, P(270 + dy) + y * P(220) * scale))
        d.polygon(pts, fill=color)
    blob(1.0, (255, 98, 0, 255))
    blob(0.58, (255, 200, 0, 255), 80)
    return finish(im, "flame", "Flame")


def cool():
    """An emoji-style face with sunglasses."""
    im = canvas()
    d = ImageDraw.Draw(im)
    d.ellipse([P(36), P(36), P(476), P(476)], fill=(255, 204, 51, 255))
    # glasses
    d.rounded_rectangle([P(92), P(176), P(244), P(272)], radius=P(36), fill=(25, 25, 30, 255))
    d.rounded_rectangle([P(268), P(176), P(420), P(272)], radius=P(36), fill=(25, 25, 30, 255))
    d.rectangle([P(236), P(196), P(276), P(214)], fill=(25, 25, 30, 255))
    d.line([P(92), P(196), P(56), P(186)], fill=(25, 25, 30, 255), width=P(14))
    d.line([P(420), P(196), P(456), P(186)], fill=(25, 25, 30, 255), width=P(14))
    d.polygon([(P(120), P(190)), (P(150), P(190)), (P(118), P(240))], fill=(255, 255, 255, 110))
    # smile
    d.arc([P(150), P(250), P(362), P(400)], start=20, end=160, fill=(120, 60, 20, 255), width=P(18))
    return finish(im, "cool", "Cool face (sunglasses)")


def main():
    OUT.mkdir(exist_ok=True)
    items = [pin(), heart(), star(), sparkle(), arrow(), ring(), flame(), cool()]
    (OUT / "stickers.json").write_text(json.dumps({"stickers": items}, indent=1), encoding="utf-8")
    (OUT / "LICENSE.md").write_text(
        "# Stickers\n\nThese stickers were drawn from simple shapes by `scripts/make_stickers.py` (no third-party artwork).\n"
        "They are dedicated to the public domain under **CC0 1.0** (https://creativecommons.org/publicdomain/zero/1.0/).\n",
        encoding="utf-8")
    print("wrote", len(items), "stickers to", OUT)


if __name__ == "__main__":
    main()
