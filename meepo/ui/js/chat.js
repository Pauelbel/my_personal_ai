// Чат: история сессии и идущий ход. Разговор идёт ходами: реплика пользователя, под ней одна свёрнутая строка
// «ход работы» (рассуждения, промежуточные реплики, вызовы инструментов, журнал), затем итоговый ответ.
// Пока ход идёт, в чате только его строка: результатов ещё нет, и наружу ничего не выходит.
import { renderDiagrams } from "./diagrams.js";
import { highlightCode } from "./highlight.js";
import { appendMarkdown } from "./markdown.js";
import { el, parse, plural } from "./util.js";

const STEP_ICON = { done: "✓", failed: "✕", pending: "◌" };
const USER_FOLD_CHARS = 600;
const USER_FOLD_LINES = 10;
const STOPPED_TEXT = "_(ход остановлен)_";
// Раскрытые строки остаются раскрытыми после перерисовки.
const opened = new Set();
const unfolded = new Set();

// Шаги хода человеческими словами: [что сделано, группа для заголовка строки хода].
const TOOL_STEPS = {
  list_dir: ["Содержимое папки", "files"], read_file: ["Прочитан файл", "files"], search_files: ["Поиск по файлам", "files"],
  write_file: ["Записан файл", "files"], edit_file: ["Изменён файл", "files"], delete_path: ["Удалено", "files"],
  move_path: ["Перемещено", "files"], make_dir: ["Создана папка", "files"], plan: ["План задачи", null],
  read_skill: ["Прочитан скилл", "skills"], create_skill: ["Создан скилл", "skills"], update_skill: ["Изменён скилл", "skills"],
  run_skill: ["Запуск скилла", "skills"], run_command: ["Команда", "command"], browser: ["Браузер", "browser"],
  remember: ["Запомнено", "memory"], web_search: ["Поиск в интернете", "web"], fetch_url: ["Прочитана страница", "web"],
  view_image: ["Просмотрена картинка", "web"],
};
const ACTIVITY = {
  files: ["Работаю с файлами", "Работал с файлами"], skills: ["Работаю со скиллами", "Работал со скиллами"],
  command: ["Выполняю команды", "Выполнял команды"], browser: ["Работаю в браузере", "Работал в браузере"],
  memory: ["Вспоминаю", "Обращался к памяти"], web: ["Ищу в интернете", "Искал в интернете"],
  other: ["Выполняю шаги", "Выполнил шаги"],
};

function describe(name) {
  return TOOL_STEPS[name] ?? [name, "other"];
}

// Заголовок строки хода: чем модель была занята больше всего. Без вызовов — «Думаю».
export function activityOf(names, now) {
  const counts = new Map();
  for (const name of names) {
    const group = describe(name)[1];
    if (group) counts.set(group, (counts.get(group) ?? 0) + 1);
  }
  if (!counts.size) return names.length ? ACTIVITY.other[now ? 0 : 1] : now ? "Думаю" : "Думал";
  const [top] = [...counts].sort((a, b) => b[1] - a[1])[0];
  return ACTIVITY[top][now ? 0 : 1];
}

// running — ход в последней реплике ещё идёт: его процесс показывает строка идущего хода (Live), а не история.
export function renderMessages(container, messages, { running = false } = {}) {
  container.replaceChildren();
  if (!messages.length) {
    const empty = el("div", "empty");
    const mascot = el("img");
    Object.assign(mascot, { src: "/ui/assets/meepo-mascot.png", alt: "" });
    empty.append(mascot, el("strong", "", "Привет! Я Мипо."), "Опишите задачу — я сделаю её в папке проекта.");
    container.append(empty);
    return;
  }
  const results = new Map(messages.filter((item) => item.role === "tool").map((item) => [item.tool_call_id, item]));
  const all = turns(messages);
  all.forEach((turn, index) => {
    if (turn.head) container.append(userNode(turn.head));
    if (!(running && index === all.length - 1)) renderTurn(container, turn, results, index === all.length - 1);
  });
}

// Ход начинается репликой пользователя; ответ на карточку словами (card_reply) — часть того же хода.
function turns(messages) {
  const list = [];
  let turn = null;
  for (const message of messages) {
    if (message.role === "tool") continue;
    if (message.role === "user" && !message.card_reply) {
      list.push(turn = { head: message, items: [] });
      continue;
    }
    if (!turn) list.push(turn = { head: null, items: [] });
    turn.items.push(message);
  }
  return list;
}

function renderTurn(container, turn, results, last) {
  const tail = turn.items.at(-1);
  const final = tail?.role === "assistant" && !tail.tool_calls?.length ? tail : null;
  const work = [];
  for (const item of turn.items) {
    if (item.reasoning) work.push({ thought: item });
    if (item === final) continue;
    if (item.role === "user") work.push({ reply: item });
    else if (item.content?.trim()) work.push({ note: item });
    if (item.tool_calls?.length) work.push({ calls: item.tool_calls, message: item });
  }
  const log = final?.log ?? [];
  if (work.length || log.length) container.append(workNode(work, results, log, last && !final ? "прервано" : ""));
  if (final) container.append(assistantNode(final, last));
}

