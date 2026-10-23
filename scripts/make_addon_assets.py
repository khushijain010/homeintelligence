"""Generate the Alexa+ add-on icons (72, 64, 88, 126, 180, 241 px, light) and the 600x900 carousel image."""

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1] / "alexa-addon"
OUT = ROOT / "assets"
SLATE, SKY, WHITE, LIGHT = (15, 23, 42), (14, 165, 233), (255, 255, 255), (241, 245, 249)


def font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def shield(draw: ImageDraw.ImageDraw, cx: float, top: float, w: float, h: float, fill, check) -> None:
    """A shield with a check mark: 'evidence before action'."""
    pts = [
        (cx - w / 2, top + h * 0.12),
        (cx, top),
        (cx + w / 2, top + h * 0.12),
        (cx + w / 2, top + h * 0.5),
        (cx, top + h),
        (cx - w / 2, top + h * 0.5),
    ]
    draw.polygon(pts, fill=fill)
    lw = max(2, int(w * 0.11))
    draw.line(
        [(cx - w * 0.22, top + h * 0.47), (cx - w * 0.04, top + h * 0.64), (cx + w * 0.25, top + h * 0.3)],
        fill=check,
        width=lw,
        joint="curve",
    )


def icon(size: int) -> Image.Image:
    scale = 4  # draw large, downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=LIGHT)
    shield(d, s / 2, s * 0.16, s * 0.56, s * 0.68, SLATE, SKY)
    return img.resize((size, size), Image.LANCZOS)


def carousel() -> Image.Image:
    w, h = 600, 900
    img = Image.new("RGB", (w, h), SLATE)
    d = ImageDraw.Draw(img)
    for y in range(h):  # subtle vertical gradient
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(15 + 15 * t), int(23 + 30 * t), int(42 + 50 * t)))
    shield(d, w / 2, 120, 220, 270, WHITE, SKY)
    d.text((w / 2, 450), "Guardian", font=font(64), fill=WHITE, anchor="mm")
    d.text((w / 2, 520), "Evidence before action.", font=font(30), fill=(186, 230, 253), anchor="mm")
    phrases = json.loads((ROOT / "listing.json").read_text(encoding="utf-8"))["examplePhrases"]
    y = 600
    for p in phrases:
        d.rounded_rectangle([30, y - 26, w - 30, y + 26], radius=26, fill=(30, 41, 59))
        d.text((w / 2, y), f"“{p}”", font=font(18), fill=WHITE, anchor="mm")
        y += 68
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (72, 64, 88, 126, 180, 241):
        icon(size).save(OUT / f"icon-{size}.png")
    carousel().save(OUT / "carousel-600x900.png")
    print(f"wrote 6 icons + carousel to {OUT}")


if __name__ == "__main__":
    main()
