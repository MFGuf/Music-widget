#!/usr/bin/env python
"""Проверки native messaging хоста (native/host.py).

Хост - единственная часть проекта, которая разговаривает с Firefox, и
проверить её иначе нечем: настоящий браузер не запустить из теста. Поэтому
здесь Firefox подменён парами каналов у дочернего процесса, а виджет -
сокетом. Всё остальное - настоящее.

Что проверяется:
  * хост стартует и не умирает, даже когда виджета нет;
  * подключается, когда виджет появился, и обменивается токеном;
  * сообщения пересылаются в обе стороны без изменений;
  * после перезапуска виджета хост переподключается сам.

Запуск:
    python native\\host_test.py
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "widget"))
sys.path.insert(0, str(ROOT))

import ipc  # noqa: E402
from report import Checks  # noqa: E402

checks = Checks("host", "native messaging хост (native/host.py)")


def check(name, condition, detail=""):
    return checks.ok(name, condition, detail)


def check_equal(name, actual, expected):
    return checks.equal(name, actual, expected)


class SocketStream:
    """Файловый интерфейс поверх сокета.

    socket.makefile() внутри дублирует дескриптор, и закрытие самого сокета
    тогда НЕ разрывает соединение - а именно это и нужно, чтобы проверить
    переподключение хоста. Поэтому здесь честная обёртка над recv/sendall.
    """

    def __init__(self, sock):
        self.sock = sock

    def read(self, size):
        return self.sock.recv(size)

    def write(self, data):
        sent = 0
        while sent < len(data):
            sent += self.sock.send(data[sent:])
        return sent

    def flush(self):
        pass


class FakeWidget(threading.Thread):
    """Виджет с настоящим сервером: рукопожатие, приём и отправка."""

    def __init__(self, app_dir: Path, token="токен-виджета"):
        super().__init__(daemon=True)
        self.app_dir = app_dir
        self.token = token
        self.endpoint = app_dir / "MusicWidget" / "endpoint.json"
        self.server = socket.socket()
        self.server.bind((ipc.HOST, 0))
        self.server.listen(2)
        self.port = self.server.getsockname()[1]
        self.stop_flag = threading.Event()
        self.received: list[dict] = []
        self.connections = 0
        self._live: list[socket.socket] = []
        self.publish()

    def publish(self):
        self.endpoint.parent.mkdir(parents=True, exist_ok=True)
        self.endpoint.write_text(
            json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}),
            encoding="utf-8",
        )

    def unpublish(self):
        try:
            self.endpoint.unlink()
        except OSError:
            pass

    def run(self):
        while not self.stop_flag.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        # Принятые соединения держим списком: закрытие сокета-слушателя
        # их не обрывает, и хост так и не заметил бы, что виджет исчез.
        self._live.append(conn)
        stream = SocketStream(conn)
        try:
            auth = ipc.read_message(stream)
            self.received.append(auth)
            if not isinstance(auth, dict) or auth.get("token") != self.token:
                return
            ipc.write_message(stream, {"t": "auth-ok"})
            while True:
                message = ipc.read_message(stream)
                if message is None:
                    return
                self.received.append(message)
                # Отвечаем на команду командой state - так проверяется
                # путь обратно от виджета к Firefox.
                if message.get("t") == "cmd":
                    ipc.write_message(stream, {"t": "state", "items": [], "note": "ok"})
        except (OSError, ipc.ProtocolError):
            pass

    def shutdown(self):
        """Рвёт всё: и слушатель, и живые соединения."""
        self.stop_flag.set()
        for conn in list(self._live):
            try:
                conn.close()
            except OSError:
                pass
        self._live.clear()
        try:
            self.server.close()
        except OSError:
            pass
        self.unpublish()


def start_host(app_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(app_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, str(ROOT / "native" / "host.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(ROOT),
    )


def make_reader(stream) -> "queue.Queue":
    """Постоянный читатель канала хоста, раскладывающий кадры в очередь.

    Отдельный читатель на каждое ожидание - ловушка: поток, не получивший
    кадр за таймаут, остаётся висеть и забирает следующее сообщение. Тогда
    тест видел бы пустоту там, где сообщение есть.
    """
    inbox: "queue.Queue" = queue.Queue()

    def pump():
        try:
            while True:
                message = ipc.read_message(stream)
                if message is None:
                    break
                inbox.put(message)
        except (OSError, ipc.ProtocolError, ValueError):
            pass

    threading.Thread(target=pump, daemon=True).start()
    return inbox


def take(inbox: "queue.Queue", timeout: float = 5.0):
    try:
        return inbox.get(timeout=timeout)
    except queue.Empty:
        return None


def await_message(widget, kind: str, timeout: float = 5.0):
    """Ждёт сообщение нужного типа.

    Ищем по содержимому, а не по номеру в списке: хост может успеть
    подключиться повторно, и тогда индексы седут.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for message in list(widget.received):
            if isinstance(message, dict) and message.get("t") == kind:
                return message
        time.sleep(0.1)
    return None


