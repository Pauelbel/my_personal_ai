"""browser: живой браузер на Playwright, один инструмент с action. Группа доверия browser.

open(url), snapshot() — текст страницы и интерактивные элементы с ref, ролью, именем и готовым локатором
Playwright (по нему агент пишет автотест), click(ref), type(ref, text), press(key), select(ref, value),
highlight(refs) — визуально выделить найденные элементы на странице, screenshot() — файл в .meepo/screenshots/, close().
Браузер запускается при первом вызове, свой на каждую сессию, и живёт до конца сессии или close.
Окно видимое (headed) по умолчанию — переключатель в настройках. Нет скачанного Chromium — пробуется
установленный Google Chrome; нет и его — понятная ошибка с командой установки.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from meepo.agent.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_MS = 10_000
TEXT_CHARS = 4000
MAX_ELEMENTS = 120
INSTALL_HINT = "Браузер не установлен: выполните один раз `uv run playwright install chromium` в папке Мипо"
SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
ACTIONS = ("open", "snapshot", "highlight", "click", "type", "press", "select", "screenshot", "close")
INTERACTIVE_ACTIONS = {"click", "type", "press", "select"}

HIGHLIGHT_JS = """async (refs) => {
  document.querySelectorAll('[data-meepo-highlighted]').forEach((node) => node.removeAttribute('data-meepo-highlighted'));
  document.querySelectorAll('.meepo-highlight-overlay').forEach((node) => node.remove());
  const found = refs.map((ref) => [ref, document.querySelector(`[data-meepo-ref="${CSS.escape(ref)}"]`)]).filter(([, node]) => node);
  if (found[0]) found[0][1].scrollIntoView({block: 'center', inline: 'center'});
  await new Promise((resolve) => requestAnimationFrame(resolve));
  for (const [ref, node] of found) {
    node.setAttribute('data-meepo-highlighted', '');
    let target = node;
    let rect = target.getBoundingClientRect();
    while ((rect.width < 2 || rect.height < 2) && target.parentElement) {
      target = target.parentElement;
      rect = target.getBoundingClientRect();
    }
    const box = document.createElement('div');
    box.className = 'meepo-highlight-overlay';
    Object.assign(box.style, {position:'absolute',left:`${rect.left + scrollX - 4}px`,top:`${rect.top + scrollY - 4}px`,width:`${Math.max(rect.width + 8, 16)}px`,height:`${Math.max(rect.height + 8, 16)}px`,zIndex:'2147483646',border:'4px solid #ff2d20',borderRadius:'5px',boxSizing:'border-box',background:'rgba(255,230,0,.18)',boxShadow:'0 0 0 2px #fff,0 0 0 6px rgba(255,45,32,.3)',pointerEvents:'none'});
    const label = document.createElement('div');
    label.textContent = ref;
    Object.assign(label.style, {position:'absolute',left:'-4px',top:'-27px',padding:'3px 8px',borderRadius:'4px 4px 0 0',background:'#ff2d20',color:'#fff',font:'bold 14px/20px sans-serif',whiteSpace:'nowrap'});
    box.append(label);
    document.body.append(box);
  }
  return found.map(([ref]) => ref);
}"""


class BrowserPool:
    """Браузеры сессий. headed() — показывать ли окно (настройка читается при запуске браузера)."""

    def __init__(self, headed: Callable[[], bool]) -> None:
        self._headed = headed
        self._playwright = None
        self._sessions: dict[str, dict] = {}

    def described(self, session_id: str, ref: str) -> str:
        """Как элемент назывался в последнем снимке: «кнопка «Войти»» — для карточки подтверждения."""
        item = self._sessions.get(session_id, {}).get("elements", {}).get(ref, {})
        return item.get("description", "") if isinstance(item, dict) else str(item)

    def effect(self, session_id: str, ref: str) -> str:
        item = self._sessions.get(session_id, {}).get("elements", {}).get(ref, {})
        return item.get("effect", "") if isinstance(item, dict) else ""

    async def page(self, session_id: str):
        entry = self._sessions.get(session_id)
        if entry and not entry["page"].is_closed():
            return entry["page"]
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        headed = self._headed()
        browser = await self._launch(headed)
        # У видимого Chrome страница занимает всё настоящее окно. Фиксированный viewport нужен только
        # фоновому браузеру: иначе в развёрнутом окне остаётся узкая область страницы слева.
        context = await browser.new_context(**({"no_viewport": True} if headed else {"viewport": {"width": 1280, "height": 860}}))
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        page = await context.new_page()
        self._sessions[session_id] = {"browser": browser, "page": page, "elements": {}}
        return page

    async def _launch(self, headed: bool):
        from playwright.async_api import Error

        options = {"headless": not headed, "args": ["--start-maximized"] if headed else []}
        try:
            return await self._playwright.chromium.launch(**options)
        except Error as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
        try:
            return await self._playwright.chromium.launch(**options, channel="chrome")
        except Error:
            raise ValueError(INSTALL_HINT) from None

    def remember(self, session_id: str, elements: dict[str, str]) -> None:
        if session_id in self._sessions:
            self._sessions[session_id]["elements"] = elements

    async def close(self, session_id: str) -> bool:
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False
        try:
            await entry["browser"].close()
        except Exception:  # noqa: BLE001 — браузер мог уже закрыть сам пользователь
            logger.debug("Браузер сессии %s уже закрыт", session_id)
        return True

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def locator(item: dict, counts: dict) -> str:
    """Готовый локатор Playwright для автотеста: роль и имя, подпись, placeholder, test id, id или name — что однозначно."""
    role, name = item["role"], item["name"]
    if name and role != "generic" and counts[("role", role, name)] == 1:
        return f"page.get_by_role({_quote(role)}, name={_quote(name)})"
    if item.get("label") and counts[("label", item["label"])] == 1:
        return f"page.get_by_label({_quote(item['label'])})"
    if item.get("placeholder") and counts[("placeholder", item["placeholder"])] == 1:
        return f"page.get_by_placeholder({_quote(item['placeholder'])})"
    if item.get("testid"):
        return f"page.get_by_test_id({_quote(item['testid'])})"
    if item.get("id"):
        return f"page.locator({_quote('#' + item['id'])})"
    if item.get("nameAttr"):
        return f"page.locator({_quote(item['tag'] + '[name=' + _quote(item['nameAttr']) + ']')})"
    return f"page.get_by_role({_quote(role)}, name={_quote(name)}).nth({item['nth']})"


def render_snapshot(data: dict) -> tuple[str, dict[str, str]]:
    """Текст снимка и {ref: «роль «имя»»} для карточек."""
    items = data["items"][:MAX_ELEMENTS]
    counts: dict = {}
    for item in data["items"]:
        for key in (("role", item["role"], item["name"]), ("label", item.get("label")), ("placeholder", item.get("placeholder"))):
            counts[key] = counts.get(key, 0) + 1
    seen: dict = {}
    lines, elements = [], {}
    for item in items:
        key = (item["role"], item["name"])
        item["nth"] = seen.get(key, 0)
        seen[key] = item["nth"] + 1
        state = "".join(f", {word}" for word, on in (("отмечен", item.get("checked")), ("недоступен", item.get("disabled"))) if on)
        value = f", значение «{item['value']}»" if item.get("value") else ""
        lines.append(f"{item['ref']} {item['role']} «{item['name']}»{value}{state} — {locator(item, counts)}")
        elements[item["ref"]] = {"description": f"{item['role']} «{item['name']}»", "effect": item.get("effect", "")}
    text = data["text"].strip()
    if len(text) > TEXT_CHARS:
        text = text[:TEXT_CHARS] + f"\n… (текст страницы обрезан: {len(data['text'])} символов)"
    more = f"\n… и ещё {len(data['items']) - MAX_ELEMENTS} элементов" if len(data["items"]) > MAX_ELEMENTS else ""
    body = (f"Страница: {data['title']} — {data['url']}\n\nТекст страницы:\n{text or '(пусто)'}\n\n"
            "Элементы (ref роль «имя» — локатор Playwright):\n" + ("\n".join(lines) or "(нет)") + more)
    return body, elements


def _ref(args: dict) -> str:
    ref = str(args.get("ref") or "").strip()
    if not ref:
        raise ValueError(f"Для {args['action']} нужен ref элемента из последнего snapshot")
    return ref


def _highlight_refs(args: dict) -> list[str]:
    """Один ref тоже принимается: небольшим моделям так проще вызвать highlight."""
    values = args.get("refs")
    if not isinstance(values, list):
        values = [args.get("ref")] if args.get("ref") else []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


async def _element(page, args: dict):
    element = page.locator(f'[data-meepo-ref="{_ref(args)}"]')
    if await element.count() == 0:
        raise ValueError(f"Нет элемента {_ref(args)} на странице: сними snapshot заново и возьми ref оттуда")
    return element


def preview(args: dict, ctx: ToolContext) -> dict:
    action = args["action"]
    pool: BrowserPool = ctx.services["browsers"]
    if action == "open" and not str(args.get("url") or "").startswith(("http://", "https://", "file://")):
        raise ValueError("Для open нужен url, начинающийся с http:// или https://")
    target = {"open": args.get("url", ""), "press": args.get("key", "")}.get(action, "")
    if action in ("click", "type", "select"):
        ref = _ref(args)
        target = f"{ref} {pool.described(ctx.session_id, ref)}".strip()
        if action in ("type", "select"):
            target += f" ← «{args.get('text') or args.get('value') or ''}»"
    if action == "highlight":
        target = ", ".join(_highlight_refs(args))
    effect = pool.effect(ctx.session_id, str(args.get("ref") or "")) if action in INTERACTIVE_ACTIONS else ""
    text = effect or ("Это действие может изменить страницу или отправить сетевой запрос."
                      if action in INTERACTIVE_ACTIONS else "Открытие и чтение страницы без взаимодействия.")
    return {"title": f"Браузер: {action} {target}".strip(), "text": text, "effect": effect}


def authorize(args: dict, ctx: ToolContext) -> str | None:
    action = args.get("action")
    if action in INTERACTIVE_ACTIONS and ctx.browser_mode != "interact":
        return ("Действие заблокировано: для этого хода выбран режим браузера «Только изучать». "
                "Не взаимодействуй со страницей; объясни, что для click/type/select/press нужно выбрать режим "
                "«Взаимодействовать» перед отправкой следующей задачи")
    return None


async def browser(args: dict, ctx: ToolContext) -> ToolResult:
    from playwright.async_api import Error

    try:
        return await _act(args, ctx)
    except Error as exc:
        # Элемент не найден, не кликабелен, страница не открылась: модель получает текст ошибки и пробует иначе.
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return ToolResult(f"Браузер: {first}. Сними snapshot и попробуй снова.", True)


async def _act(args: dict, ctx: ToolContext) -> ToolResult:
    action = args["action"]
    pool: BrowserPool = ctx.services["browsers"]
    if action == "close":
        return ToolResult("Браузер закрыт" if await pool.close(ctx.session_id) else "Браузер и так не был открыт")
    page = await pool.page(ctx.session_id)
    if action == "open":
        await page.goto(args["url"], wait_until="domcontentloaded")
    elif action == "snapshot":
        body, elements = render_snapshot(await page.evaluate(SNAPSHOT_JS))
        pool.remember(ctx.session_id, elements)
        return ToolResult(body, meta={"snapshot": True})
    elif action == "highlight":
        refs = _highlight_refs(args)
        if not refs:
            raise ValueError("Для highlight нужен ref или список refs из последнего snapshot")
        found = await page.evaluate(HIGHLIGHT_JS, refs)
        missing = [ref for ref in refs if ref not in found]
        if not found:
            raise ValueError("Элементы не найдены: " + ", ".join(missing) + ". Сними snapshot заново")
        suffix = f"; не найдены: {', '.join(missing)}" if missing else ""
        return ToolResult(f"На странице выделены элементы: {', '.join(found)}{suffix}")
    elif action == "click":
        await (await _element(page, args)).click()
    elif action == "type":
        await (await _element(page, args)).fill(str(args.get("text", "")))
    elif action == "select":
        await (await _element(page, args)).select_option(str(args.get("value", "")))
    elif action == "press":
        await page.keyboard.press(str(args.get("key") or "Enter"))
    elif action == "screenshot":
        folder = ctx.project / ".meepo" / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        await page.screenshot(path=str(path), full_page=True)
        return ToolResult(f"Скриншот сохранён: {path.relative_to(ctx.project).as_posix()}")
    if action in ("click", "press"):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001 — страница не перезагружалась: ждать нечего
            pass
    return ToolResult(f"Готово: {action}. Сейчас открыто: {await page.title()} — {page.url}. "
                      "Чтобы увидеть страницу и элементы, сними snapshot.")


TOOLS = [
    Tool("browser", "Браузер: action open (url) — открыть страницу; snapshot — текст страницы и элементы с ref и готовыми "
         "локаторами Playwright (по ним пиши автотесты); highlight (refs) — обвести элементы на открытой странице; "
         "click (ref), type (ref, text), select (ref, value), "
         "press (key, например Enter); screenshot — снимок экрана в файл; close — закрыть браузер. "
         "ref берутся из последнего snapshot; после перехода на другую страницу сними snapshot заново.",
         {"type": "object", "properties": {
             "action": {"type": "string", "enum": list(ACTIONS)},
             "url": {"type": "string", "description": "Для open: адрес страницы"},
             "ref": {"type": "string", "description": "Для click, type, select: ref элемента из snapshot, например e5"},
             "refs": {"type": "array", "items": {"type": "string"}, "description": "Для highlight: ref элементов из snapshot"},
             "text": {"type": "string", "description": "Для type: что ввести"},
             "value": {"type": "string", "description": "Для select: значение или подпись пункта списка"},
             "key": {"type": "string", "description": "Для press: клавиша, например Enter, Tab, Escape"},
         }, "required": ["action"], "additionalProperties": False},
         browser, "browser", preview, lambda args: args.get("action") in INTERACTIVE_ACTIONS, authorize),
]
