from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ICONSET = ROOT / "Media Toolkit.iconset"
ICONSET.mkdir(exist_ok=True)

sizes = [16, 32, 128, 256, 512, 1024]

for size in sizes:
    scale = 4
    canvas_size = size * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = int(canvas_size * 0.07)
    radius = int(canvas_size * 0.20)
    draw.rounded_rectangle(
        (margin, margin, canvas_size - margin, canvas_size - margin),
        radius=radius,
        fill=(16, 20, 25, 255),
        outline=(64, 74, 86, 255),
        width=max(2, canvas_size // 80),
    )

    # Simple MT mark. Falls back to a font available on the build machine.
    font_candidates = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    font = None
    for candidate in font_candidates:
        try:
            font = ImageFont.truetype(candidate, int(canvas_size * 0.36))
            break
        except Exception:
            continue

    if font is None:
        font = ImageFont.load_default()

    text = "M"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((canvas_size - tw) / 2 - bbox[0], (canvas_size - th) / 2 - bbox[1] - canvas_size * 0.01),
        text,
        font=font,
        fill=(242, 245, 248, 255),
    )

    if size >= 128:
        sub_font = None
        for candidate in font_candidates:
            try:
                sub_font = ImageFont.truetype(candidate, int(canvas_size * 0.075))
                break
            except Exception:
                continue
        if sub_font:
            sub = "MEDIA"
            sb = draw.textbbox((0, 0), sub, font=sub_font)
            sw = sb[2] - sb[0]
            draw.text(
                ((canvas_size - sw) / 2 - sb[0], canvas_size * 0.72),
                sub,
                font=sub_font,
                fill=(133, 145, 158, 255),
            )

    img = img.resize((size, size), Image.Resampling.LANCZOS)

    if size == 16:
        img.save(ICONSET / "icon_16x16.png")
        img.resize((32, 32), Image.Resampling.LANCZOS).save(ICONSET / "icon_16x16@2x.png")
    elif size == 32:
        # 32 normal is covered by 16@2x.
        pass
    elif size == 128:
        img.save(ICONSET / "icon_128x128.png")
        img.resize((256, 256), Image.Resampling.LANCZOS).save(ICONSET / "icon_128x128@2x.png")
    elif size == 256:
        pass
    elif size == 512:
        img.save(ICONSET / "icon_512x512.png")
        img.resize((1024, 1024), Image.Resampling.LANCZOS).save(ICONSET / "icon_512x512@2x.png")
    elif size == 1024:
        img.save(ICONSET / "icon_1024x1024.png")

# iconutil expects these canonical names. Copy the larger size pairs.
# Use Python only here; macOS `iconutil` turns the iconset into .icns.
print(f"Created iconset: {ICONSET}")
print("Run iconutil -c icns 'Media Toolkit.iconset' -o 'Media Toolkit.icns' on macOS.")
