"""Regenerate the simple, code-drawn DynaVal application icons with uv."""

from pathlib import Path

from PIL import Image, ImageDraw


def create_icons(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 28, 996, 996), radius=216, fill="#101935")
    draw.rounded_rectangle((246, 196, 756, 820), radius=66, fill="#9ad4d6")
    draw.rounded_rectangle((186, 146, 696, 770), radius=66, fill="#f2fdff")
    for y, end in ((288, 562), (372, 572), (456, 462)):
        draw.rounded_rectangle((282, y, end, y + 30), radius=15, fill="#9ad4d6")
    draw.ellipse((506, 516, 868, 878), fill="#a31621")
    draw.line([(591, 696), (662, 765), (790, 625)], fill="#f2fdff", width=48, joint="curve")
    for x, y in ((591, 696), (662, 765), (790, 625)):
        draw.ellipse((x - 24, y - 24, x + 24, y + 24), fill="#f2fdff")
    draw.ellipse((783, 193, 837, 247), fill="#f78764")
    image.save(destination / "dynaval.png")
    image.save(destination / "dynaval.icns", format="ICNS")
    image.save(
        destination / "dynaval.ico",
        format="ICO",
        sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)],
    )


if __name__ == "__main__":
    create_icons(Path(__file__).resolve().parent / "icons")
