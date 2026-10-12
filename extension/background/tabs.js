"use strict";

// Поиск вкладок VK Музыки и сбор из них списка источников для виджета.
//
// Вкладка считается источником, если это страница VK Музыки И её content
// script ответил. Вкладка, которая ещё грузится или в которую расширение
// только что установили, отвечает не сразу - её не показываем, иначе
// список мигает при каждой перезагрузке страницы.

let pollTimer = null;
let polling = false;

async function pollTabs() {
  // Опрос занимает время (запрос к каждой вкладке), а интервал короче
  // этого времени. Без защиты запросы накапливались бы.
  if (polling) {
    return;
  }
  polling = true;
  try {
    await refreshSources();
    rebuildItems();
    // Обложки грузятся асинхронно: ждать их здесь нельзя, иначе опрос
    // вкладок встанет на время скачивания картинки. Виджет получит их в
    // следующем снимке - это меньше секунды.
    updateArt();
    sendState();
  } catch (e) {
    setError("опрос вкладок: " + (e && e.message ? e.message : e));
  } finally {
    polling = false;
  }
}

async function refreshSources() {
  let tabs;
  try {
    tabs = await browser.tabs.query({});
  } catch (e) {
    return;
  }

  const next = new Map();
  for (const tab of tabs || []) {
    if (next.size >= MAX_TABS) {
      break;
    }
    if (typeof tab.id !== "number" || !isMusicPage(tab.url)) {
      continue;
    }
    // Незагруженная вкладка вот-вот перезагрузится, а её content script
    // может ответить на середине загрузки - лишний источник в списке.
    if (tab.status && tab.status !== "complete") {
      continue;
    }
    next.set(tab.id, {
      tabId: tab.id,
      url: tab.url,
      title: tab.title || "",
      ready: false,
      state: null,
    });
  }

  // Опрашиваем вкладки параллельно, а не по очереди. У не отвечающей
  // вкладки запрос висит до REQUEST_TIMEOUT_MS в content script, и при
  // последовательном обходе четыре вкладки растянули бы опрос на шесть
  // секунд вместо семисот миллисекунд - интервал тогда бессмыслен.
  const states = await Promise.all(
    Array.from(next.keys()).map((tabId) => requestState(tabId))
  );
  let index = 0;
  for (const source of next.values()) {
    source.state = states[index++];
    source.ready = !!source.state;
  }

  store.sources = next;
}

async function requestState(tabId) {
  try {
    const reply = await browser.tabs.sendMessage(tabId, { t: "poll" });
    return reply && typeof reply === "object" ? reply : null;
  } catch (e) {
    // Самая частая причина: content script в этой вкладке ещё не создан -
    // страница была открыта до установки расширения или после его
    // обновления. Лечится дозапуском скрипта, см. injectIntoOpenTabs().
    return null;
  }
}

// Вкладки, открытые до установки расширения, не имеют content script.
// Дозагружаем его один раз при старте, иначе виджет увидит пустой список
// до первой перезагрузки страницы.
async function injectIntoOpenTabs() {
  const patterns = ["*://vk.com/*", "*://vk.ru/*", "*://*.vk.com/*", "*://*.vk.ru/*"];
  try {
    const tabs = await browser.tabs.query({ url: patterns });
    for (const tab of tabs || []) {
      if (typeof tab.id !== "number") {
        continue;
      }
      try {
        await browser.tabs.executeScript(tab.id, { file: "content/vk.js" });
      } catch (e) {
        // Вкладку могли закрыть между запросом и вставкой скрипта.
      }
    }
  } catch (e) {
    log("не удалось дозагрузить content script:", e);
  }
}

// Собирает список источников для виджета. Смешивает ответ вкладки с тем,
// что известно из перехваченных ответов VK API.
function rebuildItems() {
  const items = [];
  for (const source of store.sources.values()) {
    if (!source.ready || !source.state) {
      continue;
    }
    items.push(buildItem(source, source.state));
  }

  // Автовыбор: удерживаем то, что выбрал пользователь, пока оно живо.
  // Иначе берём то, что играет, иначе - первую вкладку.
  const keys = new Set(items.map((it) => it.key));
  if (!store.selectedKey || !keys.has(store.selectedKey)) {
    const playing = items.find((it) => it.playing);
    store.selectedKey = playing ? playing.key : (items[0] ? items[0].key : null);
    store.userPicked = false;
  }

  store.lastItems = items;
}

function buildItem(source, state) {
  const key = makeKey(source.tabId);
  const title = trimText(state.title || source.title || "Unknown track", 200);
  const artist = trimText(state.artist || "", 200);

  // Длительность берём только точную: из media.duration или из ответа
  // VK API (его разбирает page.js и передаёт готовым числом). Оценку по
  // скорости ползунка не используем намеренно - в свёрнутой вкладке она
  // завышает длительность в разы, а это хуже, чем не показывать её вовсе.
  const durExact = !!state.durExact && Number(state.dur) > 0;
  const dur = durExact ? Number(state.dur) : 0;

  return {
    key: key,
    ck: makeCacheKey(source.tabId, title, artist),
    app: appNameForUrl(),
    title: title,
    artist: artist,
    playing: !!state.playing,
    pos: Math.max(0, Number(state.pos) || 0),
    dur: dur,
    dur_exact: durExact,
    frac: clamp01(state.frac),
    art: "",
    art_new: false,
    bands: Array.isArray(state.bands) ? state.bands.slice(0, 20) : [],
    url: source.url,
    tabId: source.tabId,
  };
}

// Перезапрос сразу, а не в конце интервала: переключение трека должно
// отражаться в виджете без задержки в 700 мс.
function onTabUpdated(tabId, changeInfo) {
  if (changeInfo && changeInfo.status === "complete") {
    schedulePoll(0);
  }
}

function onTabRemoved() {
  schedulePoll(0);
}

function schedulePoll(delay) {
  setTimeout(() => {
    pollTabs();
  }, delay || 0);
}

function startTabPolling() {
  if (pollTimer !== null) {
    return;
  }
  pollTimer = setInterval(pollTabs, POLL_INTERVAL_MS);
  log("опрос вкладок каждые", POLL_INTERVAL_MS, "мс");
}