"""Интернет и картинки: web_search ищет страницы, fetch_url читает страницу (текст и список её картинок),
view_image показывает модели картинку — по ссылке или файлом из папки проекта (у агента вики — из вики).

Ссылки — только http(s) и только публичные адреса: агент не ходит в локальную сеть и на localhost.
Картинка из view_image копируется в data/attachments: её имя модель видит в результате и может передать
в wiki_save_note, а сама картинка уходит модели рядом с результатом (agent/loop.py).
"""

import asyncio
import ipaddress
import posixpath
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from meepo.agent.tools.base import Tool, ToolContext, ToolResult, inside, relative_to

MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CHARS = 12_000
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36 Meepo"
SKIPPED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
BLOCK_TAGS = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}
# Картинки страницы в результате fetch_url: первые по порядку, без иконок и картинок-разделителей.
MAX_PAGE_IMAGES = 15
MIN_IMAGE_SIDE = 64
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Картинка в Markdown: ![подпись](путь).
MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
SEARCH_URL = "https://html.duckduckgo.com/html/"
DEFAULT_SEARCH_RESULTS = 8
# DuckDuckGo на частые запросы отвечает 202 со страницей-проверкой («anomaly») вместо выдачи — блокирует IP
# на минуты. Запросы идут не чаще раза в SEARCH_INTERVAL секунд; на проверку — один повтор через SEARCH_RETRY_PAUSE.
SEARCH_INTERVAL = 2.0
SEARCH_RETRY_PAUSE = 5.0
SEARCH_BLOCKED = ("Поисковик временно ограничил запросы (просит пройти проверку). Не повторяй поиск сейчас: "
                  "открой через fetch_url известные сайты по теме (например, ru.wikipedia.org/wiki/<Название>) "
                  "или попроси у пользователя ссылку. Поиск снова заработает через несколько минут")
_search_lock = asyncio.Lock()
_last_search = 0.0


async def fetch_url(args: dict, ctx: ToolContext) -> ToolResult:
    url = args["url"]
    limit = args.get("max_chars", DEFAULT_MAX_CHARS)
    limit = min(max(limit, 1000), 40_000)
    try:
        body, kind, final = await _download(url, MAX_DOWNLOAD_BYTES)
    except (httpx.HTTPError, ValueError) as exc:
        return ToolResult(f"Не удалось скачать страницу: {exc}", True)
    if kind.startswith("image/"):
        return ToolResult(f"По ссылке картинка, а не страница: открой её через view_image (source={url})", True)
    text = body.decode(_charset(kind), errors="replace")
    title, images = "", []
    if "html" in kind or text.lstrip().startswith("<"):
        title, text, images = html_to_text(text, final)
    text = text.strip()
    cut = len(text) > limit
    pictures = ""
    if images:
        pictures = ("\n\nКартинки страницы (посмотреть — view_image с адресом):\n"
                    + "\n".join(f"- {src}" + (f" — {alt}" if alt else "") for src, alt in images))
    return ToolResult((f"# {title}\n" if title else "") + f"Источник: {url}\n\n" + text[:limit]
                      + ("\n\n… (обрезано)" if cut else "") + pictures)


async def web_search(args: dict, ctx: ToolContext) -> ToolResult:
    query = str(args["query"]).strip()
    if not query:
        return ToolResult("Пустой запрос: напиши, что искать", True)
    count = min(max(args.get("max_results", DEFAULT_SEARCH_RESULTS), 1), 20)
    try:
        page = await _search_page(query)
    except httpx.HTTPError as exc:
        return ToolResult(f"Поиск не удался: {exc}", True)
    if page is None:
        return ToolResult(SEARCH_BLOCKED, True)
    results = search_results(page)[:count]
    if not results:
        return ToolResult(f"По запросу «{query}» ничего не найдено. Переформулируй запрос короче или попроси у пользователя ссылку.")
    lines = [f"{index}. {title}\n   {link}" + (f"\n   {snippet}" if snippet else "")
             for index, (title, link, snippet) in enumerate(results, 1)]
    return ToolResult(f"Результаты поиска «{query}» (открыть страницу — fetch_url):\n\n" + "\n".join(lines))


