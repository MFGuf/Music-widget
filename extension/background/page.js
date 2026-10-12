"use strict";

// Фоновая страница расширения. Постоянная (persistent: true), потому что
// соединение с native messaging хостом и опрос вкладок не должны прерываться.
//
// Здесь только каркас: константы протокола и логгер. Связь с хостом,
// поиск вкладок и команды добавляются следующими коммитами.

// Имя хоста должно совпадать с полем "name" в native/music_widget_host.json
// и с ключом реестра HKCU\Software\Mozilla\NativeMessagingHosts\<HOST_NAME>.
const HOST_NAME = "music_widget_host";

// Версия протокола виджет <-> расширение. Виджет отвергает сообщения
// с другой версией, чтобы старый и новый виджет не мешали друг другу.
const PROTOCOL_VERSION = 1;

const LOG_PREFIX = "[music-widget]";

// Последняя ошибка видна в виджете, поэтому лог не просто пишется в консоль,
// а копится в переменной.
let lastError = "";

function log(...args) {
  try {
    console.log(LOG_PREFIX, ...args);
  } catch (e) {
    // Консоль может быть недоступна при выгрузке фоновой страницы.
  }
}

function setError(text) {
  lastError = String(text || "");
  if (lastError) {
    log("ошибка:", lastError);
  }
}