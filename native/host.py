"""Native messaging хост: мост между Firefox и настольным виджетом.

Кто кого запускает
------------------
Firefox запускает этот скрипт сам (через native/host_launcher.bat) и говорит
с ним по каналам 0 и 1, используя тот же формат кадра, что и канал виджета:
4 байта длины + JSON. Вижет запускает пользователь отдельно, и вот с ним у
нас нет общего процесса - поэтому хост подключается к виджету по TCP на
127.0.0.1 (см. widget/ipc.py) и пересылает сообщения как есть.

Хост ничего не понимает в содержимом сообщений. Его работа - держать
соединение и перекладывать байты, чтобы правила протокола жилли в одном
месте (в расширении).

Жизненный цикл
--------------
Firefox держит каналы открытыми, пока работает дополнение, поэтому хост не
должен завершаться, даже если виджет не запущен: иначе Firefox решит, что
расширение отвалилось. Вместо этого хост терпеливо подключается к виджету
и переподключается, как только тот появится.

Почему один читатель Firefox на весь процесс
-------------------------------------------
Канал Firefox - это труба, и её нельзя прервать изнутри. Если читать её в
потоке на каждое соединение, то при обрыве соединения поток останется висеть
в чтении навсегда и продолжит забирать сообщения - уже для нового соединения.
Наблюдалось ровно это: команды от виджета доходили до старого сокета и
терялись. Поэтому чтение браузера одно на весь процесс, а на каждое
соединение с виджетом создаётся только второй поток.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "widget"))

import ipc  # noqa: E402

LOG_FILE = ipc.APP_DIR / "host.log"
LOG_LIMIT = 1 * 1024 * 1024

# Пауза между попытками подключиться к виджету. Виджет может появиться через
# минуту после запуска браузера, поэтому ждать приходится спокойно.
RETRY_DELAY_SEC = 1.0

_log_lock = threading.Lock()


def log(message: str) -> None:
    """Пишет в файл. В stdout писать нельзя - это канал протокола."""
    line = f"{time.strftime('%H:%M:%S')} {message}\n"
    with _log_lock:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_LIMIT:
                # Затираем целиком: ротация с переименованием тут не нужна.
                LOG_FILE.unlink()
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


def take_stdio():
    """Отдаёт бинарные каналы Firefox.

    Под pythonw объект sys.stdout равен None, поэтому работаем с
    дескрипторами напрямую. Дублируем их, чтобы не закрыть те, что
    принадлежат браузеру.
    """
    return os.fdopen(os.dup(0), "rb", buffering=0), os.fdopen(os.dup(1), "wb")


def guard_stdout() -> None:
    """Уводит любую печать из stdout.

    Хост иногда запускают через python.exe (если pythonw не нашёлся), и тогда
    print() ушёл бы прямо в кадр протокола и сорвал бы разбор сообщений.
    Подмена sys.stdout на stderr делает такую ошибку безобидной.
    """
    if sys.stdout is not None:
        sys.stdout = sys.stderr


class Bridge:
    """Одно соединение с виджетом на время его жизни."""

    def __init__(self, widget_in, widget_out, firefox_out):
        self._widget_in = widget_in
        self._widget_out = widget_out
        self._firefox_out = firefox_out
        self._write_lock = threading.Lock()
        self.done = threading.Event()

    def _send(self, stream, message):
        with self._write_lock:
            ipc.write_message(stream, message)

    def to_widget(self, message):
        self._send(self._widget_out, message)

    def to_firefox(self, message):
        self._send(self._firefox_out, message)

    def pump_from_widget(self):
        """Читает сокет виджета и передаёт всё Firefox."""
        try:
            while True:
                message = ipc.read_message(self._widget_in)
                if message is None:
                    log("виджет закрыл соединение")
                    break
                self.to_firefox(message)
        except (OSError, ipc.ProtocolError) as exc:
            log(f"обрыв соединения с виджетом: {exc}")
        finally:
            self.done.set()


def firefox_pump(firefox_in, firefox_out, current) -> None:
    """Единственный читатель канала Firefox на весь процесс.

    current - функция, отдающая активное соединение с виджетом. Сообщения,
    пришедшие без соединения, уходят в никуда: расширение пришлёт состояние
    снова, а очередь копить незачем.
    """
    while True:
        try:
            message = ipc.read_message(firefox_in)
        except (OSError, ipc.ProtocolError) as exc:
            log(f"канал Firefox испорчен: {exc}")
            return
        if message is None:
            log("Firefox закрыл канал, хост завершается")
            return
        bridge = current()
        if bridge is None:
            continue
        try:
            bridge.to_widget(message)
        except (OSError, ipc.ProtocolError) as exc:
            log(f"не удалось передать сообщение виджету: {exc}")


def authenticate(widget_in, widget_out) -> bool:
    """Хост представляется виджету. False - виджет нас не признал."""
    endpoint = ipc.read_endpoint()
    if not endpoint:
        log("виджет не опубликовал точку входа")
        return False
    try:
        ipc.write_message(widget_out, {"t": "auth", "token": endpoint["token"]})
        reply = ipc.read_message(widget_in)
    except (OSError, ipc.ProtocolError) as exc:
        log(f"рукопожатие с виджетом не вышло: {exc}")
        return False
    return isinstance(reply, dict) and reply.get("t") == "auth-ok"


def main() -> int:
    guard_stdout()
    log("хост запущен")
    firefox_in, firefox_out = take_stdio()

    active: list[Bridge | None] = [None]

    def current() -> Bridge | None:
        return active[0]

    threading.Thread(
        target=firefox_pump, args=(firefox_in, firefox_out, current), daemon=True
    ).start()

    while True:
        sock = ipc.connect_to_widget(timeout=0.5)
        if sock is None:
            # Виджет не запущен. Спим и пробуем снова: он может появиться
            # в любой момент, а завершаться нам нельзя - Firefox решит,
            # что дополнение сломалось.
            time.sleep(RETRY_DELAY_SEC)
            continue

        # Файловые объекты создаются один раз на соединение: два разных
        # makefile на одном сокете рассинхронизировали бы буферы чтения.
        widget_in = widget_out = None
        try:
            widget_out = sock.makefile("wb")
            widget_in = sock.makefile("rb")

            if not authenticate(widget_in, widget_out):
                continue

            log("соединение с виджетом установлено")
            bridge = Bridge(widget_in, widget_out, firefox_out)
            active[0] = bridge
            threading.Thread(target=bridge.pump_from_widget, daemon=True).start()

            bridge.done.wait()
        except Exception as exc:  # noqa: BLE001 - хост обязан не падать
            log(f"непредвиденный сбой соединения: {exc!r}")
        finally:
            active[0] = None
            try:
                sock.shutdown(2)
            except OSError:
                pass
            for stream in (widget_in, widget_out):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
            time.sleep(RETRY_DELAY_SEC)


if __name__ == "__main__":
    sys.exit(main())