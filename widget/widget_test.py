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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ipc  # noqa: E402
import theme  # noqa: E402
from report import Checks  # noqa: E402
from vk_music_widget import MusicWidget  # noqa: E402

checks = Checks("widget", "диагностика виджета (vk_music_widget)")


def ok(name, condition, detail=""):
    return checks.ok(name, condition, detail)


def equal(name, actual, expected):
    return checks.equal(name, actual, expected)


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