function workNode(work, results, log, state) {
  const key = `work:${(work[0]?.thought ?? work[0]?.note ?? work[0]?.message ?? work[0]?.reply)?.id ?? log[0]}`;
  const node = el("details", "work");
  remember(node, key);
  const names = work.flatMap((part) => (part.calls ?? []).map((call) => call.name));
  const head = el("summary", "work-head");
  head.append(el("span", "work-icon", "◇"), el("span", "work-title", activityOf(names, false)),
    el("span", "work-meta", meta([names.length && `${names.length} ${plural(names.length, "шаг", "шага", "шагов")}`, state])),
    el("span", "work-caret", "▾"));
  const body = el("div", "work-body");
  const steps = el("div", "steps");
  for (const part of work) {
    if (part.thought) steps.append(thoughtNode(part.thought.id, part.thought.reasoning));
    else if (part.note) steps.append(noteNode(part.note.id, part.note.content));
    else if (part.reply) steps.append(replyNode(part.reply.content));
    else for (const call of part.calls) steps.append(stepNode(call, results.get(call.id)));
  }
  if (steps.childElementCount) body.append(steps);
  if (log.length) body.append(logNode(log.join("\n")));
  node.append(head, body);
  return node;
}

export function meta(parts) {
  const text = parts.filter(Boolean).join(" · ");
  return text ? `· ${text}` : "";
}

