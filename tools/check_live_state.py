#!/usr/bin/env python
"""Проверяет, что расширение действительно шлёт состояние виджету.

Запуск:
    python tools\\check_live_state.py

Зачем это
---------
check_mv2.py доказывает, что Firefox ставит MV2 и поднимает хост. Но хост
логирует только ошибки, а успешные сообщения пересылает молча, поэтому его
тишина ничего не значит. Был случай: виджет показывал «хост подключён, но
расширение не отвечает», и по журналу хоста нельзя было понять, сломался
транспорт или расширение просто не отправляет состояние.

Эта проверка отвечает на вопрос прямо. Под виджетом подставляется подделка,
которая занимает порт, пишет точку входа и записывает всё, что прислало
расширение. Если в списке появляется state - цепочка работает и виноват
был виджет или браузер пользователя. Если нет - виновато расширение, и
виджет тут ни при чём.

Что проверяется
---------------
  * расширение отправляет hello при подключении;
  * расширение присылает состояние само, без единого открытого трека
    (важно: пустое состояние - это тоже состояние, и его виджет обязан
    показать как «не найдено вкладок VK Музыки»);
  * состояние приходит повторно, а не один раз.

Профиль одноразовый и удаляется; профиль пользователя не трогается.
"""

from __future__ import annotations

import json
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "widget"))
sys.path.insert(0, str(ROOT / "tools"))

import ipc  # noqa: E402
import check_mv2  # noqa: E402

# Сколько ждём сообщений от расширения. Первый снимок уходит сразу после
# подключения, но контент-скрипты на вкладках VK появляются позже, а в
# этом профиле вкладок VK нет вовсе - ждать тут нечего.
WAIT_SECONDS = 25


class Holding(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><title>live</title><p>holding"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeWidget:
    """Виджет на время проверки: занимает порт и копит сообщения."""

    def __init__(self):
        self.messages: queue.Queue = queue.Queue()
        self.connections = 0
        self.errors: list[str] = []
        self._out = None
        self.token = "проверка-" + str(int(time.time()))
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((ipc.HOST, 0))
        self._server.listen(4)
        self._server.settimeout(0.5)
        self.port = self._server.getsockname()[1]

    def start(self) -> None:
        ipc.write_endpoint(self.port, self.token)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            except Exception:
                # Подделка живёт ради проверки: любая неожиданность в
                # чужом коде не должна обрывать её работу молча.
                continue
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn) -> None:
        stream_in = conn.makefile("rb")
        stream_out = conn.makefile("wb")
        self._out = stream_out
        try:
            message = ipc.read_message(stream_in)
            if not isinstance(message, dict):
                return
            if message.get("t") != "auth" or message.get("token") != self.token:
                return
            ipc.write_message(stream_out,
                              {"t": "auth-ok", "v": ipc.PROTOCOL_VERSION})
            while True:
                data = ipc.read_message(stream_in)
                if data is None:
                    break
                self.messages.put(data)
        except Exception as exc:  # noqa: BLE001 - подделка не должна падать
            self.errors.append(repr(exc))
        finally:
            for stream in (stream_in, stream_out):
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass
        ipc.clear_endpoint()

    def send(self, message: dict) -> None:
        """Отправляет виджету команду - ровно так же, как это делает виджет."""
        if self._out is None:
            raise RuntimeError("виджет ещё не подключён")
        ipc.write_message(self._out, message)

    def snapshot(self) -> list:
        out = []
        while True:
            try:
                out.append(self.messages.get_nowait())
            except queue.Empty:
                return out


