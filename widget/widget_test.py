#!/usr/bin/env python
"""Проверки виджета (widget/vk_music_widget.py).

Запуск:
    python widget\\widget_test.py

Две части. Первая - что пользователь видит, когда трек не видно: раньше
подпись молчала, показывался только текст от расширения, а при незапущенном
Firefox он не приходил вовсе, и оставалось вечное «Подключение к браузеру...».
Вторая - работа мыши: перемотка по полосе, выбор вкладки, сворачивание,
закрепление и перетаскивание окна.
"""

from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ipc  # noqa: E402
import log as journal  # noqa: E402
import theme  # noqa: E402
import vk_music_widget as mw  # noqa: E402
from report import Checks  # noqa: E402
from vk_music_widget import MusicWidget  # noqa: E402

checks = Checks("widget", "виджет: диагностика и работа мыши (vk_music_widget)")


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


def cover_art(color: tuple) -> str:
    """Обложка в виде base64 - ровно так её присылает расширение."""
    image = Image.new("RGB", (300, 300), color)
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def count_decodes(action) -> int:
    """Сколько раз действие распаковало картинку.

    Считает вызовы Image.open: обложка приходит строкой base64, и единственное
    место, где её распаковывают, - виджет.
    """
    opened = {"n": 0}
    real_open = Image.open

    def counting_open(*args, **kwargs):
        opened["n"] += 1
        return real_open(*args, **kwargs)

    Image.open = counting_open
    try:
        action()
    finally:
        Image.open = real_open
    return opened["n"]


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

    # --- 11. Выбор активной вкладки -------------------------------------------
    other = dict(TRACK, key="tab:9", title="Земфир", artist="Другой", dur=20.0)
    w.items = [dict(TRACK), other]
    w.selected = "tab:7"
    w._render()
    rows = sorted(k for k in mw.HIT if k.startswith("src:"))
    equal("в списке обе вкладки", rows, ["src:tab:7", "src:tab:9"])

    rect = mw.HIT["src:tab:9"]
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    equal("строка вкладки ловит нажатие", w._hit(cx, cy), "src:tab:9")

    w._on_press(fake_event(w, cx, cy))
    w._on_release(fake_event(w, cx, cy))
    ok("выбор вкладки отправил команду",
       sent == [("select", "tab:9", None)], str(sent))
    equal("выбор виден на видужете сразу, не дожидаясь снимка", w.selected, "tab:9")
    equal("рисуется трек выбранной вкладки", (w._current() or {}).get("key"), "tab:9")

    w.items = [dict(TRACK)]
    w._render()
    ok("при одной вкладке список не занимает место",
       not any(k.startswith("src:") for k in mw.HIT))

    # --- 12. Сворачивание в пилюлю --------------------------------------------
    w.collapsed = False
    w._collapse()
    ok("виджет свернулся", w.collapsed is True)
    w._render()
    equal("в пилюле нажатие ловит только разворот", w._hit(30, 30), "expand")
    w._expand()
    ok("виджет развернулся", w.collapsed is False)

    # --- 13. Закрепление поверх окон -------------------------------------------
    was = w.topmost
    w._toggle_topmost()
    ok("закрепление переключилось", w.topmost is (not was),
       f"{was} -> {w.topmost}")
    w._toggle_topmost()
    ok("закрепление переключается обратно", w.topmost is was)

    # --- 14. Перетаскивание окна ----------------------------------------------
    show_track(w)
    w.gx, w.gy = 400, 300
    w._on_press(fake_event(w, 100, 200))
    w._on_drag(fake_event(w, 100 + mw.DRAG_THRESHOLD - 2, 201))
    ok("сдвиг меньше порога не тянет окно", w._did_drag is False)
    w._on_release(fake_event(w, 100 + mw.DRAG_THRESHOLD - 2, 201))

    w._on_press(fake_event(w, 100, 200))
    w._on_drag(fake_event(w, 140, 260))
    ok("сдвиг больше порога тянет окно",
       w._did_drag is True and (w.gx, w.gy) != (400, 300), f"gx={w.gx} gy={w.gy}")
    w._on_release(fake_event(w, 140, 260))
    sw, sh = w._screen()
    ok("окно не уехало за экран",
       0 <= w.gx <= sw and 0 <= w.gy <= sh, f"gx={w.gx} gy={w.gy}")

    # --- 15. Обложка трека ---------------------------------------------------
    red, blue = cover_art((255, 0, 0)), cover_art((0, 0, 255))

    def with_art(key, art=None, **extra):
        item = dict(TRACK, key=key, ck=key, **extra)
        if art is not None:
            item["art"] = art
            item["art_new"] = True
        w.items = [item]
        w.selected = key
        w.collapsed = False
        w._render_full()

    # Свёрнутый вид рисует обложку сам, минуя _draw_cover, и раньше молчал,
    # показывая картинку прошлого трека.
    with_art("k1", red)
    first = w._mini_photo
    with_art("k2", blue)
    w.collapsed = True
    w._render()
    ok("свёрнутая пилюля подхватывает обложку нового трека",
       w._mini_photo is not first)
    w.collapsed = False

    # Кадров двадцать в секунду: пересобирать картинку каждый раз слишком
    # дорого, но только если кэш ловит неизменную обложку.
    with_art("k1", red)
    decoded = count_decodes(lambda: [w._render_full() for _ in range(50)])
    equal("неизменная обложка не декодируется каждый кадр", decoded, 0)

    # Обложка приходит позже того же трека, поэтому одного признака трека
    # мало - нужен ещё и сам кусок данных.
    w.items = [dict(TRACK, key="k1", ck="k1")]
    w.selected = "k1"
    w._render_full()
    w.items[0]["art"] = red
    w.items[0]["art_new"] = True
    equal("пришедшая позже обложка обновляет картинку",
          count_decodes(w._render_full), 1)

    with_art("k9")
    ok("без картинки рисуется заглушка", w._cover_panel_photo is not None)

    with_art("kx", "это-не-base64!!")
    ok("битая картинка не роняет отрисовку", w._cover_panel_photo is not None)

    # --- 16. Диагностика: журнал и окно статуса -------------------------------
    ok("журнал создан при запуске", journal.path().exists())
    written = journal.path().read_text(encoding="utf-8", errors="replace")
    ok("в журнале есть запуск виджета", "запуск" in written)
    ok("в журнале есть порт, который слушает мост", "слушаю порт" in written)

    show_track(w)
    w.states.put({"items": [dict(TRACK)], "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    ok("в журнал попало первое состояние от расширения",
       "первое состояние" in written)

    names = [name for name, _ in w._status_lines()]
    for need in ("Состояние", "Что делать", "Хост браузера", "Порт",
                 "Расширение", "Вкладок VK", "Журнал", "Виджет"):
        ok("в статусе есть строка %s" % need, need in names)

    status = dict(w._status_lines())
    ok("порт в статусе показан числом", status["Порт"].isdigit(), status["Порт"])
    equal("вкладок в статусе посчитано верно", status["Вкладок VK"], "1")
    equal("играющий трек виден в статусе", status["Сейчас играет"], TRACK["title"])
    ok("путь к журналу показан",
       status["Журнал"].endswith("widget.log"), status["Журнал"])

    # И вид, и закрепление должны быть видны в одной строке: закрепление
    # само по себе ещё ничего не говорит о том, что на экране.
    ok("закреплённый развёрнутый вид описан целиком",
       "развёрнут" in status["Виджет"] and "поверх окон" in status["Виджет"],
       status["Виджет"])
    w.topmost = False
    status = dict(w._status_lines())
    ok("незакреплённый вид описан целиком",
       "развёрнут" in status["Виджет"] and "не закреплён" in status["Виджет"],
       status["Виджет"])
    w.topmost = True

    tail = w._log_tail(5)
    # Хвост - это последние строки, а не первые: к этому моменту журнал
    # длиннее пяти строк, и запись о запуске уже вытеснена. Проверяем
    # поэтому свежее, а не давнее.
    ok("хвост журнала содержит последнюю запись",
       "первое состояние" in tail, tail.splitlines()[-1][:60] if tail else "")
    ok("хвост журнала ограничен запрошенным числом строк",
       len(tail.splitlines()) <= 5, str(len(tail.splitlines())))
    ok("каждая строка журнала начинается со времени",
       all(line[:4].isdigit() and line[4] == "-" for line in tail.splitlines()),
       tail.splitlines()[0][:20] if tail else "")

    # --- 17. Ответ на кнопку в окне диагностики ------------------------------
    # Неисправная кнопка молчит одинаково при любой причине. Диагностика
    # обязана называть причину, иначе приходится чинить наугад.
    w.last_command = None
    ok("до нажатия кнопок сказано прямо",
       "ещё не нажимали" in dict(w._status_lines())["Кнопки"])

    w.states.put({"items": [dict(TRACK)], "selected": "tab:7", "note": "Подключено",
                  "command": {"action": "toggle", "ok": False,
                              "msg": "не нашёл кнопку. Видимые: play / next"}})
    w._drain_states()
    line = dict(w._status_lines())["Кнопки"]
    ok("неудачная команда помечена как несработавшая", "НЕ сработала" in line, line)
    ok("в диагностике виден перечень найденных кнопок",
       "play / next" in line, line)

    w.states.put({"items": [dict(TRACK)], "selected": "tab:7", "note": "Подключено",
                  "command": {"action": "next", "ok": True, "msg": "кнопка: Icon--next"}})
    w._drain_states()
    line = dict(w._status_lines())["Кнопки"]
    ok("удачная команда помечена как сработавшая",
       "сработала" in line and "НЕ" not in line, line)

    # --- 18. Сведения об обложках в диагностике ------------------------------
    # Пустое место обложки означает и "не нашлась в ответе VK", и "загрузка
    # сорвалась". Лечится по-разному, поэтому виджет обязан различать.
    w.last_art = None
    ok("без сведений сказано, что обложки просто не грузились",
       "неудач 0" in dict(w._status_lines())["Обложки"])

    w.states.put({"items": [dict(TRACK)], "selected": "tab:7", "note": "Подключено",
                  "art": {"error": "", "url": "https://sun1-96.userapi.com/a/w100.jpg",
                          "stats": {"ok": 2, "fail": 0}}})
    w._drain_states()
    line = dict(w._status_lines())["Обложки"]
    ok("удачная загрузка обложки отражена в диагностике", "загружено 2" in line, line)

    w.states.put({"items": [dict(TRACK)], "selected": "tab:7", "note": "Подключено",
                  "art": {"error": "HTTP 403", "url": "https://sun1-96.userapi.com/b.jpg",
                          "stats": {"ok": 0, "fail": 3}}})
    w._drain_states()
    line = dict(w._status_lines())["Обложки"]
    ok("причина сбоя загрузки обложки видна в диагностике", "HTTP 403" in line, line)
    ok("в диагностике видно, откуда качали", "sun1-96" in line, line)

    # --- 20. Панель в диагностике: обложка должна быть от того же трека ------
    # Пользователь получил обложку одной песни с названием другой. Видно, откуда
    # взята обложка, какой трек опознан и где лежит звук.
    w.states.put({"items": [dict(TRACK, diag={"matchedBy": "ссылка на трек",
                                              "rowId": 902,
                                              "mediaConnected": True,
                                              "coverSource": "панель плеера",
                                              "panelTitle": "Какой-то плейлист",
                                              "panel": "div.player | span «Вторая»"})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    panel_line = dict(w._status_lines())["Панель"]
    ok("в диагностике видно, какой трек опознан точно",
       "трек в строке 902" in panel_line, panel_line)
    ok("в диагностике видно, откуда взята обложка",
       "обложка из панель плеера" in panel_line, panel_line)
    ok("в диагностике видно разбор панели плеера",
       "span «Вторая»" in panel_line, panel_line)

    w.states.put({"items": [dict(TRACK, diag={"matchedBy": "", "mediaConnected": False})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    panel_line = dict(w._status_lines())["Панель"]
    ok("когда трек не опознан, видно что звук вне страницы",
       "звук вне страницы" in panel_line and "обложка из" not in panel_line,
       panel_line)

    # --- 21. По адресу обложки видно, почему качество плохое -------------
    # Пользователь жаловался на мыло: маленький размер зашит прямо в адрес,
    # а у подписанного адреса размер не поменять.
    w.states.put({"items": [dict(TRACK, diag={
                      "matchedBy": "ссылка на трек", "mediaConnected": True,
                      "coverUrl": "https://sun9-67.vkuserphoto.ru/impg/Q_eJA/w100.jpg?size=100x100&sign=abc",
                      "playButton": "button.play | div.player «Вторая»"})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    addr_line = dict(w._status_lines())["Адрес обложки"]
    ok("в диагностике виден полный адрес обложки",
       "vkuserphoto.ru/impg/Q_eJA/w100.jpg" in addr_line, addr_line)
    ok("видно, что размер зашит в адрес",
       "размер запрошен в адресе" in addr_line, addr_line)
    ok("видно, что подписанный адрес размер не поменять",
       "подписан" in addr_line, addr_line)

    w.states.put({"items": [dict(TRACK, diag={"coverUrl": ""})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    ok("когда обложки нет, сказано прямо",
       dict(w._status_lines())["Адрес обложки"] == "обложки нет",
       dict(w._status_lines())["Адрес обложки"])

    # --- 22. Разбор делается по снимку, где музыка играла -------------------
    # Диагностику копируют когда получится, а не тогда, когда играет. Снимок
    # "сейчас ничего не играет" показывает только то, что ничего не играет, и
    # разбирать его бесполезно - так выглядит поломка, которой нет.
    w.playing_item = None
    w.playing_seen_at = 0.0
    w.states.put({"items": [dict(TRACK, playing=True, pos=42.0, dur=200.0,
                                  diag={"matchedBy": "ссылка на трек", "rowId": 902,
                                        "coverUrl": "https://sun9-67.vkuserphoto.ru/impg/Q/w600.jpg"})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    rows = dict(w._status_lines())
    ok("разбор отмечен как сделанный по снимку, где играло",
       rows["Разбор сделан"] == "по снимку, где музыка играла", rows["Разбор сделан"])
    ok("по снимку игры виден правильный трек",
       "902" in rows["Панель"], rows["Панель"])

    # Музыку остановили, диагностику копируют сейчас.
    w.states.put({"items": [dict(TRACK, playing=False, pos=0.0, dur=0.0,
                                  diag={})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    rows = dict(w._status_lines())
    ok("после остановки разбор всё ещё по снимку, где играло",
       rows["Разбор сделан"] == "по снимку, где музыка играла", rows["Разбор сделан"])
    ok("остановившийся снимок не подменяет снимок игры",
       "902" in rows["Панель"], rows["Панель"])
    ok("название видно из снимка игры, а не из заголовка раздела",
       rows["Сейчас играет"] == TRACK["title"], rows["Сейчас играет"])

    # Если музыка не играла ни разу - честно говорим об этом.
    w.playing_item = None
    w.playing_seen_at = 0.0
    w.states.put({"items": [dict(TRACK, playing=False, pos=0.0, dur=0.0, diag={})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    ok("если музыка не играла, сказано прямо",
       dict(w._status_lines())["Разбор сделан"] == "по текущему снимку: музыка не играла",
       dict(w._status_lines())["Разбор сделан"])

    # --- 19. Диагностику можно скопировать ------------------------------------
    # Переписывать сведения руками смысла нет: написанное от руки рано или
    # поздно становится неправдой. Копируется ровно то, что показано в окне.
    copied = w._diagnostics_text()
    ok("в копии есть все строки окна",
       all(("%s: %s" % pair) in copied for pair in w._status_lines()),
       copied[:200])
    ok("в копии есть и хвост журнала", "--- журнал ---" in copied, copied[-120:])
    ok("в копии видны сведения об обложках", "HTTP 403" in copied, copied[:200])

    # Главное, ради чего копирование и затевалось: строка про ответы VK
    # целиком, с опознанием и полями - её и присылают.
    w.states.put({"items": [dict(TRACK, diag={"apiHits": 73, "apiTracks": 201,
                                               "matchedBy": "длительность",
                                               "fields": ["id", "title", "thumb_src"]})],
                  "selected": "tab:7", "note": "Подключено"})
    w._drain_states()
    copied = w._diagnostics_text()
    ok("в копии виден полный разбор ответов VK",
       "Ответов VK API: 73 ответов" in copied
       and "треков в кэше 201" in copied
       and "опознан по: длительность" in copied
       and "поля VK: id, title, thumb_src" in copied,
       copied[:300])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mw-widget-"))
    # Свой журнал: иначе проверка писала бы в журнал пользователя, и тот
    # читал бы в диагностике следы наших прогонов как свои поломки.
    saved_log = os.environ.get("MUSIC_WIDGET_LOG_DIR")
    os.environ["MUSIC_WIDGET_LOG_DIR"] = str(tmp / "log")
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
        if saved_log is None:
            os.environ.pop("MUSIC_WIDGET_LOG_DIR", None)
        else:
            os.environ["MUSIC_WIDGET_LOG_DIR"] = saved_log
        try:
            widget.root.destroy()
        except Exception:
            pass

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())