function preview(text, limit = 110) {
  const line = text.replace(/[#*_`>|]|-{3,}|:-+:?/g, " ").replace(/\s+/g, " ").trim();
  return line.length > limit ? `${line.slice(0, limit - 1)}…` : line;
}

function remember(node, key) {
  node.open = opened.has(key);
  node.addEventListener("toggle", () => (node.open ? opened.add(key) : opened.delete(key)));
}

function row(className, key, icon, name, target, metaText, body) {
  const node = el("details", `step ${className}`);
  remember(node, key);
  const summary = el("summary");
  summary.append(el("span", "step-icon", icon), el("span", "step-name", name), el("span", "step-target", target),
    el("span", "step-meta", metaText));
  node.append(summary, body);
  return node;
}

export function thoughtNode(id, text) {
  return row("thought", `thought:${id}`, "∴", "Размышление", preview(text), `${text.length} симв.`, el("pre", "", text));
}

export function noteNode(id, text) {
  const body = el("div", "text step-text");
  renderMarkdown(body, text);
  return row("note", `note:${id}`, "›", "Заметка", preview(text), "", body);
}

function replyNode(text) {
  return row("note", `reply:${text}`, "↳", "Ваш ответ", preview(text), "", el("pre", "", text));
}

export function stepNode(call, result) {
  const state = !result ? "pending" : result.is_error ? "failed" : "done";
  const args = parse(call.arguments);
  const target = args.path || args.name || args.query || args.url || args.source || args.command || args.action || "";
  const text = el("pre");
  text.textContent = `${call.name} ${JSON.stringify(args, null, 2)}\n\n→ ${result?.content ?? "—"}`;
  // Картинка, которую посмотрел агент (view_image), — миниатюрой над подробностями шага.
  const images = result?.meta?.images ?? [];
  const body = images.length ? el("div") : text;
  if (images.length) body.append(imagesNode(images), text);
  const metaText = state === "failed" ? "ошибка" : state === "pending" ? "…" : "";
  const node = row(state, `call:${call.id}`, STEP_ICON[state], describe(call.name)[0], String(target), metaText, body);
  node.querySelector(".step-name").title = call.name;
  return node;
}

export function logNode(text) {
  const node = el("details", "live-log");
  node.append(el("summary", "", "журнал"), el("pre", "", text));
  return node;
}

function userNode(message) {
  const node = el("article", "msg user");
  const bubble = el("div", "bubble");
  bubble.append(el("div", "bubble-text", message.content));
  if (message.attachments?.length) bubble.prepend(imagesNode(message.attachments));
  node.append(bubble);
  if (message.content.length > USER_FOLD_CHARS || message.content.split("\n").length > USER_FOLD_LINES) {
    const fold = el("button", "msg-action fold");
    fold.type = "button";
    const sync = () => {
      const open = unfolded.has(message.id);
      node.classList.toggle("folded", !open);
      fold.textContent = open ? "Свернуть" : "Показать полностью";
    };
    fold.addEventListener("click", () => {
      if (!unfolded.delete(message.id)) unfolded.add(message.id);
      sync();
    });
    sync();
    bubble.append(fold);
  }
  return node;
}

// Картинки реплики или шага view_image: миниатюры, щелчок открывает картинку целиком.
function imagesNode(names) {
  const box = el("div", "msg-images");
  for (const name of names) {
    const link = el("a");
    Object.assign(link, { href: `/api/attachments/${name}`, target: "_blank", rel: "noopener" });
    const image = el("img");
    Object.assign(image, { src: link.href, alt: name, loading: "lazy" });
    link.append(image);
    box.append(link);
  }
  return box;
}

function assistantNode(message, last) {
  const node = el("article", "msg assistant");
  const body = el("div", "text");
  renderMarkdown(body, message.content);
  node.append(body);
  if (message.content.trim() === STOPPED_TEXT) return node;
  const foot = el("div", `msg-foot${message.stats && last ? "" : " bare"}`);
  if (message.stats) foot.append(el("span", "stats", statsText(message.stats)));
  foot.append(copyButton("msg-action push", () => message.content));
  node.append(foot);
  return node;
}

function statsText(stats) {
  return [
    duration(stats.seconds), stats.input_tokens != null && `вход ${compact(stats.input_tokens)}`,
    stats.output_tokens != null && `выход ${compact(stats.output_tokens)}`, stats.rounds > 1 && `запросов: ${stats.rounds}`,
  ].filter(Boolean).join(" · ");
}

export function compact(value) {
  if (value < 1000) return String(value);
  return value < 1e4 ? `${(value / 1e3).toFixed(1).replace(/\.0$/, "").replace(".", ",")}k` : `${Math.round(value / 1e3)}k`;
}

export function duration(seconds) {
  if (seconds < 60) return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1).replace(".", ",")} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${Math.round(seconds - minutes * 60)} с`;
}

function copyButton(className, text) {
  const button = el("button", className, "Копировать");
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text());
      button.textContent = "Скопировано";
    } catch {
      button.textContent = "Не удалось";
    }
    setTimeout(() => { button.textContent = "Копировать"; }, 1500);
  });
  return button;
}

// images — адрес картинки по ссылке из текста (заметки вики); по умолчанию картинки не показываются.
export function renderMarkdown(target, text, withTools = true, images = () => null) {
  appendMarkdown(target, text);
  for (const image of target.querySelectorAll("img[data-src]")) {
    const url = images(image.dataset.src);
    if (!url) {
      // Чужие адреса не грузим: картинка из интернета выдала бы, что страницу открыли.
      image.replaceWith(document.createTextNode(`[картинка: ${image.alt || image.dataset.src}]`));
      continue;
    }
    const link = el("a", "md-image");
    Object.assign(link, { href: url, target: "_blank", rel: "noopener" });
    Object.assign(image, { src: url, loading: "lazy" });
    image.replaceWith(link);
    link.append(image);
  }
  for (const code of target.querySelectorAll("pre > code")) {
    highlightCode(code, code.dataset.language);
    if (withTools) code.parentElement.append(copyButton("copy", () => code.textContent));
  }
  // Диаграммы — только в готовом тексте: пока ответ печатается, блок ещё не дописан.
  if (withTools) renderDiagrams(target);
}

// Похоже ли на путь: с «/» — любое, без — имя файла с известным расширением. «:42» в конце — строка.
const FILE_EXTENSIONS = new Set(("md markdown txt py js mjs cjs ts tsx jsx json yaml yml toml ini cfg conf env xml html htm css scss "
  + "sh bash ps1 sql csv log go rs java kt c h cpp hpp cs rb php vue lock").split(" "));

function pathOf(text) {
  const value = text.trim();
  if (!value || value.length > 300 || /[\n<>{}()=,;|*?"'`]|:\/\//.test(value) || value.startsWith("-")) return null;
  const [, raw, line] = /^(.*?)(?::(\d+))?(?::\d+)?$/.exec(value);
  const path = raw.replace(/\/+$/, "");
  if (!path) return null;
  if (!path.includes("/")) {
    const extension = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase();
    if (/\s/.test(path) || !FILE_EXTENSIONS.has(extension)) return null;
  }
  return { path, line: line ? Number(line) : null };
}

export function markPath(node, text) {
  const found = pathOf(text);
  if (!found) return;
  node.classList.add("path-link");
  node.dataset.path = found.path;
  if (found.line) node.dataset.line = String(found.line);
  node.title = `Открыть во вкладке «Файлы»${found.line ? `, строка ${found.line}` : ""}`;
}

// Пути в `коде` ответов — ссылки на вкладку «Файлы»: щелчок ловит app.js по классу path-link.
export function markPaths(root) {
  for (const code of root.querySelectorAll(".msg :not(pre) > code, .step-target")) markPath(code, code.textContent);
}

export function scrollDown(container) {
  container.scrollTop = container.scrollHeight;
}
