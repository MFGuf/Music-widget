"""Оформление виджета: цвета, размеры и подготовка картинок.

Всё, что относится к внешнему виду, собрано здесь, чтобы правки не
размазывались по окну. Окно само (widget/vk_music_widget.py) занимается
только размещением элементов.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ASSETS = Path(__file__).resolve().parent / "assets"

# --- размеры -----------------------------------------------------------------

FULL_W, FULL_H = 320, 470
CARD_RADIUS = 24

# Свёрнутый вид - только обложка, без кнопок и подписей.
COVER = 120
MINI_COVER = 64

BTN = 46
GAP = 14
ROW_W = FULL_W - 40

# --- цвета -------------------------------------------------------------------

# Цвет, который Windows делает полностью прозрачным. Углы скруглённой
# карточки заливаются им, поэтому окно выглядит без рамки. В фотографии
# такого цвета быть не может - выбран малиновый, а картинка чёрно-белая.
TRANSPARENT = "#FF00FF"

CARD = "#14141b"
ACCENT = "#7c5cff"
ACCENT_HOVER = "#9276ff"
ACCENT_SOFT = "#2f2554"
TEXT = "#ececf4"
MUTED = "#9a9aa8"
DIM = "#6d6d7d"
DIVIDER = "#262631"

# Полупрозрачные пятна поверх обоев. Tcl не понимает запись rgba(), а
# подложки нужны именно полупрозрачные, поэтому берём сплошные оттенки,
# которые на тёмном снимке читаются как просвет.
OVERLAY = "#3a3a46"
OVERLAY_HI = "#4a4a58"
OVERLAY_DIM = "#2a2a33"

WINDOW_ALPHA = 1.0

# --- обои --------------------------------------------------------------------

WALLPAPER = "opianarium.jpg"

# Какая часть фотографии попадает в карточку. Карточка узкая и высокая, а
# снимок широкий, поэтому берём вертикальный кусок. Точка 0.5 - середина.
# Если обои заменить, эту долю можно подвинуть.
WALLPAPER_FOCUS_X = 0.5
WALLPAPER_FOCUS_Y = 0.42

# Затемнение под текст. Фотография пёстрая и местами светлая, а надписи
# должны читаться: без этого белый текст пропадает на светлых местах.
WALLPAPER_DIM = 0.58
WALLPAPER_SCRIM_TOP = 0.30
WALLPAPER_SCRIM_BOTTOM = 0.42

# --- шрифты ------------------------------------------------------------------

ICON_FONT = "Segoe MDL2 Assets"
ICON_PLAY = "\ue768"
ICON_PAUSE = "\ue769"
ICON_PREV = "\ue892"
ICON_NEXT = "\ue893"
ICON_CLOSE = "\ue8bb"
ICON_PIN = "\ue718"
ICON_UNPIN = "\ue77a"
ICON_COLLAPSE = "\ue70d"
ICON_EXPAND = "\ue70e"

MUSIC_NOTE = "\u266a"


def dpi_aware() -> None:
    """Чтобы окно не размывалось на экранах с высоким масштабированием."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --- картинки ----------------------------------------------------------------


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


# На сколько процентов скругляется обложка.
COVER_RADIUS_RATIO = 0.2


def cover_radius(size: int) -> int:
    return max(1, int(size * COVER_RADIUS_RATIO))


def cover_square(image: Image.Image, size: int) -> Image.Image:
    """Обложка без скругления и без служебной заливки.

    Нужна для вклейки в обои: углы обложки нельзя делать малиновыми -
    этот цвет окно считает прозрачным, и на его месте появился бы кусок
    рабочего стола.
    """
    image = image.convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )
    return mask


def rounded_rect_mask(width: int, height: int, radius: int) -> Image.Image:
    """То же для прямоугольника: у карточки 320x470, а не квадрат."""
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, width - 1, height - 1], radius=radius, fill=255
    )
    return mask


