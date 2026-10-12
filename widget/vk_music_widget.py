"""Настольный виджет с текущим треком.

Окно безрамочное и всегда поверх остальных. Оно существует в двух видах:

  * развёрнутое - обои, обложка, название, прогресс, кнопки, список вкладок;
  * свёрнутое - только обложка, квадрат MINI_COVER на MINI_COVER.

Почему всё рисуется на холсте, а не виджетами customtkinter
------------------------------------------------------------
Подписи customtkinter не умеют быть поверх картинки: даже с
fg_color="transparent" они подставляют цвет родителя, и на обоях появляются
тёмные полосы под текстом. На холсте текст не имеет подложки в принципе, а
картинка обоев закрывает весь прямоугольник виджета, так что малиновые
скруглённые углы остаются единственными «пустыми» пикселями и окно выглядит
без рамки.

Данные приходят из расширения через BridgeWorker: поток кладёт снимки в
states, а команды интерфейса уходят в cmds. Пока расширение не подключено,
виджет показывает заглушку и объяснение - так пользователь понимает, что
делать дальше, а не видит пустое окно.

Запуск:
    python widget\\vk_music_widget.py
"""

from __future__ import annotations

import base64
import io
import math
import queue
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageTk

import theme
from theme import (
    ACCENT, ACCENT_HOVER, ACCENT_SOFT, CARD, COVER, DIM, FULL_H, FULL_W,
    MINI_COVER, MUTED, OVERLAY, OVERLAY_HI, OVERLAY_DIM, ROW_W, TEXT,
    TRANSPARENT,
)
from bridge_worker import BridgeWorker

# Как часто окно перерисовывает себя. Состояние приходит раз в 700 мс, но
# часы и спектр должны идти плавно.
FRAME_MS = 50

# Через сколько секунд без снимков считаем, что расширения нет.
STALE_AFTER_SEC = 5.0

# Геометрия развёрнутого вида. Полоса прогресса и кнопки сдвинуты вниз:
# между исполнителем и ними было пустое поле.
#
#   44  обложка 120
#   176 надпись приложения
#   192 название трека
#   216 исполнитель
#   262 полоса прогресса
#   296 кнопки транспорта
#   356 заголовок источников
#   368 строки источников, две по 40
#   456 диагностика или спектр
COVER_Y = 44
APP_Y = 176
TITLE_Y = 192
ARTIST_Y = 216
PROGRESS_Y = 262
PROGRESS_H = 5
TIME_Y = 272
BUTTONS_Y = 296
LIST_HEAD_Y = 356
LIST_Y = 368
ROW_H = 40
FOOTER_Y = 456
SPECTRUM_H = 14

# Сколько вкладок VK видно в списке. Больше не нужно: выбирать из шести
# вкладок мышью неудобно, а список прокручивать незачем.
MAX_SOURCE_ROWS = 2

# На сколько пикселей можно сдвинуть мышь, чтобы это ещё считалось кликом.
# Всё большее - перетаскивание окна.
DRAG_THRESHOLD = 4

HEAD_SIZE = 28
HEAD_Y = 10
TRANSPORT = theme.BTN

# Кнопки шапки: имя -> прямоугольник. Заполняется при отрисовке.
HIT: dict[str, tuple] = {}


