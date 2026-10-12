"use strict";

// Фоновая страница расширения. Постоянная (persistent: true), потому что
// соединение с native messaging хостом и опрос вкладок не должны прерываться.
//
// Схема работы:
//   1. на старте подключаемся к хосту виджета (native.js);
//   2. каждые POLL_INTERVAL_MS спрашиваем у вкладок VK их состояние;
//   3. собираем список источников и отправляем виджету снимок;
//   4. команды из виджета маршрутизируем в нужную вкладку.
//
// Зависит от protocol.js и native.js, подключённых раньше в page.html.

// Источники: key -> описание вкладки (id, url).
const sources = new Map();

// Что виджет видит как выбранный источник.
let selectedKey = null;

// Последний снимок - чтобы не гонять команды в устаревшую вкладку.
let lastItems = [];

// Что расширение уже знает о треках из перехваченных ответов VK API:
// cacheKey -> { title, artist, dur, artUrl }
const trackMeta = new Map();

function note() {
  if (!host.connected) {
    return hostNote() || "нет связи с виджетом";
  }
  if (sources.size === 0) {
    return "Откройте вкладку VK Музыки и включите трек";
  }
  return lastError ? lastError : "Подключено";
}

function sendState() {
  sendToHost({
    t: "state",
    items: lastItems,
    selected: selectedKey,
    note: note(),
  });
}

// --- Команды из виджета -----------------------------------------------------

host.onCommand = async (msg) => {
  const action = msg.action;
  const key = msg.key || selectedKey;
  const id = msg.id;

  if (action === "select") {
    if (sources.has(key)) {
      selectedKey = key;
      sendState();
      ack(id, true, "");
    } else {
      ack(id, false, "источник не найден");
    }
    return;
  }

  if (!key || !sources.has(key)) {
    ack(id, false, "нет вкладки для команды");
    return;
  }

  const source = sources.get(key);
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
    // Самая частая причина: вкладка перезагрузилась и content script
    // в ней ещё не успел подняться.
    ok = false;
    text = String((e && e.message) || e);
  }

  ack(id, ok, text);
};

function ack(id, ok, msg) {
  if (id === undefined || id === null) {
    return;
  }
  sendToHost({ t: "ack", id: id, ok: !!ok, msg: trimText(msg, 120) });
}

// --- Жизненный цикл ---------------------------------------------------------

browser.runtime.onInstalled.addListener(() => startHost());
browser.runtime.onStartup.addListener(() => startHost());

startHost();