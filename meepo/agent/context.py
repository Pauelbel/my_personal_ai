"""Контекст запроса к модели: системный промпт, затем история сессии в формате OpenAI API, и сжатие под окно.

Ответ модели с вызовами и результаты этих вызовов идут вместе: у каждого вызова в контексте есть результат
(ход могли прервать посреди раунда — тогда результат «вызов был прерван»).

Сжатие (fit) без вызова модели: не влезает — сначала старые снимки браузера, затем самые старые результаты
инструментов заменяются пометкой «[результат read_file docs/a.md убран из контекста …]»; не помогло —
выпадают самые старые сообщения целиком (вызов вместе с результатами). Последняя реплика пользователя
и последние результаты остаются всегда. Огромная вставка пользователя уходит в файл проекта (stash_paste).
"""

import hashlib
import json
import math
from collections.abc import Callable
from datetime import date
from pathlib import Path

MESSAGE_OVERHEAD_TOKENS = 4
INTERRUPTED_CALL = "Вызов инструмента был прерван"
# Последние результаты инструментов нужны целиком: по ним модель делает следующий шаг.
KEEP_RECENT_RESULTS = 2
# Короткие результаты заменять пометкой нет смысла: она почти такой же длины.
MIN_COMPACT_CHARS = 300
# Огромная вставка в чат — файлом в проекте, в разговоре — её начало, конец и путь.
INBOX = ".meepo/inbox"
PASTE_TOKENS = 8000
PASTE_HEAD_CHARS = 1500
PASTE_TAIL_CHARS = 500
# Картинки модель видит в последних репликах пользователя — чтобы можно было переспросить о них; у более
# старых остаются только имена. Картинка стоит модели сотни токенов, оценка — с запасом.
IMAGE_MESSAGES = 3
IMAGE_TOKENS = 800
NO_VISION = "Эта модель не видит картинок: что на ней, ты не знаешь — так и скажи пользователю. Имя картинки для вставки в заметку — выше."


def estimate_tokens(text: str | None) -> int:
    """Грубая оценка: около 4 байт UTF-8 на токен, так же считает UI."""
    return math.ceil(len((text or "").encode("utf-8")) / 4)


def message_tokens(message: dict) -> int:
    calls = sum(estimate_tokens(call["name"] + call["arguments"]) for call in message.get("tool_calls") or ())
    return MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.get("content")) + calls + IMAGE_TOKENS * len(message.get("images") or ())


def history_messages(history: list[dict], images: Callable[[str], Path | None] | None = None) -> list[dict]:
    """Сохранённые сообщения → сообщения для модели. Служебные поля (meta, stats, reasoning) модели не нужны.
    images — файл картинки по имени: имена картинок пишутся в текст реплики, сами картинки (присланные в чат
    и открытые через view_image) модель видит в IMAGE_MESSAGES последних сообщениях с ними."""
    results = {item["tool_call_id"]: item for item in history if item["role"] == "tool"}
    with_images = [item["id"] for item in history if item.get("attachments") or item.get("images")]
    seen = set(with_images[-IMAGE_MESSAGES:])
    messages: list[dict] = []
    for item in history:
        role = item["role"]
        if role == "tool":
            # Результаты встают сразу за своим вызовом, ниже.
            continue
        if role == "assistant" and item.get("tool_calls"):
            messages.append({"role": "assistant", "content": item.get("content") or None, "tool_calls": item["tool_calls"]})
            for call in item["tool_calls"]:
                result = results.get(call["id"])
                message = {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "about": about(call),
                           "content": result["content"] if result else INTERRUPTED_CALL}
                if result and result.get("images"):
                    files = [str(path) for name in result["images"] if images and result["id"] in seen and (path := images(name))]
                    if files:
                        message["images"] = files
                    else:
                        message["content"] += "\n\n[Картинку уже не видно: открой её через view_image снова, если нужна]"
                messages.append(message)
            continue
        if role == "user" and item.get("attachments"):
            files = [str(path) for name in item["attachments"] if images and item["id"] in seen and (path := images(name))]
            names = ", ".join(item["attachments"])
            note = f"[Картинки: {names}]" if files else f"[Картинки: {names} — их уже не видно, попроси прислать снова]"
            message = {"role": "user", "content": f"{item['content']}\n\n{note}".strip()}
            if files:
                message["images"] = files
            messages.append(message)
            continue
        if role == "user" and messages and messages[-1]["role"] == "user" and not messages[-1].get("images"):
            # После неудачного хода в истории остаётся реплика без ответа: склеиваем, чтобы роли чередовались.
            messages[-1] = {"role": "user", "content": f"{messages[-1]['content']}\n\n{item['content']}"}
            continue
        messages.append({"role": role, "content": item.get("content") or ""})
    return messages


