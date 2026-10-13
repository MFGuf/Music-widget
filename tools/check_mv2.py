#!/usr/bin/env python
"""Проверяет, что Firefox ставит наше расширение MV2.

Запуск:
    python tools\\check_mv2.py

Зачем это отдельным файлом
-------------------------
Остальные проверки (tools/run_tests.py) грузят скрипты расширения в обычную
страницу. Это показывает, что логика работает в движке браузера, но ничего
не говорит о том, примет ли Firefox само дополнение. А это отдельный риск:
Firefox объявлял, что будет выключать MV2, и когда это случается, весь
проект перестаёт работать, хотя все 215 проверок остаются зелёными.

Как проверяется
---------------
Firefox поднимает свой отладочный протокол (--start-debugger-server). У его
корневого актора есть addonsActor с методом installTemporaryAddon - ровно
то, что делает человек, нажимая «Load Temporary Add-on» в about:debugging.
Ставим дополнение этим способом и смотрим, что ответил Firefox.

Почему не «положить XPI в extensions/»
--------------------------------------
Пробовали и это: Firefox 158 молча удаляет и MV2, и MV3, положенные туда
при старте, и не вносит их в extensions.json. То есть такой способ больше
ничего не проверяет - отказ выглядит одинаково и при живом MV2, и при
мёртвом. Подтверждено контрольным MV3: он исчезает так же.

Профиль одноразовый: он удаляется вместе с проверкой, а профиль
пользователя не трогается и прав администратора не требует.
"""

from __future__ import annotations

import json
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
EXT = ROOT / "extension"
EXT_ID = "music-widget@local.dev"

FIREFOX_CANDIDATES = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]
DEBUGGER_PORT = 6000

PREFS = """
user_pref("devtools.debugger.remote-enabled", true);
user_pref("devtools.debugger.prompt-connection", false);
user_pref("devtools.debugger.force-local", true);
user_pref("devtools.chrome.enabled", true);
user_pref("devtools.console.stdout.content", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
"""


def find_firefox() -> str | None:
    import os

    override = os.environ.get("FIREFOX_PATH")
    if override and Path(override).exists():
        return override
    for path in FIREFOX_CANDIDATES:
        if Path(path).exists():
            return path
    return None


class Holding(BaseHTTPRequestHandler):
    """Пустая страница, чтобы Firefox не закрылся сам."""

    def do_GET(self):
        body = b"<!doctype html><title>mv2</title><p>holding"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Rdp:
    """Минимальный клиент отладочного протокола Firefox.

    Формат пакета простой: длина, двоеточие, JSON. Длина - в байтах, а не
    в символах, поэтому счётчик идёт по байтам.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = b""
        self.prefix = ""

    def send(self, message: dict) -> None:
        payload = json.dumps(message).encode("utf-8")
        self.sock.sendall(str(len(payload)).encode("ascii") + b":" + payload)

    def receive(self, timeout: float = 20.0) -> dict:
        self.sock.settimeout(timeout)
        while True:
            while b":" not in self.buffer:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OSError("отладочный сервер закрылся")
                self.buffer += chunk
            head, _, rest = self.buffer.partition(b":")
            length = int(head)
            while len(rest) < length:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OSError("отладочный сервер закрылся")
                rest += chunk
            self.buffer = rest[length:]
            message = json.loads(rest[:length].decode("utf-8"))
            if "error" in message:
                raise RuntimeError(message["error"])
            return message


def wait_for_port(port: int, proc: subprocess.Popen, seconds: float) -> socket.socket:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Firefox завершился, не подняв отладочный сервер")
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            time.sleep(1.0)
    raise RuntimeError("отладочный сервер не поднялся за %d с" % seconds)


def host_pids() -> set[int]:
    """Процессы нашего native messaging хоста.

    Хост - главный признак того, что фоновая страница расширения
    действительно запустилась: она вызывает connectNative, и только тогда
    Firefox поднимает процесс хоста. Отсутствие процесса при установленном
    дополнении означало бы, что фон не работает.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" | "
             "Where-Object { $_.CommandLine -like '*host.py*' }).ProcessId"],
            capture_output=True, text=True, timeout=90,
        ).stdout
    except Exception:
        return set()
    found = set()
    for line in out.split():
        if line.isdigit():
            found.add(int(line))
    return found


