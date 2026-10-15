#!/usr/bin/env python
"""Проверяет целостность расширения без запуска Firefox.

Что проверяется:
  * manifest.json разбирается и содержит обязательные поля MV2;
  * все файлы, на которые ссылается манифест, существуют;
  * фоновые скрипты подключены в правильном порядке и все на месте;
  * скрипты главного мира и content script присутствуют;
  * имя хоста совпадает между манифестом расширения и манифестом хоста;
  * в JS нет кириллических слов, завязанных на \\w (ловушка из порта с Python);
  * нет запрещённых перехватов, удалённых из проекта.

Запуск:
    python tools\\check_extension.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"

# Порядок важен: protocol.js даёт константы, от которых зависят остальные.
EXPECTED_BACKGROUND = [
    "protocol.js",
    "store.js",
    "native.js",
    "tabs.js",
    "art.js",
    "page.js",
]

problems = []


def check(condition, message):
    if not condition:
        problems.append(message)


def check_manifest():
    path = EXT / "manifest.json"
    check(path.exists(), "нет manifest.json")
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"manifest.json не разбирается: {e}")
        return None

    check(manifest.get("manifest_version") == 2,
          "manifest_version должен быть 2, а не %r" % manifest.get("manifest_version"))

    gecko = manifest.get("browser_specific_settings", {}).get("gecko", {})
    ext_id = gecko.get("id")
    check(bool(ext_id), "не задан browser_specific_settings.gecko.id")
    check(bool(gecko.get("strict_min_version")),
          "не задан strict_min_version")

    permissions = manifest.get("permissions", [])
    check("nativeMessaging" in permissions,
          "нет права nativeMessaging - виджет не подключится")
    check("tabs" in permissions, "нет права tabs - не увидим вкладки")
    check("<all_urls>" in permissions,
          "нет <all_urls> - не скачаем обложки с CDN VK")

    background = manifest.get("background", {})
    check("page" in background or "scripts" in background,
          "не описан background")
    if "page" in background:
        check((EXT / background["page"]).exists(),
              "нет файла фоновой страницы %s" % background["page"])

    for group in manifest.get("content_scripts", []):
        for js in group.get("js", []):
            check((EXT / js).exists(), "нет content script %s" % js)
        check("run_at" in group,
              "у content script не задан run_at: без document_start хуки "
              "плеера поставятся слишком поздно")

    for size, icon in manifest.get("icons", {}).items():
        check((EXT / icon).exists(), "нет иконки %s (%s)" % (icon, size))

    check_manifest_version(manifest)
    check_web_accessible(manifest)
    check_background_scripts(manifest)
    return manifest


# Firefox и AMO принимают в версии только числа через точки: не больше
# четырёх чисел, каждое до девяти цифр и без ведущих нулей. Строка вида
# "0.2.0-alpha.1" проходит как версия расширения (только с предупреждением
# при загрузке), но на подпись не пройдёт вовсе.
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){0,3}$")


def check_manifest_version(manifest):
    version = str(manifest.get("version") or "")
    check(bool(VERSION_PATTERN.match(version)),
          "версия %r не подходит: Firefox и AMO ждут числа через точки, "
          "как в 0.2.0, без букв и суффиксов вида -alpha.1" % version)


def check_web_accessible(manifest):
    """Скрипт главного мира обязан быть виден странице.

    content/vk.js вставляет page/page.js тегом <script> как moz-extension://
    URL. Пока файл не объявлен в web_accessible_resources, Firefox его
    странице не отдаёт: подгрузка молча проваливается, скрипт главного мира
    не стартует, а виджет показывает «вкладка не отвечает» без причины.
    """
    allowed = manifest.get("web_accessible_resources") or []
    # В MV2 это список строк, в MV3 - список объектов с matches. Разбираем
    # оба вида, чтобы проверка не сломалась при переходе.
    paths = []
    for entry in allowed:
        paths.append(entry if isinstance(entry, str) else entry.get("resources", []))

    content_dir = EXT / "content"
    for script in sorted(content_dir.glob("*.js")):
        text = script.read_text(encoding="utf-8")
        for match in re.finditer(r'getURL\(\s*"([^"]+)"\s*\)', text):
            target = match.group(1)
            if not (EXT / target).exists():
                check(False, "%s грузит %s, которого нет" % (script.name, target))
            if target not in paths:
                check(False,
                      "%s грузит %s в страницу, но %s не объявлен в "
                      "web_accessible_resources: Firefox не отдаст файл "
                      "странице, и обмен не поднимется"
                      % (script.name, target, target))


def check_background_scripts(manifest):
    html_name = manifest.get("background", {}).get("page")
    if not html_name:
        return
    html = (EXT / html_name).read_text(encoding="utf-8")
    found = re.findall(r'<script src="([^"]+)"></script>', html)
    check(found == EXPECTED_BACKGROUND,
          "порядок фоновых скриптов неверный: %s, ожидался %s"
          % (found, EXPECTED_BACKGROUND))
    for name in found:
        check((EXT / "background" / name).exists(),
              "фоновый скрипт %s подключён, но отсутствует" % name)


def check_host_name(manifest):
    """Имя хоста должно совпадать в расширении и в манифесте хоста."""
    host_manifest = ROOT / "native" / "music_widget_host.json"
    check(host_manifest.exists(), "нет native/music_widget_host.json")
    if not host_manifest.exists():
        return
    spec = json.loads(host_manifest.read_text(encoding="utf-8"))
    check(spec.get("type") == "stdio",
          "тип хоста должен быть stdio, а не %r" % spec.get("type"))

    protocol = (EXT / "background" / "protocol.js").read_text(encoding="utf-8")
    found = re.search(r'HOST_NAME\s*=\s*"([^"]+)"', protocol)
    check(bool(found), "в protocol.js не найдено HOST_NAME")
    if found:
        check(found.group(1) == spec.get("name"),
              "имя хоста разошлось: расширение ждёт %r, манифест хоста %r"
              % (found.group(1), spec.get("name")))

    ext_id = manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id")
    allowed = spec.get("allowed_extensions", [])
    check(ext_id in allowed,
          "ID расширения %r не разрешён для хоста (в манифесте: %s)"
          % (ext_id, allowed))


def strip_comments(text):
    """Убирает комментарии, чтобы проверки ловили только рабочий код.

    Иначе поиск запрещённых конструкций срабатывает на пояснениях в
    комментариях - например, на фразе "в Firefox нет mediaSession".
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = []
    for line in text.split("\n"):
        # Строковые литералы не трогаем: '// внутри строки' - это не комментарий.
        stripped = re.sub(r"\\.", "", line)
        cut = stripped.find("//")
        if cut >= 0:
            line = stripped[:cut]
        lines.append(line)
    return "\n".join(lines)


