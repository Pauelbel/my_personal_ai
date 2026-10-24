"""Формат заметок вики: OKF-фронтматтер, тело по шаблону, строка в index.md, ссылки для графа.

Заметка — Markdown-файл:

    ---
    type: note
    title: "RAG: основы"
    description: "Как модель ищет куски документов перед ответом"
    tags: ["rag", "llm"]
    ---

    # RAG: основы

    **Источник:** https://example.org/rag

    ## Ключевые мысли
    - …

    ## Ключевые слова
    rag, поиск, эмбеддинги

Значения фронтматтера пишутся JSON-строками и списками — это тоже YAML, а читаются и простые
YAML-значения без кавычек, если заметку поправили руками.
"""

import json
import math
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from meepo.agent.tools.web import markdown_images

INDEX_FILE = "index.md"
INDEX_HEADER = (
    "# Индекс вики\n\n"
    "Одна строка на заметку: путь — описание — ключевые слова. Обновляется агентом автоматически.\n"
)
KEYWORDS_HEADING = "Ключевые слова"
# Совпало столько ключевых слов с заметкой из индекса — новый текст дополняет её, а не создаёт новую.
MATCH_THRESHOLD = 2
MAX_SLUG_CHARS = 80

FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
# Картинки, вставленные в заметки из чата, лежат в этой папке вики.
IMAGES_DIR = "images"
INDEX_LINE = re.compile(r"^- \[[^\]]*\]\(([^)]+)\) — (.*)$")
SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Note:
    path: str
    type: str = ""
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    links: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class IndexEntry:
    path: str
    description: str
    keywords: tuple[str, ...]

    def line(self) -> str:
        line = f"- [{self.path}]({self.path}) — {self.description}"
        return f"{line} — {', '.join(self.keywords)}" if self.keywords else line


# ---------- имена файлов и ключевые слова ----------


def slug(title: str) -> str:
    """Имя файла из заголовка: нижний регистр, без знаков препинания, пробелы → «_». «RAG: основы» → rag_основы.md."""
    words = re.sub(r"[^\w\s-]", " ", title.casefold()).split()
    name = "_".join(words)[:MAX_SLUG_CHARS].strip("_-")
    if not name:
        raise ValueError("Из заголовка не получается имя файла: нужны буквы или цифры")
    return f"{name}.md"


