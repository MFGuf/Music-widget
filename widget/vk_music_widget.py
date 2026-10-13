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
import platform
import queue
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageTk

import ipc
import theme
from theme import (
    ACCENT, ACCENT_HOVER, ACCENT_SOFT, CARD, COVER, DIM, EDGE_MARGIN, FULL_H,
    FULL_W, MINI_COVER, MUTED, OVERLAY, OVERLAY_DIM, OVERLAY_HI, ROW_W,
    SNAP_DISTANCE, TEXT, TRANSPARENT,
)
import log as journal
from bridge_worker import BridgeWorker

# Как часто окно перерисовывает себя. Состояние приходит раз в 700 мс, но
# часы и спектр должны идти плавно.
FRAME_MS = 50

# Через сколько секунд без снимков считаем, что расширения нет.
STALE_AFTER_SEC = 5.0

# Геометрия развёрнутого вида. Блок текста сдвинут вниз, а между
# надписями стало больше воздуха.
#
#   50  обложка 120
#   184 надпись приложения
#   204 название трека
#   234 исполнитель
#   276 полоса прогресса
#   308 кнопки транспорта
#   368 заголовок источников
#   380 строки источников, две по 38
#   456 диагностика или спектр
COVER_Y = 50
APP_Y = 184
TITLE_Y = 204
ARTIST_Y = 234
PROGRESS_Y = 276
PROGRESS_H = 5
TIME_Y = 286
BUTTONS_Y = 308
LIST_HEAD_Y = 368
LIST_Y = 380
ROW_H = 38
FOOTER_Y = 456
SPECTRUM_H = 14

# Сколько вкладок VK видно в списке. Больше не нужно: выбирать из шести
# вкладок мышью неудобно, а список прокручивать незачем.
MAX_SOURCE_ROWS = 2

# На сколько пикселей можно сдвинуть мышь, чтобы это ещё считалось кликом.
# Всё большее - перетаскивание окна.
DRAG_THRESHOLD = 4

