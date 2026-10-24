// Снимок страницы для инструмента browser: видимые интерактивные элементы получают атрибут data-meepo-ref
// (e1, e2, …), по нему агент кликает и вводит текст. Возвращает заголовок, адрес, текст страницы и элементы
// с ролью, доступным именем и тем, из чего строится локатор Playwright (подпись, placeholder, test id, id, name).
() => {
  for (const node of document.querySelectorAll("[data-meepo-ref]")) node.removeAttribute("data-meepo-ref");
  const selector = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=switch]", "[role=combobox]", "[role=textbox]", "[contenteditable=''], [contenteditable=true]",
  ].join(", ");
  const clean = (text) => (text || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const visible = (node) => {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const roleOf = (node) => {
    const explicit = node.getAttribute("role");
    if (explicit) return explicit;
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute("type") || "text").toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "select") return node.multiple || node.size > 1 ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox" || type === "radio") return type;
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      return "textbox";
    }
    if (node.isContentEditable) return "textbox";
    return "generic";
  };
  const labelOf = (node) => {
    if (node.labels && node.labels.length) return clean(node.labels[0].innerText);
    const ids = node.getAttribute("aria-labelledby");
    if (ids) return clean(ids.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
    return "";
  };
  const nameOf = (node, role, label) => {
    const aria = node.getAttribute("aria-label");
    if (aria) return clean(aria);
    if (label) return label;
    if (["textbox", "searchbox", "combobox", "spinbutton"].includes(role)) {
      return clean(node.getAttribute("placeholder") || node.getAttribute("title"));
    }
    if (node.tagName === "INPUT" && ["button", "submit", "reset"].includes(node.type)) return clean(node.value);
    if (node.tagName === "INPUT" && node.type === "image") return clean(node.alt);
    return clean(node.innerText || node.getAttribute("title") || node.querySelector("img")?.alt);
  };
  const effectOf = (node, role, name) => {
    const operation = node.closest(".opblock");
    if (operation) {
      const method = clean(operation.querySelector(".opblock-summary-method")?.textContent).toUpperCase();
      const path = clean(operation.querySelector(".opblock-summary-path")?.getAttribute("data-path")
        || operation.querySelector(".opblock-summary-path")?.textContent);
      if (method && path) return `${method} ${path}${/execute/i.test(name) ? " — отправка API-запроса" : ""}`;
    }
    const form = node.closest("form");
    if (form && (role === "button" || ["INPUT", "SELECT", "TEXTAREA"].includes(node.tagName))) {
      return `${(form.method || "GET").toUpperCase()} ${form.action || location.href} — отправка формы`;
    }
    if (node.tagName === "A" && node.href) return `Переход на ${node.href}`;
    if (role === "button") return `Нажатие кнопки «${name}» может изменить состояние страницы или отправить запрос`;
    return "";
  };
  const items = [];
  for (const node of document.querySelectorAll(selector)) {
    if (!visible(node)) continue;
    const role = roleOf(node);
    const label = labelOf(node);
    const ref = `e${items.length + 1}`;
    node.setAttribute("data-meepo-ref", ref);
    const secret = node.type === "password";
    const name = nameOf(node, role, label);
    items.push({
      ref, role, label, name, effect: effectOf(node, role, name), tag: node.tagName.toLowerCase(),
      placeholder: clean(node.getAttribute("placeholder")),
      testid: node.getAttribute("data-testid") || node.getAttribute("data-test") || "",
      id: node.id && /^[A-Za-z][\w-]*$/.test(node.id) ? node.id : "",
      nameAttr: node.getAttribute("name") || "",
      value: ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName) && !secret ? clean(node.value) : "",
      checked: Boolean(node.checked), disabled: Boolean(node.disabled),
    });
  }
  const text = (document.body?.innerText || "").replace(/\n\s*\n+/g, "\n").trim();
  return { title: document.title, url: location.href, text, items };
}
