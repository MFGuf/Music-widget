#!/usr/bin/env python
"""Проверки диагностики виджета (widget/vk_music_widget.py).

Запуск:
    python widget\\widget_test.py

Проверяется то, что пользователь видит, когда трек не видно. Раньше подпись
молчала: показывался только текст от расширения, а при незапущенном Firefox
он не приходил вовсе, и оставалось вечное «Подключение к браузеру...».
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ipc  # noqa: E402
import theme  # noqa: E402
import vk_music_widget as mw  # noqa: E402
from report import Checks  # noqa: E402
from vk_music_widget import MusicWidget  # noqa: E402

checks = Checks("widget", "диагностика виджета (vk_music_widget)")


def ok(name, condition, detail=""):
    return checks.ok(name, condition, detail)


def equal(name, actual, expected):
    return checks.equal(name, actual, expected)


def fake_event(w: MusicWidget, x: int, y: int):
    """Событие мыши в координатах виджета.

    Отдельный простой объект вместо настоящего Tk-события: обработчики
    читают только x, y и координаты на экране, а настоящее событие в тесте
    пришлось бы подделывать через canvas.event_generate с очередью событий.
    """
    return SimpleNamespace(x=x, y=y, x_root=w.gx + x, y_root=w.gy + y)


def catch_commands(w: MusicWidget) -> list:
    """Подменяет отправку команд списком - их потом можно проверить."""
    sent: list = []
    w.bridge.send = lambda action, key=None, ratio=None: sent.append(
        (action, key, ratio))
    return sent


TRACK = {
    "key": "tab:7", "title": "Кино", "artist": "Группа", "app": "VK Музыка",
    "dur": 200.0, "pos": 20.0, "playing": True,
}


def show_track(w: MusicWidget) -> None:
    w.items = [dict(TRACK)]
    w.selected = "tab:7"
    w._seek_ratio = None
    w._seeking = False
    w._render()


def setup(w: MusicWidget, *, connected, note="", problem=None,
          extension_note="", state_age=None, connected_age=0.0) -> None:
    """Готовит виджет к проверке одного сценария.

    state_age - сколько секунд назад пришёл последний снимок; None означает,
    что снимка не было вовсе (last_state_at = 0). Различать это обязательно:
    снимок секундной давности и отсутствие снимка дают в коде разные вещи.

    connected_age - сколько секунд назад подключился хост.
    """
    now = time.monotonic()
    w.bridge._connected = connected
    w.bridge._set_note(note, problem)
    w.note = extension_note
    w.last_state_at = 0.0 if state_age is None else now - state_age
    w._connected = connected
    w._connected_since = now - connected_age


def run_checks(w: MusicWidget) -> None:
    # --- 1. Браузер не запущен ------------------------------------------------
    setup(w, connected=False, note="Ждём браузер")
    note, hint = w._diagnosis()
    equal("нет браузера: видна причина", note, "Ждём браузер")
    equal("нет браузера: видно что делать", hint, "Откройте Firefox с расширением")

    # --- 2. Порт занял второй виджет ------------------------------------------
    # Подсказка тут не про браузер: хоть он и нужен, но занятый порт он не
    # освободит.
    setup(w, connected=False, note="Кто-то ещё занял порт виджета",
          problem="port-taken")
    note, hint = w._diagnosis()
    equal("занятый порт: видна причина", note, "Кто-то ещё занял порт виджета")
    equal("занятый порт: видно что делать", hint, "Закройте второй виджет")

    # --- 3. Порт вообще не удаётся занять ------------------------------------
    setup(w, connected=False,
          note="не удалось занять порт для связи с хостом", problem="no-port")
    note, hint = w._diagnosis()
    equal("нет порта: видно что делать", hint, "Освободите порт и запустите виджет")

    # --- 4. Хост только что подключился ---------------------------------------
    # Ругаться на расширение в первые секунды после старта нельзя: первый
    # снимок оно присылает по своему интервалу опроса вкладок.
    setup(w, connected=True, note="Подключено", connected_age=1.0)
    note, hint = w._diagnosis()
    ok("после подключения ждём данные, а не ругаемся",
       "ждём данные" in note, note)

    # --- 5. Хост жив, расширение молчит --------------------------------------
    setup(w, connected=True, note="Подключено", connected_age=30.0)
    note, hint = w._diagnosis()
    equal("молчащее расширение: видна причина", note,
          "Хост подключён, но расширение не отвечает")
    equal("молчащее расширение: видно что делать", hint,
          "Загрузите расширение в about:debugging")

    # --- 6. Расширение отвечает, вкладок VK нет ------------------------------
    setup(w, connected=True, note="Подключено", state_age=1.0, connected_age=1.0,
          extension_note="Не найдено вкладок VK Музыки")
    note, hint = w._diagnosis()
    equal("нет вкладок: показан текст расширения", note,
          "Не найдено вкладок VK Музыки")
    equal("нет вкладок: видно что делать", hint, "Включите трек в VK Музыке")

    # --- 7. Устаревший снимок перестаёт считаться свежим -----------------------
    setup(w, connected=True, note="Подключено", state_age=30.0, connected_age=30.0,
          extension_note="Подключено")
    ok("снимок старше запаса не считается свежим", not w._extension_alive())
    note, _ = w._diagnosis()
    ok("устаревший снимок не показывается как штатная работа",
       "не отвечает" in note, note)

    # --- 8. Момент подключения запоминается ----------------------------------
    w.bridge._connected = False
    w._connected = False
    w._connected_since = 0.0
    w._track_connection()
    equal("до подключения момент не запомнен", w._connected_since, 0.0)

    w.bridge._connected = True
    w._track_connection()
    first = w._connected_since
    ok("подключение запомнило момент", first > 0.0, str(first))

    # Пока связь жива, момент не должен сдвигаться: иначе «молчит» сбрасывалось
    # бы каждые 50 миллисекунд и жалобы не появлялось бы никогда.
    time.sleep(0.05)
    w._track_connection()
    equal("повторные кадры не сдвигают момент", w._connected_since, first)

    w.bridge._connected = False
    w._track_connection()
    equal("разрыв связи сбрасывает признак подключения", w._connected, False)
    w.bridge._connected = True
    w._track_connection()
    ok("новое подключение запомнило новый момент", w._connected_since > first)

    # --- 9. Причина и подсказка не совпадают ---------------------------------
    # Одна строка в двух местах пользователю нечего, как и раньше.
    setup(w, connected=False, note="Ждём браузер")
    note, hint = w._diagnosis()
    ok("причина и подсказка различаются", note != hint, f"{note!r} / {hint!r}")

    # --- 10. Перемотка мышью по полосе прогресса ------------------------------
    sent = catch_commands(w)
    show_track(w)
    x0 = (mw.FULL_W - mw.ROW_W) // 2
    mid_y = mw.PROGRESS_Y + mw.PROGRESS_H / 2

    rect = mw.HIT.get("seek")
    ok("полоса прогресса видна как зона нажатия", rect is not None)
    # По вертикали зона шире полоски: в PROGRESS_H пикселей попасть нельзя.
    # По горизонтали - ровно полоска: поля по бокам остаются местом, за
    # которое тянут окно.
    ok("зона нажатия выше и ниже полоски",
       rect is not None and (rect[3] - rect[1]) > mw.PROGRESS_H,
       str(rect))
    ok("зона нажатия по ширине равна полоске",
       rect is not None and (rect[2] - rect[0]) == mw.ROW_W)
    equal("нажатие над полоской ловится",
          w._hit(x0 + mw.ROW_W // 2, mw.PROGRESS_Y - 5), "seek")
    equal("нажатие под полоской ловится",
          w._hit(x0 + mw.ROW_W // 2, mw.PROGRESS_Y + mw.PROGRESS_H + 5), "seek")
    ok("нажатие сбоку от полоски перемоткой не считается",
       w._hit(x0 - 5, mid_y) != "seek")

    # Клик по середине.
    w._on_press(fake_event(w, x0 + mw.ROW_W // 2, mid_y))
    ok("нажатие на полосе начало перемотку", w._seeking is True)
    ok("нажатие на полосе не захватило окно", w._drag_from is None)
    equal("клик по середине дал половину", w._seek_ratio, 0.5)
    w._on_release(fake_event(w, x0 + mw.ROW_W // 2, mid_y))
    ok("клик отправил ровно одну команду",
       len(sent) == 1 and sent[0][0] == "seek"
       and abs(sent[0][2] - 0.5) < 1e-6, str(sent))

    # Протяжка: команда одна и в конце, а не на каждое движение мыши.
    sent.clear()
    w._on_press(fake_event(w, x0 + 10, mid_y))
    for step in (20, 40, 60, mw.ROW_W):
        w._on_drag(fake_event(w, x0 + step, mid_y))
    ok("протяжка не шлёт команд на каждый кадр", not sent, str(sent))
    equal("протяжка дошла до конца дорожки", w._seek_ratio, 1.0)
    w._on_release(fake_event(w, x0 + mw.ROW_W, mid_y))
    ok("протяжка отправила одну команду в конце",
       len(sent) == 1 and abs(sent[0][2] - 1.0) < 1e-6, str(sent))

    # Новый снимок отменяет недовыполненную перемотку.
    w.states.put({"items": [dict(TRACK)], "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    ok("новый снимок отменил недовыполненную перемотку",
       w._seek_ratio is None, str(w._seek_ratio))

    # Без трека перематывать нечего.
    show_track(w)
    sent.clear()
    w.items = []
    w._on_press(fake_event(w, x0 + mw.ROW_W // 2, mid_y))
    w._on_release(fake_event(w, x0 + mw.ROW_W // 2, mid_y))
    ok("без трека перемотка не отправляется", not sent, str(sent))

    # За пределами полосы окно по-прежнему тянется.
    show_track(w)
    w._on_press(fake_event(w, 5, mw.APP_Y + 4))
    w._on_drag(fake_event(w, 40, mw.APP_Y + 40))
    ok("вне полосы мышь тянет окно, а не перематывает",
       w._did_drag is True and w._seeking is False,
       f"seeking={w._seeking} gx={w.gx} gy={w.gy}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mw-widget-"))
    # Своя точка входа, иначе проверка перезаписала бы файл работающего у
    # пользователя виджета и тот потерял бы связь с браузером.
    saved_file, saved_dir = ipc.ENDPOINT_FILE, ipc.APP_DIR
    ipc.ENDPOINT_FILE = tmp / "endpoint.json"
    ipc.APP_DIR = tmp

    theme.dpi_aware()
    widget = MusicWidget()
    try:
        widget.root.after(300, run_checks, widget)
        widget.root.after(15000, widget.root.destroy)
        widget.run()
    finally:
        widget.bridge.stop()
        ipc.ENDPOINT_FILE, ipc.APP_DIR = saved_file, saved_dir
        try:
            widget.root.destroy()
        except Exception:
            pass

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())