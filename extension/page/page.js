"use strict";

// Скрипт ГЛАВНОГО мира страницы VK (main world).
//
// Зачем он нужен: Firefox не даёт расширению доступ к JS-объектам страницы.
// Content script видит только DOM, но не может подменить HTMLMediaElement
// или AudioContext. Поэтому этот файл вставляется в страницу обычным
// тегом <script src="moz-extension://.../page/page.js">, выполняется в её
// собственном мире и общается с content script через window.postMessage.
//
// Он внедряется на document_start - это принципиально: хуки
// HTMLMediaElement.play/pause должны быть поставлены до того, как плеер
// VK их вызовет.
//
// Обмен с content script:
//   toPage     { __mw:1, dir:"toPage",  id, token, t:"getState" | "cmd" }
//   toContent  { __mw:1, dir:"toContent", id, token, payload }
//
// token - случайная строка, которую content script сообщает при инициализации.
// Она защищает от чужих сообщений: скрипты самой страницы VK о ней не знают.

(function () {
  const MSG_VERSION = 1;

  // Токен приходит от content script при инициализации. До него скрипт
  // молчит: иначе любой скрипт страницы мог бы слать нам сообщения.
  let token = null;

  // --- хуки воспроизведения -------------------------------------------------

  // Firefox не поддерживает navigator.mediaSession, поэтому единственный
  // надёжный признак "играет" - перехват play()/pause() на прототипе.
  const playback = {
    playing: false,
    playCount: 0,
    hooked: false,
    // Элемент, на котором вызвали play(). Плеер VK держит звук вне
    // страницы, и иначе до него не добраться.
    element: null,
  };

  function hookMediaPrototypes() {
    if (playback.hooked) {
      return;
    }
    const proto = window.HTMLMediaElement && HTMLMediaElement.prototype;
    if (!proto) {
      return;
    }
    const origPlay = proto.play;
    const origPause = proto.pause;
    proto.play = function () {
      playback.playing = true;
      playback.playCount++;
      // Плеер VK создаёт звук через new Audio() и никогда не вставляет
      // элемент в страницу, поэтому querySelectorAll его не видит. Здесь
      // мы получаем его из хука - единственного места, где он доступен.
      // Без этого у нас не было ни длительности, ни позиции.
      rememberPlayingElement(this);
      return origPlay.apply(this, arguments);
    };
    proto.pause = function () {
      playback.playing = false;
      return origPause.apply(this, arguments);
    };
    // currentTime намеренно НЕ перехватывается: позиция и так читается
    // напрямую, а подмена свойства на прототипе лишний раз конфликтует
    // с плеером страницы.
    playback.hooked = true;
  }

// Хуки ставятся в самом конце файла: состояние, к которому они обращаются,
  // объявлено через const/let выше, и обращение выше объявления дало бы
  // ReferenceError. Для document_start это не страшно - весь скрипт
  // выполняется синхронно до любого кода страницы.
  //
  // Перехват ответов VK API -----------------------------------------------

  // В DOM у VK Музыки длительности трека нет вообще, а navigator.mediaSession
  // в Firefox отсутствует. Точное значение лежит в ответе API, который
  // плеер запрашивает при смене трека, поэтому перехватываем fetch и XHR.
  //
  // Перехват сделан здесь, а не через webRequest расширения: скрипт и так
  // живёт в главном мире страницы до загрузки её собственных скриптов,
  // поэтому ловит запросы гарантированно и не требует лишних прав.
  // Состояние перехвата живёт на window, а не в замыкании: после обновления
  // расширения в странице остаётся прежняя копия скрипта со своими хуками.
  // Если бы новая копия навесила fetch поверх старого, ответы разбирались
  // бы дважды на каждый вызов. Общее состояние даёт один хук на страницу.
  // Хук держит ссылку на absorbApiText первой копии, но метаданные лежат в
  // общем объекте - поэтому найденное ею не теряется после обновления.
const apiState = window.__musicWidgetApiState ||
    (window.__musicWidgetApiState = {
      meta: new Map(),
      hooked: false,
      current: null,
      hits: 0,
      lastUrls: [],
      seenAudio: false,
      seenDuration: false,
      // Подписка на сообщения вешается один раз на страницу: скрипт
      // главного мира вставляется и манифестом, и дозагрузкой в открытые
      // вкладки, а вторая копия выполняла бы каждую команду дважды.
      listening: false,
      // Последний трек, опознанный надёжно: по API или по панели плеера.
      // Нужен, чтобы пауза не отбрасывала виджет на заголовок раздела.
      resolved: null,
    });
const apiMeta = apiState.meta;

  // Ответы больших плейлистов весят мегабайты - такие разбирать незачем.
  const MAX_API_TEXT = 4 * 1024 * 1024;

  function metaKey(artist, title) {
    return String(artist || "").trim().toLowerCase() + "\u0000" +
           String(title || "").trim().toLowerCase();
  }

  // Какие поля на самом деле присылает VK в треках.
//
// Мы ищем обложку по семи именам полей и ни одну не нашли на 211 попытках.
// Значит имена другие - а угадывать их дальше бессмысленно. Собираем все
// имена, что встречаются, и показываем в диагностике: по этому списку
// видно, чем на самом деле оформлена обложка.
const MAX_SEEN_FIELDS = 40;

  function rememberFields(node) {
    const fields = apiState.fields || (apiState.fields = []);
    for (const key of Object.keys(node)) {
      if (fields.indexOf(key) < 0) {
        fields.push(key);
      }
    }
    while (fields.length > MAX_SEEN_FIELDS) {
      fields.shift();
    }
  }

  // Что вообще считать адресом картинки.
  //
  // Прежняя проверка требовала и https://, и расширения .jpg. Таких
  // обложек в ответе не нашлось ни одной за 211 попыток: VK отдаёт адреса
  // и без расширения, и относительные ("/images/..."), а сам домен
  // картинок - userapi.com - расширением не заканчивается.
  function isImageUrl(value) {
    if (typeof value !== "string" || !value) {
      return false;
    }
    const text = value.trim();
    if (!/^(https?:)?\/\//i.test(text) && text.charAt(0) !== "/") {
      return false;
    }
    return /\.(jpe?g|png|webp|gif)(\?|$)/i.test(text)
      || /userapi\.com/i.test(text)
      || /\/(thumb|cover|album_thumb|image)/i.test(text);
  }

  // Относительный адрес VK открывать на полном.
  function absoluteUrl(value) {
    const text = String(value || "").trim();
    if (!text) {
      return "";
    }
    return /^https?:\/\//i.test(text) ? text
      : text.charAt(0) === "/" ? "https://vk.com" + text : "";
  }

  // Обход ответа вглубь: обложка лежит не только в самом треке, но и
  // рядом - в owner.photo у каждого трека повторяется, и она же годна
  // как обложка, если отдельной у трека не нашлось.
  function findImageDeep(value, depth) {
    if (depth > 3 || value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return isImageUrl(value) ? absoluteUrl(value) : "";
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = findImageDeep(item, depth + 1);
        if (found) {
          return found;
        }
      }
      return "";
    }
    if (typeof value !== "object") {
      return "";
    }
    const keys = Object.keys(value);
    // Сначала поля, в имени которых есть смысл картинки: так обложка
    // находится быстрее и не зависит от её вложенности.
    for (const key of keys) {
      if (/cover|thumb|photo|image|img|pic/i.test(key)) {
        const found = findImageDeep(value[key], depth + 1);
        if (found) {
          return found;
        }
      }
    }
    for (const key of keys) {
      const found = findImageDeep(value[key], depth + 1);
      if (found) {
        return found;
      }
    }
    return "";
  }

  function pickCoverUrl(node) {
    // Сначала поля, которые точно обложка: они короткие и встречаются
    // чаще всего. Список неполон - VK отдаёт и другие имена, и вложенные
    // массивы размеров, - поэтому дальше ищем по всей разметке ответа.
    const fields = [
      "thumb_src", "thumb", "thumb_200", "thumb_100",
      "cover", "album_thumb", "artist_photo",
    ];
    for (const field of fields) {
      const value = node[field];
      if (isImageUrl(value)) {
        return absoluteUrl(value);
      }
    }

    // Вложенные размеры: sizes: [{ url: "...", w: 200, h: 200 }].
    const sizes = node.sizes;
    if (Array.isArray(sizes)) {
      for (const item of sizes) {
        if (item && isImageUrl(item.url)) {
          return absoluteUrl(item.url);
        }
      }
    }

    // Общий случай: обложка может лежать в любом поле ответа и на любой
    // глубине - у трека её может не быть вовсе, зато рядом, в owner.photo,
    // лежит картинка сообщества, и она же годится как обложка.
    return findImageDeep(node, 0);
  }

  // Доступ к разбору обложек нужен проверкам: иначе единственный способ
// убедиться, что адрес распознан, - это дождаться, пока VK его пришлёт.
window.__musicWidgetCover = {
  pick: pickCoverUrl,
  signature: coverSignature,
};

  // Устойчивый признак одной и той же картинки.
  //
  // Одну и ту же обложку VK отдаёт по разным адресам разного размера:
  // .../AbCdEf/w100.jpg и .../AbCdEf/w300.jpg. Сравнивать адреса целиком
  // бессмысленно, а вот часть пути до размера - это и есть сама обложка.
  function coverSignature(url) {
    const text = String(url || "");
    const sized = text.match(/\/([A-Za-z0-9_-]{6,})\/w\d+/i);
    if (sized) {
      return sized[1].toLowerCase();
    }
    const withExt = text.match(/\/([A-Za-z0-9_-]{6,})\.(jpe?g|png|webp)/i);
    if (withExt) {
      return withExt[1].toLowerCase();
    }
    // Адрес может прийти без расширения - тогда обложку узнаёт последний
    // кусок пути. Служебные части вроде "w100" отбрасываем.
    const parts = text.split("?")[0].split("/").filter(Boolean);
    for (let i = parts.length - 1; i >= 0; i--) {
      const part = parts[i].replace(/\.(jpe?g|png|webp|gif)$/i, "");
      if (part.length >= 6 && !/^w\d+$/i.test(part) && part.indexOf(".") < 0) {
        return part.toLowerCase();
      }
    }
    return "";
  }

  // Текущий трек по данным API.
  //
  // Разбирать вёрстку VK по селекторам ненадёжно: разметка меняется без
  // предупреждения, и на странице плейлиста селектор названия трека
  // попадает в заголовок раздела - виджет показывал «Моя музыка».
  // Ответы API куда надёжнее: когда начинает играть трек, VK запрашивает
  // его отдельно, и там лежат название, исполнитель, точная длительность
  // и обложка.
  const CURRENT_TTL_MS = 60 * 1000;

  function rememberTrack(artist, title, dur, coverUrl, asCurrent, id) {
    if (!title || !isFinite(dur) || dur <= 0) {
      return;
    }
    if (asCurrent) {
      apiState.current = {
        artist: artist || "",
        title: title,
        dur: dur,
        coverUrl: coverUrl || "",
        at: Date.now(),
      };
    }
    const key = metaKey(artist, title);
    const previous = apiMeta.get(key);
    // Один и тот же трек встречается в плейлисте десятки раз; обновляем
    // запись только если данные изменились. Метку времени обновляем всегда:
    // по ней среди совпавших по длительности треков выбирается самый
    // свежий из остальных.
    if (!previous || previous.dur !== dur || previous.coverUrl !== coverUrl) {
      if (apiMeta.size > 200) {
        const oldest = apiMeta.keys().next();
        if (!oldest.done) {
          apiMeta.delete(oldest.value);
        }
      }
    }
    apiMeta.set(key, {
      dur: dur,
      coverUrl: coverUrl || (previous ? previous.coverUrl : ""),
      title: title,
      artist: artist || "",
      id: id || (previous ? previous.id : 0),
      at: Date.now(),
    });
  }

  // Приводим название к сравнимому виду: разные пробелы, регистр и
  // хвост вида " - Исполнитель" не должны мешать узнать один и тот же трек.
  function normTitle(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/\s*[-–—]\s*[^-–—]+$/, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function sameTrack(meta, wanted) {
    const a = normTitle(meta.title);
    const b = normTitle(wanted);
    if (!a || !b) {
      return false;
    }
    return a === b || a.indexOf(b) >= 0 || b.indexOf(a) >= 0;
  }

  // По длительности. Длительность у трека одна на страницу, но у разных
  // песен она иногда совпадает - ремиксы, записи концерта. В плейлисте на
  // сто песен совпадений достаточно, чтобы выбрать чужую песню.
  //
  // Поэтому сначала пробуем сузить выборку названием, которое сейчас
  // показано в панели плеера: из всех треков с такой длительностью берём
  // тот, чьё название туда похоже.
  function trackByDuration(dur, wanted) {
    if (!dur || !isFinite(dur) || dur <= 0) {
      return null;
    }
    let byName = null;
    let closest = null;
    let closestGap = Infinity;
    for (const meta of apiMeta.values()) {
      // Полсекунды - щедрый запас: длительности приходят целыми, но
      // округление в разных местах иногда отличается на доли секунды.
      const gap = Math.abs(meta.dur - dur);
      if (gap > 0.75) {
        continue;
      }
      // Из совпавших берём БЛИЖАЙШИЙ по времени, а не самый свежий.
      // Просто свежий брал последний трек в списке, а он у пользователя
      // оказывался тем, что на секунду короче играющего, - и виджет
      // показывал чужое название.
      if (!closest || gap < closestGap) {
        closest = meta;
        closestGap = gap;
      }
      if (!byName && sameTrack(meta, wanted)) {
        byName = meta;
      }
    }
    return byName || closest;
  }

  // По обложке. Надёжнее длительности: у разных песен длительность иногда
  // совпадает (ремиксы, записи концерта), а обложка у каждой своя.
  function trackByCover(signature) {
    if (!signature) {
      return null;
    }
    let best = null;
    for (const meta of apiMeta.values()) {
      if (coverSignature(meta.coverUrl) !== signature) {
        continue;
      }
      if (!best || meta.at > best.at) {
        best = meta;
      }
    }
    return best;
  }

  // То же самое, но по идентификатору из адреса звука, когда он там есть.
  // Надёжнее длительности, но встречается не всегда.
  function trackById(src) {
    if (!src) {
      return null;
    }
    const ids = String(src).match(/\d{3,}/g);
    if (!ids || !ids.length) {
      return null;
    }
    let best = null;
    for (const meta of apiMeta.values()) {
      if (!meta.id) {
        continue;
      }
      const id = String(meta.id);
      // Ищем именно число целиком: простое "вхождение" давало ложные
      // совпадения на коротких номерах вроде 42 внутри 1420.
      for (const token of ids) {
        if (token === id) {
          if (!best || meta.at > best.at) {
            best = meta;
          }
          break;
        }
      }
    }
    return best;
  }

  // Точный идентификатор трека из самой разметки.
  //
  // Плеер VK держит ссылку на играющий трек в строке плейлиста: адрес
  // вида /audio-2000_1234567. Если медиаэлемент лежит в этой строке,
  // идентификатор достаётся прямо из неё, и это единственный способ узнать
  // трек ТОЧНО. Всё остальное - сравнения по времени и обложке, они
  // ошибаются, когда рядом лежат похожие песни.
  function trackIdFromRow(media) {
    if (!media || !media.isConnected) {
      return 0;
    }
    let node = media;
    for (let step = 0; step < 7 && node; step++) {
      if (node.getAttribute) {
        const own = node.getAttribute("data-audio-id") || node.getAttribute("data-id");
        if (own && /^\d+$/.test(own.trim())) {
          return Number(own.trim());
        }
      }
      const link = node.querySelector
        && node.querySelector("a[href*='audio-'], a[href*='/audio']");
      const href = link ? String(link.getAttribute("href") || "") : "";
      const match = href.match(/audio-?\d*_(\d+)/) || href.match(/[?&]id=(\d+)/);
      if (match) {
        return Number(match[1]);
      }
      node = node.parentElement;
    }
    return 0;
  }

  function trackByRowId(media) {
    const id = trackIdFromRow(media);
    if (!id) {
      return null;
    }
    for (const meta of apiMeta.values()) {
      if (meta.id && String(meta.id) === String(id)) {
        return meta;
      }
    }
    return null;
  }

  // Ответы VK бывают вложенными (execute.multiple), поэтому обходим дерево.
// asCurrent - «это ответ про один конкретный трек», а не список плейлиста.
  function absorbApiJson(node, depth, asCurrent) {
    if (!node || typeof node !== "object" || depth > 8) {
      return;
    }
    if (Array.isArray(node)) {
      for (const item of node) {
        absorbApiJson(item, depth + 1, false);
      }
      return;
    }
    const title = typeof node.title === "string" ? node.title : "";
    const artist = typeof node.artist === "string" && node.artist
      ? node.artist
      : (typeof node.owner_name === "string" ? node.owner_name : "");
    const dur = Number(node.duration);
    if (title && isFinite(dur) && dur > 0) {
      rememberFields(node);
      rememberTrack(artist, title, dur, pickCoverUrl(node), asCurrent,
                    node.id || node.audio_id || node.audioId || 0);
    }
    for (const key of Object.keys(node)) {
      absorbApiJson(node[key], depth + 1, false);
    }
  }

// Запрос ровно одного трека. Ответ на список плейлиста текущим треком не
// станет: его items - массив, и absorbApiJson помечает элементы как
// "не текущие". Поэтому достаточно узнать сам метод, а не разбирать адрес.
function isSingleTrackRequest(url) {
    return /audio\.get\b/i.test(String(url || ""));
  }

  // Имя метода VK прячет в теле запроса: у execute это act=... или code=...,
  // а в адресе остаётся только /method/execute. Без тела мы такой вызов
  // не узнаём и решаем, что "текущего" трека не было.
  function requestBody(init) {
    try {
      if (!init || typeof init !== "object") {
        return "";
      }
      if (typeof init.body === "string") {
        return init.body.substring(0, 2000);
      }
    } catch (e) {
      // Тело бывает FormData и прочим - это не наш случай, молча пропускаем.
    }
    return "";
  }

  function absorbApiText(text, url, body) {
    if (!text || text.length > MAX_API_TEXT) {
      return;
    }
    // Дешёвый отсев: почти все ответы VK не содержат длительностей.
    if (text.indexOf("\"duration\"") < 0) {
      return;
    }
    let json;
    try {
      json = JSON.parse(text);
    } catch (e) {
      return;
    }

    // Ответ про ОДИН трек узнаётся по форме, а не по имени метода: в нём
    // лежит объект с названием и длительностью, а не массив items. Так
    // правило работает независимо от того, как VK назовёт запрос сегодня:
    // замерялось audio.get, а приходит execute с act=do_play.
    const byName = isSingleTrackRequest(url) || isSingleTrackRequest(body);
    const response = json && json.response;
    const byShape = !!response && typeof response === "object"
      && !Array.isArray(response)
      && typeof response.title === "string" && response.title
      && Number(response.duration) > 0;
    const single = byName || byShape;

    absorbApiJson(json, 0, single);
    if (single && response && typeof response === "object") {
      absorbApiJson(response, 0, true);
    }
  }

  // Ответы VK приходят не только с api.vk.com/method: тот же плеер ходит на
// vk.ru/method, на /al_... и на /api/.... Узкий список адресов приводил к
// тому, что мы не видели ни одного ответа и гадали, где ошибка.
function looksLikeApi(url) {
    const u = String(url || "");
    if (/\/\/api\.vk\.(com|ru)\//i.test(u)) {
      return true;
    }
    return /vk\.(com|ru)\/(method\/|al_|api\b)/i.test(u);
  }

  // Счётчик перехваченных ответов и последние адреса. Нужны для диагностики:
  // без них не отличить "VK не отдаёт трек в API" от "мы его не видим",
  // а чинить это приходится вслепую.
  function noteApiHit(url) {
    apiState.hits += 1;
    apiState.lastUrls.push(String(url).substring(0, 120));
    if (apiState.lastUrls.length > 5) {
      apiState.lastUrls.shift();
    }
  }

  function requestUrl(input) {
    try {
      if (typeof input === "string") {
        return input;
      }
      if (input && typeof input.url === "string") {
        return input.url;
      }
      return String(input);
    } catch (e) {
      return "";
    }
  }

  function hookNetwork() {
    if (apiState.hooked) {
      return;
    }
    apiState.hooked = true;

    const origFetch = window.fetch;
    if (typeof origFetch === "function") {
      window.fetch = function () {
        const args = arguments;
        const url = requestUrl(args[0]);
        if (!looksLikeApi(url)) {
          return origFetch.apply(this, args);
        }
        const body = requestBody(args[1]);
        noteApiHit(url);
        return origFetch.apply(this, args).then(function (response) {
          try {
            // Клонируем ДО чтения: оригинал должен остаться нетронутым
            // для самой страницы.
            const copy = response.clone();
            copy.text().then(function (text) {
              absorbApiText(text, url, body);
            }, function () {});
          } catch (e) {
            // Тело уже прочитано страницей - просто пропускаем.
          }
          return response;
        });
      };
    }

    const XHRProto = window.XMLHttpRequest && XMLHttpRequest.prototype;
    if (XHRProto) {
      const origOpen = XHRProto.open;
      const origSend = XHRProto.send;
      XHRProto.open = function (method, url) {
        this.__mwUrl = String(url || "");
        return origOpen.apply(this, arguments);
      };
      XHRProto.send = function () {
        const self = this;
        const body = requestBody(arguments[0]);
        if (looksLikeApi(self.__mwUrl)) {
          noteApiHit(self.__mwUrl);
          self.addEventListener("load", function () {
            try {
              if (self.responseType === "" || self.responseType === "text") {
                absorbApiText(self.responseText, self.__mwUrl, body);
              }
            } catch (e) {
              // responseText недоступен для не-текстовых ответов.
            }
          });
        }
        return origSend.apply(this, arguments);
      };
    }
  }

  function durationFor(artist, title) {
    const found = apiMeta.get(metaKey(artist, title));
    return found ? found : null;
  }

  // --- поиск элементов плеера ------------------------------------------------

  function classText(el) {
    const c = el && el.className;
    if (!c) {
      return "";
    }
    return typeof c === "string" ? c : (c.baseVal || "");
  }

  // Короткий осмысленный текст из элемента. Длинные строки игнорируем:
  // это почти всегда контейнер со всем списком треков.
  function shortText(el, max) {
    const text = ((el && el.textContent) || "").replace(/\s+/g, " ").trim();
    if (!text || text.length > (max || 120)) {
      return "";
    }
    return text;
  }

  // Первый непустой текст по списку селекторов. Селекторы идут от более
  // точных к более общим: VK периодически переименовывает классы, и старые
  // имена встречаются в загруженных страницах дольше новых.
  //
  // maxChildren отсекает контейнеры. Элемент с потомками почти всегда
  // обёртка, а текст такой обёртки - это смесь названия и исполнителя:
  // без проверки виджет показал бы вместо названия трека имя артиста.
  function pickText(selectors, max) {
    let best = "";
    let bestChildren = 99;
    for (const sel of selectors) {
      let nodes;
      try {
        nodes = document.querySelectorAll(sel);
      } catch (e) {
        continue;
      }
      for (const el of nodes) {
        const text = shortText(el, max);
        if (!text) {
          continue;
        }
        const children = el.children.length;
        if (children >= 2) {
          continue;
        }
        if (children < bestChildren) {
          best = text;
          bestChildren = children;
          if (children === 0) {
            return best;
          }
        }
      }
    }
    return best;
  }

  // Общие селекторы в конце списков намеренно требуют, чтобы в имени
  // класса были ОБА слова - "track" и "title"/"art". Раньше здесь стоял
  // селектор [class*='track-name'], который попадал и в контейнер с
  // названием, и в контейнер с исполнителем одновременно.
  const TITLE_SELECTORS = [
    "[data-test-id='track-title']",
    ".track-name__title",
    "[class*='track-name__title']",
    "[class*='TrackNameTitle']",
    "[class*='track__title']",
    "[class*='player__track-name']",
    "[class*='track'][class*='title']",
    "[class*='title'][class*='track']",
  ];

  const ARTIST_SELECTORS = [
    "[data-test-id='track-artist']",
    ".track-name__art",
    "[class*='track-name__art']",
    "[class*='TrackNameArtists']",
    "[class*='track__artists']",
    "[class*='player__track-artist']",
    "[class*='track'][class*='art' i]",
  ];

  const COVER_SELECTORS = [
    "[class*='player__cover'] img",
    "[class*='track-cover'] img",
    "[class*='Cover'] img",
    "img[class*='cover']",
    "[class*='Artwork'] img",
  ];

  // Заглушки, которые VK показывает вместо названия, когда ничего не играет.
  const PLACEHOLDER = /^(ничего не играет|nothing playing|выберите трек|нет трека|muziek|нет музыки)$/i;

  // Название вкладки VK выглядит как "Название — Исполнитель | VK Музыка",
  // поэтому режем по разделителям.
  function titleFromDocumentTitle() {
    const raw = String(document.title || "")
      .split("|")[0]
      .split("—")[0]
      .replace(/\s+/g, " ")
      .trim();
    if (!raw || raw.length > 120 || PLACEHOLDER.test(raw)) {
      return "";
    }
    return raw;
  }

  // Что сейчас показано в панели плеера: название играющего трека из
  // разметки. Само по себе оно ненадёжно - на странице плейлиста это может
  // оказаться заголовок раздела, - но как признак для разведения
  // совпадающих длительностей годится.
  function panelTrackTitle() {
    const panel = titleFromPlayerPanel();
    if (panel) {
      return panel;
    }
    return pickText(TITLE_SELECTORS);
  }

  // Снимок записи кэша в том же виде, что и current.
  function fromMeta(meta) {
    return {
      artist: meta.artist || "",
      title: meta.title,
      dur: meta.dur,
      coverUrl: meta.coverUrl || "",
      at: meta.at,
    };
  }

  // Медиаэлемента может не быть вовсе: на паузе звук пропадает. Способ
// опознания тогда не проверялся, и в диагностике оставалось значение от
// прошлого вызова - виджет сообщал об опознании по длительности, хотя
// в тот раз не опознал ничего. Вводит в заблуждение ровно тогда, когда
// нужен настоящий ответ.
function currentTrack(media) {
    apiState.matchedBy = "";
    const now = Date.now();
    const current = apiState.current;
    const fresh = current && current.title && now - current.at <= CURRENT_TTL_MS;

    // Порядок - сначала свидетельства о том, что играет СЕЙЧАС, и лишь
    // потом ответ VK.
    //
    // Раньше было наоборот, и это путало песни длиной в минуты: одиночный
    // запрос VK бывает и про СЛЕДУЮЩИЙ трек - VK готовит его заранее, -
    // и такой ответ висел на виджете, пока отвечал за играющий. Две песни
    // менялись местами, как только VK догружал следующую. Сам звук о том,
    // что играет сейчас, не врёт, поэтому он и главнее.
    if (media) {
      const src = media.currentSrc || media.src || "";
      // Ссылка на строку плейлиста - самое точное, что есть: она указывает
      // на конкретный трек, а не на похожий по времени.
      const byRow = trackByRowId(media);
      if (byRow) {
        apiState.current = null;
        apiState.matchedBy = "ссылка на трек";
        return fromMeta(byRow);
      }
      // Порядок - от самого верного к наименее верному. Обложка идёт перед
      // длительностью: у разных песен длительность совпадает иногда, а
      // обложка у каждой своя.
      const byId = trackById(src);
      if (byId) {
        apiState.current = null;
        apiState.matchedBy = "id";
        return fromMeta(byId);
      }
      const signature = coverSignature(panelCoverUrl());
      const byCover = trackByCover(signature);
      if (byCover) {
        apiState.current = null;
        apiState.matchedBy = "обложка";
        return fromMeta(byCover);
      }
      const dur = Number(media.duration);
      if (isFinite(dur) && dur > 0) {
        // Название берём из панели плеера: оно, хоть и из разметки, зато
        // принадлежит играющему треку, и им мы разводим совпадающие
        // длительности.
        const byDur = trackByDuration(dur, panelTrackTitle());
        if (byDur) {
          // Ответ VK с этим не сходится - значит он про другой трек, и
          // держать его дальше незачем: он снова вылез бы, когда по звуку
          // опознать не удастся.
          if (fresh && Math.abs(current.dur - dur) > 1) {
            apiState.current = null;
          }
          apiState.matchedBy = sameTrack(byDur, panelTrackTitle())
            ? "длительность и название"
            : "длительность";
          return fromMeta(byDur);
        }
      }
    }

    // Звук опознать не вышло, но ответ VK ещё годен - он и сгодится.
    if (fresh) {
      apiState.matchedBy = "ответ VK";
      return current;
    }

    apiState.matchedBy = "";
    return null;
  }

  // Обложка, нарисованная в панели плеера. Именно её видит человек,
  // значит именно она соответствует играющему треку.
  function panelCoverUrl() {
    const panel = playerPanel();
    if (!panel) {
      return "";
    }
    const images = panel.querySelectorAll(
      "img,[class*='cover'],[class*='thumb'],[class*='art']," +
      "[class*='image'],[style*='background']");
    for (const el of images) {
      const src = el.currentSrc || el.src
        || el.getAttribute("src") || el.getAttribute("data-src") || "";
      if (isImageUrl(src)) {
        return src;
      }
      const style = el.getAttribute && el.getAttribute("style");
      const url = style && String(style).match(/url\((['"]?)([^)'"]+)\1\)/);
      if (url && isImageUrl(url[2])) {
        return url[2];
      }
    }
    return "";
  }

  // Название из панели годится не всегда.
//
// Рядом с кнопкой VK показывает и название плейлиста: на странице «Моя
// музыка» это «Hip Hop All Stars 2012», и в виджет оно попадало вместо
// песни. Отличить плейлист от трека можно по данным, которые уже есть: в
// кэше лежат настоящие названия треков, и если панельного среди них нет -
// значит это не песня.
//
// Если кэш пуст, отвергнуть нечем: тогда верим панели, как и раньше.
function panelTitleLooksLikeTrack(text) {
    const title = String(text || "").trim();
    if (!title) {
      return false;
    }
    if (!apiMeta.size) {
      return true;
    }
    for (const meta of apiMeta.values()) {
      if (sameTrack(meta, title)) {
        return true;
      }
    }
    return false;
  }

  function readTrackText(media) {
    // Разметка VK меняется без предупреждения, и на странице плейлиста
    // селектор названия трека попадает в заголовок раздела - виджет
    // показывал «Моя музыка». Данные API надёжнее, но и они не должны
    // забивать разметку, когда та попала в точку: если заголовок со
    // страницы известен как настоящий трек, доверяем ему.
    let title = pickText(TITLE_SELECTORS);
    if (!title || PLACEHOLDER.test(title)) {
      title = titleFromDocumentTitle();
    }
    const artist = pickText(ARTIST_SELECTORS);
    const current = currentTrack(media);

    if (current && !durationFor(artist, title)) {
      return { title: current.title, artist: current.artist || "", fromApi: true };
    }

    // API не помог: такое бывает на «Моя музыке», где отдельного запроса
    // на играющий трек не приходит. Тогда смотрим панель плеера - играющий
    // трек нарисован рядом с кнопкой, и это единственное место, где он есть
    // в разметке надёжно.
    //
    // Включаем этот способ только когда обычный поиск ничего не дал или дал
    // заголовок раздела. Иначе он мешал бы работать разметке там, где она
    // ещё в порядке.
    // Сравнение строгое, без "входит в": иначе заголовок "Название" считали бы
  // мы самим названием трека "Название трека" и полезли бы искать панель
  // там, где разметка в порядке. Равенство - достаточно: на «Моя музыке»
  // в заголовок раздела попадает ровно то же самое, что и в подпись вкладки.
  function sameText(a, b) {
    const x = String(a || "").trim().toLowerCase();
    const y = String(b || "").trim().toLowerCase();
    return x !== "" && x === y;
  }

    const heading = sectionHeading();
    const titleIsUseless = !title || PLACEHOLDER.test(title)
      || sameText(title, heading) || sameText(title, titleFromDocumentTitle());
    const panelTitle = titleIsUseless ? titleFromPlayerPanel() : "";
    if (panelTitle && panelTitleLooksLikeTrack(panelTitle)) {
      return {
        title: panelTitle,
        artist: pickText(ARTIST_SELECTORS),
        fromApi: false,
        fromPanel: true,
      };
    }
    // Панель назвала не песню, а плейлист. Показывать это вместо названия
    // хуже, чем не показать ничего: пустое место хотя бы честно молчит.
    apiState.panelTitleRejected = panelTitle || "";

    // На паузе звук пропадает, и опознать трек уже нечем: медиаэлемента нет,
    // одиночного запроса не будет, и в панели может быть пусто. Но показать
    // заголовок раздела вместо того, что играло секунду назад, - хуже всего.
    // Поэтому держим последний надёжно опознанный трек.
    //
    // Только когда разметка бесполезна: если страница сама называет трек,
    // верьем ей, а не памяти.
    const resolved = apiState.resolved;
    if (titleIsUseless && resolved && Date.now() - resolved.at <= CURRENT_TTL_MS) {
      return {
        title: resolved.title,
        artist: resolved.artist || "",
        fromApi: false,
        fromPanel: true,
        remembered: true,
      };
    }

    return { title: title, artist: artist, fromApi: false, fromPanel: false };
  }

  // Разбор панели плеера: что в ней есть на самом деле.
//
// Селекторы VK приходится угадывать, и каждая догадка стоит пользователю
// круг. Здесь видно, что панель содержит на самом деле: имена узлов, их
// текст и адреса. По этому списку следующий шаг делается без угадывания.
function panelDump() {
    const panel = playerPanel();
    if (!panel) {
      return "";
    }
    const parts = [];
    for (const el of panel.querySelectorAll("*")) {
      if (parts.length >= 14) {
        break;
      }
      const cls = typeof el.className === "string"
        ? el.className.trim().split(/\s+/).slice(0, 2).join(".")
        : "";
      const name = el.tagName.toLowerCase() + (cls ? "." + cls : "");
      const text = shortText(el, 40);
      const href = el.getAttribute && el.getAttribute("href");
      parts.push(name
        + (text ? " «" + text + "»" : "")
        + (href ? " href=" + href : "")
        + (el.getAttribute && el.getAttribute("data-id")
          ? " data-id=" + el.getAttribute("data-id") : ""));
    }
    return parts.join(" | ");
  }

  // Что вокруг кнопки игры - цепочкой вверх.
//
// Селекторы VK приходилось угадывать, и каждая догадка стоила пользователю
// круг: панель искалась по одним правилам и на живой странице оказывалась
// не той. Разбор самой панели был пуст именно поэтому. Цепочка вверх от
// кнопки всегда не пуста и показывает, где на самом деле лежит название и
// где картинка.
function playButtonDump() {
    const button = findPlayButton();
    if (!button) {
      return "кнопки игры нет";
    }
    const parts = [];
    let node = button;
    for (let step = 0; step < 7 && node; step++) {
      const cls = typeof node.className === "string"
        ? node.className.trim().split(/\s+/).slice(0, 3).join(".")
        : "";
      const name = node.tagName.toLowerCase()
        + (node.id ? "#" + node.id : "")
        + (cls ? "." + cls : "");
      const text = shortText(node, 60);
      const pics = node.querySelectorAll
        ? node.querySelectorAll("img,[style*='background'],[class*='art'],[class*='cover']").length
        : 0;
      parts.push(name
        + (text ? " «" + text + "»" : "")
        + (pics ? " картинок=" + pics : "")
        + (node.getAttribute && node.getAttribute("data-audio-id")
          ? " data-audio-id=" + node.getAttribute("data-audio-id") : ""));
      node = node.parentElement;
    }
    return parts.join(" | ");
  }

  function readCoverUrl(fromApi, media) {
    // Обложка из API точнее: в разметке страницы легко схватить картинку
    // альбома плейлиста вместо обложки играющего трека. Но если название
    // взято из разметки, обложку тоже берём оттуда - она соответствует
    // тому, что сейчас на экране.
    if (fromApi) {
      const current = currentTrack(media);
      if (current && current.coverUrl) {
        return current.coverUrl;
      }
    }

    // Обложка из панели плеера.
    //
    // В ответе VK обложки нет вовсе - проверено на живых данных: среди полей
    // трека (artist, id, owner_id, title, duration, url, track_code...) НИ
    // ОДНОГО с картинкой. А на экране она есть: панель плеера показывает
    // обложку играющего трека, и это единственное место, где она есть.
    //
    // Но брать её можно не всегда. Обложка из панели принадлежит тому, что
    // играет СЕЙЧАС, а название мы в этот раз угадали по времени. Получится
    // обложка одной песни с названием другой - ровно то, на что указал
    // пользователь. Поэтому при угадывании по времени обложку из панели не
    // берём: пусть лучше её не будет, чем будет не та.
    const RELIABLE_MATCH = [
      "ссылка на трек", "id", "обложка", "ответ VK", "длительность и название",
    ];
    const known = RELIABLE_MATCH.indexOf(apiState.matchedBy) >= 0;
    const panelUrl = known ? absoluteUrl(panelCoverUrl()) : "";
    if (panelUrl) {
      apiState.coverSource = "панель плеера";
      return panelUrl;
    }

    for (const sel of COVER_SELECTORS) {
      let nodes;
      try {
        nodes = document.querySelectorAll(sel);
      } catch (e) {
        continue;
      }
      for (const el of nodes) {
        const src = el.currentSrc || el.src || "";
        if (src && !src.startsWith("data:")) {
          return src;
        }
      }
    }
    return "";
  }

  // --- медиаэлементы ---------------------------------------------------------

  function mediaElements() {
    const all = document.querySelectorAll("audio, video");
    const out = [];
    for (const el of all) {
      if (el.currentSrc || el.src || el.querySelector("source")) {
        out.push(el);
      }
    }
    return out;
  }

  function rememberPlayingElement(el) {
    if (!el || typeof el !== "object") {
      return;
    }
    playback.element = el;
  }

  // Тот самый элемент, который сейчас звучит. Их на странице может быть
  // несколько - VK держит готовые плееры под очереди.
  function bestMedia() {
    const list = mediaElements();
    const playing = list.find((el) => !el.paused && !el.ended && el.readyState >= 2);
    if (playing) {
      playback.element = playing;
      return playing;
    }
    // Пойманный в play() элемент выручает, когда звука нет в документе: так
    // работает плеер VK (new Audio без appendChild). Условие readyState
    // здесь не украшение: у элемента, который реально играет, есть
    // загруженные данные, а у покойного или выброшенного страницей их нет.
    // Без этого мы держались бы за потухший элемент вместо отсутствующего.
    const hooked = playback.element;
    if (hooked && !hooked.paused && !hooked.ended && hooked.readyState >= 2) {
      return hooked;
    }
    if (list.length) {
      return list[0];
    }
    // Ни живых элементов, ни надёжного пойманного - значит звука нет.
    return null;
  }

  // --- признак воспроизведения ----------------------------------------------

  // Кнопка плеера надёжнее DOM: она подписана "Пауза", когда играет, и
  // "Воспроизвести", когда стоит.
  const PLAY_LABEL = /^(пауза|воспроизвести|воспроизведение|играть|play|pause|resume)$/i;

  function findPlayButton() {
    const nodes = document.querySelectorAll('button,[role="button"],[class*="Icon"]');
    let fallback = null;
    for (const el of nodes) {
      const label = (el.getAttribute("aria-label") || "").trim();
      const own = (el.textContent || "").trim();
      const name = label || own;
      if (!name || !PLAY_LABEL.test(name)) {
        continue;
      }
      const rect = el.getBoundingClientRect();
      if (rect.width < 16 || rect.height < 16) {
        continue;
      }
      // Кнопка внизу страницы - это плеер, а не что-то в шапке.
      if (rect.y > window.innerHeight * 0.55) {
        return el;
      }
      if (!fallback) {
        fallback = el;
      }
    }
    return fallback;
  }

  function playButtonState() {
    const el = findPlayButton();
    if (!el) {
      return null;
    }
    const name = (el.getAttribute("aria-label") || el.textContent || "").trim();
    return /пауза|pause/i.test(name) ? "playing" : "paused";
  }

  // --- название трека рядом с кнопкой плеера ---------------------------------
  //
  // Селекторы по названию класса - лотерея: VK меняет разметку, и на
  // странице «Моя музыка» они попадали в заголовок раздела. Но плеер у
  // любой страницы один, и играющий трек нарисован рядом с его кнопкой.
  // Поэтому ищем не по всей странице, а внутри панели плеера, и
  // отбрасываем текст, совпадающий с заголовком раздела.
  // Классы элементов, где лежит исполнитель, а не название трека. Ищем по
// границам слова, иначе "artist" нашёлся бы и внутри "track__name".
const ARTIST_CLASS = /(^|[\s_-])arts?(ist)?($|[\s_-])/i;

  function playerPanel() {    const button = findPlayButton();
    if (!button) {
      return null;
    }
    let node = button.parentElement;
    // Поднимаемся, пока не встретим что-то похожее на панель: обложку или
    // поля с названием. Слишком далеко уходить нельзя - там вся страница.
    for (let step = 0; step < 5 && node; step++) {
      const wide = node.getBoundingClientRect().width > window.innerWidth * 0.5;
      if (wide && node.querySelector("img,[class*='cover'],[class*='thumb']")) {
        return node;
      }
      if (!wide && node.querySelector("[class*='track'],[class*='name'],[class*='artist'],[class*='title']")) {
        return node;
      }
      node = node.parentElement;
    }
    return button.parentElement;
  }

  function sectionHeading() {
    // Заголовок раздела: именно он попадал в виджет вместо названия.
    const h1 = document.querySelector("h1");
    if (h1) {
      const text = (h1.textContent || "").trim();
      if (text) {
        return text;
      }
    }
    return (document.title || "").trim();
  }

  const PLAYER_TEXT_SELECTOR =
    "[class*='track'],[class*='name'],[class*='title'],[class*='artist'],h1,h2,h3,span,div,a";

  function titleFromPlayerPanel() {
    const panel = playerPanel();
    if (!panel) {
      return "";
    }
    const heading = sectionHeading();
    const docTitle = (document.title || "").trim();
    const candidates = panel.querySelectorAll(PLAYER_TEXT_SELECTOR);
    let best = "";
    for (const el of candidates) {
      if (panel !== el && !panel.contains(el)) {
        continue;
      }
      // Исполнитель рядом с названием есть всегда, а название - одно.
      // Смешав их, мы получили бы вместо песни её исполнителя.
      if (ARTIST_CLASS.test(classText(el))) {
        continue;
      }
      const text = shortText(el, 80);
      if (!text || PLACEHOLDER.test(text)) {
        continue;
      }
      // Заголовок раздела и подпись вкладки - не название трека.
      if (text === heading || text === docTitle) {
        continue;
      }
      // Контейнеры пропускаем. Их собственный текст - это всё, что внутри:
      // у панели плеера, где название пустое, остаётся один исполнитель,
      // и он оказывался короче настоящего названия.
      if (hasOwnChildText(el)) {
        continue;
      }
      // Короткое из длинного: в панели есть и исполнитель, и вся строка
      // целиком, а название трека - самое короткое из осмысленных.
      if (!best || text.length < best.length) {
        best = text;
      }
    }
    return best;
  }

  function hasOwnChildText(el) {
    const inner = el.querySelectorAll(PLAYER_TEXT_SELECTOR);
    for (const child of inner) {
      if (child !== el && shortText(child, 80)) {
        return true;
      }
    }
    return false;
  }

  // Признак берём один раз за опрос: playButtonState() обходит все кнопки
  // страницы, а вызывать его дважды смысла нет.
  function detectPlaying(media, fromButton) {
    if (fromButton) {
      return fromButton === "playing";
    }
    if (media) {
      return !media.paused && !media.ended && media.readyState >= 2;
    }
    return playback.playing;
  }

  // --- позиция воспроизведения -------------------------------------------------

  // Главное отличие от прошлой версии (CDP): currentTime у медиаэлемента
  // считается по медиа-часам, а не по таймерам страницы. В свёрнутой вкладке
  // Firefox душит setInterval, но часы аудио продолжают идти - поэтому
  // позиция всегда точная и не откатывается назад.

  // Счёт прошедшего времени нужен только когда медиаэлемента нет вообще
  // (плеер на WebAudio) и известна лишь доля.
  const elapsed = {
    key: null,
    startedAt: 0,
    pausedMs: 0,
    lastFrac: 0,
    lastSeen: 0,
  };

  function trackElapsed(trackKey, frac, playing, now) {
    if (elapsed.key !== trackKey) {
      elapsed.key = trackKey;
      elapsed.startedAt = now;
      elapsed.pausedMs = 0;
      elapsed.lastFrac = frac;
      elapsed.lastSeen = now;
      return 0;
    }
    const lastSeen = elapsed.lastSeen;
    // Новый трек начался с нуля - прежний отсчёт больше не подходит.
    if (frac < 0.02 && elapsed.lastFrac >= 0.02) {
      elapsed.startedAt = now;
      elapsed.pausedMs = 0;
    } else if (playing && lastSeen > 0 && now - lastSeen > 0.4 && frac <= elapsed.lastFrac) {
      // Доля не идёт - пауза или буферизация. Время на паузе не считаем,
      // иначе после перемотки время уехало бы далеко вперёд.
      elapsed.pausedMs += now - lastSeen;
    }
    elapsed.lastFrac = frac;
    elapsed.lastSeen = now;
    return Math.max(0, now - elapsed.startedAt - elapsed.pausedMs);
  }

  // Ползунок позиции. Их на странице несколько, и нужен тот, который
  // РЕАЛЬНО движется, поэтому запоминаем, какой из них изменился между
  // двумя опросами.
  const sliderWatch = {
    prev: null,
    movingIndex: -1,
  };

  // Элементы управления, которые похожи на шкалу позиции, но ею не являются.
  const NOT_POSITION = /volume|громкост|mute|muted|seek=|\bzoom\b/i;

  function listSliders() {
    const nodes = document.querySelectorAll("[role='slider'],[aria-valuenow],input[type='range']");
    const out = [];
    for (const el of nodes) {
      const label = (el.getAttribute("aria-label") || "") + " " + classText(el);

      // Громкость часто выглядит ровно так же, как шкала позиции: у
      // input[type=range] отличается лишь max. Без этой проверки
      // перемотка уезжала бы в громкость.
      if (el.tagName === "INPUT" && el.type === "range") {
        const max = parseFloat(el.max);
        if (isFinite(max) && max <= 10) {
          continue;
        }
      }
      if (NOT_POSITION.test(label)) {
        continue;
      }

      const rect = el.getBoundingClientRect();
      // Широкий и низкий - так выглядит шкала позиции; узкие и круглые
      // элементы - это громкость и прочее.
      if (rect.width < 60 || rect.height > 48 || rect.height <= 0) {
        continue;
      }
      let value = parseFloat(el.getAttribute("aria-valuenow"));
      if (el.tagName === "INPUT") {
        value = parseFloat(el.value);
      }
      if (!isFinite(value)) {
        value = 0;
      }
      out.push({ value: value, rect: rect, el: el, label: label });
    }
    return out;
  }

  function pickSlider() {
    const list = listSliders();
    if (!list.length) {
      return null;
    }
    const prev = sliderWatch.prev;
    if (prev && prev.length === list.length) {
      for (let i = 0; i < list.length; i++) {
        if (Math.abs(list[i].value - prev[i]) > 0.01) {
          sliderWatch.movingIndex = i;
          break;
        }
      }
    }
    sliderWatch.prev = list.map((x) => x.value);

    let index = sliderWatch.movingIndex;
    if (index < 0 || !list[index]) {
      const byLabel = list.findIndex((x) => /позици|воспроизвед|position|progress|seek|перемот/i.test(x.label));
      index = byLabel >= 0 ? byLabel : 0;
    }
    const chosen = list[index];
    // Значение приходит то в долях (0..1), то в процентах (0..100),
    // хотя aria-valuemax при этом часто равен 1.
    const raw = chosen.value;
    const frac = raw > 1.5 ? raw / 100 : raw;
    return {
      frac: Math.max(0, Math.min(1, frac)),
      rect: chosen.rect,
      el: chosen.el,
    };
  }

  function readPosition(media, playing, trackKey, now) {
    if (media) {
      const pos = Math.max(0, Number(media.currentTime) || 0);
      const raw = Number(media.duration);
      const dur = isFinite(raw) && raw > 0 ? raw : 0;
      if (dur > 0) {
        return { pos: pos, dur: dur, durExact: true, frac: Math.max(0, Math.min(1, pos / dur)) };
      }
      return { pos: pos, dur: 0, durExact: false, frac: 0 };
    }

    const slider = pickSlider();
    const frac = slider ? slider.frac : 0;
    // Плеер может держать ползунок на 1.0, когда ничего не играет: это не
    // конец трека, а простой.
    const idle = !playing && frac > 0.999;
    const effectiveFrac = idle ? 0 : frac;
    return {
      pos: trackElapsed(trackKey, effectiveFrac, playing, now),
      dur: 0,
      durExact: false,
      frac: effectiveFrac,
    };
  }

  // --- спектр ---------------------------------------------------------------

  // Полосы рисуются в виджете. Взять их можно двумя способами:
  //
  //  1) Подключиться к уже существующему графу WebAudio плеера. Анализатор
  //     подвешивается к источнику и НЕ соединяется с destination - звук
  //     при этом не меняется вообще, и, главное, не может заглохнуть.
  //
  //  2) Если плеер играет через обычный <audio> без WebAudio - снять копию
  //     потока через captureStream(). Это тоже не трогает звук: элемент
  //     по-прежнему сам играет в колонки, а AudioContext только слушает.
  //
  // Чего делать нельзя: createMediaElementSource() на элементе плеера
  // навсегда перенаправляет его звук в контекст. Если контекст окажется
  // приостановленным (политика автозапуска), музыка замолчит - поэтому
  // этот путь не используется.

  const BANDS = 20;

  // Тот же приём с состоянием спектра: после обновления расширения старая
  // копия скрипта остаётся в странице с уже навешенным хуком AudioContext.
  // Дублировать хук нельзя - на каждый узел плеера повисло бы по два
  // анализатора.
  const spectrum = window.__musicWidgetSpectrum ||
    (window.__musicWidgetSpectrum = {
      ctx: null,
      analyser: null,
      node: null,
      element: null,
      // Анализаторы, которые создаёт сам плеер: пригодятся, если у него
      // своя визуализация.
      foreign: null,
      hooked: false,
    });

  function makeAnalyser(ctx) {
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 128;
    analyser.smoothingTimeConstant = 0.75;
    return analyser;
  }

  // Вешает наш анализатор на узел источника звука плеера.
  function tapNode(ctx, node) {
    if (spectrum.node === node) {
      return;
    }
    try {
      const analyser = makeAnalyser(ctx);
      node.connect(analyser);
      spectrum.ctx = ctx;
      spectrum.node = node;
      spectrum.analyser = analyser;
    } catch (e) {
      // Узел уже подключён куда-то ещё - оставляем как есть.
    }
  }

  function hookAudioGraph() {
    if (spectrum.hooked) {
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || !AC.prototype) {
      return;
    }
    spectrum.hooked = true;

    const proto = AC.prototype;
    const origSource = proto.createMediaElementSource;
    const origAnalyser = proto.createAnalyser;
    const origStreamSource = proto.createMediaStreamSource;

    proto.createMediaElementSource = function () {
      const node = origSource.apply(this, arguments);
      tapNode(this, node);
      return node;
    };
    proto.createMediaStreamSource = function () {
      const node = origStreamSource.apply(this, arguments);
      tapNode(this, node);
      return node;
    };
    proto.createAnalyser = function () {
      const analyser = origAnalyser.apply(this, arguments);
      // Запоминаем чужой анализатор: он уже подключён к звуку.
      if (!spectrum.foreign) {
        spectrum.foreign = analyser;
      }
      return analyser;
    };
  }

  // Отдельный контекст только для чтения: он ни к чему не подключён и
  // потому не влияет на вывод звука.
  function tapThroughCaptureStream(media) {
    if (spectrum.element === media && spectrum.analyser) {
      return spectrum.analyser;
    }
    if (typeof media.captureStream !== "function") {
      return null;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) {
      return null;
    }
    try {
      const stream = media.captureStream();
      if (!stream) {
        return null;
      }
      const ctx = new AC();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = makeAnalyser(ctx);
      // Только чтение: в destination ничего не ведём.
      src.connect(analyser);
      if (ctx.state === "suspended" && typeof ctx.resume === "function") {
        ctx.resume().catch(() => {});
      }
      spectrum.ctx = ctx;
      spectrum.analyser = analyser;
      spectrum.element = media;
      spectrum.node = null;
      return analyser;
    } catch (e) {
      return null;
    }
  }

  function currentAnalyser() {
    if (spectrum.analyser) {
      return spectrum.analyser;
    }
    if (spectrum.foreign) {
      return spectrum.foreign;
    }
    const media = anyMedia();
    if (media) {
      return tapThroughCaptureStream(media);
    }
    return null;
  }

  // Полосы собираются логарифмически: низкие частоты иначе занимают весь
  // экран, а высокие не видны совсем.
  function readBands() {
    const out = new Array(BANDS).fill(0);
    const analyser = currentAnalyser();
    if (!analyser || typeof analyser.getByteFrequencyData !== "function") {
      return out;
    }
    try {
      const bins = analyser.frequencyBinCount;
      if (!bins) {
        return out;
      }
      const data = new Uint8Array(bins);
      analyser.getByteFrequencyData(data);

      // Полосы не должны наезжать друг на друга. При логарифмической
      // шкале первые несколько полос попадают в один и тот же бин, и
      // басы зажигали бы две-три полосы сразу, поэтому начало каждой
      // полосы прижимается к началу предыдущей плюс один бин.
      // Начинаем с -1, иначе первая полоса сдвинулась бы с бина 0.
      let prevFrom = -1;
      for (let i = 0; i < BANDS; i++) {
        let from = Math.floor(Math.pow(i / BANDS, 1.7) * bins);
        if (from < prevFrom + 1) {
          from = prevFrom + 1;
        }
        if (from > bins - 1) {
          from = bins - 1;
        }
        let to = Math.floor(Math.pow((i + 1) / BANDS, 1.7) * bins);
        if (to < from + 1) {
          to = from + 1;
        }
        if (to > bins) {
          to = bins;
        }
        let peak = 0;
        for (let k = from; k < to && k < bins; k++) {
          if (data[k] > peak) {
            peak = data[k];
          }
        }
        out[i] = peak / 255;
        prevFrom = from;
      }
    } catch (e) {
      return new Array(BANDS).fill(0);
    }
    return out;
  }

  // --- сборка состояния -------------------------------------------------------

  function buildState() {
    const media = bestMedia();
    // Медиаэлемент идёт первым: по его длительности и адресу мы узнаём
    // играющий трек, и без него название пришлось бы искать по разметке.
    const text = readTrackText(media);
    const fromButton = playButtonState();
    const playing = detectPlaying(media, fromButton);
    const trackKey = text.title + "|" + text.artist;
    const where = readPosition(media, playing, trackKey, Date.now());
    const api = durationFor(text.artist, text.title);
    const current = currentTrack(media);

    // Приоритет длительности: media.duration (точные часы плеера), затем
    // ответ API. Если ничего точного нет - не показываем ничего, оценка по
    // скорости ползунка в свёрнутой вкладке в разы завышает результат.
    let dur = where.dur;
    let durExact = where.durExact;
    // Длительность из "текущего трека" берём только если название взято
    // из него же. Иначе они могут разойтись: в разметке уже другой трек,
    // а из API пришёл предыдущий - и полоса показывала бы чужое время.
    if (!durExact && text.fromApi && current && current.dur > 0) {
      dur = current.dur;
      durExact = true;
    }
    if (!durExact && api && api.dur > 0) {
      dur = api.dur;
      durExact = true;
    }

    // Без медиаэлемента позиция идёт по часам; если длительность известна
    // точно, доля даёт гораздо лучшую оценку.
    let pos = where.pos;
    if (durExact && !media && where.frac > 0) {
      pos = where.frac * dur;
    }

    const coverUrl = readCoverUrl(text.fromApi, media) || (api ? api.coverUrl : "");

    // Запоминаем только то, во что есть основание верить: данные API или
    // панель плеера. Всё, что нашлось общими селекторами, может оказаться
    // заголовком раздела, и тогда мы запомнили бы именно ошибку.
    if (text.fromApi || text.fromPanel) {
      apiState.resolved = {
        title: text.title,
        artist: text.artist || "",
        coverUrl: coverUrl || "",
        at: Date.now(),
      };
    }

    return {
      title: text.title,
      artist: text.artist,
      coverUrl: coverUrl,
      playing: playing,
      hasMedia: !!media || playback.playing || playback.playCount > 0,
      hasPlayButton: !!fromButton,
      url: location.href,
      pos: pos,
      dur: dur,
      durExact: durExact,
      frac: where.frac,
      bands: readBands(),
      // Диагностика для виджета: по ней видно, откуда взялись данные и
      // почему названия может не быть. Без неё приходится гадать, что
      // именно сломалось: VK не отдаёт трек в API или мы его не видим.
      diag: {
        apiHits: apiState.hits,
        apiUrls: apiState.lastUrls.slice(-3),
        apiTracks: apiMeta.size,
        fromApi: !!text.fromApi,
        hasCurrent: !!apiState.current,
        media: !!media,
        mediaDuration: where.durExact ? dur : 0,
        titleSource: text.fromApi ? "api" : "страница",
        matchedBy: apiState.matchedBy || "",
        // Какое название панели мы отвергли как название плейлиста.
        panelTitle: apiState.panelTitleRejected || "",
        // Ссылка на строку плейлиста даёт точный номер трека.
        rowId: trackIdFromRow(media),
        // Звук лежит в странице или существует сам по себе.
        mediaConnected: !!(media && media.isConnected),
        // Откуда взята обложка: важно, чтобы она была от того же трека,
        // что и название.
        coverSource: apiState.coverSource || "",
        // Что в панели плеера на самом деле.
        panel: panelDump(),
        // И главное: что вокруг кнопки игры.
        playButton: playButtonDump(),
        // Адрес обложки целиком: в нём видно, откуда картинка и почему
        // плохое качество.
        coverUrl: coverUrl || "",
        // Какие поля приходят в треках: по ним ищем обложку.
        fields: (apiState.fields || []).slice(0, 40),
      },
    };
  }

  // --- команды ----------------------------------------------------------------

  // Кнопки VK не имеют стабильных id, поэтому ищем их по подписям.
  // Селектор широкий намеренно: подписи у VK от версии к версии меняются,
  // и кнопка то подписана через aria-label, то помечена классом, то лежит
  // под data-test-id.
  const BUTTON_SELECTOR =
    'button,[role="button"],[class*="Icon"],[class*="icon"],[class*="Tappable"],[data-test-id],[aria-label],[title]';

  // Подпись собираем из всех имён разом: одна только aria-label находила
  // кнопки переключения через раз.
  function buttonMeta(el) {
    return [
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || "",
      el.getAttribute("data-test-id") || "",
      classText(el),
      (el.textContent || "").trim().slice(0, 40),
    ].join(" ");
  }

  function isVisible(el) {
    if (el.disabled || el.getAttribute("aria-disabled") === "true") {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  }

  // Первая видимая кнопка, чья подпись подходит под pos и не подходит под
  // bad. Список bad длинный намеренно: у VK рядом с плеером лежат
  // перемешивание, громкость, лайки и карусели с очень похожими словами.
  function findButton(pos, bad) {
    const nodes = document.querySelectorAll(BUTTON_SELECTOR);
    for (const el of nodes) {
      if (!isVisible(el)) {
        continue;
      }
      const meta = buttonMeta(el);
      if (bad.test(meta)) {
        continue;
      }
      if (pos.test(meta)) {
        return el;
      }
    }
    return null;
  }

  // Подпись кнопки собирается из всех её имён разом: aria-label, title,
  // data-test-id, классы и подпись внутри. У VK кнопки переключения
  // подписаны по-разному от версии к версии, и одна только aria-label
  // находила их через раз.
  // --- кнопки переключения трека ---------------------------------------------
  //
  // По подписи находятся почти всегда. Если не нашлись - берём расположение:
  // в панели плеера кнопки стоят слева направо «предыдущий, пауза,
  // следующий», и это единственный ориентир, который не зависит от слов.
  function transportButtons() {
    // Опора - кнопка паузы: её VK подписывает всегда ("Воспроизвести" или
    // "Приостановить"), и найти её надёжно. Кнопки переключения подписей не
    // имеют вовсе, но стоят с ней в одной строке: левее - предыдущая,
    // правее - следующая.
    const play = clickTarget(findToggleButton());
    if (!play) {
      return null;
    }

    let row = play.parentElement;
    let items = [];
    for (let step = 0; step < 4 && row; step++) {
      const found = [];
      for (const el of row.querySelectorAll(BUTTON_SELECTOR)) {
        if (!isVisible(el)) {
          continue;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) {
          continue;
        }
        // Нажимаемым быть необязательно: иконки VK - это <div> без метода
        // click, и нажатие на них идёт через отправку события (press).
        found.push({ el: el, x: rect.left + rect.width / 2 });
      }
      if (found.length >= 3) {
        items = found;
        break;
      }
      row = row.parentElement;
    }
    if (items.length < 3) {
      return null;
    }

    items.sort((a, b) => a.x - b.x);
    const center = (el) => {
      const rect = el.getBoundingClientRect();
      return rect.left + rect.width / 2;
    };
    const playX = center(play);

    // Кнопки слева от паузы - предыдущие, справа - следующие.
    const left = items.filter((item) => center(item.el) < playX);
    const right = items.filter((item) => center(item.el) > playX);
    if (!left.length || !right.length) {
      return null;
    }
    // Ближайшие к паузе: остальные в той же строке к переключению отношения
    // не имеют.
    left.sort((a, b) => center(b.el) - center(a.el));
    right.sort((a, b) => center(a.el) - center(b.el));
    return { prev: left[0].el, toggle: play, next: right[0].el };
  }

  function findTransport(kind) {
    const byLabel = kind === "prev" ? findPrevButton()
      : kind === "next" ? findNextButton() : findToggleButton();
    if (byLabel) {
      return byLabel;
    }
    const guess = transportButtons();
    return guess ? guess[kind] : null;
  }

  // Общие слова, которые почти всегда означают не то, что нужно.
  const COMMON_BAD = /shuffle|shuf|repeat|volume|mute|like|favou?rite|share|download|queue|playlist|lyric|expand|more|setting|quality|comment|sponsor|add|remove|delete|open|fullscreen|cast|pip|scroll|carousel|arrow|swiper|slider|dots|navigation/i;
  const PREV_WORDS = /prev|previous|предыдущ|предыд|назад/i;
  const NEXT_WORDS = /next|следующ|впер[её]д|вперед/i;

  // Слова проверяем с границами: иначе "play" совпадёт с "playlist",
  // а "next" - с "nextPage".
  const RE_TOGGLE = /(^|[\s_-])(play|pause|resume|воспроизвести|воспроизведение|пауза|играть)([\s_-]|$)/i;
  // ВНИМАНИЕ: окончания заданы как [а-яё]*, а не \w*. В JavaScript \w -
  // это только [A-Za-z0-9_], кириллицу он не покрывает: вариант
  // "следующ\w*" молча переставал находить «Следующий трек». В Python,
  // откуда пришёл этот код, \w был Unicode-адресным и всё работало.
  const RE_NEXT = /(^|[\s_-])(next|следующ[а-яё]*|впер[её]д|вперед)([\s_-]|$)/i;
  const RE_PREV = /(^|[\s_-])(prev|previous|предыдущ[а-яё]*|предыд[а-яё]*|назад)([\s_-]|$)/i;

  function findToggleButton() {
    return findButton(RE_TOGGLE, new RegExp(COMMON_BAD.source + "|" + PREV_WORDS.source + "|" + NEXT_WORDS.source, "i"));
  }

  function findNextButton() {
    return findButton(RE_NEXT, new RegExp(COMMON_BAD.source + "|" + PREV_WORDS.source, "i"));
  }

  function findPrevButton() {
    return findButton(RE_PREV, new RegExp(COMMON_BAD.source + "|" + NEXT_WORDS.source, "i"));
  }

  // Ищем видимый медиаэлемент: на паузе он всё равно подходит.
  function anyMedia() {
    const list = mediaElements();
    if (list.length) {
      return list[0];
    }
    // Плеер VK держит звук вне страницы - элемент помнит хук play(). Без
    // этого «пауза» и «воспроизведение» не находили медиаэлемента и
    // отвечали «не нашёл ни кнопку, ни медиаэлемент», хотя звук был.
    const hooked = playback.element;
    if (hooked && !hooked.ended) {
      return hooked;
    }
    return null;
  }

  function toggleViaMedia() {
    const media = anyMedia();
    if (!media) {
      return "нет медиаэлемента";
    }
    if (media.paused || media.ended) {
      const result = media.play();
      if (result && typeof result.catch === "function") {
        result.catch(function () {});
      }
      return "media-play";
    }
    media.pause();
    return "media-pause";
  }

  function commandToggle() {
    const found = findTransport("toggle");
    const button = found ? clickTarget(found) : null;
    if (button) {
      press(button);
      return { ok: true, msg: "кнопка: " + buttonMeta(button).slice(0, 60) };
    }
    // Кнопки могут быть не найдены (плеер свёрнут, вёрстка изменилась) - тогда
    // управляем медиаэлементом напрямую.
    const what = toggleViaMedia();
    if (what === "нет медиаэлемента") {
      const seen = visibleButtons(8);
      return {
        ok: false,
        msg: "не нашёл ни кнопку плеера, ни медиаэлемент. Видимые кнопки: "
          + (seen.length ? seen.join(" / ") : "нет"),
      };
    }
    return { ok: true, msg: what };
  }

  // Полной остановки в HTMLMediaElement нет, поэтому останавливаем так,
  // как это ожидает пользователь: пауза и возврат в начало.
  function commandStop() {
    const media = anyMedia();
    if (media) {
      media.pause();
      try {
        media.currentTime = 0;
      } catch (e) {
        // currentTime может быть недоступен до загрузки метаданных.
      }
    }
    const found = findToggleButton();
    const button = found ? clickTarget(found) : null;
    if (button && (!media || !media.paused)) {
      // Плеер ещё играет - нажимаем кнопку, чтобы он встал.
      press(button);
      return { ok: true, msg: "кнопка: " + buttonMeta(button).slice(0, 60) };
    }
    const seen = visibleButtons(8);
    return media
      ? { ok: true, msg: "media-pause + в начало" }
      : {
          ok: false,
          msg: "не нашёл ни кнопку плеера, ни медиаэлемент. Видимые кнопки: "
            + (seen.length ? seen.join(" / ") : "нет"),
        };
  }

  // Переключение треков у VK есть только кнопкой: у медиаэлемента нет
  // методов next/previous, а плейлист живёт в закрытом JS-состоянии
  // страницы, до которого расширение не дотягивается.
  // Перечень видимых кнопок со страницы.
//
// Ошибка вида "не нашёл кнопку" сама по себе бесполезна: непонятно, то ли
// подписи не подошли, то ли кнопок нет вовсе. Список позволяет понять это
// сразу, не переустанавливая расширение наугад.
function visibleButtons(limit) {
    const seen = [];
    for (const el of document.querySelectorAll(BUTTON_SELECTOR)) {
      if (seen.length >= limit) {
        break;
      }
      if (!isVisible(el)) {
        continue;
      }
      const meta = buttonMeta(el).replace(/\s+/g, " ").trim();
      if (meta) {
        seen.push(meta.slice(0, 34));
      }
    }
    return seen;
  }

    // Что нажимать на самом деле.
  //
  // Селектор кнопок широкий намеренно, и поэтому попадает не только на
  // <button>: подпись с aria-label лежит часто на иконке или на <span>
  // внутри кнопки, а у них метода click() нет. Ошибка "button.click is not
  // a function" - это ровно оно: элемент нашли, а нажать его нечем.
  function clickTarget(el) {
    let node = el;
    for (let step = 0; node && step < 5; step++) {
      if (typeof node.click === "function") {
        return node;
      }
      node = node.parentElement;
    }
    return null;
  }

  // Нажать элемент так, чтобы сработало.
  //
  // У VK кнопки переключения - иконки без подписей, и лежат они на <div>
  // и <span>, у которых метода click() нет: попытка нажать их падала с
  // "button.click is not a function". Событие же можно отправить любому
  // элементу, и обработчик, навешенный на него, сработает как при настоящем
  // клике мышью.
  function press(el) {
    const target = clickTarget(el);
    if (target) {
      target.click();
      return true;
    }
    if (el && typeof el.dispatchEvent === "function") {
      try {
        for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
          el.dispatchEvent(new MouseEvent(type, {
            bubbles: true, cancelable: true, view: window,
          }));
        }
        return true;
      } catch (e) {
        return false;
      }
    }
    return false;
  }

  function commandSkip(kind, what) {
    const found = findTransport(kind);
    const target = found ? clickTarget(found) : null;
    if (!target) {
      const seen = visibleButtons(8);
      return {
        ok: false,
        msg: "не нашёл кнопку «" + what + "». Видимые кнопки: "
          + (seen.length ? seen.join(" / ") : "нет"),
      };
    }
    press(target);
    return { ok: true, msg: "кнопка: " + buttonMeta(target).slice(0, 60) };
  }

  // --- перемотка -----------------------------------------------------------

  // Firefox не умеет отправлять доверенный ввод (в Chromium это делал
  // Input.dispatchMouseEvent), поэтому работаем тремя способами по
  // убыванию надёжности и берём первый сработавший.

  function clampRatio(value) {
    const n = Number(value);
    if (!isFinite(n)) {
      return null;
    }
    return n < 0 ? 0 : n > 1 ? 1 : n;
  }

  // 1. Медиаэлемент с известной длительностью: самый точный способ,
  //    время выставляется напрямую.
  function seekViaMedia(ratio) {
    const media = anyMedia();
    if (!media) {
      return null;
    }
    const raw = Number(media.duration);
    if (!isFinite(raw) || raw <= 0) {
      return null;
    }
    // Чуть не до конца: иначе плеер успевает перейти на следующий трек
    // прямо во время установки времени.
    const target = Math.max(0, Math.min(raw - 0.5, ratio * raw));
    try {
      media.currentTime = target;
    } catch (e) {
      return null;
    }
    return "audio " + Math.round(ratio * 100) + "%";
  }

  // 2. Настоящий input[type=range] - половина плееров принимает значение
  //    напрямую, если выставить его и послать input/change.
  function seekViaRangeInput(ratio) {
    const inputs = document.querySelectorAll("input[type='range']");
    for (const el of inputs) {
      const max = parseFloat(el.max);
      if (!isFinite(max) || max <= 10) {
        continue;
      }
      const parsedMin = parseFloat(el.min);
      const min = isFinite(parsedMin) ? parsedMin : 0;
      const value = min + (max - min) * ratio;
      el.value = String(value);
      try {
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (e) {
        continue;
      }
      return "range " + Math.round(ratio * 100) + "%";
    }
    return null;
  }

  // 3. Шкала на div: кликаем в нужную точку. VK игнорирует вызовы
  //    element.click() без координат, поэтому шлём полноценную
  //    последовательность событий с clientX.
  function seekViaPointer(ratio) {
    const slider = pickSlider();
    if (!slider || !slider.el) {
      return null;
    }
    const rect = slider.rect;
    const x = rect.left + rect.width * ratio;
    const y = rect.top + rect.height / 2;
    const base = {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: x,
      clientY: y,
      screenX: window.screenX + x,
      screenY: window.screenY + y,
      view: window,
      button: 0,
    };
    const pointer = { pointerId: 1, pointerType: "mouse", isPrimary: true };
    try {
      if (typeof window.PointerEvent === "function") {
        slider.el.dispatchEvent(new PointerEvent("pointerdown", Object.assign({}, base, pointer, { buttons: 1 })));
      }
      slider.el.dispatchEvent(new MouseEvent("mousedown", Object.assign({}, base, { buttons: 1 })));
      if (typeof window.PointerEvent === "function") {
        slider.el.dispatchEvent(new PointerEvent("pointerup", Object.assign({}, base, pointer, { buttons: 0 })));
      }
      slider.el.dispatchEvent(new MouseEvent("mouseup", Object.assign({}, base, { buttons: 0 })));
      slider.el.dispatchEvent(new MouseEvent("click", Object.assign({}, base, { buttons: 0 })));
    } catch (e) {
      return null;
    }
    return "slider " + Math.round(ratio * 100) + "%";
  }

  function commandSeek(ratio) {
    const value = clampRatio(ratio);
    if (value === null) {
      return { ok: false, msg: "не задана доля перемотки" };
    }
    const ways = [seekViaMedia(value), seekViaRangeInput(value), seekViaPointer(value)];
    for (const result of ways) {
      if (result) {
        return { ok: true, msg: result };
      }
    }
    return { ok: false, msg: "не нашёл шкалу позиции" };
  }

  function runCommand(action, ratio) {
    switch (action) {
      case "toggle":
        return commandToggle();
      case "stop":
        return commandStop();
      case "next":
        return commandSkip("next", "следующий трек");
      case "prev":
        return commandSkip("prev", "предыдущий трек");
      case "seek":
        return commandSeek(ratio);
      default:
        return { ok: false, msg: "неизвестная команда: " + action };
    }
  }

  // --- обмен сообщениями ------------------------------------------------------

  // Оба хука - до подписки на сообщения: скрипт готов принимать запросы
  // сразу, как только о нём узнает content script.
  hookMediaPrototypes();
  hookNetwork();
  hookAudioGraph();

  function reply(id, payload) {
    if (token === null) {
      return;
    }
    try {
      window.postMessage({ __mw: MSG_VERSION, dir: "toContent", id: id, token: token, payload: payload }, "*");
    } catch (e) {
      // Страница могла разрушиться между запросом и ответом.
    }
  }

  // Подписка на сообщения тоже должна быть ровно одна.
  //
  // Хуки сети и плеера защищены флагом, а эта подписка была единственной
  // без защиты. Скрипт главного мира вставляется в страницу дважды: сам
  // манифестом и дозагрузкой в уже открытые вкладки, а также после
  // обновления расширения. Вторая копия вешала второй обработчик, и любая
  // команда выполнялась дважды: "пауза" превращалась в play, а потом в
  // pause - то есть в ничто. Кнопки выглядели мёртвыми.
  if (!apiState.listening) {
    apiState.listening = true;

    window.addEventListener("message", function (ev) {
      if (ev.source !== window) {
        return;
      }
      const data = ev.data;
      if (!data || data.__mw !== MSG_VERSION || data.dir !== "toPage") {
        return;
      }

        // init приходит на document_start, раньше скриптов страницы. Повторный
    // init - это перезапуск content script после обновления расширения:
    // у новой копии другой токен, и если бы мы его не приняли, вкладка
    // VK замолчала бы до ручной перезагрузки страницы.
    if (data.t === "init") {
        if (data.token) {
          token = data.token;
        }
        reply(data.id, { ready: true });
        return;
      }

      if (token === null || data.token !== token) {
        return;
      }

      if (data.t === "getState") {
        let payload;
        try {
          payload = buildState();
        } catch (e) {
          payload = { error: String((e && e.message) || e) };
        }
        reply(data.id, payload);
        return;
      }

      if (data.t === "cmd") {
        let result;
        try {
          result = runCommand(data.action, data.ratio);
        } catch (e) {
          result = { ok: false, msg: String((e && e.message) || e) };
        }
        reply(data.id, result);
        return;
      }
    });
  }

  })();