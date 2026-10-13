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
    });
const apiMeta = apiState.meta;

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

  // Текущий трек по данным API.
  //
  // Разбирать вёрстку VK по селекторам ненадёжно: разметка меняется без
  // предупреждения, и на странице плейлиста селектор названия трека
  // попадает в заголовок раздела - виджет показывал «Моя музыка».
  // Ответы API куда надёжнее: когда начинает играть трек, VK запрашивает
  // его отдельно, и там лежат название, исполнитель, точная длительность
  // и обложка.
  const CURRENT_TTL_MS = 10 * 60 * 1000;

  function rememberTrack(artist, title, dur, coverUrl, asCurrent) {
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
      rememberTrack(artist, title, dur, pickCoverUrl(node), asCurrent);
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

  function currentTrack() {
    // Данные о текущем треке из API, пока не устарели. После смены трека
    // приходит новый ответ VK, поэтому десяти минут с запасом хватает, а
    // старый трек не будет висеть на виджете вечно.
    const now = Date.now();
    const current = apiState.current;
    if (!current || !current.title) {
      return null;
    }
    if (now - current.at > CURRENT_TTL_MS) {
      apiState.current = null;
      return null;
    }
    return current;
  }

  function readTrackText() {
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
    const current = currentTrack();

    if (current && !durationFor(artist, title)) {
      return { title: current.title, artist: current.artist || "", fromApi: true };
    }
    return { title: title, artist: artist, fromApi: false };
  }

  function readCoverUrl(fromApi) {
    // Обложка из API точнее: в разметке страницы легко схватить картинку
    // альбома плейлиста вместо обложки играющего трека. Но если название
    // взято из разметки, обложку тоже берём оттуда - она соответствует
    // тому, что сейчас на экране.
    if (fromApi) {
      const current = currentTrack();
      if (current && current.coverUrl) {
        return current.coverUrl;
      }
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
    const text = readTrackText();
    const fromButton = playButtonState();
    const playing = detectPlaying(media, fromButton);
    const trackKey = text.title + "|" + text.artist;
    const where = readPosition(media, playing, trackKey, Date.now());
    const api = durationFor(text.artist, text.title);
    const current = currentTrack();

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

    const coverUrl = readCoverUrl(text.fromApi) || (api ? api.coverUrl : "");

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
      },
    };
  }

  // --- команды ----------------------------------------------------------------

  // Кнопки VK не имеют стабильных id, поэтому ищем их по подписям.
  const BUTTON_SELECTOR = 'button,[role="button"],[data-test-id],[class*="Icon"],[class*="Tappable"]';

  function buttonMeta(el) {
    return [
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || "",
      classText(el),
      el.getAttribute("data-test-id") || "",
      (el.textContent || "").trim().slice(0, 40),
    ].join(" | ");
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
    return list[0] || null;
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
    const button = findToggleButton();
    if (button) {
      button.click();
      return { ok: true, msg: "кнопка: " + buttonMeta(button).slice(0, 60) };
    }
    // Кнопки может не быть (плеер свёрнут, вёрстка изменилась) - тогда
    // управляем медиаэлементом напрямую.
    const what = toggleViaMedia();
    return what === "нет медиаэлемента"
      ? { ok: false, msg: "не нашёл ни кнопку плеера, ни медиаэлемент" }
      : { ok: true, msg: what };
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
    const button = findToggleButton();
    if (button && (!media || !media.paused)) {
      // Плеер ещё играет - нажимаем кнопку, чтобы он встал.
      button.click();
      return { ok: true, msg: "кнопка: " + buttonMeta(button).slice(0, 60) };
    }
    return media
      ? { ok: true, msg: "media-pause + в начало" }
      : { ok: false, msg: "не нашёл ни кнопку плеера, ни медиаэлемент" };
  }

  // Переключение треков у VK есть только кнопкой: у медиаэлемента нет
  // методов next/previous, а плейлист живёт в закрытом JS-состоянии
  // страницы, до которого расширение не дотягивается.
  function commandSkip(button, what) {
    const found = button();
    if (!found) {
      return { ok: false, msg: "не нашёл кнопку «" + what + "»" };
    }
    found.click();
    return { ok: true, msg: "кнопка: " + buttonMeta(found).slice(0, 60) };
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
        return commandSkip(findNextButton, "следующий трек");
      case "prev":
        return commandSkip(findPrevButton, "предыдущий трек");
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

  })();