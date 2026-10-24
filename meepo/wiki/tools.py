"""Инструменты агента вики (перенос старых tools/wiki.py и fetch_url без изменения поведения).

Добавление заметки — два шага: wiki_match сверяет ключевые слова с index.md, wiki_save_note пишет заметку
по шаблону и её строку в индексе. Проверка вики — wiki_lint собирает материал, wiki_propose_edits показывает
пользователю список правок с галочками, и применяются только отмеченные. Поиск в интернете, чтение
страниц и просмотр картинок — общие с основным агентом (agent/tools/web.py).
Сервис вики — ctx.services["wiki"].
"""

from datetime import date

from meepo.agent.tools.base import Tool, ToolContext, ToolResult
from meepo.wiki.notes import MATCH_THRESHOLD
from meepo.wiki.service import NoteFields
from meepo.agent.tools import web
from meepo.agent.tools.web import images_footer

MAX_RESULT_CHARS = 6000
# Тексты заметок для lint: около 13 тысяч токенов — с запасом помещается в окно от 32k.
MAX_LINT_CHARS = 40_000
EDIT_KINDS = {"merge": "слить дубли", "contradiction": "противоречие", "outdated": "устарело", "fix": "исправить"}
# Сколько тегов показывать агенту: самые частые, редкие — хвост, который только раздувает ответ.
MAX_TAGS_SHOWN = 40
# Просьба пользователя в конце материала lint: что искать (действие из меню «Действия ▾» или своими словами).
MAX_REQUEST_CHARS = 1000


def _wiki(ctx: ToolContext):
    return ctx.services["wiki"]


def wiki_search(args: dict, ctx: ToolContext) -> ToolResult:
    rows = _wiki(ctx).search(args["query"])
    if not rows:
        return ToolResult("В вики ничего не нашлось.")
    parts, budget = [], MAX_RESULT_CHARS
    for path, title, _line, text in rows:
        body = text[:budget]
        budget -= len(body)
        parts.append(f"## {path} — {title}\n{body}")
        if budget <= 0:
            break
    return ToolResult("\n\n".join(parts) + "\n\nСтраницу целиком читай через wiki_read.")


def wiki_read(args: dict, ctx: ToolContext) -> ToolResult:
    path = str(args["path"])
    text = _wiki(ctx).read(path)
    return ToolResult(text + images_footer(path, text))


def wiki_match(args: dict, ctx: ToolContext) -> ToolResult:
    wiki = _wiki(ctx)
    keywords = [str(item) for item in args.get("keywords") or []]
    if not keywords:
        raise ValueError("Укажите ключевые слова текста")
    found = wiki.match(keywords, (), 1)
    tags = _known_tags(wiki)
    if not found or len(found[0].shared) < MATCH_THRESHOLD:
        # Одно общее слово — не повод сливать, но повод связать: так на графе появляется линия.
        near = [f"- {item.entry.path} — {item.entry.description} (общее: {item.shared[0]})" for item in found[:5]]
        return ToolResult(
            "В index.md нет заметки, с которой совпали бы 2+ ключевых слова. "
            "Создай новую заметку через wiki_save_note без path."
            + ("\nБлизкие по теме заметки — если они правда связаны, укажи их в related:\n" + "\n".join(near) if near else "")
            + tags
        )
    best = found[0]
    text = wiki.read(best.entry.path)
    others = [f"- {item.entry.path} (общие: {', '.join(item.shared)})" for item in found[1:5]]
    return ToolResult(
        f"Подходит заметка {best.entry.path} — общие ключевые слова: {', '.join(best.shared)}.\n"
        "Дополни её: собери заметку заново целиком — старые мысли и новые вместе, без повторов, "
        "проверь, что ничего не противоречит, — и сохрани через wiki_save_note "
        f"с path={best.entry.path}.\n"
        + ("Ещё похожие:\n" + "\n".join(others) + "\n" if others else "")
        + tags
        + f"\n\nТекущий текст {best.entry.path}:\n\n{text}"
    )


def wiki_save_note(args: dict, ctx: ToolContext) -> ToolResult:
    fields = NoteFields(
        title=str(args.get("title") or ""), description=str(args.get("description") or ""),
        ideas=tuple(args.get("ideas") or ()), keywords=tuple(args.get("keywords") or ()),
        type=str(args.get("type") or "note"), tags=tuple(args.get("tags") or ()), source=str(args.get("source") or ""),
        details=str(args.get("details") or ""), related=tuple(args.get("related") or ()),
        images=tuple(args.get("images") or ()),
    )
    path = args.get("path") or None
    return ToolResult(_wiki(ctx).save_note(fields, str(path) if path else None, tuple(args.get("replaces") or ())))


