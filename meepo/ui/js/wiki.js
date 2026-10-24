// Раздел «Вики»: заметки списком по темам или графом, поиск и теги над списком, читалка справа.
// Чат вики — слева (wikichat.js). Перенос старой страницы вики без изменения поведения.
import { api } from "./api.js";
import { renderMarkdown } from "./chat.js";
import { createGraph } from "./graph.js";
import { makeResizable } from "./resize.js";
import { guide, stem, treeRow } from "./tree.js";
import { el, recall, remember } from "./util.js";
import { initWikiChat } from "./wikichat.js";

const $ = (selector) => document.querySelector(selector);

// Строки детей одной группы: ветки «├─», у последней — «└─».
function branch(items, lines, build) {
  return items.flatMap((item, index) => build(item, lines, index === items.length - 1));
}

const INDEX_PAGE = "index.md";
// Сколько заметок в «Недавних» и сколько тегов видно над списком, пока не нажали «ещё».
const RECENT_SHOWN = 5;
const TAGS_SHOWN = 10;
const UNTAGGED = "без тегов";
// Открытая заметка запоминается: после перезагрузки страницы она открывается снова.
let wikiCurrent = recall("wiki-page");
const wikiClosed = new Set();
// Все заметки с описаниями и тегами: по ним список фильтруется сразу, без запроса к серверу.
let wikiPages = [];
// Выбранный тег сужает список до его заметок; null — все заметки.
let wikiTag = null;
let wikiAllTags = false;
// Последний показанный список: после открытия заметки он перерисовывается без нового запроса
// и без перестановки «Недавних» под курсором.
let wikiShown = { query: "", textHits: [], recent: [] };
// «Список» — заметки по темам, «Граф» — точки и ссылки между ними; читалка справа в обоих видах.
let wikiMode = recall("wiki-mode") === "graph" ? "graph" : "list";
let wikiGraph = null;

export async function showWiki(toast) {
  try {
    const query = $("#wiki-search").value.trim();
    const graphShown = wikiMode === "graph" && !query && !wikiTag;
    renderWikiMode(toast, graphShown);
    wikiPages = await api.wiki();
    renderWikiTags(toast);
    if (graphShown) {
      wikiGraph ??= createGraph($("#wiki-graph"), { onOpen: (path) => openWiki(path, toast) });
      wikiGraph.update(await api.wikiGraph(), wikiCurrent);
    } else {
      const textHits = query ? await api.wikiSearch(query) : [];
      renderWikiList({ query, textHits, recent: recentPages() }, toast);
    }
    if (wikiCurrent && !query) await openWiki(wikiCurrent, toast, false);
  } catch (error) {
    toast(error);
  }
}

// Стрелки ходят по списку, Enter в поиске открывает первую найденную заметку, Esc очищает поиск.
export function wikiKeys(event, toast) {
  const search = $("#wiki-search");
  const rows = [...$("#wiki-list").querySelectorAll("button.trow-main")];
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const next = rows.indexOf(document.activeElement) + (event.key === "ArrowDown" ? 1 : -1);
    (next < 0 ? search : rows[Math.min(next, rows.length - 1)])?.focus();
  } else if (event.key === "Enter" && event.target === search) {
    event.preventDefault();
    rows.find((row) => !row.closest(".group"))?.click();
  } else if (event.key === "Escape" && (search.value || wikiTag)) {
    // Esc здесь только очищает поиск: ответ в основном чате он не останавливает.
    event.stopPropagation();
    search.value = "";
    wikiTag = null;
    search.focus();
    void showWiki(toast);
  }
}

// Поиск и выбранный тег всегда показывают список: граф по ним не фильтруется.
function renderWikiMode(toast, graphShown) {
  $("#wiki-split").classList.toggle("graph-mode", graphShown);
  $("#wiki-list").hidden = graphShown;
  $("#wiki-graph").hidden = !graphShown;
  for (const tab of $("#wiki-tabs").querySelectorAll("button")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.mode === wikiMode));
    tab.onclick = () => {
      wikiMode = tab.dataset.mode;
      remember("wiki-mode", wikiMode);
      $("#wiki-search").value = "";
      wikiTag = null;
      void showWiki(toast);
    };
  }
}

