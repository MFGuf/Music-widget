"""Журнал виджета: что происходило, пока его не было видно.

Запуск:
    python widget\\vk_music_widget.py

Виджет рисуется на холсте и запускается через pythonw, то есть консоли у
него нет. Спросить «что сломалось» негде, а ломается виджет обычно тихо:
хост не зарегистрирован, расширение не установлено, порт занял кто-то
ещё. Поэтому всё важное пишется в файл, а окно диагностики его
показывает.

Правила, которые здесь важны:

  * журнал не имеет права уронить виджет. Любая ошибка записи молча
    проглатывается - иначе из-за отсутствия прав на диск перестанет
    работать музыка;
  * файл ограничен по размеру. Виджет живёт неделями, а писать в него
    можно много;
  * переносы строк принудительные. Открытый в «Блокноте» файл не должен
    слипать записи в одну простыню.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Куда пишем. Тот же каталог, что и точка входа: он уже создаётся при
# запуске и лежит в профиле пользователя, а не рядом с исходниками.
#
# Переменная окружения нужна проверкам. Иначе автопрогоны писали бы в
# журнал пользователя, и тот читал бы в диагностике чужие строки: "кто-то
# подключился с чужим токеном" или "скрипт страницы не запустился" - это
# следы наших тестов, а не его браузера. В диагностике это выглядит как
# поломка у него.
LOG_DIR = Path(
    os.environ.get("MUSIC_WIDGET_LOG_DIR")
    or (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MusicWidget")
)
LOG_FILE = LOG_DIR / "widget.log"

# Больше этого - обрезаем начало. Час работы виджета даёт единицы
# килобайт, месяц - десятки; полмегабайта хватает за глаза.
MAX_BYTES = 512 * 1024

_lock = threading.Lock()
_broken = False


def path() -> Path:
    """Путь к журналу. Отдельная функция, чтобы его можно было показать.

    Путь вычисляется заново при каждом обращении, а не берётся из
    константы: проверки перенаправляют журнал уже после импорта модуля.
    """
    if os.environ.get("MUSIC_WIDGET_LOG_DIR"):
        return Path(os.environ["MUSIC_WIDGET_LOG_DIR"]) / LOG_FILE.name
    return LOG_FILE


def _trim(target: Path) -> None:
    """Оставляет последние строки файла, если он вырос.

    Обрезаем с начала, а не с конца: в начале записи о том, что виджет
    только запустился, а в конце - о том, чем кончилось, и второе важнее.
    """
    try:
        if target.stat().st_size <= MAX_BYTES:
            return
        with target.open("rb") as handle:
            handle.seek(-MAX_BYTES // 2, os.SEEK_END)
            tail = handle.read()
        # Первая строка после обрезки, скорее всего, битая - обрезаем её.
        cut = tail.find(b"\n")
        tail = tail[cut + 1:] if cut >= 0 else tail
        target.write_bytes(tail)
    except OSError:
        pass


def write(message: str) -> None:
    """Дописывает строку в журнал. Никогда не бросает исключений."""
    global _broken
    if _broken:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = path()
    try:
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            _trim(target)
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("%s  %s\n" % (stamp, message))
    except (OSError, ValueError):
        # Диск полон, папка недоступна, антивирус держит файл. Виджет от
        # этого работать не перестаёт - перестаёт только журнал.
        _broken = True


def problem(message: str) -> None:
    """Запись о сбое с трассировкой, если исключение было настоящим."""
    exc = sys.exc_info()[1]
    if exc is None:
        write("ОШИБКА: " + message)
        return
    import traceback

    for line in traceback.format_exception(exc):
        write("ОШИБКА: " + line.rstrip())


def uptime_note(started: float) -> str:
    """«работает 3 мин» - для окна диагностики."""
    seconds = max(0, int(time.monotonic() - started))
    if seconds < 60:
        return "%d с" % seconds
    if seconds < 3600:
        return "%d мин %d с" % (seconds // 60, seconds % 60)
    return "%d ч %d мин" % (seconds // 3600, (seconds % 3600) // 60)