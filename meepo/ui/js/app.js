// Точка входа интерфейса: проекты и сессии слева, чат с идущим ходом, карточки подтверждения, «Стоп».
import { api } from "./api.js";
import { renderCard } from "./cards.js";
import { markPaths, renderMessages, scrollDown } from "./chat.js";
import { initFiles, openPath, revealChange, showFiles } from "./files.js";
import { Live } from "./live.js";
import { initProjects, openProject } from "./projects.js";
import { initMemory, showMemory } from "./memory.js";
import { initWikiPage, showWiki } from "./wiki.js";
import { refreshWikiChat } from "./wikichat.js";
import { showSettings } from "./settings.js";
import { makeResizable } from "./resize.js";
import { renderSidebar as renderSidebarTree } from "./sidebar.js";
import { el, fitInput, recall, remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);

const state = {
  projects: [],
  sessions: [],
  current: null,
  messages: [],
  // Идущий ход открытой сессии: строка хода и чем оборвать чтение потока (сам ход на сервере идёт дальше).
  live: null,
  controller: null,
  card: null,
  collapsed: new Set(JSON.parse(recall("collapsed-projects") || "[]")),
};

function toast(error) {
  const node = $("#toast");
  node.textContent = error?.message || String(error);
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

const current = () => state.sessions.find((session) => session.id === state.current) ?? null;
const projectOf = (session) => state.projects.find((project) => project.id === session?.project_id) ?? null;

// ---------- проекты и сессии ----------

async function loadSessions() {
  [state.sessions, state.projects] = await Promise.all([api.sessions(), api.projects()]);
}

async function selectSession(id) {
  detach();
  showView("chat");
  state.current = id;
  state.card = current()?.pending ?? null;
  remember("session", id);
  if (window.matchMedia("(max-width: 900px)").matches) $("#sidebar").hidden = true;
  state.messages = await api.messages(id);
  const { running } = await api.turn(id);
  render({ running });
  if (running) void follow(id);
}

async function newSession(projectId) {
  try {
    const session = await api.createSession(projectId);
    state.collapsed.delete(projectId ?? "__no_project__");
    await loadSessions();
    await selectSession(session.id);
    $("#input").focus();
  } catch (error) {
    toast(error);
  }
}

// Что делает панель проектов по щелчкам (sidebar.js).
const sidebarActions = {
  select: (id) => selectSession(id),
  create: (projectId) => newSession(projectId),
  configure: (project) => openProject(project),
  remove: (session) => deleteSession(session),
  rename: async (session, title) => {
    try {
      await api.renameSession(session.id, title);
      await loadSessions();
    } catch (error) {
      toast(error);
    }
    render();
  },
};

function renderSidebar() {
  renderSidebarTree(state, sidebarActions);
}

async function deleteSession(session) {
  if (!window.confirm(`Удалить сессию «${session.title}» вместе с историей?`)) return;
  try {
    await api.deleteSession(session.id);
    await loadSessions();
    if (session.id === state.current) await openFirst();
    else render();
  } catch (error) {
    toast(error);
  }
}

// Открыть сессию: запомненную, иначе самую свежую; проектов нет — приветствие с выбором папки.
async function openFirst() {
  const saved = recall("session");
  const target = state.sessions.find((session) => session.id === saved) ?? state.sessions[0];
  if (target) return selectSession(target.id);
  if (state.projects.length) return newSession(state.projects[0].id);
  state.current = null;
  const empty = el("div", "empty");
  const mascot = el("img");
  Object.assign(mascot, { src: "/ui/assets/meepo-mascot.png", alt: "" });
  const chat = el("button", "primary", "Начать чат");
  chat.type = "button";
  chat.addEventListener("click", () => newSession(null));
  const choose = el("button", "primary", "Выбрать папку проекта");
  choose.type = "button";
  choose.addEventListener("click", () => openProject());
  empty.append(mascot, el("strong", "", "Привет! Я Мипо."), "Начните чат или выберите папку проекта.", chat, choose);
  $("#messages").replaceChildren(empty);
  renderChrome();
  renderSidebar();
}

// ---------- ход ----------

async function send(event) {
  event?.preventDefault();
  const session = current();
  const input = $("#input");
  const content = input.value.trim();
  if (state.live) return stop();
  if (!session || !content) return;
  input.value = "";
  fitInput(input);
  state.messages.push({ id: "local", role: "user", content, created_at: new Date().toISOString() });
  render({ running: true });
  await run(session.id, (signal, onEvent) => api.send(session.id, content, signal, onEvent));
}

// Подключиться к ходу, который уже идёт на сервере (после перезагрузки страницы или смены сессии).
async function follow(sessionId) {
  await run(sessionId, (signal, onEvent) => api.follow(sessionId, 0, signal, onEvent));
}

async function run(sessionId, open) {
  detach();
  const controller = new AbortController();
  state.controller = controller;
  state.live = new Live($("#messages"));
  $("#composer-error").hidden = true;
  renderChrome();
  renderSidebar();
  scrollDown($("#messages"));
  try {
    await open(controller.signal, (event) => {
      if (state.current !== sessionId) return;
      if (event.type === "approval") {
        state.card = event.card;
        renderCards();
      } else if (["tool_result", "done", "error"].includes(event.type) && state.card) {
        state.card = null;
        renderCards();
      }
      // Агент записал или поправил файл — во вкладке «Файлы» он открыт с подсвеченными изменениями.
      if (event.type === "tool_result" && !event.is_error && event.meta?.path) {
        setFilesPanel(true);
        void revealChange(event.meta.path);
      }
      if (event.type === "error") showError(event.message);
      else if (event.type !== "done") {
        const near = isNearBottom();
        state.live?.handle(event);
        if (near) scrollDown($("#messages"));
      }
    });
  } catch (error) {
    if (controller.signal.aborted) return;
    showError(error.message);
  }
  if (state.controller !== controller) return;
  detach();
  await loadSessions();
  if (state.current === sessionId) {
    state.messages = await api.messages(sessionId);
    render();
    scrollDown($("#messages"));
  }
}

function isNearBottom() {
  const box = $("#messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

// Перестать читать поток в этой вкладке; ход на сервере это не останавливает.
function detach() {
  state.controller?.abort();
  state.controller = null;
  state.live?.finish();
  state.live = null;
}

async function stop() {
  const session = current();
  if (!session) return;
  try {
    await api.stop(session.id);
  } catch (error) {
    toast(error);
  }
}

async function decide(decision) {
  try {
    await api.decide(state.current, decision);
    state.card = null;
    renderCards();
  } catch (error) {
    toast(error);
    state.messages = await api.messages(state.current);
    await loadSessions();
    state.card = current()?.pending ?? null;
    renderCards();
  }
}

function showError(message) {
  const node = $("#composer-error");
  node.textContent = `Ошибка: ${message}`;
  node.hidden = false;
}

// ---------- отрисовка ----------

function render({ running = Boolean(state.live) } = {}) {
  const box = $("#messages");
  const live = state.live?.node;
  renderMessages(box, state.messages, { running });
  markPaths(box);
  if (live) box.append(live);
  renderChrome();
  renderSidebar();
  renderCards();
}

function renderCards() {
  renderCard($("#cards"), state.card, decide);
}

// Файлы проекта справа от чата: открыта ли панель, запоминается; дерево перечитывается, когда панель открыли
// или сменился проект.
let filesOpen = recall("files-panel") !== "closed";
let filesProject = null;

function setFilesPanel(open) {
  filesOpen = open;
  remember("files-panel", open ? "open" : "closed");
  syncFilesPanel(projectOf(current()));
}

function syncFilesPanel(project) {
  const shown = Boolean(project) && filesOpen;
  $("#view-chat").classList.toggle("files-hidden", !shown);
  $("#files-open").hidden = !project || filesOpen;
  const key = shown ? project.id : null;
  if (key === filesProject) return;
  filesProject = key;
  if (shown) void showFiles(project);
}

function renderChrome() {
  const session = current();
  const project = projectOf(session);
  syncFilesPanel(project);
  $("#project-chip").textContent = project?.name ?? "";
  $("#project-chip").hidden = !project;
  $("#session-title").textContent = session?.title ?? "—";
  const busy = Boolean(state.live);
  const send = $("#send");
  send.textContent = busy ? "■" : "↵";
  send.title = busy ? "Остановить (Esc)" : "Отправить";
  send.disabled = !session;
  $("#stop-hint").hidden = !busy;
  renderUsage(session);
  $("#input").disabled = !session;
}

// Заполненность окна модели по последнему запросу: шкала из шести делений, как в терминале, процент и скорость.
function renderUsage(session) {
  const usage = $("#usage");
  if (!session?.context_tokens || !session.context_window) {
    usage.replaceChildren();
    usage.title = "";
    return;
  }
  const share = Math.min(session.context_tokens / session.context_window, 1);
  // Хотя бы одно деление закрашено, пока контекст не пуст.
  const cells = Math.max(Math.round(share * 6), 1);
  const meter = el("span", `meter-text${share >= 0.85 ? " full" : share >= 0.6 ? " warn" : ""}`, "▰".repeat(cells) + "▱".repeat(6 - cells));
  usage.title = `Последний запрос занял ≈${session.context_tokens} из ${session.context_window} токенов окна модели. `
    + "Не влезает — старые результаты инструментов заменяются пометкой, затем выпадают старые сообщения.";
  usage.replaceChildren(el("span", "usage-label", "контекст"), meter, el("span", "", `${Math.round(share * 100)}%`));
  if (session.tokens_per_second) usage.append(el("span", "speed", `· ${Math.round(session.tokens_per_second)} ток/с`));
}

// Модель над полем ввода: список моделей текущего сервера; выбор сохраняется в настройках и действует со следующего хода.
async function showModel(settings = null) {
  const select = $("#model-select");
  try {
    settings ??= await api.settings();
    select.parentElement.hidden = !settings.configured;
    if (!settings.configured) {
      select.replaceChildren();
      return;
    }
    const names = await api.models().catch(() => []);
    const active = settings.active_model;
    select.replaceChildren(...names.map((name) => Object.assign(el("option"), { value: name, textContent: name })));
    if (active && !names.includes(active)) {
      select.append(Object.assign(el("option"), { value: active, textContent: `${active} — нет на сервере` }));
    }
    if (!active) select.prepend(Object.assign(el("option"), { value: "", textContent: "модель не выбрана" }));
    select.value = active || "";
    select.title = `Сервер: ${settings.active_base_url}${names.length ? "" : " — недоступен"}`;
  } catch (error) {
    toast(error);
  }
}

async function changeModel() {
  try {
    await showModel(await api.saveSettings({ model: $("#model-select").value }));
  } catch (error) {
    toast(error);
  }
}

function showView(view) {
  for (const button of document.querySelectorAll(".tabs button")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  for (const node of document.querySelectorAll("main.view")) node.hidden = node.id !== `view-${view}`;
  remember("view", view);
  if (view === "wiki") {
    void showWiki(toast);
    void refreshWikiChat();
  }
  if (view === "memory") void showMemory(toast);
  if (view === "settings") void showSettings(toast, (saved) => void showModel(saved));
}

// Перезапуск сервера: ждём, пока новый процесс поднимется, и обновляем страницу.
async function restart() {
  if (state.live && !window.confirm("Идёт ход — перезапуск его оборвёт. Перезапустить Мипо?")) return;
  const button = $("#restart");
  button.disabled = true;
  button.classList.add("spinning");
  try {
    detach();
    await api.restart();
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (await api.alive()) return window.location.reload();
    }
    throw new Error("Мипо не поднялся за 30 секунд — посмотрите терминал, где он запущен.");
  } catch (error) {
    toast(error);
  }
  button.disabled = false;
  button.classList.remove("spinning");
}

function showTheme() {
  $("#theme-toggle").textContent = document.documentElement.dataset.theme === "dark" ? "☀" : "☾";
}

function bind() {
  $("#composer").addEventListener("submit", send);
  $("#model-select").addEventListener("change", changeModel);
  initFiles(toast);
  makeResizable($("#files-resizer"), { grid: $("#view-chat"), variables: ["--chat-w"], min: 360 });
  makeResizable($("#files-tree-resizer"), { grid: $("#files-split"), variables: ["--tree-w"], min: 140, max: 480 });
  $("#files-open").addEventListener("click", () => setFilesPanel(true));
  $("#files-close").addEventListener("click", () => setFilesPanel(false));
  // Путь в ответе агента открывает файл в панели справа.
  $("#messages").addEventListener("click", (event) => {
    const link = event.target.closest(".path-link");
    if (!link) return;
    event.preventDefault();
    setFilesPanel(true);
    void openPath(link.dataset.path.replace(/^\.\//, ""), Number(link.dataset.line) || null);
  });
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!state.live) void send();
    }
  });
  $("#input").addEventListener("input", () => fitInput($("#input")));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.live && !$("#view-chat").hidden && !document.querySelector("dialog[open]")) void stop();
  });
  for (const button of document.querySelectorAll(".tabs button")) button.addEventListener("click", () => showView(button.dataset.view));
  initMemory(toast, current);
  initWikiPage(toast, () => showView("wiki"));
  $("#new-project").addEventListener("click", () => openProject());
  $("#project-chip").addEventListener("click", () => openProject(projectOf(current())));
  initProjects({
    toast,
    saved: async (project, created) => {
      if (created) return newSession(project.id);
      await loadSessions();
      render();
    },
    deleted: async () => {
      await loadSessions();
      if (!current()) await openFirst();
      else render();
    },
  });
  $("#sessions-toggle").addEventListener("click", () => { $("#sidebar").hidden = !$("#sidebar").hidden; });
  $("#restart").addEventListener("click", () => void restart());
  $("#theme-toggle").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    remember("app-theme", theme);
    showTheme();
  });
}

async function start() {
  bind();
  showTheme();
  void showModel();
  try {
    // После перезагрузки страница открывается на том же разделе: запоминаем его до того, как откроется сессия.
    const view = recall("view");
    await loadSessions();
    await openFirst();
    if (view && view !== "chat" && document.querySelector(`#view-${view}`)) showView(view);
  } catch (error) {
    toast(error);
  }
}

void start();
