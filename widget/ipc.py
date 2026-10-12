"""Канал между настольным виджетом и native messaging хостом.

Зачем он нужен
--------------
Хост запускает сам Firefox и говорит с ним через свои stdin/stdout. Вижет
запускает пользователь, отдельно от браузера. Соединить их напрямую нельзя,
поэтому между ними встаёт этот канал: хост подключается к виджету по TCP на
127.0.0.1 и пересылает сообщения как есть, ничего в них не разбирая.

Формат кадра
-------------
Длина (4 байта, little endian) + JSON в UTF-8. Это ровно тот формат, которым
Firefox говорит с native messaging хостом, поэтому один и тот же код читает и
пишет в обоих местах, а протокол приходится знать только расширению.

read_message/write_message работают с потоком файлового типа: sys.stdin,
sys.stdout или sock.makefile("rwb"). Сам сокет не подходит - у него нет
методов read/write, поэтому держи открытым файловый объект и закрывай его
вместе с сокетом:

    with sock.makefile("rwb") as stream:
        ipc.write_message(stream, {...})
        message = ipc.read_message(stream)

Порядок обмена
--------------
1. Вижет поднимает сервер и пишет файл точки входа (ipc.py: write_endpoint).
2. Хост читает файл, подключается и шлёт {"t": "auth", "token": ...}.
3. Вижет сверяет токен и отвечает {"t": "auth-ok"} либо закрывает соединение.
4. Дальше сообщения идут в обе стороны без преобразований.

Зачем токен
-----------
Порт слушается на всех локальных интерфейсах, и любое приложение на этой
машине может к нему подключиться. Файл точки входа лежит в %LOCALAPPDATA%,
то есть доступен только текущему пользователю, а токен делает подмену
сервера бессмысленной: чужой процесс, занявший порт, его не знает.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any

# Версия протокола виджет <-> расширение. Должна совпадать с PROTOCOL_VERSION
# в extension/background/protocol.js: расширение отбрасывает сообщения с
# другой версией, чтобы старый и новый виджет не мешали друг другу.
PROTOCOL_VERSION = 1

# Порт по умолчанию. Если занят - виджет возьмёт любой свободный и запишет
# фактический в файл точки входа, поэтому жёстко держаться порта не нужно.
DEFAULT_PORT = 47651

HOST = "127.0.0.1"

# Каталог точки входа. %LOCALAPPDATA% - уже пользовательский, поэтому
# отдельных прав доступа выставлять не нужно.
APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MusicWidget"
ENDPOINT_FILE = APP_DIR / "endpoint.json"

# 4 байта длины. Больше мегабайта кадр быть не должен: расширение тоже
# ограничивает размер сообщения.
MAX_FRAME = 1 << 20

_HEADER = struct.Struct("<I")


class ProtocolError(Exception):
    """Кадр не разбирается или пришёл не того вида."""


def read_message(stream: Any) -> dict | None:
    """Читает одно сообщение. Возвращает None, если поток закрылся.

    Работает и с сокетом, и с sys.stdin - разница только в типе объекта.
    """
    header = _read_exactly(stream, _HEADER.size)
    if header is None:
        return None

    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ProtocolError(f"кадр слишком велик: {length} байт")

    body = _read_exactly(stream, length)
    if body is None:
        raise ProtocolError("поток оборвался на середине кадра")

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"кадр не разобрался: {exc}") from exc


def write_message(stream: Any, message: dict) -> None:
    """Пишет одно сообщение в поток."""
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"сообщение слишком велико: {len(body)} байт")
    stream.write(_HEADER.pack(len(body)))
    stream.write(body)
    stream.flush()


def _read_exactly(stream: Any, size: int) -> bytes | None:
    """Читает ровно size байт.

    None - соединение закрыто на границе кадра, это штатное завершение.
    ProtocolError - поток оборвался посреди кадра, значит сообщение
    пришло битым, и молча продолжить нельзя.
    """
    if size == 0:
        return b""
    chunks: list[bytes] = []
    got = 0
    while got < size:
        chunk = stream.read(size - got)
        if not chunk:
            if got == 0:
                return None
            raise ProtocolError(f"поток оборвался на {got} из {size} байт")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


# --- файл точки входа -------------------------------------------------------


def write_endpoint(port: int, token: str) -> Path:
    """Публикует адрес виджета для хоста."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started": int(time.time()),
    }
    ENDPOINT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ENDPOINT_FILE


def read_endpoint() -> dict | None:
    """Читает адрес виджета. None, если виджет не запущен или файл битый."""
    try:
        data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    port = data.get("port")
    token = data.get("token")
    if not isinstance(port, int) or not isinstance(token, str) or not token:
        return None
    return {"port": port, "token": token, "pid": data.get("pid")}


def clear_endpoint() -> None:
    """Убирает файл точки входа, чтобы хост не стучался в мёртвый порт."""
    try:
        ENDPOINT_FILE.unlink()
    except OSError:
        pass


def endpoint_owner_alive(endpoint: dict) -> bool:
    """Проверяет, жив ли ещё процесс, который опубликовал точку входа.

    Если виджет упал, файл остался бы лежать, и хост долбился бы в порт
    понапрасну, раз в секунду, до бесконечности.
    """
    pid = endpoint.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True

    if os.name == "nt":
        # На Windows нет os.kill(pid, 0). Открываем процесс и смотрим его
        # код завершения: у живого он всегда STILL_ACTIVE.
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Открыть не смогли: процесса нет либо он чужой и запрещён.
            # Второе маловероятно для своего же PID, считаем, что его нет.
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def connect_to_widget(timeout: float = 1.0) -> socket.socket | None:
    """Подключается к виджету. None, если он не запущен или не отвечает.

    Возвращённый сокет переводится в блокирующий режим. Это важно:
    таймаут, заданный create_connection, остаётся на самом сокете и
    наследуется файловыми объектами makefile. Оставив его, мы получили бы
    обрыв соединения через полсекунды молчания - как раз того случая, когда
    виджет просто ничего не шлёт.
    """
    endpoint = read_endpoint()
    if not endpoint:
        return None
    if not endpoint_owner_alive(endpoint):
        clear_endpoint()
        return None
    try:
        sock = socket.create_connection((HOST, endpoint["port"]), timeout=timeout)
    except OSError:
        return None
    sock.settimeout(None)
    return sock