def wiki_add_images(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(_wiki(ctx).add_images(str(args["path"]), tuple(args.get("images") or ()), str(args.get("under") or "")))


def wiki_lint(args: dict, ctx: ToolContext) -> ToolResult:
    # Последняя реплика пользователя: что искать, он пишет в ней. Модель читает конец материала последним,
    # поэтому просьба повторяется там, а не тонет за текстами заметок.
    users = [message for message in ctx.sessions.messages(ctx.session_id) if message["role"] == "user"]
    request = users[-1]["content"].strip()[:MAX_REQUEST_CHARS] if users else ""
    wiki = _wiki(ctx)
    entries = wiki.index()
    if not entries:
        return ToolResult("Вики пустая — проверять нечего.")
    pairs = wiki.similar_pairs()
    parts = [f"Сегодня {date.today().isoformat()}. Заметок: {len(entries)}.",
             "index.md:\n" + "\n".join(entry.line() for entry in entries)]
    if tags := wiki.tags():
        parts.append(
            "Теги (сколько заметок): " + ", ".join(f"{tag} {count}" for tag, count in tags[:MAX_TAGS_SHOWN])
            + ". Если среди них есть синонимы или один тег на двух языках (например, qa и тестирование), "
            "предложи правку fix: в notes — заметки с лишним тегом, в change — какой тег на какой заменить."
        )
    if pairs:
        parts.append("Похожие заметки (общие ключевые слова) — проверь на дубли и противоречия:\n"
                     + "\n".join(f"- {left} + {right}: {', '.join(shared)}" for left, right, shared in pairs[:20]))
    # Сначала тексты похожих заметок, затем остальных — сколько влезет.
    order = list(dict.fromkeys([path for left, right, _ in pairs for path in (left, right)] + [entry.path for entry in entries]))
    budget, shown, texts = MAX_LINT_CHARS, [], []
    for path in order:
        try:
            text = wiki.read(path)
        except (OSError, ValueError):
            continue
        if len(text) > budget:
            break
        budget -= len(text)
        shown.append(path)
        texts.append(f"=== {path} ===\n{text}")
    parts.append("Тексты заметок:\n\n" + "\n\n".join(texts))
    if rest := [path for path in order if path not in shown]:
        parts.append(f"Не поместились (читай через wiki_read, если нужно): {', '.join(rest)}")
    parts.append(
        (f"Пользователь просит: «{request}». Найди в вики именно это. Если в просьбе не сказано, что искать, ищи "
         if request else "Найди: ")
        + "противоречия между заметками, устаревшие сведения, дубли, которые стоит слить. "
        "Ничего не меняй сам и не расписывай правки текстом: вызови wiki_propose_edits со списком правок — "
        "пользователь отметит нужные в карточке. Если править нечего — так и скажи."
    )
    return ToolResult("\n\n".join(parts))


def _check_edits(args: dict, ctx: ToolContext) -> list[dict]:
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits — непустой список правок")
    known = {entry.path for entry in _wiki(ctx).index()}
    for number, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise ValueError(f"Правка {number} должна быть объектом")
        if edit.get("kind") not in EDIT_KINDS:
            raise ValueError(f"Правка {number}: kind — одно из {', '.join(EDIT_KINDS)}")
        if isinstance(edit.get("notes"), str):
            edit["notes"] = [edit["notes"]]
        paths = edit.get("notes")
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"Правка {number}: notes — список путей заметок")
        if unknown := [path for path in paths if path not in known]:
            raise ValueError(f"Правка {number}: таких заметок нет: {', '.join(map(str, unknown))}")
        if not str(edit.get("problem") or "").strip() or not str(edit.get("change") or "").strip():
            raise ValueError(f"Правка {number}: заполни problem и change")
    return [_edit(edit) for edit in edits]


def propose_preview(args: dict, ctx: ToolContext) -> dict:
    edits = _check_edits(args, ctx)
    return {"kind": "wiki_lint", "title": f"Проверка вики: предложено правок — {len(edits)}", "edits": edits}