def round_image(image: Image.Image, size: int, radius_ratio: float = 0.2) -> Image.Image:
    """Обрезает картинку в квадрат со скруглёнными углами.

    Углы делаются малиновым - именно этот цвет Windows считает пустым,
    поэтому скругление получается без настоящей прозрачности.
    """
    image = image.convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image = image.resize((size, size), Image.LANCZOS)

    radius = max(1, int(size * radius_ratio))
    mask = rounded_mask(size, radius)
    out = Image.new("RGB", (size, size), TRANSPARENT)
    out.paste(image, (0, 0), mask)
    return out


def make_default_cover(size: int) -> Image.Image:
    """Заглушка на случай, если у трека нет обложки.

    Фиолетовый градиент и нота - то же, что было в прежней версии виджета.
    """
    out = Image.new("RGB", (size, size), CARD)
    top, bottom = (0x2A, 0x25, 0x4B), (0x1B, 0x1B, 0x24)
    draw = ImageDraw.Draw(out)
    for y in range(size):
        t = y / max(1, size - 1)
        draw.line(
            [(0, y), (size, y)],
            fill=(
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            ),
        )

    note = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/seguisym.ttf", int(size * 0.46))
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(note).text(
        (size * 0.5, size * 0.52), MUSIC_NOTE, font=font, anchor="mm",
        fill=(0xB2, 0xA4, 0xFF, 255),
    )
    out.paste(note, (0, 0), note)

    radius = max(1, int(size * 0.2))
    mask = rounded_mask(size, radius)
    flat = Image.new("RGB", (size, size), TRANSPARENT)
    flat.paste(out, (0, 0), mask)
    return flat


def _dim_with_gradient(image: Image.Image) -> Image.Image:
    """Затемняет снимок и добавляет сверху и снизу плотную тень.

    Тень нужна под кнопками в шапке и под названием трека с исполнителем:
    на фотографии там светлые пятна.

    Затемнение делается через Brightness, а тень - одной картинкой и
    одной развёрткой на высоту. Пиксельный цикл по всей карточке занял бы
    заметное время при запуске, а строить нужно всего 470 строк.
    """
    base = ImageEnhance.Brightness(image).enhance(WALLPAPER_DIM)

    width, height = image.size
    shade = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(shade)
    for y in range(height):
        v = y / max(1, height - 1)
        density = WALLPAPER_SCRIM_TOP * (1 - v) + WALLPAPER_SCRIM_BOTTOM * v
        draw.line([(0, y), (width, y)], fill=int(255 * min(1.0, density)))

    return Image.composite(Image.new("RGB", (width, height), (0, 0, 0)), base, shade)


def load_wallpaper(width: int, height: int) -> Image.Image:
    """Готовит обои под размер карточки. Возвращает пустую картинку, если
    файла нет, чтобы виджет запустился в любом случае."""
    path = ASSETS / WALLPAPER
    fallback = Image.new("RGB", (width, height), CARD)
    if not path.exists():
        return fallback

    try:
        source = Image.open(path).convert("RGB")
    except OSError:
        return fallback

    # Кадрируем по пропорциям карточки, удерживая точку интереса.
    target_ratio = width / height
    src_w, src_h = source.size
    if src_w / src_h > target_ratio:
        crop_h = src_h
        crop_w = int(round(src_h * target_ratio))
    else:
        crop_w = src_w
        crop_h = int(round(src_w / target_ratio))

    left = int(round((src_w - crop_w) * WALLPAPER_FOCUS_X))
    top = int(round((src_h - crop_h) * WALLPAPER_FOCUS_Y))
    left = max(0, min(src_w - crop_w, left))
    top = max(0, min(src_h - crop_h, top))

    cropped = source.crop((left, top, left + crop_w, top + crop_h))
    cropped = cropped.resize((width, height), Image.LANCZOS)
    cropped = _dim_with_gradient(cropped)

    # Скруглённые углы - малиновыми.
    mask = rounded_rect_mask(width, height, CARD_RADIUS)
    out = Image.new("RGB", (width, height), TRANSPARENT)
    out.paste(cropped, (0, 0), mask)
    return out


def assets_present() -> bool:
    return (ASSETS / WALLPAPER).exists()