async def _search_page(query: str) -> str | None:
    """HTML выдачи; None — поисковик и после повтора отвечает проверкой вместо результатов."""
    global _last_search
    async with _search_lock, httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                               headers={"User-Agent": USER_AGENT}) as client:
        for attempt in range(2):
            wait = SEARCH_INTERVAL - (time.monotonic() - _last_search) if attempt == 0 else SEARCH_RETRY_PAUSE
            if wait > 0:
                await asyncio.sleep(wait)
            response = await client.post(SEARCH_URL, data={"q": query, "kl": "ru-ru"})
            _last_search = time.monotonic()
            response.raise_for_status()
            if response.status_code != 202 and "anomaly" not in response.text:
                return response.text
    return None


async def view_image(args: dict, ctx: ToolContext) -> ToolResult:
    source = str(args["source"]).strip()
    store = ctx.services["attachments"]
    if source.startswith(("http://", "https://")):
        try:
            data, kind, _ = await _download(source, MAX_IMAGE_BYTES, accept="image/png,image/jpeg,image/webp,image/gif")
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(f"Не удалось скачать картинку: {exc}", True)
        if "svg" in kind:
            return ToolResult("Это SVG: такие картинки модель не видит. Поищи PNG или JPEG", True)
    else:
        path = inside(ctx.project, source)
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError(f"{relative_to(ctx.project, path)} — не картинка PNG, JPEG, WebP или GIF")
        data = await asyncio.to_thread(path.read_bytes)
    name = await asyncio.to_thread(store.save_bytes, data)
    return ToolResult(
        f"Картинка {name} (из {source}, {len(data) // 1024} КБ) — ниже. Опиши, что на ней, и реши, подходит ли она "
        f"к задаче. Вставить в заметку вики — {name} в images у wiki_add_images (готовая заметка) или wiki_save_note.",
        images=(name,), meta={"images": [name], "source": source})


def markdown_images(path: str, body: str) -> list[tuple[str, str]]:
    """Локальные картинки Markdown-файла: (подпись, путь от корня папки). Ссылки в интернет и выход за корень — мимо."""
    folder = posixpath.dirname(path)
    found: dict[str, str] = {}
    for label, target in MARKDOWN_IMAGE.findall(body):
        target = unquote(target)
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
            continue
        resolved = posixpath.normpath(posixpath.join(folder, target))
        if not resolved.startswith("../"):
            found.setdefault(resolved, label)
    return [(label, target) for target, label in found.items()]


def images_footer(path: str, body: str) -> str:
    """Приписка к прочитанному Markdown: его картинки путями для view_image — путь в тексте идёт от папки файла."""
    found = markdown_images(path, body)
    if not found:
        return ""
    return ("\n\nКартинки в файле (посмотреть — view_image с путём):\n"
            + "\n".join(f"- {target}" + (f" — {label}" if label else "") for label, target in found))