class MusicWidget:
    def __init__(self):
        self.cmds: queue.Queue = queue.Queue()
        self.states: queue.Queue = queue.Queue()
        self.bridge = BridgeWorker(self.cmds, self.states)

        self.items: list[dict] = []
        self.selected: str | None = None
        self.note = "Подключение к браузеру..."
        self.last_state_at = 0.0

        self.collapsed = False
        self.topmost = True
        self.gx = 0
        self.gy = 0
        self._placed = False

        self._anchor_key = None
        self._pos_anchor = 0.0
        self._pos_anchor_at = 0.0
        self._cur_dur = 0.0
        self._bands = [0.0] * 20
        self._hover: str | None = None
        self._pressed: str | None = None
        self._press_xy = (0, 0)
        self._did_drag = False
        self._drag_from = None

        self._build()
        self.bridge.start()
        self.root.after(FRAME_MS, self._frame)

    # --- окно ------------------------------------------------------------------

    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.title("Music Widget")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.topmost)
        # Windows считает этот цвет пустым - им залиты скруглённые углы.
        self.root.configure(bg=TRANSPARENT)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.resizable(False, False)

        self.canvas = tk.Canvas(
            self.root, width=FULL_W, height=FULL_H, bg=TRANSPARENT,
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.wallpaper = theme.load_wallpaper(FULL_W, FULL_H)
        self._wall_photo = ImageTk.PhotoImage(self.wallpaper)
        self._build_covers()

        self.gx = self.root.winfo_screenwidth() - FULL_W - 24
        self.gy = self.root.winfo_screenheight() - FULL_H - 64
        # Без явной геометрии окно появляется в левом верхнем углу:
        # координаты посчитаны, но ещё ни разу не применены.
        self._geometry(FULL_W, FULL_H)

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-3>", self._on_right_click)

    def _geometry(self, w, h) -> None:
        self.root.geometry(f"{w}x{h}+{self.gx}+{self.gy}")

    # --- состояние -------------------------------------------------------------

    def _current(self) -> dict | None:
        for item in self.items:
            if item.get("key") == self.selected:
                return item
        return self.items[0] if self.items else None

    def _extension_alive(self) -> bool:
        return (self.last_state_at > 0
                and time.monotonic() - self.last_state_at < STALE_AFTER_SEC)

    def _position(self, item) -> float:
        """Позиция по реальным часам от последнего снимка.

        Расширение спрашивает вкладку раз в 700 мс; между снимками виджет
        досчитывает время сам, иначе шкала дёргалась бы на каждом опросе.
        """
        key = item.get("ck")
        pos = float(item.get("pos") or 0)
        now = time.monotonic()
        if key != self._anchor_key or not item.get("playing"):
            self._anchor_key = key
            self._pos_anchor = pos
            self._pos_anchor_at = now
            self._cur_dur = float(item.get("dur") or 0)
            return pos
        limit = self._cur_dur or float("inf")
        return min(limit, self._pos_anchor + (now - self._pos_anchor_at))

    def _drain_states(self) -> bool:
        changed = False
        while True:
            try:
                state = self.states.get_nowait()
            except queue.Empty:
                return changed
            self.items = state.get("items") or []
            self.selected = state.get("selected")
            self.note = state.get("note") or ""
            self.last_state_at = time.monotonic()
            changed = True

    # --- отрисовка ---------------------------------------------------------------

    def _frame(self) -> None:
        self._drain_states()
        self._render()
        self.root.after(FRAME_MS, self._frame)

    def _render(self) -> None:
        self.canvas.delete("all")
        HIT.clear()

        if self.collapsed:
            self._render_collapsed()
        else:
            self._render_full()

    # -- свёрнутый вид: только обложка --

    def _render_collapsed(self) -> None:
        HIT["expand"] = (0, 0, MINI_COVER, MINI_COVER)
        self.canvas.create_image(0, 0, image=self._mini_photo, anchor="nw")
        self._cursor = "hand2"

    # -- развёрнутый вид --

    def _render_full(self) -> None:
        self._cursor = None
        self.canvas.create_image(0, 0, image=self._wall_photo, anchor="nw")

        item = self._current()
        self._draw_head()
        self._draw_cover(item)
        self._draw_text(item)
        self._draw_progress(item)
        self._draw_transport(item)
        self._draw_sources(item)
        self._draw_footer(item)

    def _draw_head(self) -> None:
        """Кнопки в шапке: закрепить, свернуть, закрыть."""
        right = FULL_W - 14
        specs = (
            ("close", theme.ICON_CLOSE, MUTED, right - HEAD_SIZE),
            ("collapse", theme.ICON_COLLAPSE, MUTED, right - HEAD_SIZE * 2 - 6),
            ("pin", theme.ICON_PIN if self.topmost else theme.ICON_UNPIN,
             ACCENT if self.topmost else MUTED, right - HEAD_SIZE * 3 - 12),
        )
        for name, glyph, color, x in specs:
            rect = (x, HEAD_Y, x + HEAD_SIZE, HEAD_Y + HEAD_SIZE)
            HIT[name] = rect
            self._rounded_rect(rect, 8, fill=OVERLAY if self._hover == name else "")
            self.canvas.create_text(
                x + HEAD_SIZE / 2, HEAD_Y + HEAD_SIZE / 2, text=glyph,
                font=(theme.ICON_FONT, 11), fill=color,
            )

    def _draw_cover(self, item) -> None:
        """Вклеивает обложку прямо в вырезку обоев.

        Отдельной картинкой обложку рисовать нельзя: у неё скруглённые
        углы, а «прозрачный» цвет в окне вырезает дыру, через которую видно
        рабочий стол. Поэтому обложка вырезается из фона и рисуется
        одним куском.
        """
        square = self._cover_square(item)
        if square is not self._cover_cache_key:
            x0 = (FULL_W - COVER) // 2
            panel = self.wallpaper.crop((x0, COVER_Y, x0 + COVER, COVER_Y + COVER))
            panel.paste(square, (0, 0), theme.rounded_mask(COVER, theme.cover_radius(COVER)))
            self._cover_panel_photo = ImageTk.PhotoImage(panel)
            self._cover_cache_key = square
        self.canvas.create_image(
            (FULL_W - COVER) // 2, COVER_Y, image=self._cover_panel_photo, anchor="nw"
        )

    def _cover_square(self, item) -> Image.Image:
        """Квадратная обложка нужного размера: трек или заглушка."""
        art = (item or {}).get("art")
        if art:
            try:
                source = Image.open(io.BytesIO(base64.b64decode(art)))
                self._mini_photo = ImageTk.PhotoImage(theme.round_image(source, MINI_COVER))
                return theme.cover_square(source, COVER)
            except Exception:
                pass
        return self.default_cover_square

    def _build_covers(self) -> None:
        self.default_cover_square = theme.cover_square(theme.make_default_cover(COVER), COVER)
        self._cover_cache_key = None
        self._cover_panel_photo = None
        self._mini_photo = ImageTk.PhotoImage(theme.make_default_cover(MINI_COVER))

    def _draw_text(self, item) -> None:
        if item is None:
            app = "NO MEDIA"
            title = "Nothing playing"
            # Под исполнителем - что делать, внизу - что произошло.
            # Одной и той же строкой в двух местах пользователю нечего.
            artist = ("Включите трек в VK Музыке" if self._extension_alive()
                      else "Подключите расширение в Firefox")
        else:
            app = (item.get("app") or "").upper()
            title = item.get("title") or "Unknown track"
            artist = item.get("artist") or "Unknown artist"

        self.canvas.create_text(
            FULL_W / 2, APP_Y, text=_clip(app, 34), font=("Segoe UI", 9, "bold"),
            fill=ACCENT, anchor="n",
        )
        self.canvas.create_text(
            FULL_W / 2, TITLE_Y, text=_clip(title, 32), font=("Segoe UI", 15, "bold"),
            fill=TEXT, anchor="n",
        )
        self.canvas.create_text(
            FULL_W / 2, ARTIST_Y, text=_clip(artist, 38), font=("Segoe UI", 11),
            fill=MUTED, anchor="n",
        )

    def _draw_progress(self, item) -> None:
        x0 = (FULL_W - ROW_W) // 2
        x1 = x0 + ROW_W
        self.canvas.create_rectangle(
            x0, PROGRESS_Y, x1, PROGRESS_Y + PROGRESS_H,
            fill=OVERLAY_HI, outline="",
        )

        if item is None:
            label = "0:00"
        else:
            pos = self._position(item)
            dur = float(item.get("dur") or 0)
            if dur > 0:
                frac = max(0.0, min(1.0, pos / dur))
                self.canvas.create_rectangle(
                    x0, PROGRESS_Y, x0 + ROW_W * frac, PROGRESS_Y + PROGRESS_H,
                    fill=ACCENT, outline="",
                )
                label = f"{theme.fmt_time(pos)} / {theme.fmt_time(dur)}"
            else:
                frac = max(0.0, min(1.0, float(item.get("frac") or 0)))
                self.canvas.create_rectangle(
                    x0, PROGRESS_Y, x0 + ROW_W * frac, PROGRESS_Y + PROGRESS_H,
                    fill=ACCENT, outline="",
                )
                # Длительности VK Музыка не отдаёт - показываем прошедшее
                # время, а не выдуманное "из".
                label = theme.fmt_time(pos)

        self.canvas.create_text(
            FULL_W / 2, TIME_Y, text=label, font=("Segoe UI", 9), fill=MUTED, anchor="n"
        )

    def _draw_transport(self, item) -> None:
        gap = 14
        total = TRANSPORT * 3 + gap * 2
        x = (FULL_W - total) // 2
        playing = bool((item or {}).get("playing"))

        specs = (
            ("prev", theme.ICON_PREV, TRANSPORT, OVERLAY),
            ("toggle", theme.ICON_PAUSE if playing else theme.ICON_PLAY,
             TRANSPORT, ACCENT),
            ("next", theme.ICON_NEXT, TRANSPORT, OVERLAY),
        )
        for name, glyph, size, color in specs:
            cy = BUTTONS_Y + TRANSPORT / 2
            rect = (x, BUTTONS_Y, x + TRANSPORT, BUTTONS_Y + TRANSPORT)
            HIT[name] = rect
            fill = color
            if self._hover == name:
                fill = ACCENT_HOVER if name == "toggle" else OVERLAY_HI
            self._circle(rect, fill)
            self.canvas.create_text(
                x + TRANSPORT / 2, cy, text=glyph, font=(theme.ICON_FONT, 17),
                fill="#ffffff",
            )
            x += TRANSPORT + gap

    def _draw_sources(self, item) -> None:
        """Список вкладок VK.

        Показывается только когда их больше одной. При единственной вкладке
        список занимал бы место впустую: выбирать не из чего.
        """
        if len(self.items) <= 1:
            return

        x0 = (FULL_W - ROW_W) // 2
        self.canvas.create_text(
            x0, LIST_HEAD_Y, text="ИСТОЧНИКИ", font=("Segoe UI", 9, "bold"),
            fill=DIM, anchor="nw",
        )

        y = LIST_Y
        for entry in self.items[:MAX_SOURCE_ROWS]:
            picked = item is not None and entry.get("key") == item.get("key")
            rect = (x0, y, x0 + ROW_W, y + 40)
            HIT["src:" + str(entry.get("key"))] = rect
            self._rounded_rect(rect, 10, fill=ACCENT_SOFT if picked else OVERLAY_DIM)

            dot = ACCENT if picked else DIM
            self.canvas.create_oval(x0 + 10, y + 17, x0 + 16, y + 23, fill=dot, outline="")

            sub = entry.get("app", "")
            if entry.get("artist"):
                sub += " · " + entry["artist"]
            self.canvas.create_text(
                x0 + 24, y + 7, text=_clip(entry.get("title") or "Unknown track", 30),
                font=("Segoe UI", 10, "bold"), fill=TEXT, anchor="nw",
            )
            self.canvas.create_text(
                x0 + 24, y + 24, text=_clip(sub, 40), font=("Segoe UI", 9),
                fill=MUTED, anchor="nw",
            )
            y += ROW_H

    def _footer_y(self) -> int:
        """Где рисовать подвал.

        Список источников занимает место до FOOTER_Y. Если вкладок не
        больше одной, списка нет - и подвал встаёт сразу под кнопками, иначе
        он повисал бы в пустоте посреди карточки.
        """
        if len(self.items) > 1:
            return FOOTER_Y
        return BUTTONS_Y + TRANSPORT + 26

    def _draw_footer(self, item) -> None:
        """Подвал: либо причина, почему ничего не играет, либо спектр.

        Одновременно они не нужны: спектр без музыки всё равно плоский, а
        диагностика при играющем треке только занимает место.
        """
        top = self._footer_y()
        if item is None:
            self.canvas.create_text(
                (FULL_W - ROW_W) // 2, top, text=_clip(self.note, 46),
                font=("Segoe UI", 8), fill=DIM, anchor="nw",
            )
            return

        bars = self._bands
        step = ROW_W / len(bars)
        width = max(2.0, step - 3)
        for index, value in enumerate(bars):
            height = max(1.5, value * SPECTRUM_H)
            x = (FULL_W - ROW_W) // 2 + index * step
            y = top + (SPECTRUM_H - height) / 2
            self.canvas.create_rectangle(
                x, y, x + width, y + height,
                fill=_accent_shade(0.45 + 0.55 * value), outline="",
            )

    # --- примитивы ---------------------------------------------------------------

    def _rounded_rect(self, rect, radius, fill) -> None:
        x0, y0, x1, y1 = rect
        if not fill:
            return
        self.canvas.create_rectangle(
            x0, y0, x1, y1, fill=fill, outline="",
        )
        # Углы Tk не скругляет, поэтому срезаем их малиновым - он же уходит
        # в прозрачность окна.
        self.canvas.create_rectangle(
            x0, y0, x1, y0 + radius, fill=fill, outline="",
        )
        self.canvas.create_rectangle(
            x0, y1 - radius, x1, y1, fill=fill, outline="",
        )

    def _circle(self, rect, fill) -> None:
        x0, y0, x1, y1 = rect
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        r = (x1 - x0) / 2
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline="")

    # --- мышь ---------------------------------------------------------------------

    def _hit(self, x, y) -> str | None:
        for name, (x0, y0, x1, y1) in HIT.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def _on_motion(self, event) -> None:
        name = self._hit(event.x, event.y)
        wanted = "hand2" if name else None
        if wanted != getattr(self, "_cursor", None):
            self._cursor = wanted
            self.canvas.configure(cursor=wanted or "")
        if name != self._hover:
            self._hover = name
            self._render()

    def _on_press(self, event) -> None:
        # Запоминаем и нажатую кнопку, и точку захвата. Разберёмся в
        # отпускании: сдвинули - тащим окно, не сдвинули - это клик.
        # Так окно перетаскивается за любую точку, включая свёрнутую
        # обложку, по которой иначе звать разворот.
        self._pressed = self._hit(event.x, event.y)
        self._press_xy = (event.x, event.y)
        self._drag_from = (event.x_root - self.gx, event.y_root - self.gy)
        self._did_drag = False

    def _on_release(self, event) -> None:
        dragged = self._did_drag
        name = None if dragged else self._pressed
        self._pressed = None
        self._drag_from = None
        self._did_drag = False
        if name is not None and name == self._hit(event.x, event.y):
            self._activate(name)

    def _on_drag(self, event) -> None:
        if self._drag_from is None:
            return
        dx = event.x - self._press_xy[0]
        dy = event.y - self._press_xy[1]
        if not self._did_drag and math.hypot(dx, dy) < DRAG_THRESHOLD:
            return
        self._did_drag = True
        self._pressed = None
        self.gx = event.x_root - self._drag_from[0]
        self.gy = event.y_root - self._drag_from[1]
        self._geometry(*self._size())

    def _on_right_click(self, event) -> None:
        # В свёрнутом виде кнопок нет вообще - меню это единственный
        # способ что-то сделать.
        menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT, bd=0,
                       activebackground=ACCENT, activeforeground="#ffffff")
        menu.add_command(label="Развернуть", command=self._expand)
        menu.add_command(label="Закрепить поверх окон", command=self._toggle_topmost)
        menu.add_command(label="Закрыть", command=self._quit)
        try:
            menu.tk_popup(int(self.root.winfo_pointerx()),
                          int(self.root.winfo_pointery()))
        finally:
            menu.grab_release()

    def _activate(self, name: str) -> None:
        if name == "close":
            self._quit()
        elif name == "collapse":
            self._collapse()
        elif name == "pin":
            self._toggle_topmost()
        elif name == "expand":
            self._expand()
        elif name in ("toggle", "prev", "next"):
            if self._current() is not None:
                self.bridge.send(name, self.selected)
        elif name.startswith("src:"):
            key = name[4:]
            self.selected = key
            self.bridge.send("select", key)

    # --- переключение вида -------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        return (MINI_COVER, MINI_COVER) if self.collapsed else (FULL_W, FULL_H)

    def _collapse(self) -> None:
        self.collapsed = True
        self._geometry(*self._size())
        self.canvas.configure(width=MINI_COVER, height=MINI_COVER)
        self._hover = None
        self._render()

    def _expand(self) -> None:
        self.collapsed = False
        self._geometry(*self._size())
        self.canvas.configure(width=FULL_W, height=FULL_H)
        self._hover = None
        self._render()

    def _toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)

    # --- спектр и завершение ---------------------------------------------------------

    def _animate_bands(self) -> None:
        item = self._current()
        target = list((item or {}).get("bands") or [])[:20]
        target += [0.0] * (20 - len(target))
        for i in range(20):
            value = target[i]
            # Поднимаемся мгновенно, опускаемся плавно - так полосы
            # выглядят как музыка, а не как шум.
            self._bands[i] = value if value > self._bands[i] else self._bands[i] * 0.78

    def _quit(self) -> None:
        self.bridge.stop()
        self.root.destroy()

    def run(self) -> None:
        self._animate_bands()
        self.root.mainloop()


def _clip(text, limit: int) -> str:
    """Обрезает строку с многоточием: длинное название разъехало бы вёрстку."""
    s = str(text or "").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "\u2026"


def _accent_shade(t: float) -> str:
    t = max(0.0, min(1.0, t))
    r = int(0x9A + (0x7C - 0x9A) * t)
    g = int(0x8F + (0x5C - 0x8F) * t)
    return f"#{r:02x}{g:02x}ff"


def main() -> None:
    theme.dpi_aware()
    widget = MusicWidget()
    widget.root.after(FRAME_MS, widget._animate_bands)
    widget.run()


if __name__ == "__main__":
    main()