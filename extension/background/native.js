"use strict";

// Слой native messaging: единственное место, где расширение общается
// с настольным виджетом. Виджет запускается отдельно (run.bat), поэтому
// соединение может отсутствовать долго - Firefox при этом просто не
// создаёт порт. Здесь же - переподключение с растущей паузой.
//
// Формат сообщений (envelope):
//   { v: 1, t: <тип>, ... }
//
// расширение -> виджет:
//   hello   рукопожатие при подключении порта
//   state   полный снимок источников: { items, selected, note }
//   ack     результат команды: { id, ok, msg }
//
// виджет -> расширение:
//   hello   рукопожатие виджета
//   cmd     команда: { id, action, key, ratio }
//   ping    проверка живости

// Firefox не примет сообщение больше 1 МБ. Обложки мы сжимаем (art.js),
// но подстраховка обязательна: обрыв порта посреди отправки обложки
// приводит к тому, что виджет вообще перестаёт получать состояние.
const MAX_MESSAGE_BYTES = 900 * 1024;

const host = {
  port: null,
  connected: false,
  retryDelayMs: 1000,
  retryTimer: null,
  // Колбэки назначает page.js, чтобы native.js не знал про вкладки.
  onCommand: null,
  onStatus: null,
};

function hostNote() {
  if (host.connected) {
    return null;
  }
  return "Виджет не запущен или хост не зарегистрирован";
}

function scheduleReconnect(reason) {
  if (host.retryTimer !== null) {
    return;
  }
  const delay = host.retryDelayMs;
  log("повторное подключение через", delay, "мс;", reason || "");
  host.retryTimer = setTimeout(() => {
    host.retryTimer = null;
    connectHost();
  }, delay);
  // Пауза растёт до 30 с, но сбрасывается при первом успехе.
  host.retryDelayMs = Math.min(host.retryDelayMs * 2, 30000);
}

function handleDisconnect(reason) {
  if (!host.connected && host.port === null) {
    // Повторное отключение того же порта - просто планируем следующую попытку.
    scheduleReconnect(reason);
    return;
  }
  host.port = null;
  host.connected = false;
  host.onStatus?.(false, reason);
  scheduleReconnect(reason);
}

function connectHost() {
  browser.runtime
    .connectNative(HOST_NAME)
    .then((port) => {
      host.port = port;
      host.connected = true;
      host.retryDelayMs = 1000;
      log("хост подключён");

      port.onMessage.addListener(onHostMessage);
      port.onDisconnect.addListener((err) => {
        const reason = err && err.message ? err.message : "порт закрыт";
        handleDisconnect(reason);
      });

      host.onStatus?.(true, null);
      sendToHost({ t: "hello", v: PROTOCOL_VERSION, role: "extension" });
    })
    .catch((err) => {
      // Самая частая причина - хост не зарегистрирован в реестре:
      // native\install_host.ps1 ещё не запускался.
      const reason = err && err.message ? err.message : String(err);
      setError("native host: " + reason);
      host.connected = false;
      host.onStatus?.(false, reason);
      scheduleReconnect(reason);
    });
}

function onHostMessage(msg) {
  if (!msg || typeof msg !== "object") {
    return;
  }
  if (msg.v !== PROTOCOL_VERSION) {
    log("сообщение другой версии протокола:", msg.v, "- пропущено");
    return;
  }
  switch (msg.t) {
    case "cmd": {
      // Обработчик асинхронный: без catch отказ внутри него всплыл бы
      // необработанным обещанием и в консоли выглядел бы загадочно.
      const result = host.onCommand?.(msg);
      if (result && typeof result.then === "function") {
        result.catch((e) => {
          log("команда", msg.action, "упала:", (e && e.message) || e);
        });
      }
      break;
    }
    case "ping":
      sendToHost({ t: "pong", v: PROTOCOL_VERSION });
      break;
    default:
      log("неизвестный тип сообщения:", msg.t);
  }
}

function sendToHost(msg) {
  if (!host.port) {
    return false;
  }
  return postPayload({ v: PROTOCOL_VERSION, ...msg });
}

// json.length считает символы, а лимит Firefox задан в байтах. Русские
// названия занимают два байта на символ, поэтому считаем честно.
const encoder = new TextEncoder();

function byteLength(json) {
  return encoder.encode(json).length;
}

function postPayload(payload) {
  try {
    let json = JSON.stringify(payload);
    if (byteLength(json) > MAX_MESSAGE_BYTES && payload.items) {
      // Обложка не влезла - шлём состояние без картинок, чтобы не потерять
      // управление плеером из-за одной неудачной загрузки.
      payload = {
        ...payload,
        items: payload.items.map((it) => ({ ...it, art: "", art_new: false })),
      };
      json = JSON.stringify(payload);
    }
    if (byteLength(json) > MAX_MESSAGE_BYTES) {
      log("сообщение слишком велико, пропущено:", byteLength(json));
      return false;
    }
    host.port.postMessage(payload);
    return true;
  } catch (e) {
    handleDisconnect("отправка не удалась: " + e);
    return false;
  }
}

function startHost() {
  if (host.port || host.retryTimer !== null) {
    return;
  }
  connectHost();
}