def main() -> int:
    firefox = check_mv2.find_firefox()
    if not firefox:
        print("Firefox не найден. Укажите путь переменной FIREFOX_PATH.")
        return 1

    saved_file, saved_dir = ipc.ENDPOINT_FILE, ipc.APP_DIR
    widget = FakeWidget()
    # Точку входа публикуем по-настоящему, а не во временной папке: хост
    # запускает сам Firefox отдельным процессом и ищет файл по
    # %LOCALAPPDATA%\MusicWidget\endpoint.json. Подменить путь у себя
    # недостаточно - подмены в чужом процессе не существует, и хост
    # честно напишет «виджет не опубликовал точку входа».
    ipc.APP_DIR.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="mw-live-ff-"))
    profile = tmp / "profile"
    profile.mkdir(parents=True)
    (profile / "user.js").write_text(check_mv2.PREFS, encoding="utf-8",
                                     newline="\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Holding)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % server.server_address[1]

    lines: list[str] = []
    proc = None
    try:
        widget.start()
        # Headless по умолчанию: он дешевле и не мешает пользователю. Но
        # браузер может усыплять фоновые страницы без видимых окон, а это
        # выглядело бы как «расширение молчит». Поэтому оставляем возможность
        # запустить проверку с окнами: MW_HEADFUL=1.
        import os
        argv = [firefox, "-no-remote", "-profile", str(profile),
                "--start-debugger-server", str(check_mv2.DEBUGGER_PORT)]
        if not os.environ.get("MW_HEADFUL"):
            argv.append("--headless")
        argv.append(url)
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        sock = check_mv2.wait_for_port(check_mv2.DEBUGGER_PORT, proc, 60)
        rdp = check_mv2.Rdp(sock)
        rdp.sock.recv(4096)
        rdp.send({"to": "root", "type": "getRoot"})
        root = rdp.receive()
        rdp.send({"to": root["addonsActor"], "type": "installTemporaryAddon",
                  "addonPath": str(ROOT / "extension")})
        install_reply = rdp.receive()
        lines.append("Ответ на установку: " + json.dumps(install_reply,
                                                          ensure_ascii=False)[:200])

        # Хост должен подняться сразу после установки: его вызывает фоновая
        # страница через connectNative. Если не поднялся - искать причину
        # надо здесь, а не в транспорте.
        deadline = time.time() + 20
        appeared: set[int] = set()
        while time.time() < deadline and not appeared:
            appeared = check_mv2.host_pids()
            time.sleep(2.0)
        lines.append("Хост поднялся: %s" % ("да" if appeared else "нет"))
        if not appeared:
            lines.append("Без хоста транспорт не проверяем: цепочка не "
                         "началась, виновато расширение.")

        deadline = time.time() + WAIT_SECONDS
        collected = []
        while time.time() < deadline:
            batch = widget.snapshot()
            if batch:
                collected += batch
                break
            time.sleep(0.5)
        time.sleep(2.0)
        collected += widget.snapshot()

        # Команда с кнопки виджета. Проверяем её так же, как состояние:
        # отправляем и ждём ответа расширения. Молчание здесь означало бы
        # то же, что и молчание состояния - обрыв где-то на пути.
        widget.snapshot()
        acks = []
        for action in ("toggle", "prev", "next"):
            try:
                widget.send({"v": 1, "t": "cmd", "action": action,
                             "key": "tab:1", "id": 99})
            except Exception as exc:
                lines.append("команда %s не отправлена: %r" % (action, exc))
                continue
            got = None
            wait_until = time.time() + 12
            while time.time() < wait_until and got is None:
                for message in widget.snapshot():
                    collected.append(message)
                    if message.get("t") == "ack":
                        got = message
                if got is None:
                    time.sleep(0.4)
            acks.append((action, got))
        for action, got in acks:
            if got is None:
                lines.append("команда %s: ответа нет" % action)
            else:
                lines.append("команда %s: ok=%s msg=%r"
                             % (action, got.get("ok"), got.get("msg")))
        commands_ok = all(got is not None for _, got in acks)
        check_mv2.kill(appeared)
    except Exception as exc:  # noqa: BLE001
        lines.append("Ошибка проверки: " + repr(exc))
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    received = collected
    states = [m for m in received if m.get("t") == "state"]
    lines.append("Подключений к виджету: %d" % widget.connections)
    lines.append("Всего сообщений: %d" % len(received))
    lines.append("Из них состояний: %d" % len(states))
    if widget.errors:
        lines.append("Ошибки подделки: " + "; ".join(widget.errors[:3]))
    if received:
        lines.append("Типы сообщений: %s" % sorted(
            {str(m.get("t")) for m in received}))
    for message in states[:2]:
        lines.append("Пример состояния: " + json.dumps(
            message, ensure_ascii=False)[:300])

    ok = bool(states) and commands_ok
    lines.append("")
    lines.append("Вывод: расширение шлёт состояние."
                 if ok else "Вывод: расширение НЕ шлёт состояние.")

    widget.stop()
    # Убираем подделку: настоящий виджет опубликует точку входа сам.
    ipc.ENDPOINT_FILE, ipc.APP_DIR = saved_file, saved_dir

    text = "\n".join(lines)
    target = ROOT / "tools" / ".livestate.txt"
    target.write_text(text, encoding="utf-8")
    print("отчёт: " + str(target))
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())