# Насколько полоса прогресса ловит нажатие шире, чем нарисована. Сама полоска
# тонкая, а промах по краю означал бы, что перемотать нельзя.
SEEK_GRAB = 6

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

        # Подключился ли хост и с какого момента. Без второго поля отличить
        # «расширение ещё не успело прислать первый снимок» от «расширение
        # молчит полминуты» нельзя, а ругаться на пользователя за первую
        # секунду после запуска нельзя.
        self._connected = False
        self._connected_since = 0.0
        self._logged_first_state = False
        self._started_at = time.monotonic()

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
        # Перемотка мышью: пока идёт протяжка, доля меняется в полосе, а
        # команду отправляем один раз - в отпускании.
        self._seeking = False
        self._seek_ratio: float | None = None

        journal.write("виджет: запуск, Python %s, журнал %s"
                      % (platform.python_version(), journal.path()))
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

        self.gx = self.root.winfo_screenwidth() - FULL_W - theme.EDGE_MARGIN
        self.gy = self.root.winfo_screenheight() - FULL_H - theme.EDGE_MARGIN
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

    def _track_connection(self) -> None:
        """Запоминает момент, когда хост подключился.

        Мост сам себя не помнит: у него есть только «подключён сейчас или
        нет». Виджету же нужно знать, сколько секунд прошло с начала связи,
        чтобы не объявить расширение молчащим в первую же секунду.
        """
        if self.bridge.connected and not self._connected:
            self._connected_since = time.monotonic()
        self._connected = self.bridge.connected

    def _diagnosis(self) -> tuple[str, str]:
        """Что показать, когда трек не видно: причина и что делать.

        Слои перебираются сверху вниз, и каждый следующий имеет смысл только
        если предыдущий в порядке:

            1. мост   - браузер и хост вообще есть ли;
            2. связь  - отвечает ли расширение;
            3. текст  - что не так со стороны VK.

        Раньше показывался только третий слой, поэтому при незапущенном
        Firefox подпись молчала, и пользователь не понимал, что делать.
        Текст первого слоя отдаёт сам мост: он различает «ждём браузер»,
        «порт занял кто-то ещё» и неудачу при попытке занять порт.
        """
        if not self.bridge.connected:
            # Подсказка зависит от того, что именно не так, иначе она
            # врёт: на занятый порт браузер не помогает, нужен второй
            # виджет, который надо закрыть.
            if self.bridge.problem == "port-taken":
                return self.bridge.note, "Закройте второй виджет"
            if self.bridge.problem == "no-port":
                return self.bridge.note, "Освободите порт и запустите виджет"
            return self.bridge.note, "Откройте Firefox с расширением"

        if not self._extension_alive():
            # Первый снимок расширение шлёт не сразу, а по своему
            # интервалу опроса вкладок. До истечения того же запаса времени,
            # что и для устаревания снимков, ждать штатно.
            waited = time.monotonic() - self._connected_since
            if waited < STALE_AFTER_SEC:
                return "Хост подключён, ждём данные...", "Подключение к VK Музыке"
            return ("Хост подключён, но расширение не отвечает",
                    "Загрузите расширение в about:debugging")

        return self.note, "Включите трек в VK Музыке"

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

    def _progress_frac(self, item, dur: float) -> float:
        """Доля пройденного для полосы.

        Незавершённая перемотка важнее расчёта по часам: пока расширение не
        прислало новый снимок, показываем то, куда попросили, а не где было.
        Иначе полоса дёрнулась бы назад в момент нажатия.
        """
        if self._seek_ratio is not None:
            return self._seek_ratio
        if dur > 0:
            return max(0.0, min(1.0, self._position(item) / dur))
        return max(0.0, min(1.0, float(item.get("frac") or 0)))

    def _seek_ratio_at(self, x: int) -> float:
        """Доля трека под курсором.

        Область нажатия шире полосы, поэтому x может выйти за её границы -
        значение зажимается в 0..1.
        """
        x0 = (FULL_W - ROW_W) // 2
        return max(0.0, min(1.0, (x - x0) / ROW_W))

    def _seek_scrub(self, x: int) -> None:
        """Показать, куда попадёт трек, пока мышь ещё нажата.

        Команду не отправляем: перемотка в браузере слоёная и стоит дорого,
        а во время протяжки событий идёт десяток в секунду.
        """
        if self._current() is None:
            return
        self._seek_ratio = self._seek_ratio_at(x)
        self._render()

    def _send_seek(self) -> None:
        if self._seek_ratio is None or self._current() is None:
            return
        self.bridge.send("seek", self.selected, self._seek_ratio)

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
            # Новый снимок отменяет недовыполненную перемотку: браузер уже
            # отчитался о реальном положении дел, доверять ему надо.
            self._seek_ratio = None
            # Первое состояние из расширения - главная точка журнала: по ней
            # видно, что цепочка браузер - хост - виджет замкнулась. Дальше
            # то же самое повторяется каждые 700 мс и только засоряет файл.
            if not self._logged_first_state:
                self._logged_first_state = True
                journal.write(
                    "виджет: первое состояние от расширения: %d вкладок, "
                    "выбрана %r, примечание %r"
                    % (len(self.items), self.selected, self.note)
                )
            changed = True

    # --- отрисовка ---------------------------------------------------------------

    def _frame(self) -> None:
        self._track_connection()
        self._drain_states()
        self._render()
        self.root.after(FRAME_MS, self._frame)

    def _render(self) -> None:
        self.canvas.delete("all")
        HIT.clear()

        if self.collapsed:
            self._render_collapsed(self._current())
        else:
            self._render_full()

    # -- свёрнутый вид: только обложка --

    def _render_collapsed(self, item) -> None:
        self._refresh_covers(item)
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
        self._refresh_covers(item)
        self.canvas.create_image(
            (FULL_W - COVER) // 2, COVER_Y, image=self._cover_panel_photo, anchor="nw"
        )

    def _cover_token(self, item) -> tuple:
        """Что считать новой обложкой: трек и сами данные картинки.

        Одного art_new мало: обложка может прийти из кэша расширения с
        art_new = false, и виджет счёл бы её той же картинкой, которой
        ещё нет, и оставил бы заглушку навсегда.
        """
        return ((item or {}).get("ck"), (item or {}).get("art") or "")

    def _refresh_covers(self, item) -> None:
        """Готовит обе картинки обложки: для карточки и для пилюли.

        Обе views ходят сюда же, а не каждая по-своему: свёрнутый вид не
        проходит через _draw_cover, и раньше он молча рисовал обложку
        прошлого трека.

        Пересобирается только при смене картинки: каждый кадр заново
        распаковывать base64 и создавать PhotoImage слишком дорого, а
        кадров в секунду двадцать.
        """
        token = self._cover_token(item)
        if token == self._cover_cache_key:
            return

        square = self._decode_cover(item)
        self._mini_photo = ImageTk.PhotoImage(theme.round_image(square, MINI_COVER))
        x0 = (FULL_W - COVER) // 2
        panel = self.wallpaper.crop((x0, COVER_Y, x0 + COVER, COVER_Y + COVER))
        panel.paste(square, (0, 0), theme.rounded_mask(COVER, theme.cover_radius(COVER)))
        self._cover_panel_photo = ImageTk.PhotoImage(panel)
        self._cover_cache_key = token

    def _decode_cover(self, item) -> Image.Image:
        """Квадратная обложка нужного размера: трек или заглушка."""
        art = (item or {}).get("art")
        if art:
            try:
                source = Image.open(io.BytesIO(base64.b64decode(art)))
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
            artist = self._diagnosis()[1]
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

        # Область нажатия шире самой полоски: ровно в PROGRESS_H пикселей
        # попасть мышью невозможно, а промах по краю означал бы, что
        # перемотки нет вовсе.
        HIT["seek"] = (x0, PROGRESS_Y - SEEK_GRAB, x1, PROGRESS_Y + PROGRESS_H + SEEK_GRAB)

        if item is None:
            label = "0:00"
        else:
            pos = self._position(item)
            dur = float(item.get("dur") or 0)
            frac = self._progress_frac(item, dur)
            if frac > 0:
                self.canvas.create_rectangle(
                    x0, PROGRESS_Y, x0 + ROW_W * frac, PROGRESS_Y + PROGRESS_H,
                    fill=ACCENT, outline="",
                )
            if dur > 0:
                label = f"{theme.fmt_time(frac * dur)} / {theme.fmt_time(dur)}"
            else:
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
        row_height = ROW_H - 4
        for entry in self.items[:MAX_SOURCE_ROWS]:
            picked = item is not None and entry.get("key") == item.get("key")
            rect = (x0, y, x0 + ROW_W, y + row_height)
            HIT["src:" + str(entry.get("key"))] = rect
            self._rounded_rect(rect, 10, fill=ACCENT_SOFT if picked else OVERLAY_DIM)

            middle = y + row_height / 2
            dot = ACCENT if picked else DIM
            self.canvas.create_oval(x0 + 10, middle - 3, x0 + 16, middle + 3,
                                    fill=dot, outline="")

            sub = entry.get("app", "")
            if entry.get("artist"):
                sub += " · " + entry["artist"]
            self.canvas.create_text(
                x0 + 24, y + 5, text=_clip(entry.get("title") or "Unknown track", 30),
                font=("Segoe UI", 10, "bold"), fill=TEXT, anchor="nw",
            )
            self.canvas.create_text(
                x0 + 24, y + 21, text=_clip(sub, 40), font=("Segoe UI", 9),
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
                (FULL_W - ROW_W) // 2, top, text=_clip(self._diagnosis()[0], 46),
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

        if self._pressed == "seek":
            # Полоса прогресса перематывает, а не двигает окно: тянуть звук
            # за собой можно только если мышь остаётся на полосе. Захват
            # окна здесь не ставим намеренно.
            self._seeking = True
            self._drag_from = None
            self._seek_scrub(event.x)

    def _on_release(self, event) -> None:
        if self._seeking:
            # Команда уходит один раз, в отпускании: во время протяжки
            # события идут десятками в секунду.
            self._seeking = False
            self._send_seek()
            return

        dragged = self._did_drag
        name = None if dragged else self._pressed
        self._pressed = None
        self._drag_from = None
        self._did_drag = False
        if dragged:
            # После перетаскивания окно примыкает к краю, даже если
            # пользователь отпустил его в паре пикселей от края.
            w, h = self._size()
            self._snap_to_edges(w, h)
            self._keep_on_screen(w, h)
            self._geometry(w, h)
        elif name is not None and name == self._hit(event.x, event.y):
            self._activate(name)

    def _on_drag(self, event) -> None:
        if self._seeking:
            self._seek_scrub(event.x)
            return
        if self._drag_from is None:
            return
        dx = event.x - self._press_xy[0]
        dy = event.y - self._press_xy[1]
        if not self._did_drag and math.hypot(dx, dy) < DRAG_THRESHOLD:
            return
        self._did_drag = True
        self._pressed = None
        w, h = self._size()
        self.gx = event.x_root - self._drag_from[0]
        self.gy = event.y_root - self._drag_from[1]
        self._snap_to_edges(w, h)
        self._keep_on_screen(w, h)
        self._geometry(w, h)

    def _on_right_click(self, event) -> None:
        # В свёрнутом виде кнопок нет вообще - меню это единственный
        # способ что-то сделать.
        menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT, bd=0,
                       activebackground=ACCENT, activeforeground="#ffffff")
        menu.add_command(label="Развернуть", command=self._expand)
        menu.add_command(label="Закрепить поверх окон", command=self._toggle_topmost)
        menu.add_command(label="Диагностика", command=self._show_status)
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

    # --- окно диагностики -------------------------------------------------------

    def _status_lines(self) -> list[tuple[str, str]]:
        """Состояние виджета парами «что - значение».

        Порядок - от внешнего к внутреннему: сначала браузер и хост, потом
        расширение, потом сам виджет. Так по списку видно, на каком шаге
        цепочка оборвалась.
        """
        note, hint = self._diagnosis()
        endpoint = ipc.read_endpoint()
        age = time.monotonic() - self.last_state_at if self.last_state_at else None
        current = self._current() or {}
        shape = "свёрнут" if self.collapsed else "развёрнут"
        pinned = "поверх окон" if self.topmost else "не закреплён"
        return [
            ("Состояние", note),
            ("Что делать", hint),
            ("Хост браузера", "подключён" if self.bridge.connected else "не подключён"),
            ("Порт", str(endpoint["port"]) if endpoint else "нет"),
            ("Точка входа", str(ipc.ENDPOINT_FILE)),
            ("Расширение", "отвечает" if self._extension_alive() else "молчит"),
            ("Последний снимок", "%s назад" % journal.uptime_note(self.last_state_at)
             if age is not None else "ни одного"),
            ("Вкладок VK", str(len(self.items))),
            ("Выбрана", str(self.selected)),
            ("Сейчас играет", str(current.get("title") or "—")),
            ("Виджет", "%s, %s" % (shape, pinned)),
            ("Работает", journal.uptime_note(self._started_at)),
            ("Журнал", str(journal.path())),
        ]

    def _show_status(self) -> None:
        """Окно со статусом и хвостом журнала.

        Отдельное окно, а не вывод в консоль: у виджета её нет. Показываем
        не всё состояние целиком, а последние строки журнала - их и надо
        читать, когда что-то сломалось.
        """
        window = tk.Toplevel(self.root)
        window.title("Music Widget - диагностика")
        window.configure(bg=CARD)
        window.resizable(False, False)
        window.attributes("-topmost", True)

        body = tk.Frame(window, bg=CARD)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        for name, value in self._status_lines():
            row = tk.Frame(body, bg=CARD)
            row.pack(fill="x", anchor="w")
            tk.Label(row, text=name + ":", bg=CARD, fg=MUTED,
                     font=("Segoe UI", 9), width=16, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=CARD, fg=TEXT,
                     font=("Segoe UI", 9), anchor="w").pack(side="left")

        tail = self._log_tail()
        block = tk.Frame(body, bg=CARD)
        block.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(block, text="Последние записи журнала", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
        text = tk.Text(block, bg="#101014", fg="#d8d8e0", wrap="none",
                       height=12, width=78, relief="flat",
                       font=("Consolas", 9))
        text.pack(fill="both", expand=True, pady=(4, 0))
        text.insert("1.0", tail)
        text.configure(state="disabled")

        def close():
            window.destroy()

        tk.Button(window, text="Закрыть", command=close, bg=ACCENT,
                  fg="#ffffff", relief="flat", takefocus=0).pack(pady=(0, 12))
        window.protocol("WM_DELETE_WINDOW", close)

    @staticmethod
    def _log_tail(lines: int = 60) -> str:
        """Хвост журнала или объяснение, почему его нет."""
        try:
            content = journal.path().read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return "журнал недоступен: %s" % exc
        tail = content.splitlines()[-lines:]
        return "\n".join(tail) if tail else "журнал пуст"

    # --- переключение вида -------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        return (MINI_COVER, MINI_COVER) if self.collapsed else (FULL_W, FULL_H)

    def _screen(self) -> tuple[int, int]:
        return self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _keep_on_screen(self, w: int, h: int) -> None:
        """Не даёт окну уехать за края так, чтобы его было не найти."""
        sw, sh = self._screen()
        self.gx = max(EDGE_MARGIN - w // 2, min(sw - w - EDGE_MARGIN // 2, self.gx))
        self.gy = max(0, min(sh - 40, self.gy))

    def _nearest_edge(self, w: int, h: int) -> tuple[int, int]:
        """К какому краю экрана ближе всего окно.

        Нужно для автосвёртывания: свёрнутая карточка должна уезжать к
        краю, а не оставаться посреди экрана.
        """
        sw, sh = self._screen()
        gaps = {
            "left": self.gx,
            "right": sw - (self.gx + w),
            "top": self.gy,
            "bottom": sh - (self.gy + h),
        }
        edge = min(gaps, key=lambda key: gaps[key])
        if edge == "left":
            return EDGE_MARGIN, self.gy
        if edge == "right":
            return sw - w - EDGE_MARGIN, self.gy
        if edge == "top":
            return self.gx, EDGE_MARGIN
        return self.gx, sh - h - EDGE_MARGIN

    def _snap_to_edges(self, w: int, h: int) -> None:
        """Притягивает окно к краям, если оно рядом с ними."""
        sw, sh = self._screen()
        if self.gx <= SNAP_DISTANCE:
            self.gx = EDGE_MARGIN
        if self.gx + w >= sw - SNAP_DISTANCE:
            self.gx = sw - w - EDGE_MARGIN
        if self.gy <= SNAP_DISTANCE:
            self.gy = EDGE_MARGIN
        if self.gy + h >= sh - SNAP_DISTANCE:
            self.gy = sh - h - EDGE_MARGIN

    def _bottom_right(self, w: int, h: int) -> tuple[int, int]:
        sw, sh = self._screen()
        return sw - w - EDGE_MARGIN, sh - h - EDGE_MARGIN

    def _collapse(self) -> None:
        # Свёрнутая версия всегда примыкает к краю, где её не закрыть
        # окнами: по центру экрана её легко потерять из виду.
        w, h = MINI_COVER, MINI_COVER
        self.gx, self.gy = self._nearest_edge(w, h)
        self.collapsed = True
        self._geometry(w, h)
        self.canvas.configure(width=w, height=h)
        self._hover = None
        self._render()

    def _expand(self) -> None:
        # Развёрнутая версия всегда встаёт в правый нижний угол: так её
        # найдёшь сразу, где бы ни оказалась свёрнутая.
        w, h = FULL_W, FULL_H
        self.gx, self.gy = self._bottom_right(w, h)
        self.collapsed = False
        self._geometry(w, h)
        self.canvas.configure(width=w, height=h)
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
        journal.write("виджет: закрытие пользователем")
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