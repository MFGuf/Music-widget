"use strict";

// Общие константы и вспомогательные функции фоновой страницы.
// Загружается первым - от него зависят native.js, tabs.js, api.js и page.js.

// Имя хоста должно совпадать с полем "name" в native/music_widget_host.json
// и с ключом реестра HKCU\Software\Mozilla\NativeMessagingHosts\<HOST_NAME>.
const HOST_NAME = "music_widget_host";

// Версия протокола виджет <-> расширение. Виджет отвергает сообщения с
// другой версией, чтобы старый и новый виджет не мешали друг другу.
const PROTOCOL_VERSION = 1;

// Как часто опрашиваем вкладки. 700 мс - компромисс:VK отдаёт состояние
// без задержки, но чаще опрашивать бессмысленно, а реже - заметно
// лагнет перемотка.
const POLL_INTERVAL_MS = 700;

// Максимум вкладок, которые опрашиваем одновременно. Больше не нужно:
// у пользователя редко открыто больше двух-трёх плееров VK.
const MAX_TABS = 6;

// Каналы VK Музыки. ВК периодически меняет поддомены, поэтому берём всё,
// что заканчивается на vk.com / vk.ru.
const VK_MUSIC_HINTS = [
  "vk.com/music",
  "vk.ru/music",
  "vk.com/audio",
  "vk.ru/audio",
  "vk.com/audios",
];

const VK_HOST_SUFFIXES = ["vk.com", "vk.ru"];

function isVkHost(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return VK_HOST_SUFFIXES.some((s) => host === s || host.endsWith("." + s));
  } catch (e) {
    return false;
  }
}

// Страница считается страницей плеера, если это VK и в адресе есть
// признак музыкального раздела. Плеер VK Музыки висит и на artist-страницах
// (vk.com/music-Artist), поэтому дополнительно принимаем /music-*.
function isMusicPage(url) {
  if (!isVkHost(url)) {
    return false;
  }
  const u = (url || "").toLowerCase();
  if (VK_MUSIC_HINTS.some((h) => u.includes(h))) {
    return true;
  }
  return /vk\.(com|ru)\/music[-_]/.test(u);
}

function appNameForUrl() {
  return "VK Музыка";
}

// Ключ источника для виджета. Используем id вкладки: он стабилен в
// пределах сессии Firefox и однозначно определяет вкладку.
function makeKey(tabId) {
  return "tab:" + tabId;
}

// Ключ кэша обложки: та же вкладка + тот же трек. Строка вместо кортежа,
// потому что по сети идёт только JSON.
function makeCacheKey(tabId, title, artist) {
  return [tabId, title || "", artist || ""].join("|");
}

function clamp01(value) {
  const n = Number(value);
  if (!isFinite(n)) {
    return 0;
  }
  return n < 0 ? 0 : n > 1 ? 1 : n;
}

// Приводит любое сообщение к строке ограниченной длины: в виджет уходит
// прямо в DOM, длинная строка разъедет вёрстку.
function trimText(value, max = 200) {
  const s = String(value == null ? "" : value)
    .replace(/\s+/g, " ")
    .trim();
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

const LOG_PREFIX = "[music-widget]";

// Последняя ошибка показывается в виджете, поэтому она не только пишется
// в консоль, но и хранится.
let lastError = "";

function log(...args) {
  try {
    console.log(LOG_PREFIX, ...args);
  } catch (e) {
    // Консоль может быть недоступна при выгрузке фоновой страницы.
  }
}

function setError(text) {
  lastError = trimText(text, 120);
  if (lastError) {
    log("ошибка:", lastError);
  }
}