"""Retake the README screenshots from `fleetwatch --demo`, rendered by Textual itself.

Run from the repository root with the board's dependencies installed:

    FLEETWATCH_NO_MIGRATE=1 TZ=America/New_York python scripts/capture_screenshots.py

It writes docs/images/board.png and docs/images/board-indexes.png. Converting the SVG to PNG
needs rsvg-convert on the PATH. Every session in the pictures is the demo's fictional fleet in a
temporary folder; nothing reads the user's own files.
"""
import asyncio
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from fleetwatch import app as fapp, data, demo  # noqa: E402

IMAGES = os.path.join(HERE, "..", "docs", "images")

# (output name, (columns, rows), keys pressed after the first refresh). 120 columns is the hotkey
# window's width. The radar mark floats over the bottom right corner and needs 32 rows and 100
# columns; a shorter board hides it.
VIEWS = (
    ("board", (120, 32), "e"),
    ("board-indexes", (120, 40), "5 e"),
)


async def shoot(paths, size, keys):
    app = fapp.FleetwatchApp(lambda: fapp.gather(paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                             3600, paths=paths)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(1.5)
        for key in keys.split():
            await pilot.press(key)
            await pilot.pause(0.3)
        await pilot.pause(1.0)
        svg = app.export_screenshot(title="fleetwatch --demo")
    # librsvg drops the no-break spaces that open a text run unless whitespace is preserved,
    # which runs adjacent words together.
    return svg.replace('<svg class="rich-terminal"', '<svg xml:space="preserve" class="rich-terminal"', 1)


def main():
    demo_dir, paths = demo.prepare()
    try:
        with tempfile.TemporaryDirectory() as work:
            for name, size, keys in VIEWS:
                svg = os.path.join(work, name + ".svg")
                with open(svg, "w", encoding="utf-8") as fh:
                    fh.write(asyncio.run(shoot(paths, size, keys)))
                png = os.path.join(IMAGES, name + ".png")
                subprocess.run(["rsvg-convert", "-w", "2223", "-o", png, svg], check=True)
                print("wrote", os.path.relpath(png))
    finally:
        demo_dir.cleanup()


if __name__ == "__main__":
    main()
