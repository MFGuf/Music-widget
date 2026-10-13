# Music Widget for Firefox + VK Music

Настольный виджет с текущим треком для музыки VK. Виджет — небольшое
безрамочное окно поверх всего, которое показывает обложку, название,
исполнителя, полосу прогресса и спектр. Управление плеером уходит
**расширением Firefox** через Native Messaging.

Браузер запускается обычным образом, без `--remote-debugging-port`: Firefox
не имеет протокола CDP, поэтому состояние плеера снимается скриптом в самой
вкладке VK, а обмен с виджетом идёт через native messaging хост.

## Как это устроено

```
Firefox (расширение MV2)
  │
  ├─ background/page.js  — ищет вкладки VK Music, шлёт команды,
  │                        перехватывает ответы VK API
  ├─ content/vk.js       — изоляция: связка background ↔ page
  └─ page/page.js        — MAIN world: хуки play/pause, AudioContext,
                           кнопки плеера, перемотка
  │  nativeMessaging
  ▼
native/host.py           — хост браузера: stdio ↔ TCP 127.0.0.1
  ▼
widget/bridge_worker.py  — поток-обменник
  ▼
widget/vk_music_widget.py — окно виджета
```

Firefox **не поддерживает** `navigator.mediaSession`, поэтому «играет ли музыка»
и позиция воспроизведения берутся из хуков `HTMLMediaElement`, а точная
длительность трека — из перехвата ответов VK API.

## Структура

| Папка      | Назначение                                          |
|------------|-----------------------------------------------------|
| `extension/` | WebExtension для Firefox (MV2)                    |
| `native/`    | Native messaging хост + регистрация в Windows      |
| `widget/`    | Python-виджет, обменник, журнал                     |
| `tools/`     | Сборка XPI и проверки                               |

## Требования

- Firefox 115+ (разработка велась на 157.0.1)
- Python 3.10+ с Pillow
- Windows

## Установка

1. Зависимости:

   ```
   python -m pip install -r requirements.txt
   ```

2. Регистрация native messaging хоста — **обязательный шаг**, без него
   браузер не достучится до виджета:

   ```
   powershell -ExecutionPolicy Bypass -File native\install_host.ps1
   ```

3. Сборка расширения:

   ```
   python tools\build_xpi.py
   ```

   Готовый файл появляется в `dist/music-widget-<версия>.xpi`.

4. Установка расширения в Firefox. Нужен аккаунт на
   [addons.mozilla.org](https://addons.mozilla.org): пакет загружается
   как **unlisted** (без публикации) и подписывается там же. Подробности —
   в разделе «Установка расширения» ниже.

5. Запуск виджета:

   ```
   run.bat
   ```

## Установка расширения

Firefox релизной сборки ставит только **подписанные** дополнения, поэтому
голый `.xpi` не установится. Есть два пути.

### Путь 1. AMO, подпись (рекомендуется)

1. Зарегистрироваться на addons.mozilla.org и подтвердить почту.
2. «Submit a New Add-on» → выбрать «On your own, not distributed on AMO»
   (unlisted), прицелиться в Firefox.
3. Загрузить `dist/music-widget-<версия>.xpi`.
4. AMO проверит пакет и выдаст файл `.xpi` — его и ставим через
   «about:addons» → шестерёнка → «Install Add-on From File».

Версию в `manifest.json` перед каждой загрузкой нужно повышать: AMO не
принимает два одинаковых номера.

### Путь 2. Временная установка, для разработки

`about:debugging#/runtime/this-firefox` → «Load Temporary Add-on…» →
выбрать `extension/manifest.json` или папку. Расширение живёт до перезапуска
браузера, но позволяет проверять изменения без пересборки пакета.

## Проверки

```
python widget\widget_test.py     # виджет: диагностика и мышь
python widget\bridge_test.py     # обменник
python widget\ipc_test.py        # протокол IPC
python native\host_test.py       # хост браузера
python tools\run_tests.py        # расширение в настоящем Firefox
python tools\check_extension.py  # целостность расширения
python tools\build_xpi.py        # сборка пакета
```

Отчёты пишутся в `tools/.*.txt` — в консоли Windows кириллица часто
превращается в мусор из-за кодовой страницы терминала.

## Если виджет молчит

Правая кнопка по виджету → **Диагностика**: показывает, на каком шаге
цепочки браузер → хост → виджет оборвалась, и хвост журнала. Сам журнал
лежит в `%LOCALAPPDATA%\MusicWidget\widget.log`.

## Лицензия

Частный проект, лицензия не задана.