def kill(pids: set[int]) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check(firefox: str) -> tuple[bool, list[str]]:
    tmp = Path(tempfile.mkdtemp(prefix="mw-mv2-"))
    profile = tmp / "profile"
    profile.mkdir(parents=True)
    (profile / "user.js").write_text(PREFS, encoding="utf-8", newline="\n")

    # Хосты, работающие до нас, не в счёт: считаем только те, что
    # поднялись из-за установленного дополнения.
    hosts_before = host_pids()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Holding)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % server.server_address[1]

    proc = subprocess.Popen(
        [firefox, "-no-remote", "-profile", str(profile),
         "--start-debugger-server", str(DEBUGGER_PORT), "--headless", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    lines: list[str] = []
    try:
        sock = wait_for_port(DEBUGGER_PORT, proc, 60)
        rdp = Rdp(sock)

        # Подсказка отладчика приходит сама, первым пакетом. Ответ на неё
        # не нужен, поэтому отправляем свой запрос и читаем всё подряд.
        rdp.sock.recv(4096)
        rdp.send({"to": "root", "type": "getRoot"})
        root = rdp.receive()
        prefix = root.get("testConnectionPrefix", "")

        addons = root.get("addonsActor")
        if not addons:
            return False, ["у Firefox нет актора дополнений - проверка невозможна"]
        lines.append("Актор дополнений: " + addons)

        rdp.send({"to": addons, "type": "installTemporaryAddon",
                  "addonPath": str(EXT)})
        reply = rdp.receive()
        installed = reply.get("addon", reply)
        lines += [
            "Ответ Firefox: " + json.dumps(installed, ensure_ascii=False),
        ]

        addon_id = installed.get("id")
        if addon_id != EXT_ID:
            lines.append("ID не совпал: ожидался " + EXT_ID + ", получен " + str(addon_id))
            return False, lines

        # Самое важное: расширение должно быть активно. Установленное, но
        # выключенное дополнение для нас то же самое, что неустановленное.
        if installed.get("active") is False:
            lines.append("Расширение установлено, но не активно.")
            return False, lines

        lines.append("Расширение активно: да")

        # Доказываем, что фоновая страница MV2 не просто принята, а
        # работает: только она вызывает connectNative, а это поднимает хост.
        # Снимок процессов снят до запуска Firefox, поэтому «новый» хост
        # точно поднялся именно из-за установленного дополнения.
        deadline = time.time() + 40
        while time.time() < deadline:
            appeared = host_pids() - hosts_before
            if appeared:
                break
            time.sleep(2.0)
        kill(appeared)

        if appeared:
            lines.append("Хост поднялся после установки: да "
                         "(процесс %d)" % min(appeared))
        else:
            lines.append("Хост не поднялся: фоновая страница, похоже, не "
                         "запустилась.")
            return False, lines

        return True, lines
    except Exception as exc:
        lines.append("Ошибка проверки: " + repr(exc))
        return False, lines
    finally:
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


def main() -> int:
    firefox = find_firefox()
    if not firefox:
        print("Firefox не найден. Укажите путь переменной FIREFOX_PATH.")
        return 1

    version = subprocess.run(
        [firefox, "--version"], capture_output=True, text=True, timeout=60
    ).stdout.strip()

    ok, lines = check(firefox)

    report = ["Firefox: " + version, "Расширение: " + EXT_ID + " (MV2)", ""]
    report += lines
    report.append("")
    report.append("Вывод: MV2 принимается Firefox."
                  if ok else "Вывод: расширение не установилось.")

    text = "\n".join(report)
    target = ROOT / "tools" / ".mv2.txt"
    target.write_text(text, encoding="utf-8")
    print("отчёт: " + str(target))
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())