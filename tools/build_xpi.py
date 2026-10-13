#!/usr/bin/env python
"""Собирает расширение в XPI для установки в Firefox.

Запуск:
    python tools\\build_xpi.py

XPI - это обычный zip, у которого manifest.json лежит в корне архива.
Firefox ставит такой файл как дополнение, а addons.mozilla.org принимает
его же на подпись. Отдельного формата нет.

Сборка отказывается работать, если расширение не цельно: сначала
прогоняются те же проверки, что и в check_extension.py. Собрать и
загрузить на подпись сломанный пакет - значит потратить попытку на
подпись и получить отказ.

Сборка повторяемая: время файлов в архиве фиксировано, поэтому две
сборки из одного состояния репозитория дают побайтово одинаковый файл.
Это заметно при проверке, что на подпись ушло именно то, что собирали.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
DIST = ROOT / "dist"

# Фиксированное время внутри архива. zip не хранит настоящие даты файлов,
# а без этого две сборки отличаются побайтово только из-за времени записи.
STAMP = (1980, 1, 1, 0, 0, 0)

# Мусор, который мог накопиться в папке расширения при отладке. В архив он
# не должен попасть: AMO проверяет содержимое и лишнее только насторожит.
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vs", ".vscode", ".pytest_cache"}
SKIP_SUFFIXES = (".py", ".pyc", ".log", ".txt", ".md", ".bak", ".orig")
SKIP_NAMES = {".DS_Store", "Thumbs.db"}


def collect_files() -> list[Path]:
    """Файлы расширения в порядке: сначала манифест, потом остальное.

    Порядок не влияет на работу, но manifest.json первым удобен и человеку,
    и распаковщикам, которые показывают первое содержимое архива.
    """
    found: list[Path] = []
    for path in sorted(EXT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(EXT)
        if set(relative.parts) & SKIP_DIRS:
            continue
        if path.name in SKIP_NAMES:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if relative.as_posix() == "manifest.json":
            continue
        found.append(path)

    manifest = EXT / "manifest.json"
    if manifest.is_file():
        found.insert(0, manifest)
    return found


def check_extension() -> list[str]:
    """Прогоняет проверки целостности и возвращает список претензий."""
    sys.path.insert(0, str(ROOT / "tools"))
    import check_extension  # noqa: E402

    # Список претензий модульный, и он же накапливается при импорте. Обнулять
    # надо перед прогоном, иначе повторный вызов в том же процессе вернёт
    # старые претензии.
    check_extension.problems = []
    manifest = check_extension.check_manifest()
    if manifest:
        check_extension.check_host_name(manifest)
    check_extension.check_js_pitfalls()
    check_extension.check_no_stale_files()
    return list(check_extension.problems)


def version() -> str:
    return json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))["version"]


def build(target: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(EXT).as_posix(), STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0644: обычные права на файлы. Без этого zip кладёт 0600, и
            # распаковка на чужой машине даёт файлы, которые не читаются.
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def verify(target: Path, files: list[Path]) -> list[str]:
    """Проверяет собранный файл, а не исходники.

    Ошибка упаковки видна только в архиве: забытый файл, уехавший в
    подкаталог manifest.json - Firefox не скажет об этом понятным
    языком, а просто не поставит дополнение.
    """
    problems: list[str] = []
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        if names[0] != "manifest.json":
            problems.append("manifest.json не в корне архива, а в %r" % names[0])
        if archive.testzip() is not None:
            problems.append("в архиве повреждённые записи")

        expected = {path.relative_to(EXT).as_posix() for path in files}
        missing = sorted(expected - set(names))
        if missing:
            problems.append("в архив не попали: %s" % ", ".join(missing))

        try:
            manifest = json.loads(
                archive.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            problems.append("manifest.json в архиве не разбирается: %s" % exc)
        else:
            if manifest.get("version") != version():
                problems.append("в архиве другая версия")

            # Каждый файл, на который ссылается манифест, обязан лежать
            # в архиве по тому же пути.
            referenced = [manifest.get("background", {}).get("page")]
            referenced += [icon for icon in manifest.get("icons", {}).values()]
            for group in manifest.get("content_scripts", []):
                referenced += list(group.get("js", []))
            for name in filter(None, referenced):
                if name not in names:
                    problems.append("манифест ссылается на отсутствующий %s" % name)
    return problems


def main() -> int:
    if not (EXT / "manifest.json").exists():
        print("нет extension/manifest.json - нечего собирать")
        return 1

    problems = check_extension()
    if problems:
        print("расширение не цельно, сборка отменена:")
        for item in problems:
            print("  - " + item)
        return 1

    files = collect_files()
    DIST.mkdir(exist_ok=True)
    target = DIST / ("music-widget-%s.xpi" % version())
    build(target, files)

    problems = verify(target, files)
    if problems:
        target.unlink(missing_ok=True)
        print("собранный файл не проходит проверку, он удалён:")
        for item in problems:
            print("  - " + item)
        return 1

    size = target.stat().st_size
    print("собрано: %s" % target.relative_to(ROOT))
    print("файлов в архиве: %d, размер: %.1f КБ" % (len(files), size / 1024))
    print("версия %s, ID %s" % (
        version(),
        json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        ["browser_specific_settings"]["gecko"]["id"],
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())