async def _download(url: str, limit: int, accept: str = "") -> tuple[bytes, str, str]:
    """Тело ответа (не больше limit байт), его content-type и адрес после редиректов."""
    await asyncio.to_thread(check_public_url, url)
    headers = {"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            # Редирект мог увести во внутреннюю сеть.
            await asyncio.to_thread(check_public_url, str(response.url))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    break
            return bytes(body), response.headers.get("content-type", "").lower(), str(response.url)


def _charset(kind: str) -> str:
    for part in kind.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "charset" and value:
            return value.strip("\"'")
    return "utf-8"


def check_public_url(url: object) -> None:
    if not isinstance(url, str):
        raise ValueError("Нужна ссылка строкой")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Поддерживаются только ссылки http и https")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except OSError as exc:
        raise ValueError(f"Адрес {parsed.hostname} не найден") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Ссылки на локальную сеть и localhost не открываются")


class _TextExtractor(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.title = ""
        self.images: dict[str, str] = {}
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("property") in ("og:image", "og:image:url"):
            # Главная картинка страницы (постер, обложка) — первой в списке.
            self._image(values.get("content", ""), "главная картинка страницы", first=True)
        elif tag == "img":
            self._image(values.get("src") or values.get("data-src") or values.get("srcset", "").split(" ")[0],
                        values.get("alt") or values.get("title", ""), sizes=(values.get("width"), values.get("height")))
        if tag in SKIPPED_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
            if tag in {"h1", "h2", "h3"}:
                self.parts.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.parts.append("- ")

    def _image(self, src: str, alt: str, sizes=(None, None), first: bool = False) -> None:
        src = src.strip()
        if not src or src.startswith("data:") or urlparse(src).path.lower().endswith(".svg"):
            return
        # Иконки и пиксели-счётчики: размер в атрибутах меньше MIN_IMAGE_SIDE.
        if any(side and side.isdigit() and int(side) < MIN_IMAGE_SIDE for side in sizes):
            return
        url = urljoin(self.base, src)
        if url in self.images:
            return
        if first:
            self.images = {url: alt, **self.images}
        else:
            self.images[url] = " ".join(alt.split())

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str, base: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    """Заголовок, текст страницы и её картинки [(адрес, подпись)]."""
    parser = _TextExtractor(base)
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    images = list(parser.images.items())[:MAX_PAGE_IMAGES]
    return " ".join(parser.title.split()), "\n".join(line for line in lines if line), images


class _SearchParser(HTMLParser):
    """Выдача html.duckduckgo.com: ссылка результата — a.result__a, описание — .result__snippet; реклама пропускается."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[list[str]] = []
        self._field: str | None = None
        self._ad = False

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = values.get("class", "").split()
        if tag == "div" and "result" in classes:
            self._ad = "result--ad" in classes
        if self._ad:
            return
        if tag == "a" and "result__a" in classes:
            self.results.append(["", _target(values.get("href", "")), ""])
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "td", "div"):
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            index = 0 if self._field == "title" else 2
            self.results[-1][index] += data


def _target(href: str) -> str:
    """Ссылки выдачи бывают переходом через duckduckgo.com/l/?uddg=<адрес>: достаём сам адрес."""
    parsed = urlparse(href if "://" in href else "https:" + href if href.startswith("//") else href)
    if parsed.path == "/l/" and "uddg" in parse_qs(parsed.query):
        return parse_qs(parsed.query)["uddg"][0]
    return href


def search_results(html: str) -> list[tuple[str, str, str]]:
    parser = _SearchParser()
    parser.feed(html)
    return [(" ".join(title.split()), link, " ".join(snippet.split()))
            for title, link, snippet in parser.results if link.startswith(("http://", "https://"))]


TOOLS = [
    Tool("web_search", "Поиск в интернете (DuckDuckGo): заголовки, ссылки и описания страниц. Найденную страницу "
         "читай через fetch_url; картинки — через fetch_url страницы, затем view_image",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "Что искать, например «Sakamoto Days постер аниме»"},
             "max_results": {"type": "integer", "description": "Сколько результатов, по умолчанию 8"},
         }, "required": ["query"], "additionalProperties": False}, web_search),
    Tool("fetch_url", "Скачать веб-страницу по http(s)-ссылке: её текст без разметки и список картинок страницы "
         "(адрес и подпись)",
         {"type": "object", "properties": {
             "url": {"type": "string", "description": "Адрес страницы, http или https"},
             "max_chars": {"type": "integer", "description": "Сколько символов текста вернуть, по умолчанию 12000"},
         }, "required": ["url"], "additionalProperties": False}, fetch_url),
    Tool("view_image", "Посмотреть картинку: она придёт тебе вместе с результатом, и ты увидишь, что на ней. "
         "Источник — http(s)-адрес картинки (из fetch_url) или путь к файлу PNG/JPEG/WebP/GIF от корня папки "
         "(картинки из Markdown-файлов и заметок: ![подпись](путь)). Возвращает имя картинки для вставки в заметку",
         {"type": "object", "properties": {
             "source": {"type": "string", "description": "Адрес картинки или путь к файлу от корня папки"},
         }, "required": ["source"], "additionalProperties": False}, view_image),
]