// Теги над списком — самые частые первыми, с числом заметок.
function renderWikiTags(toast) {
  const tags = tagCounts();
  const shown = wikiAllTags ? tags : tags.slice(0, TAGS_SHOWN);
  if (wikiTag && !shown.some(([tag]) => tag === wikiTag)) shown.push([wikiTag, 0]);
  const chips = shown.map(([tag, count]) => tagChip(tag, toast, count));
  if (tags.length > TAGS_SHOWN) {
    const more = el("button", "chip", wikiAllTags ? "свернуть" : `ещё ${tags.length - TAGS_SHOWN}`);
    more.type = "button";
    more.addEventListener("click", () => {
      wikiAllTags = !wikiAllTags;
      renderWikiTags(toast);
    });
    chips.push(more);
  }
  $("#wiki-tags").replaceChildren(...chips);
}

function tagCounts() {
  const counts = new Map();
  for (const page of wikiPages) for (const tag of page.tags) counts.set(tag, (counts.get(tag) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ru"));
}

// Тег над списком и под заметкой: щелчок оставляет в списке только его заметки, повторный — снимает фильтр.
function tagChip(tag, toast, count = 0) {
  const chip = el("button", `chip${tag === wikiTag ? " on" : ""}`, `#${tag}`);
  chip.type = "button";
  chip.title = tag === wikiTag ? "Показать все заметки" : `Только заметки с тегом «${tag}»`;
  if (count) chip.append(el("span", "chip-count", ` ${count}`));
  chip.addEventListener("click", () => {
    wikiTag = tag === wikiTag ? null : tag;
    void showWiki(toast);
  });
  return chip;
}

// Недавние: заметки по последнему касанию — открыли здесь или изменили (вы или агент), что позже.
// Только что созданная заметка поэтому сразу наверху, а не за открытыми раньше.
function recentPages() {
  const opened = openedAt();
  const touched = (page) => Math.max(opened[page.path] ?? 0, Date.parse(page.updated_at) || 0);
  return [...wikiPages].sort((a, b) => touched(b) - touched(a)).slice(0, RECENT_SHOWN);
}

// Когда заметки открывали здесь: { путь: время }. Старая запись — просто список путей, без времени.
function openedAt() {
  try {
    const stored = JSON.parse(recall("wiki-opened"));
    if (stored && typeof stored === "object" && !Array.isArray(stored)) return stored;
  } catch {
    // Испорченная запись — просто без открытых.
  }
  return {};
}

// Без поиска — «Недавние» и заметки по темам (главный тег — самый частый в вики из тегов заметки);
// с поиском — совпадения в названии, описании и тегах (сразу, без сервера), ниже — найденное в тексте.
function renderWikiList(shown, toast) {
  wikiShown = shown;
  const { query, textHits, recent } = shown;
  const root = $("#wiki-list");
  if (!wikiPages.length) {
    root.replaceChildren(el("p", "hint", "Заметок пока нет."));
    return;
  }
  const pages = wikiTag ? wikiPages.filter((page) => page.tags.includes(wikiTag)) : wikiPages;
  // Длинное название в узкой колонке обрезано: целиком — с описанием — во всплывающей подсказке.
  const item = (page, detail = "") => ({ path: page.path, title: page.title, hint: [page.title, page.description].join("\n"), detail });
  let sections;
  if (query) {
    const words = query.toLowerCase().split(/\s+/);
    const named = pages.filter((page) => {
      const text = [page.title, page.description, ...page.tags].join(" ").toLowerCase();
      return words.every((word) => text.includes(word));
    });
    const skip = new Set(named.map((page) => page.path));
    const allowed = new Set(pages.map((page) => page.path));
    const inText = [...new Map(textHits.map((hit) => [hit.path, hit])).values()]
      .filter((hit) => allowed.has(hit.path) && !skip.has(hit.path));
    sections = [
      ["В названии и тегах", named.map((page) => item(page, page.description))],
      ["В тексте", inText.map((hit) => ({ path: hit.path, title: hit.title, hint: hit.path, detail: hit.text }))],
    ];
  } else if (wikiTag) {
    sections = [[`#${wikiTag}`, byTitle(pages).map((page) => item(page))]];
  } else {
    const counts = new Map(tagCounts());
    const topics = new Map();
    for (const page of pages) {
      const topic = page.tags.reduce((best, tag) => (counts.get(tag) > (counts.get(best) ?? 0) ? tag : best), null);
      const name = topic ? `#${topic}` : UNTAGGED;
      topics.set(name, [...(topics.get(name) ?? []), page]);
    }
    const ordered = [...topics].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0], "ru"));
    sections = [
      ["Недавние", recent.map((page) => item(page))],
      ...ordered.map(([name, group]) => [name, byTitle(group).map((page) => item(page))]),
    ];
  }
  const rows = sections.filter(([, items]) => items.length).flatMap(([name, items]) => {
    // Найденное не сворачивается: свёрнутая группа спрятала бы результат поиска.
    const opened = Boolean(query) || !wikiClosed.has(name);
    const head = treeRow({
      label: `${name} · ${items.length}`, group: true, open: opened,
      onClick: query ? undefined : () => {
        if (!wikiClosed.delete(name)) wikiClosed.add(name);
        renderWikiList(wikiShown, toast);
      },
    });
    return opened ? [head, ...branch(items, [], (entry, lines, last) => treeRow({
      prefix: guide(lines, last), label: entry.title, title: entry.hint, current: entry.path === wikiCurrent,
      onClick: () => openWiki(entry.path, toast), detail: entry.detail, detailPrefix: stem(lines, last),
    }))] : [head];
  });
  if (!query && !wikiTag) {
    rows.push(treeRow({
      label: INDEX_PAGE, note: "индекс", current: wikiCurrent === INDEX_PAGE, onClick: () => openWiki(INDEX_PAGE, toast),
      title: "Индекс: строка на каждую заметку, агент обновляет его сам",
    }));
  }
  root.replaceChildren(...(rows.length ? rows : [el("p", "hint", "Ничего не нашлось.")]));
}

