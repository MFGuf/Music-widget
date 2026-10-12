"use strict";

// Content script: работает в изолированном мире, видит DOM страницы VK,
// но не имеет доступа к JS-переменным самой страницы.
//
// Схема обмена:
//   background/page.js  <-> этот скрипт      через browser.runtime.sendMessage
//   этот скрипт        <-> page/page.js      через window.postMessage
//
// page/page.js внедряется в ГЛАВНЫЙ мир страницы (main world), потому что
// Firefox не даёт расширению доступа к JS-объектам страницы напрямую,
// а хуки HTMLMediaElement и AudioContext должны быть поставлены ДО того,
// как плеер VK их создаст.

const MSG_VERSION = 1;

// Скрипт главного мира может не ответить: он не загрузился, страница
// зависла на перерисовке или сообщение потерялось. Тогда не блокируем фон,
// а возвращаем пустой ответ - виджет сам разберётся.
const REQUEST_TIMEOUT_MS = 1500;

// Токен защищает обмен от сообщений чужих скриптов страницы: никто, кроме
// нас, о нём не знает, а скрипт главного мира принимает только сообщения
// с правильным токеном.
const TOKEN = randomToken();

// Ожидающие ответы: id -> { resolve, timer }
const pending = new Map();
let nextId = 1;

// Помечает, что скрипт уже внедрён в эту страницу. Значение - версия
// расширения, а не просто флаг: после обновления расширения прежний
// скрипт выгружается, а атрибут остаётся на странице. Со флагом вкладка
// VK перестала бы работать навсегда - до ручной перезагрузки страницы.
// С версией повторная вставка после обновления проходит, а вот две копии
// одной версии - нет.
const GUARD_ATTR = "data-music-widget-content";
const GUARD_VALUE = String((browser.runtime.getManifest() || {}).version || "0");

function randomToken() {
  const bytes = new Uint8Array(16);
  (window.crypto || window.msCrypto).getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// --- внедрение скрипта главного мира -----------------------------------------

// Тег <script> выполняется АСИНХРОННО: пока он грузится, нашего слушателя
// сообщений ещё не существует. Поэтому инициализация выносится в обработчик
// load - если отправить init сразу после appendChild, он уйдёт в пустоту и
// обмен не поднимется до перезагрузки страницы.
function injectPageScript() {
  const root = document.head || document.documentElement;
  if (!root) {
    // Совсем ранний document_start: <head> ещё не создан. Повторяем попытку.
    document.addEventListener("DOMContentLoaded", injectPageScript, { once: true });
    return;
  }
  const script = document.createElement("script");
  script.src = browser.runtime.getURL("page/page.js");
  // Страница не должна знать, откуда пришёл скрипт, а главный мир он
  // запускает только будучи добавленным в DOM.
  script.async = false;
  script.addEventListener("load", () => {
    script.remove();
    initPageScript();
  });
  root.appendChild(script);
}

// Скрипт главного мира может и не запуститься: страница способна запретить
// подгрузку по своей политике безопасности (CSP). Тогда обмена нет, и
// виджет должен сказать об этом прямо, а не молчать.
let mainWorldReady = false;
let injectAttempts = 0;
const MAX_INJECT_ATTEMPTS = 3;

function ensurePageScript() {
  if (mainWorldReady || injectAttempts >= MAX_INJECT_ATTEMPTS) {
    return;
  }
  injectAttempts++;
  injectPageScript();
}

function reportBlocked(reason) {
  const message = "Скрипт страницы VK не запустился (" + reason +
    "). Если включена жёсткая политика безопасности, возможности виджета ограничены.";
  browser.runtime.sendMessage({ t: "pageBlocked", reason: message }).catch(() => {});
}

function start() {
  document.documentElement.setAttribute(GUARD_ATTR, GUARD_VALUE);
  browser.runtime.onMessage.addListener(onRuntimeMessage);
  ensurePageScript();
}

// Страница VK - SPA: при переходе между треками документ не перезагружается,
// но скрипт главного мира остаётся жить. Переустанавливать его не нужно.

// --- обмен со скриптом главного мира -----------------------------------------

function postToPage(type, payload) {
  return new Promise((resolve) => {
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      resolve(null);
    }, REQUEST_TIMEOUT_MS);
    pending.set(id, { resolve, timer });
    try {
      window.postMessage(
        { __mw: MSG_VERSION, dir: "toPage", id, token: TOKEN, t: type, ...payload },
        "*"
      );
    } catch (e) {
      clearTimeout(timer);
      pending.delete(id);
      resolve(null);
    }
  });
}

window.addEventListener("message", (ev) => {
  if (ev.source !== window) {
    return;
  }
  const data = ev.data;
  if (!data || data.__mw !== MSG_VERSION || data.dir !== "toContent") {
    return;
  }
  if (data.token !== TOKEN) {
    return;
  }
  const waiter = pending.get(data.id);
  if (!waiter) {
    return;
  }
  pending.delete(data.id);
  clearTimeout(waiter.timer);
  waiter.resolve(data.payload);
});

// Скрипт главного мира подтверждает, что он загрузился и слушает сообщения.
async function initPageScript() {
  const ready = await postToPage("init", {});
  if (ready) {
    mainWorldReady = true;
    browser.runtime.sendMessage({ t: "pageReady" }).catch(() => {});
    return;
  }
  if (!mainWorldReady) {
    reportBlocked("не ответил на init");
  }
}

// --- обмен с фоновой страницей ----------------------------------------------

function onRuntimeMessage(msg) {
  if (!msg || !msg.t) {
    return;
  }
  if (msg.t === "poll") {
    // Обмен мог не подняться при загрузке страницы - пробуем ещё раз,
    // но не бесконечно: иначе вкладка без поддержки будет грузить скрипт
    // каждые две трети секунды.
    ensurePageScript();
    if (!mainWorldReady) {
      return Promise.resolve(null);
    }
    return postToPage("getState", {});
  }
  if (msg.t === "cmd") {
    if (!mainWorldReady) {
      return Promise.resolve({ ok: false, msg: "обмен со страницей не работает" });
    }
    return postToPage("cmd", { action: msg.action, ratio: msg.ratio });
  }
  return undefined;
}

// Повторная вставка скрипта той же версии (например, гонка между
// внедрением из манифеста и injectIntoOpenTabs) не должна заводить вторую
// копию обработчиков. Другая версия - значит обновление расширения, и
// скрипт должен запуститься заново.
if (document.documentElement.getAttribute(GUARD_ATTR) !== GUARD_VALUE) {
  start();
}