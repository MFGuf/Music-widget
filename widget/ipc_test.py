#!/usr/bin/env python
"""Проверки канала виджет <-> хост (widget/ipc.py).

Канал - единственное место, где виджет и браузер соприкасаются, поэтому
ошибка в нём выглядит как "виджет молчит" и искать её придётся вслепую.

Запуск:
    python widget\\ipc_test.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ipc  # noqa: E402

passed = 0
failed: list[str] = []


def check(name, condition, detail=""):
    global passed
    if condition:
        passed += 1
    else:
        failed.append(f"{name}{': ' + detail if detail else ''}")


def check_equal(name, actual, expected):
    check(name, actual == expected, f"получено {actual!r}, ожидалось {expected!r}")


# --- кадры ------------------------------------------------------------------


def test_frames():
    message = {"t": "state", "items": [], "note": "Подключено"}
    stream = BytesIO()
    ipc.write_message(stream, message)
    raw = stream.getvalue()

    expected_body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    check("размер кадра совпадает с телом",
          int.from_bytes(raw[:4], "little") == len(expected_body),
          f"заголовок {int.from_bytes(raw[:4], 'little')}, тело {len(expected_body)}")
    check("тело кадра верное", raw[4:] == expected_body)

    got = ipc.read_message(BytesIO(raw))
    check_equal("сообщение читается обратно", got, message)


def test_unicode():
    stream = BytesIO()
    ipc.write_message(stream, {"title": "Кино — Дельфин", "n": "𝄞"})
    got = ipc.read_message(BytesIO(stream.getvalue()))
    check_equal("кириллица и эмодзи переживают круг", got["title"], "Кино — Дельфин")
    check_equal("эмодзи не побился", got["n"], "𝄞")


def test_empty_stream():
    check_equal("пустой поток - это конец соединения", ipc.read_message(BytesIO(b"")), None)


def test_truncated():
    try:
        ipc.read_message(BytesIO(b"\x10\x00\x00\x00ab"))
        check("оборванный кадр замечен", False, "исключения не было")
    except ipc.ProtocolError:
        check("оборванный кадр замечен", True)


def test_bad_json():
    body = "{не json}".encode("utf-8")
    frame = len(body).to_bytes(4, "little") + body
    try:
        ipc.read_message(BytesIO(frame))
        check("битый JSON замечен", False, "исключения не было")
    except ipc.ProtocolError:
        check("битый JSON замечен", True)


def test_oversized():
    frame = (ipc.MAX_FRAME + 1).to_bytes(4, "little") + b"x"
    try:
        ipc.read_message(BytesIO(frame))
        check("слишком большой кадр отвергнут", False, "исключения не было")
    except ipc.ProtocolError:
        check("слишком большой кадр отвергнут", True)


# --- файл точки входа -------------------------------------------------------


def test_endpoint(tmp: Path):
    saved = ipc.ENDPOINT_FILE
    saved_dir = ipc.APP_DIR
    ipc.ENDPOINT_FILE = tmp / "endpoint.json"
    ipc.APP_DIR = tmp
    try:
        check_equal("точки входа ещё нет", ipc.read_endpoint(), None)

        ipc.write_endpoint(47123, "секрет")
        found = ipc.read_endpoint()
        check("точка входа записана", found is not None)
        check_equal("порт прочитан", found["port"], 47123)
        check_equal("токен прочитан", found["token"], "секрет")
        check_equal("pid прочитан", found["pid"], os.getpid())

        ipc.clear_endpoint()
        check_equal("точка входа убрана", ipc.read_endpoint(), None)

        ipc.ENDPOINT_FILE.write_text("{ битый", encoding="utf-8")
        check_equal("битый файл читается как отсутствие", ipc.read_endpoint(), None)

        ipc.ENDPOINT_FILE.write_text(
            json.dumps({"port": "не число", "token": "x"}), encoding="utf-8"
        )
        check_equal("файл без нормального порта игнорируется", ipc.read_endpoint(), None)

        ipc.ENDPOINT_FILE.write_text(
            json.dumps({"port": 100, "token": ""}), encoding="utf-8"
        )
        check_equal("пустой токен не принимается", ipc.read_endpoint(), None)
    finally:
        ipc.ENDPOINT_FILE = saved
        ipc.APP_DIR = saved_dir


def test_owner_alive():
    check("свой процесс считается живым",
          ipc.endpoint_owner_alive({"pid": os.getpid()}), "свой pid")
    # Большой pid заведомо не существует и не попадает в диапазон ядра.
    check("мёртвый процесс опознан",
          not ipc.endpoint_owner_alive({"pid": 0x7FFFFFF0}), "pid 0x7FFFFFF0")
    check("без pid считаем живым",
          ipc.endpoint_owner_alive({}), "пустой записью")
    check("мусорный pid не роняет проверку",
          isinstance(ipc.endpoint_owner_alive({"pid": "не число"}), bool), "строкой")


# --- живое соединение --------------------------------------------------------


def test_live_connection(tmp: Path):
    """Поднимает настоящий сервер виджета и подключается, как хост."""
    saved_file, saved_dir = ipc.ENDPOINT_FILE, ipc.APP_DIR
    ipc.ENDPOINT_FILE = tmp / "endpoint.json"
    ipc.APP_DIR = tmp

    server = socket.socket()
    server.bind((ipc.HOST, 0))
    server.listen(1)
    port = server.getsockname()[1]
    ipc.write_endpoint(port, "токен-виджета")

    received: list[dict] = []

    def serve():
        try:
            conn, _ = server.accept()
            with conn, conn.makefile("rwb") as stream:
                auth = ipc.read_message(stream)
                received.append(auth)
                ipc.write_message(stream, {"t": "auth-ok"})
                # Хост шлёт сообщение расширения, виджет отвечает командой.
                ipc.write_message(stream, {"t": "state", "items": [{"key": "tab:1"}]})
                reply = ipc.read_message(stream)
                received.append(reply)
                ipc.write_message(stream, {"t": "ack", "id": 1, "ok": True})
        except Exception as exc:  # noqa: BLE001 - сюда попадёт и обрыв
            received.append({"error": str(exc)})

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        # Одно соединение на оба конца: сервер обслуживает по очереди
        # клиентов, и лишняя проверка подключения заняла бы его собой.
        sock = ipc.connect_to_widget(timeout=2.0)
        if sock is None:
            check("подключение к виджету удалось", False, "виджет не ответил")
            return

        # Сокет не умеет read/write - работаем через файловый объект,
        # ровно так же, как это делает настоящий хост.
        with sock, sock.makefile("rwb") as stream:
            sock.settimeout(5.0)
            ipc.write_message(stream, {"t": "auth", "token": "токен-виджета"})
            auth_ok = ipc.read_message(stream)
            check_equal("виджет подтвердил токен", auth_ok, {"t": "auth-ok"})

            state = ipc.read_message(stream)
            check_equal("сообщение расширения дошло до видажета",
                        state, {"t": "state", "items": [{"key": "tab:1"}]})

            ipc.write_message(stream, {"t": "cmd", "id": 1, "action": "toggle"})
            ack = ipc.read_message(stream)
            check_equal("ответ на команду дошёл обратно",
                        ack, {"t": "ack", "id": 1, "ok": True})

        thread.join(timeout=5)
        check_equal("виджет получил обе посылки", len(received), 2)
        if len(received) == 2:
            check_equal("токен от хоста верен", received[0].get("token"), "токен-виджета")
            check_equal("команда от хоста в точности",
                        received[1], {"t": "cmd", "id": 1, "action": "toggle"})
    finally:
        server.close()
        ipc.clear_endpoint()
        ipc.ENDPOINT_FILE, ipc.APP_DIR = saved_file, saved_dir


def test_connect_without_widget(tmp: Path):
    saved_file, saved_dir = ipc.ENDPOINT_FILE, ipc.APP_DIR
    ipc.ENDPOINT_FILE = tmp / "endpoint2.json"
    ipc.APP_DIR = tmp

    # Порт, который гарантированно свободен: занимаем его, запоминаем
    # номер и сразу освобождаем.
    spare = socket.socket()
    spare.bind((ipc.HOST, 0))
    closed_port = spare.getsockname()[1]
    spare.close()

    try:
        check_equal("без точки входа подключение не выдумывается",
                    ipc.connect_to_widget(timeout=0.3), None)

        # Вижет упал, а файл точки входа остался: хост не должен ни долбиться
        # в мёртвый порт, ни ругаться исключением.
        ipc.write_endpoint(closed_port, "токен")
        data = json.loads(ipc.ENDPOINT_FILE.read_text(encoding="utf-8"))
        data["pid"] = 0x7FFFFFF0
        ipc.ENDPOINT_FILE.write_text(json.dumps(data), encoding="utf-8")

        check_equal("мёртвый владелец отсекается без попытки подключиться",
                    ipc.connect_to_widget(timeout=0.3), None)
        check_equal("файл мёртвого владельца убран", ipc.read_endpoint(), None)

        # Живой владелец, но порт свободен: connect обязан молча вернуть None.
        ipc.write_endpoint(closed_port, "токен")
        result = ipc.connect_to_widget(timeout=0.3)
        check_equal("свободный порт даёт None, а не исключение", result, None)
        if result is not None:
            result.close()
    finally:
        ipc.ENDPOINT_FILE, ipc.APP_DIR = saved_file, saved_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mw-ipc-"))

    test_frames()
    test_unicode()
    test_empty_stream()
    test_truncated()
    test_bad_json()
    test_oversized()
    test_endpoint(tmp)
    test_owner_alive()
    test_live_connection(tmp)
    test_connect_without_widget(tmp)

    print()
    if failed:
        print("ПРОВАЛЕНО: %d, успешно: %d" % (len(failed), passed))
        for item in failed:
            print("  - " + item)
        return 1
    print("Все проверки пройдены: %d" % passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
