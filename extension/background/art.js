"use strict";

// Загрузка обложек треков.
//
// Обложку качает фоновая страница расширения, а не скрипт страницы:
// у фоновой страницы права <all_urls>, поэтому запрос к CDN VK идёт без
// CORS-проблем, а результат не нужно тащить через postMessage.
//
// Дальше картинка уменьшается до 320 пикселей и кодируется: в виджет
// уходит base64 по native messaging, и полноразмерная обложка в 400 КБ
// съела бы почти весь лимит сообщения.

// По VK картинки бывают и 1000x1000, а виджет рисует их в квадрат 120px.
// Картинка не крупнее этого по стороне: этого хватает и виджету, и
// сообщениям о переключении.
const ART_MAX_SIDE = 320;
// Размер, который просим у VK. Плеер просит маленькую картинку для панели,
// мы же показываем обложку крупно: отсюда было мыло.
const ART_WANT_SIDE = 600;
const ART_JPEG_QUALITY = 0.85;

// Больше 8 МБ - это точно не обложка, а посторонний ответ.
const ART_MAX_BYTES = 8 * 1024 * 1024;

// Кэш ограничен: иначе за неделю прослушивания накопится сотня треков.
const ART_CACHE_LIMIT = 24;

// ck -> base64 без префикса data:...;base64,
const artCache = new Map();

// ck -> промис текущей загрузки. Не просто метка: повторный вызов
// updateArt() должен ДОЖДАТСЯ уже начатой загрузки, иначе он вернётся
// мгновенно и обложка в снимок не попадёт.
const artPending = new Map();

// Последняя ошибка загрузки и адрес, по которому пробовали. Без них сбой
// выглядит как "обложка просто не появилась", и гадать нечем: неизвестно ни
// что именно сломалось, ни откуда качали.
let artLastError = "";
let artLastTried = "";
let artOk = 0;
let artFail = 0;

function artError() {
  return artLastError;
}

function artLastUrl() {
  return artLastTried;
}

function artStats() {
  return { ok: artOk, fail: artFail };
}

function putArtCache(ck, data) {
  artCache.set(ck, data);
  while (artCache.size > ART_CACHE_LIMIT) {
    const oldest = artCache.keys().next();
    if (oldest.done) {
      break;
    }
    artCache.delete(oldest.value);
  }
}

// Просим у VK картинку крупнее.
//
// Размер зашит в адрес: .../AbCdEfGh/w100.jpg - это та же самая картинка
// шириной 100 пикселей. Панель плеера просит именно столько, сколько нужно
// маленькой иконке, а мы показываем обложку во всю карточку и растягиваем
// её вчетверо. Меняем размер в адресе на побольше - VK отдаст ту же
// картинку, но настоящую.
//
// Адрес без размера менять нечем: он один на все размеры.
function biggerCoverUrl(url) {
  const text = String(url || "");
  const sized = text.match(/\/w(\d+)(\.(?:jpe?g|png|webp))?$/i);
  if (!sized) {
    return text;
  }
  return text.slice(0, sized.index) + "/w" + ART_WANT_SIDE + (sized[2] || ".jpg");
}

function artUrlFor(item) {
  const source = store.sources.get(item.tabId);
  const url = source && source.state ? source.state.coverUrl : "";
  return typeof url === "string" ? biggerCoverUrl(url) : "";
}

// Уменьшает картинку и возвращает base64 без служебного префикса.
async function encodeArt(blob) {
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, ART_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0, width, height);
  if (typeof bitmap.close === "function") {
    bitmap.close();
  }

  // Firefox умеет webp, но если вдруг нет - откатываемся на jpeg.
  for (const type of ["image/webp", "image/jpeg"]) {
    const dataUrl = canvas.toDataURL(type, ART_JPEG_QUALITY);
    const comma = dataUrl.indexOf(",");
    if (comma > 0 && dataUrl.length > comma + 32) {
      return dataUrl.slice(comma + 1);
    }
  }
  return "";
}

async function downloadArt(url) {
  const response = await fetch(url, { credentials: "include", cache: "force-cache" });
  if (!response.ok) {
    throw new Error("HTTP " + response.status);
  }
  const blob = await response.blob();
  if (!blob.size || blob.size > ART_MAX_BYTES) {
    throw new Error("подозрительный размер: " + blob.size);
  }
  return encodeArt(blob);
}

// Дописывает обложку в готовый список источников. Вызывается после
// rebuildItems(), а увидят её виджет и в этом же, и в следующем снимке.
async function fillArt(item) {
  if (item.art) {
    return;
  }

  const cached = artCache.get(item.ck);
  if (cached) {
    item.art = cached;
    item.art_new = false;
    return;
  }

  // Загрузка уже идёт - ждём её, второй запрос не нужен.
  const running = artPending.get(item.ck);
  if (running) {
    const data = await running;
    if (data) {
      item.art = data;
      item.art_new = true;
    }
    return;
  }

  const url = artUrlFor(item);
  if (!url) {
    artFail++;
    artLastError = "обложка не найдена в ответе VK";
    return;
  }
  artLastTried = url;

  const task = downloadArt(url)
    .then((data) => {
      if (data) {
        putArtCache(item.ck, data);
        artOk++;
        artLastError = "";
      } else {
        artFail++;
      }
      return data || "";
    })
    .catch((e) => {
      // Обложка - необязательная часть: сбой молча оставляем, плеер
      // продолжает работать, а виджет покажет заглушку. Причину же
      // отдаём в диагностику, иначе чинить пришлось бы наугад.
      artFail++;
      artLastError = String((e && e.message) || e);
      log("обложка не загрузилась:", artLastError);
      return "";
    })
    .then((data) => {
      artPending.delete(item.ck);
      return data;
    });

  artPending.set(item.ck, task);
  const data = await task;
  if (data) {
    item.art = data;
    item.art_new = true;
  }
}

function updateArt() {
  const items = store.lastItems;
  if (!items.length) {
    return Promise.resolve();
  }
  return Promise.all(items.map(fillArt)).then(() => undefined);
}