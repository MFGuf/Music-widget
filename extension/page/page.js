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
  const NS = "__mw";
  const MSG_VERSION = 1;

  // Токен приходит от content script при инициализации. До него скрипт
  // молчит: иначе любой скрипт страницы мог бы слать нам сообщения.
  let token = null;

  // Последняя перемотка, замеченная в сеттере currentTime. Объявлено до
  // хуков: installCurrentTimeHook() вызывается сразу и обращается к этим
  // переменным - при обратном порядке было бы ReferenceError.
  let seekHint = 0;
  let seekAt = 0;

  // --- хуки воспроизведения -------------------------------------------------

  // Firefox не поддерживает navigator.mediaSession, поэтому единственный
  // надёжный признак "играет" - перехват play()/pause() на прототипе.
  const playback = {
    playing: false,
    lastChange: 0,
    playCount: 0,
    hooked: false,
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
      playback.lastChange = Date.now();
      playback.playCount++;
      return origPlay.apply(this, arguments);
    };
    proto.pause = function () {
      playback.playing = false;
      playback.lastChange = Date.now();
      return origPause.apply(this, arguments);
    };
    // Перемотка меняет currentTime напрямую - запоминаем, чтобы позиция
    // не выглядела как "прыжок назад".
    const desc = Object.getOwnPropertyDescriptor(proto, "currentTime");
    if (desc && desc.set) {
      Object.defineProperty(proto, "currentTime", {
        configurable: true,
        enumerable: desc.enumerable,
        get: desc.get,
        set: function (v) {
          seekHint = Number(v) || 0;
          seekAt = Date.now();
          return desc.set.call(this, v);
        },
      });
    }
    playback.hooked = true;
  }

  // Хуки ставятся в самом конце файла: состояние, к которому они обращаются,
