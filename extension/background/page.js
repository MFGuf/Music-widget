"use strict";

// Фоновая страница расширения. Постоянная (persistent: true), потому что
// соединение с native messaging хостом и опрос вкладок не должны прерываться.
//
// Схема работы:
//   1. на старте подключаемся к хосту виджета (native.js);
//   2. каждые POLL_INTERVAL_MS вкладки отдают своё состояние (tabs.js);
//   3. к снимку добавляются обложки (art.js);
//   4. отправляем виджету снимок и выполняем его команды.
//
// Модули подключены в page.html в порядке:
//   protocol -> store -> native -> tabs -> art -> page
//
// Длительность и обложка трека приезжают уже разобранными: page/page.js
// перехватывает ответы VK API прямо в странице и отдаёт готовые поля.

// Почему виджету не отправляется снимок при каждом событии вкладки:
// состояние всегда нормализуется в rebuildItems(), а виджет сам решает,
// что показывать.

function note() {
  if (!host.connected) {
    return hostNote() || "нет связи с виджетом";
  }
  if (store.lastPageError) {
    return store.lastPageError;
  }
  if (store.sources.size === 0) {
    return "Откройте вкладку VK Музыки и включите трек";
  }
  if (store.lastItems.length === 0) {
    // Подробность важнее общего «не отвечает»: молчать может и
    // невнедрённый скрипт вкладки, и скрипт главного мира, и лечатся
    // они по-разному.
    const reason = tabProblem();
    return reason ? reason : "Вкладка VK Музыки ещё не отвечает";
  }
  return lastError ? lastError : "Подключено";
}

function sendState() {
  sendToHost({
    t: "state",
    items: store.lastItems,
    selected: store.selectedKey,
    note: note(),
  });
}

// --- Команды из виджета -----------------------------------------------------

host.onCommand = async (msg) => {
  const action = msg.action;
  const id = msg.id;

  if (action === "select") {
    if (store.sources.has(parseTabId(msg.key))) {
      store.selectedKey = msg.key;
      store.userPicked = true;
      sendState();
      ack(id, true, "");
    } else {
      ack(id, false, "источник не найден");
    }
    return;
  }

  const source = resolveSource(msg.key);
  if (!source) {
    ack(id, false, "нет вкладки для команды");
    return;
  }

  let ok = true;
  let text = "";
  try {
    const reply = await browser.tabs.sendMessage(source.tabId, {
      t: "cmd",
      action: action,
      ratio: typeof msg.ratio === "number" ? msg.ratio : null,
    });
    ok = !!(reply && reply.ok);
    text = (reply && reply.msg) || "";
  } catch (e) {
    // Обычно это перезагрузка вкладки: content script в ней ещё не поднят.
    ok = false;
    text = String((e && e.message) || e);
  }
  ack(id, ok, text);
};

// Ключ приходит от виджета строкой ("tab:42"), а хранилище ключуется
// числом id вкладки.
function parseTabId(key) {
  const text = String(key == null ? "" : key);
  const id = text.startsWith("tab:") ? text.slice(4) : text;
  const num = Number(id);
  return isFinite(num) ? num : null;
}

// Виджет может не присылать ключ вовсе - тогда команда идёт в выбранный
// источник. Если ключ пришёл, но вкладки больше нет, тоже откатываемся на
// выбранную: вкладка могла закрыться между опросом и нажатием кнопки, и
// пользователь ожидает реакцию, а не ошибку.
function resolveSource(key) {
  if (key != null) {
    const byKey = parseTabId(key);
    if (byKey !== null && store.sources.has(byKey)) {
      return store.sources.get(byKey);
    }
  }
  const selected = parseTabId(store.selectedKey);
  if (selected !== null && store.sources.has(selected)) {
    return store.sources.get(selected);
  }
  const first = store.sources.values().next();
  return first.done ? null : first.value;
}

function ack(id, ok, msg) {
  if (id === undefined || id === null) {
    return;
  }
  sendToHost({ t: "ack", id: id, ok: !!ok, msg: trimText(msg, 120) });
}

// --- Жизненный цикл ---------------------------------------------------------

// Content script сообщает, что скрипт главного мира загрузился. Не ждём
// конца интервала: первый снимок полезен сразу.
browser.runtime.onMessage.addListener((msg) => {
  if (!msg) {
    return undefined;
  }
  if (msg.t === "pageReady") {
    // Скрипт главного мира поднялся - снимаем прежнюю жалобу на него
    // и не ждём конца интервала.
    if (store.lastPageError) {
      store.lastPageError = "";
    }
    schedulePoll(0);
  } else if (msg.t === "pageBlocked") {
    store.lastPageError = trimText(msg.reason, 200);
    log(store.lastPageError);
  }
  return undefined;
});

browser.runtime.onInstalled.addListener(() => start());
browser.runtime.onStartup.addListener(() => start());

function start() {
  startHost();
  startTabPolling();
  browser.tabs.onUpdated.addListener(onTabUpdated);
  browser.tabs.onRemoved.addListener(onTabRemoved);
  injectIntoOpenTabs().then(() => pollTabs());
}

start();