def main() -> int:
    app_dir = Path(tempfile.mkdtemp(prefix="mw-host-"))

    # --- 1. Хост переживает отсутствие виджета --------------------------------
    host = start_host(app_dir)
    inbox = make_reader(host.stdout)
    try:
        time.sleep(1.5)
        check("хост не умирает без виджета", host.poll() is None,
              "код возврата " + str(host.poll()))
        check("хост пишет журнал", (app_dir / "MusicWidget" / "host.log").exists())

        # --- 2. Подключение и обмен -----------------------------------------
        widget = FakeWidget(app_dir)
        widget.start()
        try:
            # Ждём, пока хост заметит виджета (он пробует раз в секунду).
            deadline = time.time() + 25
            while time.time() < deadline and widget.connections == 0:
                time.sleep(0.2)
            check("хост подключился к виджету", widget.connections >= 1,
                  "соединений: " + str(widget.connections))

            # Пока виджет сам ничего не присылал, хост молчит - в этом и
            # состоит его роль: перекладывать, а не инициировать.
            quiet = take(inbox, timeout=1.0)
            check_equal("до ответа виджета хост в канал ничего не пишет", quiet, None)

            check_equal("хост передал рукопожатие виджету",
                        (widget.received[0] or {}).get("t") if widget.received else None,
                        "auth")

            # Firefox -> хост -> виджет.
            ipc.write_message(host.stdin, {"v": 1, "t": "state", "items": [{"key": "tab:1"}]})
            forwarded = await_message(widget, "state", timeout=5.0)
            check_equal("сообщение расширения дошло до виджета",
                        forwarded, {"v": 1, "t": "state", "items": [{"key": "tab:1"}]})

            # Виджет ответил на команду - хост обязан передать это в Firefox.
            ipc.write_message(host.stdin, {"v": 1, "t": "cmd", "id": 1, "action": "toggle"})
            reply = take(inbox, timeout=6.0)
            check_equal("ответ виджета дошёл до Firefox", reply,
                        {"t": "state", "items": [], "note": "ok"})
            check("хост пережил обмен в обе стороны", host.poll() is None)

            # --- 3. Переподключение после перезапуска виджета ---------------
            widget.shutdown()
            time.sleep(2.5)
            check("хост держится при упавшем видежете", host.poll() is None,
                  "код возврата " + str(host.poll()))

            revived = FakeWidget(app_dir)
            revived.start()
            try:
                deadline = time.time() + 25
                while time.time() < deadline and revived.connections == 0:
                    time.sleep(0.2)
                check("хост переподключился к новому виджету",
                      revived.connections >= 1, "соединений: " + str(revived.connections))

                ipc.write_message(host.stdin, {"v": 1, "t": "ping"})
                got = await_message(revived, "ping", timeout=5.0)
                check_equal("после переподключения обмен работает", got, {"v": 1, "t": "ping"})
            finally:
                revived.shutdown()
        finally:
            widget.shutdown()
    finally:
        host.terminate()
        try:
            host.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host.kill()

    # --- 4. В журнале нет жалоб на синтаксис/исключения ------------------------
    log_file = app_dir / "MusicWidget" / "host.log"
    if log_file.exists():
        text = log_file.read_text(encoding="utf-8")
        bad = [line for line in text.splitlines()
               if "Traceback" in line or "непредвиденный" in line]
        check("в журнале нет необработанных сбоев", not bad, "\n".join(bad))
    else:
        check("хост завёл журнал", False, "файла журнала нет")

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
