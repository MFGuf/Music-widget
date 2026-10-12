"""Единая выдача результатов проверок на Python.

Зачем
----
Проверки запускаются в консоли Windows, где русский текст превращается в
мусор из-за кодовой страницы терминала. Разбирать такие строки бесполезно -
непонятно, ошибка это в коде или в выводе. Поэтому отчёт всегда пишется в
UTF-8 файл, а на консоль выводится короткая сводка.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPORT_DIR = Path(__file__).resolve().parent / "tools"


class Checks:
    """Считает успехи и копит текст отчёта."""

    def __init__(self, name: str, title: str):
        self.name = name
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            return True
        self.failed.append(f"{name}{': ' + detail if detail else ''}")
        return False

    def equal(self, name: str, actual, expected) -> bool:
        same = actual == expected
        return self.ok(name, same, "" if same else f"получено {actual!r}, ожидалось {expected!r}")

    def finish(self) -> int:
        lines = [f"=== {self.title} ==="]
        for item in self.failed:
            lines.append("  FAIL " + item)
        lines.append("")
        lines.append(f"проверок успешно: {self.passed}, провалено: {len(self.failed)}")
        text = "\n".join(lines)

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f".{self.name}.txt"
        path.write_text(text, encoding="utf-8")

        if self.failed:
            print(f"{self.name}: провалено {len(self.failed)}, успешно {self.passed}")
            print(f"отчёт: {path}")
            return 1
        print(f"{self.name}: все {self.passed} проверок пройдены")
        print(f"отчёт: {path}")
        return 0


def wait_for(predicate, timeout: float = 8.0, step: float = 0.1) -> bool:
    """Ждёт выполнения условия. False, если время вышло."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


if __name__ == "__main__":
    print(__doc__, file=sys.stdout)