def wiki_propose_edits(args: dict, ctx: ToolContext) -> ToolResult:
    edits = _check_edits(args, ctx)
    # Номера отмеченных правок подставляет цикл из решения пользователя; без них подтверждены все.
    selected = args.get("selected")
    chosen = [(number, edit) for number, edit in enumerate(edits, start=1) if selected is None or number in selected]
    if not chosen:
        return ToolResult("Пользователь не отметил ни одной правки: вики не меняй.")
    skipped = [str(number) for number in range(1, len(edits) + 1) if number not in dict(chosen)]
    # Слабая модель после подтверждения норовит снова расписать правки текстом, поэтому здесь
    # для каждой правки — готовые path/replaces и сразу тексты заметок: дочитывать ничего не нужно.
    steps = []
    for number, edit in chosen:
        first, *rest = edit["notes"]
        if edit["kind"] == "merge" and rest:
            call = f'wiki_save_note с path="{first}", replaces={rest} — одна заметка из всех'
        else:
            call = " и ".join(f'wiki_save_note с path="{path}"' for path in edit["notes"])
        steps.append(f"{number}. {EDIT_KINDS[edit['kind']]}: {edit['change']}\n   Вызови {call}.")
    paths = list(dict.fromkeys(path for _, edit in chosen for path in edit["notes"]))
    texts, budget = [], MAX_LINT_CHARS
    for path in paths:
        try:
            text = _wiki(ctx).read(path)
        except (OSError, ValueError):
            continue
        if len(text) > budget:
            texts.append(f"=== {path} === (не поместилась — прочитай через wiki_read)")
            continue
        budget -= len(text)
        texts.append(f"=== {path} ===\n{text}")
    return ToolResult(
        "Пользователь подтвердил правки. Сейчас же выполни их вызовами инструментов, не спрашивай "
        "подтверждения ещё раз и не пересказывай их текстом:\n" + "\n".join(steps) + "\n"
        + (f"Правки {', '.join(skipped)} не подтверждены — их не применяй.\n" if skipped else "")
        + "В wiki_save_note передай заметку целиком: заголовок, описание, все ключевые мысли и ключевые слова "
        "из исходных заметок без потерь. После сохранения коротко скажи, что сделано.\n\n"
        + "Тексты заметок:\n\n" + "\n\n".join(texts)
    )


def _known_tags(wiki) -> str:
    """Теги, которые уже есть в вики: агент выбирает из них, а не заводит синонимы."""
    tags = wiki.tags()[:MAX_TAGS_SHOWN]
    if not tags:
        return ""
    return ("\nТеги вики (сколько заметок): " + ", ".join(f"{tag} {count}" for tag, count in tags)
            + ". Для tags бери подходящие из них; новый тег — только для темы, которой в вики ещё нет.")


def _edit(edit: object) -> dict:
    edit = edit if isinstance(edit, dict) else {}
    return {"kind": edit.get("kind") if edit.get("kind") in EDIT_KINDS else "fix",
            "notes": [str(path) for path in edit.get("notes") or []],
            "problem": " ".join(str(edit.get("problem") or "").split()),
            "change": " ".join(str(edit.get("change") or "").split())}


def lists(*names: str, by_comma: tuple[str, ...] = ()):
    """prepare для инструмента: модель иногда присылает список строкой «a, b» или по строке на пункт."""
    def prepare(arguments: dict) -> None:
        for name in names:
            value = arguments.get(name)
            if isinstance(value, str):
                separator = "\n" if "\n" in value or name not in by_comma else ","
                arguments[name] = [part.strip(" -•") for part in value.split(separator) if part.strip(" -•")]
            elif isinstance(value, list):
                arguments[name] = [str(item) for item in value]
    return prepare


STRINGS = {"type": "array", "items": {"type": "string"}}
SAVE_LISTS = ("ideas", "keywords", "tags", "related", "replaces", "images")
ADD_IMAGES_LISTS = ("images",)

