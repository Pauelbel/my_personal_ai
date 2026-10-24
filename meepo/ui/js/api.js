// HTTP API приложения. Ошибки сервера превращаются в Error с понятным текстом.

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) throw await responseError(response);
  return response.status === 204 ? null : response.json();
}

async function responseError(response) {
  let message = `Ошибка ${response.status}`;
  try {
    const detail = (await response.json()).detail;
    message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((item) => item.msg).join("; ") : message;
  } catch {
    // Ответ без JSON: оставляем код ошибки.
  }
  return new Error(message);
}

const json = (method, body) => ({ method, body: JSON.stringify(body) });
const q = (params) => new URLSearchParams(params).toString();

// Поток событий хода (SSE). Заканчивается событием done или error. body === null — подключение к идущему ходу.
async function stream(path, body, signal, onEvent) {
  const response = await fetch(`/api${path}`, body === null ? { signal } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  });
  if (!response.ok) throw await responseError(response);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = chunk.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
      if (!data) continue;
      const event = JSON.parse(data);
      finished ||= ["done", "error"].includes(event.type);
      onEvent(event);
    }
  }
  if (!finished) throw new Error("Соединение закрыто до конца ответа.");
}

export const api = {
  projects: () => request("/projects"),
  createProject: (name, path) => request("/projects", json("POST", { name, path })),
  updateProject: (id, name, path) => request(`/projects/${id}`, json("PUT", { name, path })),
  deleteProject: (id) => request(`/projects/${id}`, { method: "DELETE" }),
  files: (projectId, path) => request(`/projects/${projectId}/files?${q({ path })}`),
  file: (projectId, path) => request(`/projects/${projectId}/files/content?${q({ path })}`),
  saveFile: (projectId, path, content) => request(`/projects/${projectId}/files/content?${q({ path })}`, json("PUT", { content })),
  moveFile: (projectId, path, newPath) => request(`/projects/${projectId}/files/move?${q({ path })}`, json("POST", { new_path: newPath })),
  deleteFile: (projectId, path) => request(`/projects/${projectId}/files?${q({ path })}`, { method: "DELETE" }),
  folders: (path) => request(`/folders${path ? `?${q({ path })}` : ""}`),

  sessions: () => request("/sessions"),
  createSession: (projectId) => request("/sessions", json("POST", { project_id: projectId })),
  renameSession: (id, title) => request(`/sessions/${id}/title`, json("PUT", { title })),
  deleteSession: (id) => request(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id) => request(`/sessions/${id}/messages`),

  send: (id, content, signal, onEvent, attachments = []) => stream(`/sessions/${id}/turn`, { content, attachments }, signal, onEvent),
  // Картинка как data URL → её имя для сообщения (чат вики).
  attach: (data) => request("/attachments", json("POST", { data })),
  turn: (id) => request(`/sessions/${id}/turn`),
  follow: (id, start, signal, onEvent) => stream(`/sessions/${id}/turn/stream?${q({ start })}`, null, signal, onEvent),
  stop: (id) => request(`/sessions/${id}/turn/stop`, { method: "POST" }),
  decide: (id, decision) => request(`/sessions/${id}/approve`, json("POST", decision)),

  skills: () => request("/skills"),
  skill: (name) => request(`/skills/${encodeURIComponent(name)}`),
  saveSkill: (name, files) => request(`/skills/${encodeURIComponent(name)}/files`, json("PUT", { files })),
  setSecret: (name, secret, value) => request(`/skills/${encodeURIComponent(name)}/secrets/${secret}`, json("PUT", { value })),
  deleteSecret: (name, secret) => request(`/skills/${encodeURIComponent(name)}/secrets/${secret}`, { method: "DELETE" }),
  rollback: (name, version) => request(`/skills/${encodeURIComponent(name)}/rollback`, json("POST", { version })),

  wiki: () => request("/wiki"),
  wikiSearch: (query) => request(`/wiki/search?${q({ q: query })}`),
  wikiPage: (path) => request(`/wiki/page?${q({ path })}`),
  wikiGraph: () => request("/wiki/graph"),
  // Чат вики — служебная сессия вне проектов: её сообщения, ход и карточки — обычными запросами сессии.
  wikiChat: () => request("/wiki/chat"),
  ensureWikiChat: () => request("/wiki/chat", { method: "POST" }),
  clearWikiChat: () => request("/wiki/chat", { method: "DELETE" }),
  wikiActions: () => request("/wiki/actions"),
  saveWikiActions: (actions) => request("/wiki/actions", json("PUT", { actions })),

  memory: () => request("/memory"),
  saveMemory: (name, content) => request(`/memory/${encodeURIComponent(name)}`, json("PUT", { content })),
  updateMemory: (sessionId) => request(`/sessions/${sessionId}/memory/update`, { method: "POST" }),

  settings: () => request("/settings"),
  saveSettings: (fields) => request("/settings", json("PUT", fields)),
  // Заплатки для маленькой модели: включена ли, описание и исходный код файла.
  guards: () => request("/guards"),
  // Встроенные инструменты агента по темам: описание, параметры, исходный код; у браузера — окно.
  tools: () => request("/tools"),
  // Модели сервера: текущего или по адресу (проверка до сохранения настроек).
  models: (baseUrl = "") => request(`/models${baseUrl ? `?${q({ base_url: baseUrl })}` : ""}`),
  modelInfo: (model) => request(`/models/info?${q({ model })}`),
  probeModels: (baseUrl, apiKey = "") => request("/models/probe", json("POST", { base_url: baseUrl, api_key: apiKey })),

  restart: () => request("/restart", { method: "POST" }),
  // Сервер снова отвечает (после перезапуска): false, пока он не поднялся.
  alive: () => fetch("/api/settings").then((response) => response.ok, () => false),
};
