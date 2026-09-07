"""Create a small, entirely synthetic dataset and two local reference cards."""

import argparse
import csv
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _card(path: Path, record: int, name: str, age: str, label: str) -> None:
    image = Image.new("RGB", (1200, 860), "#f2fdff")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=38)
    small = ImageFont.load_default(size=25)
    large = ImageFont.load_default(size=56)
    draw.rounded_rectangle((55, 55, 1145, 805), radius=30, outline="#9ad4d6", width=3)
    draw.text((108, 105), "SOURCE CARD", fill="#a31621", font=small)
    draw.text((108, 175), f"Record {record:02}", fill="#101935", font=large)
    draw.line((108, 265, 1090, 265), fill="#9ad4d6", width=3)
    for index, (title, value) in enumerate((("Name", name), ("Age", age), ("Label", label))):
        y = 325 + index * 115
        draw.text((108, y), title, fill="#101935", font=font)
        draw.text((440, y), value, fill="#101935", font=font)
    draw.text((108, 729), "Synthetic demonstration data", fill="#101935", font=small)
    image.save(path)


def create_demo(destination: Path) -> Path:
    """Publish a new demo directory; never overwrite an existing folder."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ValueError("Choose a new directory; the demo never overwrites existing files.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dynaval-demo-", dir=destination.parent))
    try:
        (staging / "images").mkdir()
        _card(staging / "images/record-01.png", 1, "Ada Vale", "38", "blue")
        _card(staging / "images/record-02.png", 2, "Lin Park", "29", "green")
        with (staging / "dataset.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["image", "name", "age", "label"])
            writer.writerow(["images/record-01.png", "Ada Vale", "37", "blue"])
            writer.writerow(["images/record-02.png", "Lin Park", "29", "green"])
        (staging / "README.txt").write_text(
            "Open dataset.csv in DynaVal. Select image as the reference column and any other "
            "fields to review. If uploading, choose this directory as the reference base.\n"
            "Record 1 deliberately says age 37 in the dataset; its source card says 38. "
            "All people and records are fictional.\n",
            encoding="utf-8",
        )
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination / "dataset.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(create_demo(args.directory))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Demo creation failed: {error}\n")


if __name__ == "__main__":
    main()
