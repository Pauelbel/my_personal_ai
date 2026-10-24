// Идущий ход: строится из событий хода, поэтому после перезагрузки страницы восстанавливается их повтором.
import { activityOf, duration, logNode, meta, noteNode, renderMarkdown, stepNode, thoughtNode } from "./chat.js";
import { el, plural } from "./util.js";

const FLUSH_MS = 80;
// Раскрыл пользователь строку хода — следующие ходы тоже показываются раскрытыми.
let liveOpen = false;

// Идущий ход — одна строка «◌ Работаю с файлами · 4 шага · 17 с ›». Внутри — шаги, размышление и текст текущего
// запроса, журнал. Строится из событий хода, поэтому после перезагрузки страницы восстанавливается их повтором.
export class Live {
  constructor(container) {
    this.container = container;
    this.node = el("article", "msg assistant streaming");
    this.details = el("details", "work live");
    this.details.open = liveOpen;
    this.details.addEventListener("toggle", () => { liveOpen = this.details.open; });
    const head = el("summary", "work-head live-head");
    this.title = el("span", "work-title");
    this.meta = el("span", "work-meta");
    head.append(el("span", "spinner"), this.title, this.meta, el("span", "work-caret", "▾"));
    this.steps = el("div", "steps");
    this.steps.hidden = true;
    this.reasoning = el("pre", "work-thought");
    this.reasoning.hidden = true;
    this.textNode = el("div", "work-note text");
    this.textNode.hidden = true;
    this.log = logNode("");
    const body = el("div", "work-body");
    body.append(this.steps, this.reasoning, this.textNode, this.log);
    this.details.append(head, body);
    this.node.append(this.details);
    this.calls = new Map();
    this.names = [];
    this.text = "";
    this.thought = "";
    this.state = "";
    this.progress = "";
    this.timer = null;
    this.started = Date.now();
    this.ticker = setInterval(() => this.#status(), 1000);
    this.#status();
    container.append(this.node);
  }

  // Одно событие хода: delta, reasoning, tool_call, tool_result, progress, approval, log.
  handle(event) {
    // Любое событие после карточки значит, что решение принято и ход идёт дальше.
    this.state = event.type === "approval" ? "ждёт решения" : "";
    if (event.type === "delta") this.#write(event.text);
    else if (event.type === "reasoning") {
      this.thought += event.text;
      this.reasoning.hidden = false;
      this.reasoning.textContent = this.thought;
    } else if (event.type === "tool_call") this.#call(event.call);
    else if (event.type === "retry") this.#retry(event.keep);
    else if (event.type === "tool_result") this.#result(event);
    else if (event.type === "progress") this.progress = event.text;
    else if (event.type === "log") this.log.querySelector("pre").textContent += `${event.text}\n`;
    this.#status();
  }

  finish() {
    clearTimeout(this.timer);
    clearInterval(this.ticker);
    this.node.remove();
  }

  #write(text) {
    this.text += text;
    this.timer ??= setTimeout(() => {
      this.timer = null;
      this.textNode.hidden = !this.text.trim();
      this.textNode.replaceChildren();
      renderMarkdown(this.textNode, this.text, false);
    }, FLUSH_MS);
  }

  // Модель позвала инструмент: её текст и рассуждения этого запроса становятся строками хода.
  #call(call) {
    this.steps.hidden = false;
    if (this.thought.trim()) this.steps.append(thoughtNode(`live-${this.names.length}`, this.thought));
    if (this.text.trim()) this.steps.append(noteNode(`live-${this.names.length}`, this.text));
    this.thought = this.text = "";
    this.reasoning.hidden = this.textNode.hidden = true;
    const node = stepNode(call, null);
    this.calls.set(call.id, { call, node });
    this.names.push(call.name);
    this.steps.append(node);
  }

  // Guard вернул модель к работе: напечатанное остаётся строкой хода (keep) или выбрасывается (повтор, ответ не по-русски).
  #retry(keep) {
    if (keep && this.text.trim()) {
      this.steps.hidden = false;
      this.steps.append(noteNode(`live-retry-${this.steps.childElementCount}`, this.text));
    }
    clearTimeout(this.timer);
    this.timer = null;
    this.thought = this.text = "";
    this.reasoning.hidden = this.textNode.hidden = true;
    this.textNode.replaceChildren();
  }

  #result(event) {
    const entry = this.calls.get(event.call_id);
    if (!entry) return;
    const result = { content: event.content, is_error: event.is_error, meta: event.meta };
    const node = stepNode(entry.call, result);
    entry.node.replaceWith(node);
    entry.node = node;
    this.state = "";
    this.progress = "";
  }

  #status() {
    const seconds = Math.floor((Date.now() - this.started) / 1000);
    const now = this.names.length && this.calls.size && [...this.calls.values()].at(-1).node.classList.contains("pending");
    this.title.textContent = now || this.names.length ? activityOf(this.names, true) : this.text ? "Пишу ответ" : "Думаю";
    this.meta.textContent = meta([
      this.names.length && `${this.names.length} ${plural(this.names.length, "шаг", "шага", "шагов")}`,
      this.progress, seconds && duration(seconds), this.state,
    ]);
  }
}
