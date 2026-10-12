# Music Widget for Firefox + VK Music

Настольный виджет с текущим треком для музыки VK. Виджет — это небольшое
безрамочное окно поверх всего (customtkinter), которое показывает обложку,
название, исполнителя, полосу прогресса и эквалайзер, а управление плеером
уходит **расширением Firefox** через Native Messaging.

В отличие от прошлой версии, браузер больше не запускается с
`--remote-debugging-port`: Firefox не имеет протокола CDP, поэтому состояние
плеера снимается content script'ом в самой вкладке VK, а обмен с виджетом
идёт через native messaging хост.

## Как это устроено

```
Firefox (расширение MV2, постоянная установка)
  │
  ├─ background/page.js  — ищет вкладки VK Music, шлёт команды,
  │                        перехватывает ответы VK API (webRequest)
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
| `widget/`    | Python-виджет и обменник                            |
| `tools/`     | Сборка XPI, утилиты                                 |

## Требования

- Firefox 115+ (проверено на 157.0.1)
- Python 3.10+
- Windows (регистрация хоста в `HKCU`)

## Установка

Полная инструкция появится после сборки расширения. Кратко:

1. `python -m pip install -r requirements.txt`
2. `powershell -ExecutionPolicy Bypass -File native\install_host.ps1` —
   регистрирует native messaging хост
3. Собрать и установить XPI: `python tools\build_xpi.py`
4. Запуск: `run.bat`

## Лицензия

Частный проект, лицензия не задана.