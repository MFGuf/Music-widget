"""Виджетная сторона канала: сервер, который ждёт хоста.

Хост подключается к нам, а не наоборот: Firefox запускает хост сам, и тот
может появиться в любой момент - хоть через минуту после старта виджета.
Поэтому виджет слушает порт постоянно и обслуживает хоста, пока тот жив.

Виджет отдаёт два потока наружу:
  * states - снимки от расширения, готовые к отрисовке;
  * cmds   - команды, которые виджет хочет отправить расширению.

Формат тот же, что у расширения: {"v": 1, "t": "state", "items": [...],
"selected": "...", "note": "..."}. Ничего не переименовывается и не
додумывается: этот словарь рисуется виджетом как есть.
"""

from __future__ import annotations

import queue
import secrets
import socket
import threading
import time
from typing import Any

import ipc

# Виджет шлёт это сразу при подключении. Расширение по нему понимает, что
# перед ним живой виджет, а не просто занятый порт.
HELLO = {"t": "hello", "role": "widget", "v": ipc.PROTOCOL_VERSION}


class BridgeWorker(threading.Thread):
    """Слушает порт и перекладывает сообщения между хостом и виджетом."""

    # Метка в очереди команд, означающая "виджет закрывается". Объект вместо
    # строки - чтобы случайно отправить команду с таким значением нельзя.
    STOP = object()

    def __init__(self, cmds: queue.Queue, states: queue.Queue):
        super().__init__(daemon=True, name="bridge")
        self.cmds = cmds
        self.states = states

        self._server: socket.socket | None = None
        self._token = secrets.token_urlsafe(24)
        self._conn: socket.socket | None = None
        self._conn_in: Any = None
        self._conn_out: Any = None
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._connected = False
        self._note = "Запуск..."

    # --- состояние ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def note(self) -> str:
        return self._note

    def _set_note(self, text: str) -> None:
        self._note = text

    # --- приём соединения ----------------------------------------------------

    def _open_server(self) -> socket.socket:
        """Поднимает сервер.

        Сначала пробуем удобный постоянный порт, но он может быть занят -
        например, если виджет уже запущен. Тогда берём любой свободный.

        SO_REUSEADDR здесь ставить нельзя: на Windows эта опция разрешает
        двум сокетам занять ОДИН И ТОТ ЖЕ порт, в отличие от Unix, где она
        касается только TIME_WAIT. Второй виджет тогда объявил бы в файле
        точки входа чужой порт, соединения уходили бы к первому виджету, а
        токены не совпали бы: связь молча не работала бы. Поэтому при
        занятом порте bind обязан провалиться, и мы уходим на случайный.
        """
        for port in (ipc.DEFAULT_PORT, 0):
            try:
                server = socket.socket()
                server.bind((ipc.HOST, port))
                server.listen(4)
                server.settimeout(0.5)
                return server
            except OSError:
                try:
                    server.close()
                except OSError:
                    pass
        raise RuntimeError("не удалось занять порт для связи с хостом")

    def _accept(self) -> None:
        assert self._server is not None
        while not self._stopped.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._serve(conn)

    def _authenticate(self, stream_in, stream_out) -> bool:
        try:
            message = ipc.read_message(stream_in)
        except (OSError, ipc.ProtocolError):
            return False
        token = message.get("token") if isinstance(message, dict) else None
        # Токен живёт в файле точки входа, доступном только текущему
        # пользователю, поэтому чужой процесс его не знает.
        if token != self._token:
            self._set_note("Кто-то ещё занял порт виджета")
            return False
        try:
            ipc.write_message(stream_out, {"t": "auth-ok", "v": ipc.PROTOCOL_VERSION})
        except (OSError, ipc.ProtocolError):
            return False
        return True

    def _serve(self, conn: socket.socket) -> None:
        stream_out = conn.makefile("wb")
        stream_in = conn.makefile("rb")
        try:
            if not self._authenticate(stream_in, stream_out):
                conn.close()
                return

            with self._lock:
                self._conn = conn
                self._conn_in = stream_in
                self._conn_out = stream_out
                self._connected = True
            self._set_note("Подключено")
            self._send(HELLO)

            # Событие ставит поток чтения, когда соединение рвётся. Без него
            # ждал бы мёртвый сокет вечно: наличие self._conn ещё ничего не
            # говорит о живости связи.
            closed = threading.Event()
            threading.Thread(
                target=self._pump_from_host, args=(stream_in, closed), daemon=True
            ).start()

            while not self._stopped.is_set() and not closed.wait(0.2):
                pass
        finally:
            self._close_connection()
            try:
                conn.close()
            except OSError:
                pass

    def _close_connection(self) -> None:
        with self._lock:
            conn = self._conn
            self._conn = None
            self._conn_in = None
            self._conn_out = None
            self._connected = False
        if conn is not None:
            try:
                conn.shutdown(2)
            except OSError:
                pass
        self._set_note("Виджет ждёт браузер")

    # --- обмен ---------------------------------------------------------------

    def _pump_from_host(self, stream, closed: threading.Event) -> None:
        """Сообщения от расширения - в очередь отрисовки."""
        try:
            while True:
                message = ipc.read_message(stream)
                if message is None:
                    break
                self._handle(message)
        except (OSError, ipc.ProtocolError):
            pass
        finally:
            closed.set()

    def _handle(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("t")
        if kind == "state":
            self.states.put({
                "items": message.get("items") or [],
                "selected": message.get("selected"),
                "note": message.get("note") or "",
            })
        elif kind == "hello":
            self._set_note("Подключено")
        elif kind == "ack":
            # Ответ на команду: визуально показывать нечего, но в журнале
            # виджета пригодится.
            pass

    def _pump_commands(self) -> None:
        """Команды от интерфейса виджета - в хост."""
        while not self._stopped.is_set():
            try:
                item = self.cmds.get(timeout=0.3)
            except queue.Empty:
                continue
            if item is None or item is self.STOP:
                return
            try:
                self._send(self._as_command(item))
            except (OSError, ipc.ProtocolError):
                # Виджет ещё не подключён или связь оборвалась - команда
                # пропадёт, но интерфейс не должен от неё падать.
                pass

    @staticmethod
    def _as_command(item: Any) -> dict:
        """Превращает элемент очереди в сообщение для расширения.

        В очередь кладутся кортежи (действие, ключ, доля) - так же, как это
        делал прежний виджет, и это же разбирает background/page.js.
        """
        if isinstance(item, dict):
            return item
        action, key, ratio = (list(item) + [None, None])[:3]
        command = {"t": "cmd", "v": ipc.PROTOCOL_VERSION, "action": action}
        if key is not None:
            command["key"] = key
        if ratio is not None:
            command["ratio"] = ratio
        return command

    def _send(self, message: dict) -> None:
        with self._lock:
            stream = self._conn_out
        if stream is None:
            return
        with self._write_lock:
            ipc.write_message(stream, message)

    # --- команда от интерфейса ------------------------------------------------

    def send(self, action: str, key: str | None = None, ratio: float | None = None) -> None:
        """Кладёт команду в очередь. Вызывается из потока интерфейса."""
        self.cmds.put((action, key, ratio))

    # --- жизненный цикл --------------------------------------------------------

    def run(self) -> None:
        try:
            self._server = self._open_server()
        except RuntimeError as exc:
            self._set_note(str(exc))
            # Виджет без связи бессмыслен, но закрывать окно из-за этого
            # нельзя: пусть пользователь увидит объяснение.
            while not self._stopped.is_set():
                time.sleep(0.3)
            return

        port = self._server.getsockname()[1]
        ipc.write_endpoint(port, self._token)
        self._set_note("Ждём браузер")

        # Накачивание команд виджета - один поток на весь процесс, а не на
        # каждое соединение: иначе при каждом переподключении добавлялся бы
        # ещё один, и они дрались бы за очередь.
        threading.Thread(target=self._pump_commands, daemon=True).start()

        try:
            self._accept()
        finally:
            ipc.clear_endpoint()

    def stop(self) -> None:
        self._stopped.set()
        self.cmds.put(self.STOP)
        self._close_connection()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        ipc.clear_endpoint()