// Раздел «Настройки», подвкладки: «Основные» (сервер модели и модель, параметры модели, окно по умолчанию,
// лимит шагов, память),
// «Инструменты» агента (tools.js; там же окно браузера), «Скиллы» (skills.js) и «Заплатки» (guards.js).
// Всё хранится в data/settings.json и действует со следующего хода.
import { api } from "./api.js";
import { showGuards } from "./guards.js";
import { showSkills } from "./skills.js";
import { showTools } from "./tools.js";
import { el, recall, remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);

const PANES = ["general", "tools", "skills", "guards"];

let bound = false;

// Открытая подвкладка запоминается: после перезагрузки страница возвращается на неё.
export function showSettings(toast, onSaved) {
  if (!bound) {
    bound = true;
    for (const button of document.querySelectorAll("#settings-tabs button")) {
      button.addEventListener("click", () => showPane(button.dataset.pane, toast, onSaved));
    }
  }
  const saved = recall("settings-pane");
  return showPane(PANES.includes(saved) ? saved : "general", toast, onSaved);
}

async function showPane(pane, toast, onSaved) {
  remember("settings-pane", pane);
  for (const button of document.querySelectorAll("#settings-tabs button")) button.setAttribute("aria-pressed", String(button.dataset.pane === pane));
  for (const name of PANES) $(`#settings-${name}`).hidden = name !== pane;
  if (pane === "skills") return showSkills(toast);
  if (pane === "guards") return showGuards(toast);
  if (pane === "tools") return showTools(toast);
  try {
    render(await api.settings(), toast, onSaved);
  } catch (error) {
    toast(error);
  }
}

function field(label, input, hint = "") {
  const node = el("label", "setting");
  node.append(el("span", "setting-name", label), input);
  if (hint) node.append(el("span", "hint", hint));
  return node;
}

function input(type, value, attributes = {}) {
  const node = el("input");
  Object.assign(node, { type, value: value ?? "", ...attributes });
  return node;
}

function option(value, label) {
  return Object.assign(el("option"), { value, textContent: label });
}

// Подключение добавляется явно. Оба типа используют OpenAI-совместимый протокол; у внешнего API есть ключ.
function modelBlock(settings, toast, onSaved) {
  const kind = el("select");
  kind.append(option("llama", "Локальный LLaMA-сервер"), option("openai", "OpenAI-совместимый API"));
  kind.value = settings.llm_kind || (settings.llm_api_key_set ? "openai" : "llama");
  const address = input("url", settings.llm_base_url || settings.env_base_url, { placeholder: "http://127.0.0.1:8080/v1" });
  const key = input("password", "", { placeholder: settings.llm_api_key_set ? "Ключ сохранён; оставьте пустым, чтобы не менять" : "API-ключ" });
  let keyChanged = false;
  key.addEventListener("input", () => { keyChanged = true; });
  const check = el("button", "", "Проверить подключение");
  check.type = "button";
  const probe = el("span", "hint");
  const model = el("select");
  const load = async () => {
    key.closest("label").hidden = kind.value !== "openai";
    const chosen = model.value || settings.model;
    const url = address.value.trim();
    if (!url) {
      model.replaceChildren(option("", "Сначала укажите адрес"));
      probe.textContent = "";
      return;
    }
    probe.textContent = "Проверяю…";
    probe.className = "hint";
    let names = [];
    try {
      const current = url.replace(/\/$/, "") === settings.active_base_url.replace(/\/$/, "") && !keyChanged;
      names = current ? await api.models() : await api.probeModels(url, kind.value === "openai" ? key.value : "");
      probe.textContent = `✓ Сервер доступен, моделей: ${names.length}`;
      probe.className = "ok";
    } catch (error) {
      probe.textContent = `✕ ${error.message}`;
      probe.className = "error";
    }
    model.replaceChildren(option("", "Выберите модель"), ...names.map((name) => option(name, name)));
    // Сохранённой модели нет на этом сервере: показываем это, а не подставляем молча другую.
    if (chosen && !names.includes(chosen)) model.append(option(chosen, `${chosen} — нет на сервере`));
    model.value = chosen;
  };
  kind.addEventListener("change", () => {
    key.closest("label").hidden = kind.value !== "openai";
  });
  check.addEventListener("click", load);
  const server = el("div", "setting-row");
  server.append(address, check);
  const keyField = field("API-ключ", key, "Хранится локально в data/settings.json и отправляется только выбранному API.");
  keyField.hidden = kind.value !== "openai";
  const reset = el("button", "", "Сбросить подключение");
  reset.type = "button";
  reset.addEventListener("click", async () => {
    try {
      const saved = await api.saveSettings({ llm_kind: "disabled", llm_base_url: "", llm_api_key: "", model: "" });
      onSaved(saved);
      render(saved, toast, onSaved);
    } catch (error) {
      toast(error);
    }
  });
  const params = paramsBlock(settings);
  model.addEventListener("change", () => params.show(model.value));
  const nodes = [
    field("Тип подключения", kind, "Локальный llama.cpp/LM Studio/Ollama или любой OpenAI-совместимый API."),
    field("Адрес API", server, "Адрес OpenAI-совместимого API, обычно с /v1 на конце."), keyField, probe,
    field("Модель", model, `Сейчас: ${settings.active_model || "не выбрана"}. Её же можно сменить над полем ввода чата.`),
    reset,
  ];
  void load().then(() => params.show(model.value));
  return { nodes, params: params.node, values: () => ({
    llm_kind: kind.value, llm_base_url: address.value.trim(), model: model.value, ...params.values(),
    ...(keyChanged ? { llm_api_key: kind.value === "openai" ? key.value : "" } : {}),
  }) };
}