function byTitle(pages) {
  return [...pages].sort((a, b) => a.title.localeCompare(b.title, "ru"));
}

// Заметка по пути — из чата вики: пути в ответах агента открывают её здесь. Модель иногда портит в имени
// букву-две (латинская s вместо «с»): тогда открывается заметка с ближайшим именем.
export function openNote(path, toast) {
  void openWiki(closestNote(path), toast);
}

const TYPOS_ALLOWED = 2;

function closestNote(path) {
  if (wikiPages.some((page) => page.path === path)) return path;
  let best = null;
  let bestDistance = TYPOS_ALLOWED + 1;
  for (const page of wikiPages) {
    const distance = editDistance(path, page.path);
    if (distance < bestDistance) [best, bestDistance] = [page.path, distance];
  }
  return best ?? path;
}

function editDistance(a, b) {
  if (Math.abs(a.length - b.length) > TYPOS_ALLOWED) return TYPOS_ALLOWED + 1;
  let row = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i++) {
    const next = [i];
    for (let j = 1; j <= b.length; j++) {
      next[j] = Math.min(row[j] + 1, next[j - 1] + 1, row[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    row = next;
  }
  return row[b.length];
}

async function openWiki(path, toast, refreshList = true) {
  try {
    const page = await api.wikiPage(path);
    wikiCurrent = path;
    remember("wiki-page", path);
    if (path !== INDEX_PAGE) {
      const opened = Object.entries({ ...openedAt(), [path]: Date.now() }).sort((a, b) => b[1] - a[1]);
      remember("wiki-opened", JSON.stringify(Object.fromEntries(opened.slice(0, RECENT_SHOWN))));
    }
    const reader = $("#wiki-page");
    reader.replaceChildren(el("p", "hint", `data/wiki/${path}`));
    // Теги — как в списке: варианты одного слова там уже сведены к одному тегу.
    const tags = wikiPages.find((item) => item.path === path)?.tags ?? page.meta?.tags.map((tag) => tag.toLowerCase().trim());
    if (page.meta) reader.append(noteMeta(page.meta, tags, toast));
    // Картинки заметки лежат в вики рядом с ней: адрес — от папки заметки.
    renderMarkdown(reader, page.body, true, (src) => {
      const file = wikiPath(path, src);
      return file ? `/api/wiki/file?path=${encodeURIComponent(file)}` : null;
    });
    // Ссылки на другие заметки открываются здесь же, а не переходом браузера.
    for (const link of reader.querySelectorAll("a[data-href]")) {
      const target = wikiTarget(path, link.dataset.href);
      if (!target) continue;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        void openWiki(target, toast);
      });
    }
    wikiGraph?.setCurrent(path);
    if (refreshList && !$("#wiki-list").hidden) renderWikiList(wikiShown, toast);
  } catch (error) {
    if (refreshList) {
      toast(error);
      return;
    }
    // Запомненной заметки больше нет (удалили или слили): забываем её без ошибки.
    wikiCurrent = null;
    remember("wiki-page", "");
    $("#wiki-page").replaceChildren(el("p", "hint", "Выберите страницу."));
  }
}

