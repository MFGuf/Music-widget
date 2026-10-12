#!/usr/bin/env python
"""Проверки виджетной стороны канала (widget/bridge_worker.py).

Запуск:
    python widget\\bridge_test.py
"""

from __future__ import annotations

import queue
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import bridge_worker  # noqa: E402
import ipc  # noqa: E402
from report import Checks, wait_for  # noqa: E402

checks = Checks("bridge", "виджетная сторона канала (bridge_worker)")


def ok(name, condition, detail=""):
    return checks.ok(name, condition, detail)


def equal(name, actual, expected):
    return checks.equal(name, actual, expected)


class SocketStream:
    """Файловый интерфейс поверх сокета.

    socket.makefile() дублирует дескриптор, поэтому закрытие самого сокета
    НЕ разрывает соединение - а это нужно, чтобы проверить обрыв и
    переподключение. Здесь честная обёртка над recv/sendall.
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


class FakeHost:
    """Хост в минимуме: подключается, рукопожатится, читает и пишет."""

    def __init__(self, token=None):
        self.token = token
        self.sock = None
        self.stream = None
        self.received: list[dict] = []
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def connect(self, timeout: float = 8.0) -> bool:
        endpoint = ipc.read_endpoint()
        if endpoint is None:
            return False
        if self.token is None:
            self.token = endpoint["token"]
        try:
            self.sock = socket.create_connection((ipc.HOST, endpoint["port"]), timeout=timeout)
            self.stream = SocketStream(self.sock)
        except OSError:
            return False
        threading.Thread(target=self._pump, daemon=True).start()
        try:
            ipc.write_message(self.stream, {"t": "auth", "token": self.token})
        except OSError:
            return False
        return True

    def _pump(self):
        try:
            while True:
                message = ipc.read_message(self.stream)
                if message is None:
                    break
                with self.lock:
                    self.received.append(message)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.closed.set()

    def send(self, message):
        ipc.write_message(self.stream, message)

    def try_send(self, message):
        """Отправка, которая не роняет набор.

        Виджет вправе закрыть соединение в любой момент - тогда мы хотим
        увидеть это как проваленную проверку с его состоянием, а не как
        трассировку, из-за которой отчёт не пишется вовсе.
        """
        try:
            self.send(message)
            return True
        except OSError as exc:
            self.send_error = repr(exc)
            return False

    def seen(self, kind):
        with self.lock:
            return [m for m in self.received
                    if isinstance(m, dict) and m.get("t") == kind]

    def close(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mw-bridge-"))
    saved_file, saved_dir = ipc.ENDPOINT_FILE, ipc.APP_DIR
    ipc.ENDPOINT_FILE = tmp / "endpoint.json"
    ipc.APP_DIR = tmp

    cmds: queue.Queue = queue.Queue()
    states: queue.Queue = queue.Queue()
    worker = bridge_worker.BridgeWorker(cmds, states)

    try:
        # --- 1. Виджет ждёт браузера -----------------------------------------
        worker.start()
        ok("точка входа опубликована",
           wait_for(lambda: ipc.read_endpoint() is not None), "файл не появился")
        ok("виджет ждёт браузер",
           any(word in worker.note for word in ("Ждём", "Ждёт", "ждёт", "Ждем")),
           worker.note)
        ok("виджет ещё не подключён", worker.connected is False)

        # --- 2. Чужой токен не проходит ---------------------------------------
        stranger = FakeHost(token="чужой-токен")
        ok("чужой процесс подключился к порту", stranger.connect(),
           "точка входа=" + str(ipc.read_endpoint()) + " note=" + worker.note)
        ok("виджет рвёт соединение с чужим токеном",
           wait_for(lambda: stranger.closed.is_set(), timeout=5.0),
           "соединение осталось открытым")
        ok("чужой токен не считается подключением", worker.connected is False,
           worker.note)

        # --- 3. Настоящий хост -------------------------------------------------
        host = FakeHost()
        ok("настоящий хост подключился", host.connect())
        ok("виджет увидел браузер", wait_for(lambda: worker.connected), worker.note)
        ok("виджет поздоровался сам", wait_for(lambda: bool(host.seen("hello"))),
           "hello не пришёл")

        ok("сообщение расширения ушло виджету",
           host.try_send({"v": 1, "t": "state", "items": [{"key": "tab:1", "title": "Кино"}],
                          "selected": "tab:1", "note": "Подключено"}),
           "note=" + worker.note + " ошибка=" + getattr(host, "send_error", ""))
        ok("состояние дошло до очереди", wait_for(lambda: not states.empty()))
        if not states.empty():
            state = states.get_nowait()
            equal("снимок содержит источники", len(state["items"]), 1)
            equal("снимок содержит выбранное", state["selected"], "tab:1")
            equal("снимок содержит диагностику", state["note"], "Подключено")

        ok("второй снимок отправлен",
           host.try_send({"v": 1, "t": "state", "items": [{"key": "tab:2"}],
                          "selected": "tab:2", "note": "Ещё раз"}),
           "note=" + worker.note + " ошибка=" + getattr(host, "send_error", ""))
        states.queue.clear()

        # --- 4. Команды из интерфейса ------------------------------------------
        worker.send("toggle", key="tab:1")
        ok("команда долетела до хоста", wait_for(lambda: bool(host.seen("cmd"))),
           "команда не пришла")
        commands = host.seen("cmd")
        if commands:
            equal("действие команды верно", commands[0].get("action"), "toggle")
            equal("ключ команды верен", commands[0].get("key"), "tab:1")
            ok("у команды есть версия протокола", commands[0].get("v") == 1)

        worker.send("seek", key="tab:1", ratio=0.5)
        ok("перемотка ушла",
           wait_for(lambda: any(c.get("action") == "seek" for c in host.seen("cmd"))))
        seeks = [c for c in host.seen("cmd") if c.get("action") == "seek"]
        if seeks:
            equal("доля перемотки верна", seeks[0].get("ratio"), 0.5)

        # --- 5. Обрыв и переподключение -----------------------------------------
        host.close()
        ok("обрыв соединения замечен", wait_for(lambda: not worker.connected),
           "виджет всё ещё считает себя подключённым")
        ok("точка входа на месте после обрыва", ipc.read_endpoint() is not None)

        host2 = FakeHost()
        ok("виджет принимает нового хоста", wait_for(lambda: host2.connect(timeout=1.0)),
           "повторное подключение не вышло")
        ok("виджет снова подключён", wait_for(lambda: worker.connected), worker.note)
        ok("после переподключения снова шлёт hello",
           wait_for(lambda: bool(host2.seen("hello"))),
           "note=" + worker.note + " получено=" + str(host2.received[:2]))

        ok("снимок после переподключения отправлен",
           host2.try_send({"v": 1, "t": "state", "items": [], "selected": None, "note": "ok"}),
           "note=" + worker.note)
        ok("после переподключения состояние снова идёт", wait_for(lambda: not states.empty()))
        states.queue.clear()

        # --- 6. Команда без связи не роняет виджет -------------------------------
        host2.close()
        wait_for(lambda: not worker.connected)
        worker.send("next", key="tab:1")
        time.sleep(0.4)
        ok("виджет пережил команду без связи", worker.is_alive())

        # --- 7. Преобразование команды ------------------------------------------
        equal("команда из кортежа превращается в сообщение",
              bridge_worker.BridgeWorker._as_command(("toggle", "tab:1", None)),
              {"t": "cmd", "v": 1, "action": "toggle", "key": "tab:1"})
        equal("готовое сообщение проходит без изменений",
              bridge_worker.BridgeWorker._as_command({"t": "cmd", "action": "prev"}),
              {"t": "cmd", "action": "prev"})
    finally:
        worker.stop()
        time.sleep(0.4)
        ok("точка входа убрана при остановке", ipc.read_endpoint() is None, "файл остался")
        ok("виджет остановился", worker._stopped.is_set())
        ipc.ENDPOINT_FILE, ipc.APP_DIR = saved_file, saved_dir

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())