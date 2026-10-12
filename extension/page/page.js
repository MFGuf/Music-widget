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

  hookMediaPrototypes();

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

  // --- сборка состояния -------------------------------------------------------

  function buildState() {
    const media = bestMedia();
    const text = readTrackText();
    const playing = detectPlaying(media);
    return {
      title: text.title,
      artist: text.artist,
      coverUrl: readCoverUrl(),
      playing: playing,
      hasMedia: !!media || playback.playing || playback.playCount > 0,
      hasPlayButton: !!playButtonState(),
      url: location.href,
      // Поля позиции заполняются в следующем коммите (задача 8).
      pos: media ? Number(media.currentTime) || 0 : 0,
      dur: media && isFinite(media.duration) ? Number(media.duration) || 0 : 0,
      frac: 0,
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