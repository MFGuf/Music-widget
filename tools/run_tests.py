#!/usr/bin/env python
"""Прогоняет тесты расширения в настоящем Firefox (headless).

Зачем это нужно: node.js на машине нет, поэтому синтаксис и поведение
JavaScript нечем проверять. Firefox умеет рендерить страницу в PNG, а
значит мы можем запускать настоящий движок, выполнять в нём код
расширения и получать текстовый результат обратно в Python.

Схема:
  1. поднимаем HTTP-сервер на 127.0.0.1 - файлы отдаются с того же
     origin, что и страница тестов, поэтому никаких CORS-проблем;
  2. запускаем Firefox в headless с отдельным профилем (чтобы не
     трогать уже открытый браузер пользователя);
  3. страница tools/tests/index.html прогоняет наборы и отправляет
     результат POST-ом на /results;
  4. печатаем результат и возвращаем код возврата 0/1.

Запуск:
    python tools\\run_tests.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIREFOX_CANDIDATES = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]
TIMEOUT_SECONDS = 90


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_firefox():
    override = os.environ.get("FIREFOX_PATH")
    if override:
        return override
    for path in FIREFOX_CANDIDATES:
        if Path(path).exists():
            return path
    return None


class Results:
    def __init__(self):
        self.payload = None
        self.done = threading.Event()


class Handler(SimpleHTTPRequestHandler):
    results = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/results":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            self.results.payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self.results.payload = {"error": f"не разобрал ответ: {e}"}
        self.results.done.set()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()


def run_suite(firefox, port):
    results = Results()
    Handler.results = results

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    profile = ROOT / "tools" / ".testprofile"
    profile.mkdir(parents=True, exist_ok=True)

    url = f"http://127.0.0.1:{port}/tools/tests/index.html"
    proc = subprocess.Popen(
        [
            firefox, "-no-remote", "-profile", str(profile),
            "--headless", url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not results.done.wait(TIMEOUT_SECONDS):
            return None, "тесты не ответили за %d с" % TIMEOUT_SECONDS
        return results.payload, None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        server.shutdown()


def report(payload):
    if not isinstance(payload, dict):
        print("неожиданный ответ:", payload)
        return 2

    total = 0
    failed = 0
    for suite in payload.get("suites", []):
        print()
        print("==", suite.get("name", "?"))
        if suite.get("error"):
            print("   ОШИБКА НАБОРА:", suite["error"])
            failed += 1
            continue
        for check in suite.get("checks", []):
            total += 1
            mark = "ok  " if check.get("ok") else "FAIL"
            if not check.get("ok"):
                failed += 1
            print("  [%s] %s%s" % (
                mark, check.get("name", "?"),
                "" if check.get("ok") else "  <- " + str(check.get("detail", "")),
            ))
    print()
    print("-" * 60)
    print("проверок: %d, провалено: %d" % (total, failed))
    return 1 if failed else 0


def main():
    firefox = find_firefox()
    if not firefox:
        print("Firefox не найден. Задайте переменную окружения FIREFOX_PATH.")
        return 2

    payload, error = run_suite(firefox, free_port())
    if error:
        print(error)
        return 2
    return report(payload)


if __name__ == "__main__":
    sys.exit(main())