def build(system: str, history: list[dict], images: Callable[[str], Path | None] | None = None) -> list[dict]:
    return [{"role": "system", "content": system}, *history_messages(history, images)]


def total_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    return sum(message_tokens(item) for item in messages) + (estimate_tokens(str(tools)) if tools else 0)


def about(call: dict) -> str:
    """«read_file docs/a.md» — чем был вызов: для пометки, которая заменяет его результат при сжатии."""
    try:
        arguments = json.loads(call.get("arguments") or "{}")
    except ValueError:
        arguments = {}
    arguments = arguments if isinstance(arguments, dict) else {}
    target = next((str(arguments[key]) for key in ("path", "url", "command", "name", "query", "action") if arguments.get(key)), "")
    return f"{call['name']} {target[:80]}".strip()


def reserve(window: int) -> int:
    """Запас окна под ответ модели."""
    return min(4096, max(window // 4, 512))


def _marker(message: dict) -> str:
    again = "сними snapshot заново" if message.get("name") == "browser" else "прочитай снова"
    return f"[результат {message.get('about') or message.get('name', 'инструмента')} убран из контекста — при необходимости {again}]"


def _groups(messages: list[dict]) -> list[list[int]]:
    """Индексы сообщений после системного промпта группами: вызов вместе с его результатами — одна группа."""
    groups: list[list[int]] = []
    for index in range(1, len(messages)):
        if messages[index]["role"] == "tool" and groups:
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def fit(messages: list[dict], budget: int) -> tuple[list[dict], str]:
    """Сообщения, которые помещаются в budget токенов, и что пришлось сделать (пусто — ничего)."""
    total = sum(message_tokens(item) for item in messages)
    if total <= budget:
        return messages, ""
    result = [dict(item) for item in messages]
    tools = [index for index, item in enumerate(result) if item["role"] == "tool"]
    recent = set(tools[-KEEP_RECENT_RESULTS:])
    snapshots = [index for index in tools if result[index].get("name") == "browser" and "Элементы (ref" in (result[index].get("content") or "")]
    # Старые снимки страницы — первыми: они самые большие и устаревают быстрее всего.
    order = [index for index in snapshots[:-1]] + [index for index in tools if index not in snapshots[:-1]]
    replaced = 0
    for index in order:
        if total <= budget:
            break
        if index in recent or len(result[index].get("content") or "") < MIN_COMPACT_CHARS:
            continue
        before = message_tokens(result[index])
        result[index]["content"] = _marker(result[index])
        total -= before - message_tokens(result[index])
        replaced += 1
    dropped = 0
    if total > budget:
        groups = _groups(result)
        last_user = max((number for number, group in enumerate(groups) if result[group[0]]["role"] == "user"), default=len(groups) - 1)
        # Выпадают сначала группы до последней реплики пользователя, затем шаги текущего хода, кроме последних.
        order = list(range(last_user)) + list(range(last_user + 1, max(len(groups) - KEEP_RECENT_RESULTS, last_user + 1)))
        removed: set[int] = set()
        for number in order:
            if total <= budget:
                break
            removed.update(groups[number])
            total -= sum(message_tokens(result[index]) for index in groups[number])
            dropped += len(groups[number])
        result = [item for index, item in enumerate(result) if index not in removed]
    parts = [f"заменено результатов: {replaced}" if replaced else "", f"выпало сообщений: {dropped}" if dropped else ""]
    return result, ", ".join(part for part in parts if part)


def stash_paste(project: Path, content: str, budget: int) -> str:
    """Реплика больше четверти окна (но не больше PASTE_TOKENS) → файл .meepo/inbox/<дата>-<хэш>.md в проекте;
    возвращает текст для разговора: начало, конец и путь. Обычная реплика возвращается как есть."""
    if estimate_tokens(content) <= min(PASTE_TOKENS, budget // 4):
        return content
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    relative = f"{INBOX}/{date.today().isoformat()}-{digest}.md"
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(content, encoding="utf-8")
    note = (f"[Вставлен большой текст: {len(content)} символов, {content.count(chr(10)) + 1} строк. Целиком он сохранён "
            f"в файл проекта {relative}, здесь — только начало и конец. Читай файл через read_file (сначала оглавление, "
            "затем куски по offset); работу по всему тексту делай скиллом.]")
    return f"{content[:PASTE_HEAD_CHARS].rstrip()}\n\n…\n\n{content[-PASTE_TAIL_CHARS:].lstrip()}\n\n{note}"