// Параметры выбранной модели. Умеет ли она думать и её собственные значения сообщает сервер (Ollama);
// не сообщает — переключатель рассуждений доступен, а поля пустые: решает сервер.
// Параметр без галки в запрос не идёт; сохраняются только заданные.
function paramsBlock(settings) {
  const node = el("section", "settings-group");
  let rows = () => ({});
  let current = "";
  const show = async (model) => {
    current = model;
    rows = () => ({});
    if (!model) {
      node.replaceChildren(el("h3", "", "Параметры модели"), el("p", "hint", "Сначала выберите модель."));
      return;
    }
    node.replaceChildren(el("h3", "", `Параметры модели · ${model}`), el("p", "hint", "Узнаю у сервера…"));
    let info;
    try {
      info = await api.modelInfo(model);
    } catch (error) {
      node.replaceChildren(el("h3", "", "Параметры модели"), el("p", "error", error.message));
      return;
    }
    if (current !== model) return;
    const saved = settings.model_options?.[model] ?? {};
    const thinking = thinkingRow(info.thinking, saved.thinking);
    const params = info.parameters.map((spec) => paramRow(spec, info.defaults[spec.name], saved[spec.name]));
    node.replaceChildren(el("h3", "", `Параметры модели · ${model}`), thinking.node, ...params.map((row) => row.node));
    rows = () => Object.assign({}, thinking.value(), ...params.map((row) => row.value()));
  };
  return {
    node, show,
    values: () => (current ? { model_options: { ...settings.model_options, [current]: rows() } } : {}),
  };
}

function thinkingRow(capable, saved) {
  const box = Object.assign(el("input"), { type: "checkbox", checked: capable === false ? false : saved ?? true, disabled: capable === false });
  const node = el("label", "setting check");
  const hint = capable === true ? "Включено — модель сначала рассуждает (видно в чате над ответом): лучше на сложных задачах "
    + "и многошаговых планах, но в 2–3 раза медленнее и тратит токены окна. Выключено — быстрые ответы на простое."
    : capable === false ? "Модель не умеет думать." : "Сервер не сообщает, умеет ли модель думать.";
  node.append(box, el("span", "setting-name", "Думать перед ответом"), el("span", "hint", hint));
  return { node, value: () => (capable === false ? {} : { thinking: box.checked }) };
}

function paramRow(spec, fallback, saved) {
  const own = saved !== undefined;
  const box = Object.assign(el("input"), { type: "checkbox", checked: own, title: "Задать своё значение" });
  const number = input("number", own ? saved : fallback ?? "", {
    min: spec.min, max: spec.max, step: spec.step, disabled: !own, placeholder: "решает сервер",
  });
  box.addEventListener("change", () => {
    number.disabled = !box.checked;
    if (!box.checked) number.value = fallback ?? "";
  });
  const node = el("div", "setting param");
  const head = el("label", "param-head");
  head.append(box, el("span", "setting-name", spec.label));
  node.append(head, number, el("span", "hint", `${spec.hint} ${fallback === undefined
    ? "Без галки — решает сервер." : `Без галки — значение модели: ${fallback}.`}`));
  return { node, value: () => (box.checked && number.value !== "" ? { [spec.name]: Number(number.value) } : {}) };
}

function render(settings, toast, onSaved, adding = false) {
  const configured = settings.configured || adding;
  const block = configured ? modelBlock(settings, toast, onSaved) : null;
  const windowSize = input("number", settings.default_context_tokens, { min: 1024, step: 1024 });
  const rounds = input("number", settings.max_tool_rounds, { min: 1, max: 200 });
  const memory = input("number", settings.memory_auto_update_messages, { min: 0 });
  const status = el("span", "hint");
  const save = el("button", "primary", "Сохранить");
  save.type = "button";
  save.addEventListener("click", async () => {
    try {
      const saved = await api.saveSettings({
        ...(block ? block.values() : {}),
        default_context_tokens: Number(windowSize.value), max_tool_rounds: Number(rounds.value),
        memory_auto_update_messages: Number(memory.value),
      });
      status.textContent = "Сохранено: действует со следующего хода.";
      onSaved(saved);
    } catch (error) {
      status.textContent = error.message;
    }
  });
  const actions = el("div", "editor-actions settings-actions");
  actions.append(status, save);
  const add = el("button", "primary", "Добавить подключение");
  add.type = "button";
  add.addEventListener("click", () => render(settings, toast, onSaved, true));
  const modelGroup = configured
    ? group("Модель", ...block.nodes)
    : group("Модель", el("p", "hint", "Подключение к модели не настроено."), add);
  $("#settings").replaceChildren(
    modelGroup,
    ...(block ? [block.params] : []),
    group("Ход агента",
      field("Окно по умолчанию, токенов", windowSize, "Если сервер модели сам не сообщает размер окна."),
      field("Лимит шагов за ход", rounds, "Дальше — карточка «продолжить или остановить».")),
    group("Память", field("Обновлять память каждые N сообщений", memory, "0 — только вручную, кнопкой в разделе «Память».")),
    actions,
  );
}

// Группа настроек — колонка: на широком экране группы стоят рядом.
function group(title, ...nodes) {
  const node = el("section", "settings-group");
  node.append(el("h3", "", title), ...nodes);
  return node;
}
