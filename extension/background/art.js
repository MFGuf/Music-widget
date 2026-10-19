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
// Ниже этой стороны картинку видно как мыло: её показывают во всю
// карточку, и на экране получаются крупные размытые квадраты.
const ART_MIN_SIDE = 200;
// Размер, который просим у VK. Плеер просит иконку для себя, мы же
// показываем обложку во всю карточку: отсюда было мыло.
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
// Сколько раз обложки просто не было откуда взять. Отдельно от сбоев
// загрузки: счётчик один на обе причины показывал 65 неудач, из которых
// почти все были вовсе не сбоями.
let artNoUrl = 0;
// Сторона последней принятой картинки: видно, мыло это или обложка.
let artLastSide = 0;

function artError() {
  return artLastError;
}

function artLastUrl() {
  return artLastTried;
}

function artStats() {
  return { ok: artOk, fail: artFail, noUrl: artNoUrl, side: artLastSide };
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
// Но только там, где это можно. Адрес с подписью (sign=) подписан под
// конкретный размер: изменим размер - получим отказ, и обложка не загрузится
// вовсе. Такой адрес оставляем как есть.
//
// Адрес без размера менять нечем: он один на все размеры.
function biggerCoverUrl(url) {
  const text = String(url || "");
  if (/[?&]sign=/i.test(text)) {
    return text;
  }
  const sized = text.match(/\/w(\d+)(\.(?:jpe?g|png|webp))?(\?|$)/i);
  if (!sized) {
    return text;
  }
  return text.slice(0, sized.index) + "/w" + ART_WANT_SIDE + (sized[2] || ".jpg") + (sized[3] || "");
}

function artUrlFor(item) {
  const source = store.sources.get(item.tabId);
  const url = source && source.state ? source.state.coverUrl : "";
  return typeof url === "string" ? biggerCoverUrl(url) : "";
}

// Уменьшает картинку и возвращает base64 без служебного префикса.
async function encodeArt(blob) {
  return encodeBitmap(await createImageBitmap(blob));
}

function encodeBitmap(bitmap) {
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

// Варианты одной и той же картинки, от крупного к исходному.
//
// Живой адрес от VK выглядит так:
//   .../UtJtSsDa4FA.jpg?size=34x34&quality=95&sign=...&type=audio
// Тридцать четыре пикселя - это размер иконки плеера. Подпись в адресе
// считается от размера, поэтому просто подменить его нельзя: придёт
// отказ. Но у VK есть и независящие от подписи способы попросить ту же
// картинку целиком, и какой из них сработает - заранее неизвестно, ведь
// VK меняет их без предупреждения. Поэтому пробуем по очереди и берём
// ту, что реально пришла.
//
// Исходный адрес идёт последним: он точно работает, и если ничего
// лучше не нашлось - будет хоть какая-то картинка.
function coverVariants(url) {
  const text = String(url || "");
  const list = [];
  const sized = text.match(/([?&])size=\d+x\d+/i);
  if (sized) {
    // Попробуем попросить крупнее с тем же подписанным адресом.
    list.push(text.replace(/([?&])size=\d+x\d+/i, "$1size="
      + ART_WANT_SIDE + "x" + ART_WANT_SIDE));
    list.push(text.replace(/[?&]size=\d+x\d+/i, "").replace(/[?&]$/, ""));
  }
        // У vkuserphoto с подписью часто работает вариант БЕЗ sign/c_uniq_tag/type.
  // Подпись считается от размера: без неё картинка отдаётся крупнее.
  if (/vkuserphoto\.ru\/impg\//i.test(text)) {
    const noQuery = text.split("?")[0];
    if (noQuery && noQuery !== text) { list.push(noQuery); }
    let t = text;
    // убрать все query-параметры
    t = noQuery;
    if (t) list.push(t);
    // с size 600 без подписи
    if (t) { const s=t.includes("?")?"&":"?"; list.push(t+s+"size="+ART_WANT_SIDE+"x"+ART_WANT_SIDE); }
    // варианты с type
    if (t) {
      list.push(t+"?type=album&size="+ART_WANT_SIDE+"x"+ART_WANT_SIDE);
      list.push(t+"?size="+ART_WANT_SIDE+"x"+ART_WANT_SIDE+"&type=album");
      list.push(t+"?type=audio&size="+ART_WANT_SIDE+"x"+ART_WANT_SIDE);
      list.push(t+"?size="+ART_WANT_SIDE+"x"+ART_WANT_SIDE+"&type=audio");
      list.push(t+"?type=large&size="+ART_WANT_SIDE+"x"+ART_WANT_SIDE);
    }
    // попробовать разные домены/пути? иногда sun* разный, но путь тот же
  }
// Если адрес без расширения jpg/png/webp — тоже пробуем добавить size.
  if (!/\.(jpe?g|png|webp|avif)(\?|$)/i.test(text)) {
    const sep = text.includes("?") ? "&" : "?";
    list.push(text + sep + "size=" + ART_WANT_SIDE + "x" + ART_WANT_SIDE);
  }
  list.push(text);
  // Уберём дубли, сохраняя порядок (крупнее-попытки первыми).
  const seen = new Set();
  const out = [];
  for (const u of list) {
    const key = u.trim();
    if (!key) continue;
    if (!seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  return out;
}

async function fetchBlob(url) {
  const response = await fetch(url, { credentials: "include", cache: "force-cache" });
  if (!response.ok) {
    throw new Error("HTTP " + response.status);
  }
  const blob = await response.blob();
  if (!blob.size || blob.size > ART_MAX_BYTES) {
    throw new Error("подозрительный размер: " + blob.size);
  }
  return blob;
}

// Качает картинку, пробуя варианты крупнее.
//
// Ключевое здесь - измерять, а не рассчитывать. Раньше размер брался из
// адреса и на слово: если VK меняет правила, мы годами показываем мыло и
// считаем, что всё в порядке. Теперь картинка распаковывается и
// сравнивается по стороне, и как только нашлась достаточно крупная -
// остальное не качается.
async function downloadArt(url) {
  let best = null;
  let bestSide = 0;
  let lastError = null;
  for (const candidate of coverVariants(url)) {
    let bitmap = null;
    try {
      bitmap = await createImageBitmap(await fetchBlob(candidate));
      const side = Math.max(bitmap.width, bitmap.height);
      if (side > bestSide) {
        if (best && typeof best.close === "function") {
          best.close();
        }
        best = bitmap;
        bestSide = side;
        lastError = null;
      } else if (typeof bitmap.close === "function") {
        bitmap.close();
      }
    } catch (e) {
      lastError = e;
    }
    if (bestSide >= ART_MIN_SIDE) {
      break;
    }
  }
  if (!best) {
    throw lastError || new Error("не пришёл ни один вариант");
  }
  artLastSide = bestSide;
  return encodeBitmap(best);
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
    // Адреса не было - это не сбой загрузки, а другое: разбираются они
    // по-разному, и в одном счётчике обе причины сливались в одну.
    artNoUrl++;
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