// Поля OKF над текстом: тип, теги (щелчок — заметки с этим тегом) и описание.
function noteMeta(meta, tags, toast) {
  const box = el("div", "note-meta");
  const chips = el("div", "note-chips");
  if (meta.type) chips.append(el("span", "chip type", meta.type));
  for (const tag of tags) chips.append(tagChip(tag, toast));
  if (chips.childElementCount) box.append(chips);
  if (meta.description) box.append(el("p", "note-description", meta.description));
  return box;
}

// Путь заметки по относительной ссылке из заметки from; внешние ссылки и не .md — null.
function wikiTarget(from, href) {
  const path = wikiPath(from, href);
  return path?.endsWith(".md") ? path : null;
}

// Путь файла вики по относительной ссылке из заметки from; внешние ссылки — null.
function wikiPath(from, href) {
  if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("/") || href.startsWith("#")) return null;
  const clean = decodeURIComponent(href.split("#")[0]);
  const parts = from.split("/").slice(0, -1);
  for (const part of clean.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/");
}

// Поиск заметок, чат вики, разделители колонок и Ctrl+K; open() — открыть раздел «Вики».
export function initWikiPage(toast, open) {
  let searchTimer = null;
  $("#wiki-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => showWiki(toast), 250);
  });
  $("#wiki-search").addEventListener("keydown", (event) => wikiKeys(event, toast));
  $("#wiki-list").addEventListener("keydown", (event) => wikiKeys(event, toast));
  initWikiChat({ toast });
  makeResizable($("#wiki-chat-resizer"), { grid: $("#view-wiki"), variables: ["--wiki-chat-w"], min: 280 });
  makeResizable($("#wiki-list-resizer"), {
    grid: $("#wiki-split"),
    variables: ["--wiki-list-w", "--wiki-graph-w"],
    pick: () => ($("#wiki-split").classList.contains("graph-mode") ? "--wiki-graph-w" : "--wiki-list-w"),
    min: 160,
  });
  // Ctrl+K — найти заметку вики из любого раздела. По коду клавиши: в русской раскладке это «Л».
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.code !== "KeyK" || document.querySelector("dialog[open]")) return;
    event.preventDefault();
    open();
    $("#wiki-search").focus();
    $("#wiki-search").select();
  });
}