def clean_keywords(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Ключевые слова в нижнем регистре, без повторов и пустых."""
    result: dict[str, None] = {}
    for value in values:
        word = " ".join(str(value).replace("—", " ").replace(",", " ").split()).casefold().strip(" .;:")
        if word:
            result[word] = None
    return tuple(result)


def same_keyword(left: str, right: str) -> bool:
    """«Модель» и «модели», «векторная база» и «векторные базы» — одно и то же ключевое слово."""
    a, b = left.split(), right.split()
    return len(a) == len(b) and all(_same_word(x, y) for x, y in zip(a, b, strict=True))


def _same_word(left: str, right: str) -> bool:
    # Без стеммера: слова одинаковы, если различаются только окончанием до двух букв.
    left, right = left.replace("ё", "е"), right.replace("ё", "е")
    if left == right:
        return True
    if min(len(left), len(right)) <= 3:
        return False
    common = len(posixpath.commonprefix([left, right]))
    return common >= 3 and common >= max(len(left), len(right)) - 2


def shared_keywords(mine: tuple[str, ...], theirs: tuple[str, ...]) -> list[str]:
    return [word for word in theirs if any(same_keyword(word, other) for other in mine)]


# ---------- фронтматтер и тело ----------


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    key = None
    for line in match.group(1).splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key is not None:
            # Список блоком: «tags:» и строки «  - rag».
            current = meta.get(key)
            meta[key] = [*(current if isinstance(current, list) else []), _scalar(item.group(1))]
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if pair:
            key = pair.group(1)
            meta[key] = _value(pair.group(2).strip())
    return meta, text[match.end():]


def _value(raw: str):
    if raw.startswith("[") and raw.endswith("]"):
        try:
            value = json.loads(raw)
            return [str(item) for item in value] if isinstance(value, list) else []
        except ValueError:
            return [_scalar(item) for item in raw[1:-1].split(",") if item.strip()]
    return _scalar(raw) if raw else []


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return str(json.loads(raw))
        except ValueError:
            pass
    return raw.strip("'\"")


def _text(value) -> str:
    return " ".join(value).strip() if isinstance(value, list) else str(value or "").strip()


def _list(value) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


def parse(path: str, text: str) -> Note:
    meta, body = split_frontmatter(text)
    title = _text(meta.get("title")) or _heading(body) or posixpath.splitext(posixpath.basename(path))[0]
    return Note(
        path=path,
        type=_text(meta.get("type")),
        title=title,
        description=_text(meta.get("description")) or _first_paragraph(body),
        tags=_list(meta.get("tags")),
        keywords=clean_keywords(_keywords(body)),
        links=links_of(path, body),
    )


def _heading(body: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*#*\s*$", body, re.MULTILINE)
    return match.group(1) if match else ""


def _first_paragraph(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        lines = [line for line in block.strip().splitlines() if not re.match(r"^\s*(#|\||```|---)", line)]
        text = " ".join(" ".join(lines).split())
        text = re.sub(r"[*_`]", "", LINK.sub(r"\1", text)).strip()
        if text:
            return text[:200]
    return ""


def _keywords(body: str) -> list[str]:
    """Слова из раздела «Ключевые слова»: через запятую или списком."""
    sections = list(SECTION.finditer(body))
    for index, match in enumerate(sections):
        if match.group(1).strip().casefold() != KEYWORDS_HEADING.casefold():
            continue
        end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
        text = re.sub(r"^\s*[-*]\s+", "", body[match.end():end], flags=re.MULTILINE)
        return [part for part in re.split(r"[,\n;]", text) if part.strip()]
    return []


def links_of(path: str, body: str) -> tuple[str, ...]:
    """Пути заметок, на которые ссылается текст: относительные ссылки на .md внутри вики."""
    folder = posixpath.dirname(path)
    found: dict[str, None] = {}
    for _label, target in LINK.findall(body):
        target = unquote(target.split("#", 1)[0])
        if not target.endswith(".md") or re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
            continue
        resolved = posixpath.normpath(posixpath.join(folder, target))
        if not resolved.startswith("../") and resolved != path:
            found[resolved] = None
    return tuple(found)


def images_of(path: str, body: str) -> list[tuple[str, str]]:
    """Картинки заметки: (подпись, путь от корня вики) — при пересборке заметки они не теряются."""
    return markdown_images(path, body)


def render(
    *, type: str, title: str, description: str, tags: tuple[str, ...], source: str, ideas: list[str],
    details: str, related: list[tuple[str, str]], keywords: tuple[str, ...], images: list[tuple[str, str]] = (),
) -> str:
    """Заметка по шаблону; related — (заголовок, относительная ссылка) связанных заметок."""
    head = [
        "---",
        f"type: {_json(type)}",
        f"title: {_json(title)}",
        f"description: {_json(description)}",
        f"tags: {json.dumps(list(tags), ensure_ascii=False)}",
        "---",
    ]
    parts = ["\n".join(head), f"# {title}", f"**Источник:** {source}"]
    parts.append("## Ключевые мысли\n\n" + "\n".join(f"- {_one_line(idea)}" for idea in ideas))
    if details := clean_details(details):
        parts.append("## Подробности\n\n" + details)
    if images:
        parts.append("## Картинки\n\n" + "\n\n".join(f"![{label}]({target})" for label, target in images))
    if related:
        parts.append("## Связанные заметки\n\n" + "\n".join(f"- [{label}]({target})" for label, target in related))
    parts.append(f"## {KEYWORDS_HEADING}\n\n" + ", ".join(keywords))
    return "\n\n".join(parts) + "\n"


def clean_details(text: str) -> str:
    """Подробности без повторов шаблона: модель иногда кладёт сюда всю заметку целиком.

    Убираются фронтматтер, заголовок «# …», строка источника, раздел ключевых слов и заголовок
    «Ключевые мысли»; заголовки второго уровня опускаются до третьего, чтобы остаться внутри раздела.
    """
    _, text = split_frontmatter(text.strip())
    sections = list(SECTION.finditer(text))
    for match in reversed(sections):
        if match.group(1).strip().casefold() == KEYWORDS_HEADING.casefold():
            following = [item.start() for item in sections if item.start() > match.start()]
            text = text[:match.start()] + text[following[0] if following else len(text):]
    lines = []
    for line in text.splitlines():
        if re.match(r"^#\s", line) or re.match(r"^\*\*Источник:?\*\*", line):
            continue
        if re.match(r"^##\s+Ключевые мысли\s*$", line, re.IGNORECASE):
            continue
        lines.append("#" + line if re.match(r"^##\s", line) else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _json(value: str) -> str:
    return json.dumps(_one_line(value), ensure_ascii=False)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def relative_link(source: str, target: str) -> str:
    """Ссылка из заметки source на target, как её пишут в Markdown: от папки source."""
    return posixpath.relpath(target, posixpath.dirname(source) or ".")


def retarget_links(path: str, text: str, moves: dict[str, str]) -> str:
    """Переписывает ссылки на переименованные или слитые заметки: moves — старый путь → новый."""
    folder = posixpath.dirname(path)

    def replace(match: re.Match) -> str:
        label, target = match.group(1), match.group(2)
        clean = unquote(target.split("#", 1)[0])
        resolved = posixpath.normpath(posixpath.join(folder, clean))
        if resolved not in moves:
            return match.group(0)
        anchor = target[len(target.split("#", 1)[0]):]
        return f"[{label}]({relative_link(path, moves[resolved])}{anchor})"

    return LINK.sub(replace, text)


# ---------- index.md ----------


def parse_index(text: str) -> list[IndexEntry]:
    entries = []
    for line in text.splitlines():
        match = INDEX_LINE.match(line.strip())
        if not match:
            continue
        path = unquote(match.group(1))
        description, _, keywords = match.group(2).rpartition(" — ")
        if not description:
            description, keywords = keywords, ""
        entries.append(IndexEntry(path, description.strip(), clean_keywords(keywords.split(","))))
    return entries


def render_index(entries: list[IndexEntry]) -> str:
    return INDEX_HEADER + ("\n" + "\n".join(entry.line() for entry in entries) if entries else "") + "\n"


def entry_of(note: Note) -> IndexEntry:
    description = _one_line(note.description).replace(" — ", " – ") or note.title
    keywords = note.keywords or clean_keywords(note.tags)
    return IndexEntry(note.path, description, keywords)


# ---------- граф ----------

# Связи по смыслу: TF-IDF по основам слов всего текста заметки и косинусная близость.
# Основа — первые STEM_CHARS букв: «тестирование» и «тестировщик» совпадут без стеммера.
STEM_CHARS = 5
# Заголовок, описание, теги и ключевые слова говорят о теме заметки сильнее, чем её текст.
PROFILE_WEIGHT = 3.0
# Ниже порога близость — случайные общие слова; у каждой заметки не больше SIMILAR_PER_NOTE самых близких.
SIMILAR_THRESHOLD = 0.08
SIMILAR_PER_NOTE = 3
URL = re.compile(r"https?://\S+")
WORD = re.compile(r"[^\W\d_]{3,}")
STOPWORDS = frozenset(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня "
    "еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там "
    "потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже "
    "себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее "
    "сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про "
    "всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им "
    "более всегда конечно всю между это эта так также каждый который которая которые которых через если "
    "the and for with that this from are was were you your not but can all any have has into its our out use "
    "using how what when which who will would also than then them they there these those".split()
)


def similarity_terms(note: Note, body: str) -> dict[str, float]:
    """Веса основ слов заметки: текст — с весом 1, заголовок, описание, теги и ключевые слова — с PROFILE_WEIGHT."""
    counts: dict[str, float] = {}

    def add(text: str, weight: float) -> None:
        for word in WORD.findall(URL.sub(" ", text).casefold().replace("ё", "е")):
            if word not in STOPWORDS:
                stem = word[:STEM_CHARS]
                counts[stem] = counts.get(stem, 0.0) + weight

    add(split_frontmatter(body)[1], 1.0)
    add(" ".join([note.title, note.description, *note.tags, *note.keywords]), PROFILE_WEIGHT)
    # Логарифм: слово, повторённое двадцать раз, не перевешивает всё остальное.
    return {stem: 1 + math.log(count) for stem, count in counts.items()}


def similar_pairs(terms: dict[str, dict[str, float]]) -> list[tuple[str, str, float]]:
    """Пары близких по смыслу заметок (путь, путь, близость 0…1) — самые близкие первыми.

    Слова, которые есть почти во всех заметках (заголовки шаблона, общие слова), веса не получают.
    """
    paths = list(terms)
    frequency: dict[str, int] = {}
    for weights in terms.values():
        for stem in weights:
            frequency[stem] = frequency.get(stem, 0) + 1
    total = len(paths)
    vectors: dict[str, dict[str, float]] = {}
    for path, weights in terms.items():
        vector = {stem: weight * math.log(total / frequency[stem]) for stem, weight in weights.items()}
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        vectors[path] = {stem: value / norm for stem, value in vector.items() if value}
    scores: dict[tuple[str, str], float] = {}
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            small, large = sorted((vectors[left], vectors[right]), key=len)
            score = sum(value * large.get(stem, 0.0) for stem, value in small.items())
            if score >= SIMILAR_THRESHOLD:
                scores[(left, right)] = score
    # Пара остаётся, если хотя бы для одной из двух заметок она среди самых близких.
    best: dict[str, list[tuple[str, str]]] = {path: [] for path in paths}
    for pair in sorted(scores, key=lambda pair: -scores[pair]):
        for path in pair:
            best[path].append(pair)
    kept = {pair for pairs in best.values() for pair in pairs[:SIMILAR_PER_NOTE]}
    return sorted(((left, right, round(scores[(left, right)], 3)) for left, right in kept), key=lambda item: -item[2])


def clusters(paths: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Кластеры графа распространением меток: заметка берёт метку, которая чаще всего у её соседей.

    Порядок обхода фиксирован, ничья — в пользу меньшей метки: одна и та же вики всегда раскрашена одинаково.
    Номера кластеров — по убыванию размера; одиночные заметки получают -1.
    """
    neighbours: dict[str, list[str]] = {path: [] for path in paths}
    for left, right in edges:
        neighbours[left].append(right)
        neighbours[right].append(left)
    label = {path: index for index, path in enumerate(paths)}
    for _ in range(30):
        changed = False
        for path in paths:
            if not neighbours[path]:
                continue
            counts: dict[int, int] = {}
            for other in neighbours[path]:
                counts[label[other]] = counts.get(label[other], 0) + 1
            best = min(counts, key=lambda value: (-counts[value], value))
            if counts.get(label[path], 0) < counts[best]:
                label[path] = best
                changed = True
        if not changed:
            break
    sizes: dict[int, int] = {}
    for path in paths:
        sizes[label[path]] = sizes.get(label[path], 0) + 1
    order = sorted((value for value in sizes if sizes[value] > 1), key=lambda value: (-sizes[value], value))
    number = {value: index for index, value in enumerate(order)}
    return {path: number.get(label[path], -1) for path in paths}