def check_js_pitfalls():
    """Ловушки, найденные при переносе кода и проверяемые автоматически."""
    for path in sorted(EXT.rglob("*.js")):
        text = strip_comments(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT)

        # \w в JavaScript не покрывает кириллицу. Такое пришло из порта
        # с Python, где \w был Unicode-адресным.
        for match in re.finditer(r"[а-яё]+\\w", text):
            line = text[:match.start()].count("\n") + 1
            problems.append(
                "%s:%d - русское слово с \\w: в JS \\w не покрывает "
                "кириллицу (было %r)" % (rel, line, match.group(0))
            )

        # Перехват ввода должен оставаться в главном мире страницы.
        if "navigator.mediaSession" in text:
            problems.append(
                "%s - используется navigator.mediaSession, которого нет "
                "в Firefox" % rel
            )


def check_no_stale_files():
    check(not (EXT / "background" / "api.js").exists(),
          "background/api.js удалён, но вернулся в репозиторий")


def main():
    manifest = check_manifest()
    if manifest:
        check_host_name(manifest)
    check_js_pitfalls()
    check_no_stale_files()

    if problems:
        lines = ["Найдено проблем: %d" % len(problems)]
        lines += ["  - " + item for item in problems]
    else:
        lines = [
            "Расширение целостно: манифест, файлы, порядок загрузки "
            "и имя хоста сходятся.",
        ]
    text = "\n".join(lines)

    # Отчёт в файл: в консоли Windows кириллица часто превращается
    # в мусор из-за кодовой страницы терминала.
    report = ROOT / "tools" / ".extensioncheck.txt"
    report.write_text(text, encoding="utf-8")
    print("отчёт: " + str(report))
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())