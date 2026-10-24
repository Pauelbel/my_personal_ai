"""fetch_url: чтение веб-страницы по ссылке — текст статьи без разметки, чтобы сделать заметку в вики.

Только http(s) и только публичные адреса: агент вики не ходит в локальную сеть и на localhost.
"""

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from meepo.agent.tools.base import ToolContext, ToolResult

MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024
DEFAULT_MAX_CHARS = 12_000
TIMEOUT = 20
SKIPPED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
BLOCK_TAGS = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}


async def fetch_url(args: dict, ctx: ToolContext) -> ToolResult:
    url = args["url"]
    limit = args.get("max_chars", DEFAULT_MAX_CHARS)
    limit = min(max(limit, 1000), 40_000)
    try:
        await asyncio.to_thread(check_public_url, url)
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (Meepo local agent)"}) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                # Редирект мог увести во внутреннюю сеть.
                await asyncio.to_thread(check_public_url, str(response.url))
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_DOWNLOAD_BYTES:
                        break
                kind = response.headers.get("content-type", "")
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
    except (httpx.HTTPError, ValueError) as exc:
        return ToolResult(f"Не удалось скачать страницу: {exc}", True)
    title = ""
    if "html" in kind or text.lstrip().startswith("<"):
        title, text = html_to_text(text)
    text = text.strip()
    cut = len(text) > limit
    return ToolResult((f"# {title}\n" if title else "") + f"Источник: {url}\n\n" + text[:limit]
                      + ("\n\n… (обрезано)" if cut else ""))


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
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
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


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return " ".join(parser.title.split()), "\n".join(line for line in lines if line)