TOOLS = [
    Tool("wiki_search", "Найти в вики (заметки по статьям и ссылкам пользователя) разделы по ключевым словам",
         {"type": "object", "properties": {"query": {"type": "string", "description": "Ключевые слова"}},
          "required": ["query"], "additionalProperties": False}, wiki_search),
    Tool("wiki_read", "Прочитать заметку вики целиком по пути (или index.md — список всех заметок)",
         {"type": "object", "properties": {"path": {"type": "string", "description": "Путь заметки, например rag_основы.md"}},
          "required": ["path"], "additionalProperties": False}, wiki_read),
    Tool("wiki_match", "Шаг 1 из 2, когда просят добавить заметку: выдели из текста 3–8 ключевых слов, и инструмент "
         "сверит их с index.md. Совпало 2+ слова — вернёт заметку, которую надо дополнить; иначе — создавай новую",
         {"type": "object", "properties": {"keywords": {**STRINGS, "description": "Ключевые слова текста в начальной форме, "
                                                        "по-русски; термины — как пишутся (RAG, reranker)"}},
          "required": ["keywords"], "additionalProperties": False},
         wiki_match, prepare=lists("keywords", by_comma=("keywords",))),
    Tool("wiki_save_note", "Шаг 2 из 2: сохранить заметку вики целиком по шаблону и обновить её строку в index.md. "
         "Новая — без path (имя файла берётся из заголовка); дополнение — с path найденной заметки, тогда заметка "
         "пересобирается полностью",
         {"type": "object", "properties": {
             "title": {"type": "string", "description": "Заголовок, обычно по-русски, например «RAG: основы»"},
             "description": {"type": "string", "description": "О чём заметка — одна короткая фраза до 160 символов"},
             "ideas": {**STRINGS, "description": "Ключевые мысли — отдельными элементами списка, по одной на пункт"},
             "keywords": {**STRINGS, "description": "3–8 ключевых слов: те, что передавал в wiki_match, по-русски"},
             "source": {"type": "string", "description": "Откуда текст: ссылка или «текст из чата»"},
             "tags": {**STRINGS, "description": "Теги-темы, 1–4 штуки"},
             "type": {"type": "string", "description": "Тип заметки: note, concept, howto, reference; по умолчанию note"},
             "details": {"type": "string", "description": "Содержание большого текста в Markdown: разделы, определения, "
                         "команды, таблицы, примеры. Для длинного текста — обязательно и подробно, не только оглавление. "
                         "Не повторяй здесь заголовок, источник и ключевые слова"},
             "related": {**STRINGS, "description": "Пути связанных заметок из index.md — станут ссылками и линиями графа"},
             "images": {**STRINGS, "description": "Новые картинки для заметки: имена из строки «[Картинки: …]» "
                        "в сообщении пользователя или из результата view_image, например 1a2b3c4d5e6f.png. Уже "
                        "вставленные в заметку картинки сохраняются сами"},
             "path": {"type": "string", "description": "Только при дополнении: путь заметки из wiki_match"},
             "replaces": {**STRINGS, "description": "Только при слиянии дублей: пути заметок, которые вошли в эту и будут удалены"},
         }, "required": ["title", "description", "ideas", "keywords"], "additionalProperties": False},
         wiki_save_note, prepare=lists(*SAVE_LISTS, by_comma=tuple(name for name in SAVE_LISTS if name != "ideas"))),
    Tool("wiki_add_images", "Добавить картинки в готовую заметку, не переписывая её текст: когда просят только "
         "вставить картинку (обложку, схему). Для правки текста заметки — wiki_save_note",
         {"type": "object", "properties": {
             "path": {"type": "string", "description": "Путь заметки, например список_медиа.md"},
             "images": {**STRINGS, "description": "Имена картинок из view_image или из строки «[Картинки: …]»"},
             "under": {"type": "string", "description": "Заголовок раздела, под которым поставить картинку, например "
                       "«Дни Сакамото»; без него — в раздел «Картинки» в конце"},
         }, "required": ["path", "images"], "additionalProperties": False},
         wiki_add_images, prepare=lists(*ADD_IMAGES_LISTS, by_comma=ADD_IMAGES_LISTS)),
    Tool("wiki_lint", "Lint вики: собрать индекс, похожие заметки и их тексты, чтобы найти противоречия, устаревшее и "
         "дубли. Сам ничего не меняет; найденное предложи через wiki_propose_edits",
         {"type": "object", "properties": {}, "additionalProperties": False}, wiki_lint),
    Tool("wiki_propose_edits", "Показать пользователю найденные lint правки вики списком: он отметит, какие применить. "
         "После подтверждения выполни только отмеченные",
         {"type": "object", "properties": {"edits": {"type": "array", "description": "Предлагаемые правки", "items": {
             "type": "object", "properties": {
                 "kind": {"type": "string", "enum": list(EDIT_KINDS), "description": "Вид правки"},
                 "notes": {**STRINGS, "description": "Пути затронутых заметок"},
                 "problem": {"type": "string", "description": "Что не так — одна-две фразы"},
                 "change": {"type": "string", "description": "Что именно поменять"},
             }, "required": ["kind", "notes", "problem", "change"]}}},
          "required": ["edits"], "additionalProperties": False},
         wiki_propose_edits, "wiki", propose_preview),
    *web.TOOLS,
]
