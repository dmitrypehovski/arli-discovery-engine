"""Снимок страницы отчёта в картинку, обрезанный по содержимому.

Зачем обрезка. Браузер снимает страницу заданной высоты, и если задать её с запасом, внизу
остаётся пустая полоса, а если впритык, текст обрезается. Поэтому снимаем с запасом и
отрезаем однотонный низ: картинка получается по содержимому, без пустоты и без обрезки.

Запуск: python -m evaluation.screenshot <html> <png> [высота]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image

CHROMIUM_CANDIDATES = ("/snap/bin/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser")
BACKGROUND = (251, 250, 247)  # цвет страницы; совпадает с оформлением отчёта


def browser() -> str:
    for candidate in CHROMIUM_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = subprocess.run(["which", "chromium"], capture_output=True, text=True).stdout.strip()
    if not found:
        raise SystemExit("no chromium to take the screenshot with")
    return found


def shot(html: Path, png: Path, *, height: int = 2600, width: int = 1000, tolerance: int = 6) -> Path:
    """Снять страницу. Прежний файл удаляется, профиль браузера отдельный.

    Без этого браузер может молча не перезаписать картинку, и в отчёт уйдёт прошлый снимок.
    Такое уже случилось: обрезался старый файл, а числа в нём были вчерашние.
    """
    # Пути только абсолютные: браузер под confinement разрешает относительный путь от своего
    # каталога и молча пишет мимо. Из-за этого один прогон уже обрезал вчерашнюю картинку.
    html = html.resolve()
    png = png.resolve()
    png.parent.mkdir(parents=True, exist_ok=True)
    if png.exists():
        png.unlink()
    # Профиль браузера не задаём: у snap-сборки confinement не пускает её в чужой каталог,
    # и снимок молча не делается. Надёжность держится на другом: прежний файл удалён выше.
    subprocess.run(
        [
            browser(), "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={width},{height}", f"--screenshot={png}", f"file://{html}",
        ],
        capture_output=True,
        timeout=120,
    )
    if not png.is_file():
        raise SystemExit(f"the browser did not write {png}")
    trim(png, tolerance=tolerance)
    return png


def trim(png: Path, *, tolerance: int = 6) -> None:
    """Отрезать однотонный низ и право, чтобы картинка была по содержимому."""
    image = Image.open(png).convert("RGB")
    pixels = image.load()
    width, height = image.size

    def row_is_background(y: int) -> bool:
        return all(
            abs(pixels[x, y][i] - BACKGROUND[i]) <= tolerance for x in range(0, width, 7) for i in range(3)
        )

    last = height - 1
    while last > 0 and row_is_background(last):
        last -= 1
    if last < height - 1:
        image = image.crop((0, 0, width, min(height, last + 24)))
    image.save(png)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        raise SystemExit("usage: python -m evaluation.screenshot <html> <png> [height]")
    height = int(argv[2]) if len(argv) > 2 else 2600
    written = shot(Path(argv[0]), Path(argv[1]), height=height)
    size = Image.open(written).size
    print(f"{written} ({size[0]}x{size[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
