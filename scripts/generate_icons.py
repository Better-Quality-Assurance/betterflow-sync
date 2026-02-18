#!/usr/bin/env python3
"""Generate icon files for BetterFlow Sync."""

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# BetterFlow purple
BF_PURPLE = "#7D69B8"
BF_PURPLE_DARK = "#614D87"


def create_icon(size: int = 1024) -> Image.Image:
    """Create the BetterFlow Sync icon.

    A purple circle with "BF" text.
    """
    # Create image with transparency
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw purple circle
    margin = size // 16
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=BF_PURPLE,
    )

    # Draw darker inner circle for depth
    inner_margin = size // 8
    draw.ellipse(
        [inner_margin, inner_margin, size - inner_margin, size - inner_margin],
        fill=BF_PURPLE_DARK,
    )

    # Try to add "BF" text
    try:
        # Try system fonts
        font_size = size // 3
        font = None
        for font_name in ["Arial Bold", "Helvetica Bold", "DejaVuSans-Bold", "Arial"]:
            try:
                font = ImageFont.truetype(font_name, font_size)
                break
            except (OSError, IOError):
                continue

        if font is None:
            # Fallback to default font (small)
            font = ImageFont.load_default()

        text = "BF"
        # Get text bounding box
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Center text
        x = (size - text_width) // 2
        y = (size - text_height) // 2 - bbox[1]  # Adjust for baseline

        draw.text((x, y), text, fill="white", font=font)
    except Exception as e:
        print(f"Could not add text: {e}")
        # Just use the circle without text

    return img


def create_simple_icon(size: int = 1024) -> Image.Image:
    """Create a simple purple circle icon (fallback)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer circle
    margin = size // 16
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=BF_PURPLE,
    )

    # Inner darker circle
    inner_margin = size // 6
    draw.ellipse(
        [inner_margin, inner_margin, size - inner_margin, size - inner_margin],
        fill=BF_PURPLE_DARK,
    )

    # Small white dot in center (sync indicator style)
    center = size // 2
    dot_radius = size // 8
    draw.ellipse(
        [center - dot_radius, center - dot_radius,
         center + dot_radius, center + dot_radius],
        fill="white",
    )

    return img


def save_png(img: Image.Image, path: Path) -> None:
    """Save as PNG."""
    img.save(path, "PNG")
    print(f"Created: {path}")


def save_ico(img: Image.Image, path: Path) -> None:
    """Save as ICO with multiple sizes."""
    sizes = [16, 32, 48, 64, 128, 256]
    icons = []
    for size in sizes:
        resized = img.resize((size, size), Image.Resampling.LANCZOS)
        icons.append(resized)

    # Save ICO with all sizes
    icons[0].save(
        path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=icons[1:],
    )
    print(f"Created: {path}")


def save_icns(img: Image.Image, path: Path) -> None:
    """Save as ICNS (macOS) using iconutil."""
    # Create iconset directory
    with tempfile.TemporaryDirectory() as tmpdir:
        iconset_dir = Path(tmpdir) / "icon.iconset"
        iconset_dir.mkdir()

        # Required sizes for iconset
        sizes = [
            (16, "16x16"),
            (32, "16x16@2x"),
            (32, "32x32"),
            (64, "32x32@2x"),
            (128, "128x128"),
            (256, "128x128@2x"),
            (256, "256x256"),
            (512, "256x256@2x"),
            (512, "512x512"),
            (1024, "512x512@2x"),
        ]

        for size, name in sizes:
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            resized.save(iconset_dir / f"icon_{name}.png", "PNG")

        # Run iconutil to create icns
        try:
            subprocess.run(
                ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(path)],
                check=True,
                capture_output=True,
            )
            print(f"Created: {path}")
        except subprocess.CalledProcessError as e:
            print(f"iconutil failed: {e.stderr.decode()}")
        except FileNotFoundError:
            print("iconutil not found (not on macOS?), skipping .icns")


def main():
    """Generate all icon files."""
    # Get resources directory
    script_dir = Path(__file__).parent
    resources_dir = script_dir.parent / "resources"
    resources_dir.mkdir(exist_ok=True)

    print("Generating BetterFlow Sync icons...")

    # Create icon
    icon = create_simple_icon(1024)

    # Save formats
    save_png(icon, resources_dir / "icon.png")
    save_ico(icon, resources_dir / "icon.ico")

    # Try to create icns (macOS only)
    import platform
    if platform.system() == "Darwin":
        save_icns(icon, resources_dir / "icon.icns")
    else:
        print("Skipping .icns (not on macOS)")

    print("Done!")


if __name__ == "__main__":
    main()