// объявлено через const/let выше, и обращение выше объявления дало бы
// ReferenceError. Для document_start это не страшно - весь скрипт
// выполняется синхронно до любого кода страницы.

  // --- перехват ответов VK API -----------------------------------------------

  // В DOM у VK Музыки длительности трека нет вообще, а navigator.mediaSession
  // в Firefox отсутствует. Точное значение лежит в ответе API, который
  // плеер запрашивает при смене трека, поэтому перехватываем fetch и XHR.
  //
  // Перехват сделан здесь, а не через webRequest расширения: скрипт и так
  // живёт в главном мире страницы до загрузки её собственных скриптов,
  // поэтому ловит запросы гарантированно и не требует лишних прав.
  const apiMeta = new Map();
  let apiHooked = false;

  // Ответы больших плейлистов весят мегабайты - такие разбирать незачем.
  const MAX_API_TEXT = 4 * 1024 * 1024;

  function metaKey(artist, title) {
    return String(artist || "").trim().toLowerCase() + "\u0000" +
           String(title || "").trim().toLowerCase();
  }

  function pickCoverUrl(node) {
    const fields = [
      "thumb_src", "thumb", "thumb_200", "thumb_100",
      "cover", "album_thumb", "artist_photo",
    ];
    for (const field of fields) {
      const value = node[field];
      if (typeof value === "string" && value.indexOf("http") === 0) {
        return value;
      }
    }
    return "";
  }

  function rememberTrack(artist, title, dur, coverUrl) {
    if (!title || !isFinite(dur) || dur <= 0) {
      return;
    }
    const key = metaKey(artist, title);
    const previous = apiMeta.get(key);
    // Один и тот же трек встречается в плейлисте десятки раз; переписываем
    // запись, только если данные изменились, иначе карта растёт без нужды.
    if (previous && previous.dur === dur && previous.coverUrl === coverUrl) {
      return;
    }
    if (apiMeta.size > 200) {
      const oldest = apiMeta.keys().next();
      if (!oldest.done) {
        apiMeta.delete(oldest.value);
      }
    }
    apiMeta.set(key, { dur: dur, coverUrl: coverUrl || (previous ? previous.coverUrl : "") });
  }

  // Ответы VK бывают вложенными (execute.multiple), поэтому обходим дерево.
  function absorbApiJson(node, depth) {
    if (!node || typeof node !== "object" || depth > 8) {
      return;
    }
    if (Array.isArray(node)) {
      for (const item of node) {
        absorbApiJson(item, depth + 1);
      }
      return;
    }
    const title = typeof node.title === "string" ? node.title : "";
    const artist = typeof node.artist === "string" ? node.artist : "";
    const dur = Number(node.duration);
    if (title && isFinite(dur) && dur > 0) {
      rememberTrack(artist, title, dur, pickCoverUrl(node));
    }
    for (const key of Object.keys(node)) {
      absorbApiJson(node[key], depth + 1);
    }
  }

  function absorbApiText(text) {
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
    absorbApiJson(json, 0);
  }

  function looksLikeApi(url) {
    return /(^|\/\/)api\.vk\.(com|ru)\/method\//i.test(String(url || ""));
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
    if (apiHooked) {
      return;
    }
    apiHooked = true;

    const origFetch = window.fetch;
    if (typeof origFetch === "function") {
      window.fetch = function () {
        const args = arguments;
        const url = requestUrl(args[0]);
        if (!looksLikeApi(url)) {
          return origFetch.apply(this, args);
        }
        return origFetch.apply(this, args).then(function (response) {
          try {
            // Клонируем ДО чтения: оригинал должен остаться нетронутым
            // для самой страницы.
            const copy = response.clone();
            copy.text().then(absorbApiText, function () {});
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
        if (looksLikeApi(self.__mwUrl)) {
          self.addEventListener("load", function () {
            try {
              if (self.responseType === "" || self.responseType === "text") {
                absorbApiText(self.responseText);
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

  function readTrackText() {
    let title = pickText(TITLE_SELECTORS);
    if (!title || PLACEHOLDER.test(title)) {
      title = titleFromDocumentTitle();
    }
    const artist = pickText(ARTIST_SELECTORS);
    return { title: title, artist: artist };
  }

  function readCoverUrl() {
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

  // Тот самый элемент, который сейчас звучит. Их на странице может быть
  // несколько - VK держит готовые плееры под очереди.
  function bestMedia() {
    const list = mediaElements();
    const playing = list.find((el) => !el.paused && !el.ended && el.readyState >= 2);
    return playing || list[0] || null;
  }

  // --- признак воспроизведения ----------------------------------------------

  // Кнопка плеера надёжнее DOM: она подписана "Пауза", когда играет, и
  // "Воспроизвести", когда стоит.
  const PLAY_LABEL = /^(пауза|воспроизвести|воспроизведение|играть|play|pause|resume)$/i;

  function playButtonState() {
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
      const state = /пауза|pause/i.test(name) ? "playing" : "paused";
      // Кнопка внизу страницы - это плеер, а не что-то в шапке.
      if (rect.y > window.innerHeight * 0.55) {
        return state;
      }
      if (!fallback) {
        fallback = state;
      }
    }
    return fallback;
  }

  function detectPlaying(media) {
    const fromButton = playButtonState();
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

  function listSliders() {
    const nodes = document.querySelectorAll("[role='slider'],[aria-valuenow],input[type='range']");
    const out = [];
    for (const el of nodes) {
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
      out.push({ value: value, rect: rect, el: el, label: (el.getAttribute("aria-label") || "") + " " + classText(el) });
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

  // --- сборка состояния -------------------------------------------------------

  function buildState() {
    const media = bestMedia();
    const text = readTrackText();
    const playing = detectPlaying(media);
    const trackKey = text.title + "|" + text.artist;
    const where = readPosition(media, playing, trackKey, Date.now());
    const api = durationFor(text.artist, text.title);

    // Приоритет длительности: media.duration (точные часы плеера), затем
    // ответ API. Если ничего точного нет - не показываем ничего, оценка по
    // скорости ползунка в свёрнутой вкладке в разы завышает результат.
    let dur = where.dur;
    let durExact = where.durExact;
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

    const coverUrl = readCoverUrl() || (api ? api.coverUrl : "");

    return {
      title: text.title,
      artist: text.artist,
      coverUrl: coverUrl,
      playing: playing,
      hasMedia: !!media || playback.playing || playback.playCount > 0,
      hasPlayButton: !!playButtonState(),
      url: location.href,
      pos: pos,
      dur: dur,
      durExact: durExact,
      frac: where.frac,
      bands: [],
      seekHint: seekHint,
      seekAt: seekAt,
    };
  }

  // --- команды ----------------------------------------------------------------

  function runCommand(action, ratio) {
    switch (action) {
      case "toggle":
      case "next":
      case "prev":
      case "stop":
      case "seek":
        // Реализуется в задачах 10-13.
        return { ok: false, msg: "команда " + action + " ещё не реализована" };
      default:
        return { ok: false, msg: "неизвестная команда: " + action };
    }
  }

  // --- обмен сообщениями ------------------------------------------------------

  // Оба хука - до подписки на сообщения: скрипт готов принимать запросы
  // сразу, как только о нём узнает content script.
  hookMediaPrototypes();
  hookNetwork();

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

  window.addEventListener("message", function (ev) {
    if (ev.source !== window) {
      return;
    }
    const data = ev.data;
    if (!data || data.__mw !== MSG_VERSION || data.dir !== "toPage") {
      return;
    }

    if (data.t === "init") {
      if (token === null) {
        token = data.token;
        reply(data.id, { ready: true });
      }
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

  })();