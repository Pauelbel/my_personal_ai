"""Интернет и картинки: разбор выдачи поиска, картинки страницы и Markdown, view_image в ходе агента и вики."""

import base64

from meepo.agent.tools.web import html_to_text, images_footer, search_results
from meepo.llm.client import _message_payloads
from tests.test_wiki import NOTE, make, wiki_turn

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000000000200015e3d4ff40000000049454e44ae426082")

SEARCH_PAGE = """
<div class="result results_links result--ad"><a class="result__a" href="https://ads.example/">Реклама</a></div>
<div class="result results_links web-result">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fru.wikipedia.org%2Fwiki%2FSakamoto_Days&amp;rut=x">Sakamoto <b>Days</b> — Википедия</a></h2>
  <a class="result__snippet" href="#">Японская <b>манга</b> Юто Судзуки.</a>
</div>
<div class="result results_links web-result">
  <h2 class="result__title"><a class="result__a" href="https://myanimelist.net/anime/58939">Sakamoto Days - MyAnimeList</a></h2>
</div>
"""


def test_search_results_skip_ads_and_unwrap_links():
    assert search_results(SEARCH_PAGE) == [
        ("Sakamoto Days — Википедия", "https://ru.wikipedia.org/wiki/Sakamoto_Days", "Японская манга Юто Судзуки."),
        ("Sakamoto Days - MyAnimeList", "https://myanimelist.net/anime/58939", ""),
    ]


def test_page_images_main_first_without_icons():
    html = """<html><head><title>Постер</title><meta property="og:image" content="https://cdn.example/poster.jpg"></head>
    <body><header><img src="/logo.png" alt="Логотип"></header><p>Текст</p>
    <img src="/i/frame.png" alt="Кадр  из аниме"><img src="/i/icon.png" width="16" height="16">
    <img src="/i/arrow.svg"><img src="data:image/png;base64,AAAA"><img src="https://cdn.example/poster.jpg"></body></html>"""
    title, text, images = html_to_text(html, "https://site.example/page")
    assert title == "Постер" and text == "Текст"
    assert images == [("https://cdn.example/poster.jpg", "главная картинка страницы"),
                      ("https://site.example/logo.png", "Логотип"), ("https://site.example/i/frame.png", "Кадр из аниме")]


def test_markdown_images_from_file_folder():
    body = "![Схема](img/a.png) ![](../shared/b.jpg) ![веб](https://x.example/c.png) ![вне](../../d.png)"
    assert images_footer("docs/guide.md", body) == ("\n\nКартинки в файле (посмотреть — view_image с путём):\n"
                                                     "- docs/img/a.png — Схема\n- shared/b.jpg")
    assert images_footer("a.md", "без картинок") == ""


def test_view_image_reaches_model_next_to_result(harness):
    h = harness([{"calls": [("read_file", {"path": "docs/guide.md"}), ("view_image", {"source": "docs/img/a.png"})]},
                 "На схеме — вход в систему."])
    (h.project_dir / "docs" / "img").mkdir(parents=True)
    (h.project_dir / "docs" / "img" / "a.png").write_bytes(PNG)
    (h.project_dir / "docs" / "guide.md").write_text("# Гайд\n\n![Схема](img/a.png)\n", encoding="utf-8")
    h.turn("что на схеме в docs/guide.md?")
    tools = [m for m in h.llm.requests[1]["messages"] if m["role"] == "tool"]
    assert "- docs/img/a.png — Схема" in tools[0]["content"]
    name = h.messages()[-2]["images"][0]
    assert name in tools[1]["content"] and tools[1]["images"][0].endswith(name)
    # Серверу картинка уходит сообщением пользователя сразу за результатом инструмента.
    sent = _message_payloads(tools[1])
    assert [part["role"] for part in sent] == ["tool", "user"]
    assert sent[1]["content"][1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    # В следующем ходе картинка снова приходит из истории.
    h.llm.script.append("Да.")
    h.turn("а цвет?")
    again = [m for m in h.llm.requests[-1]["messages"] if m["role"] == "tool"][1]
    assert again["images"][0].endswith(name)


def test_view_image_without_vision_says_so(harness):
    h = harness([{"calls": [("view_image", {"source": "a.png"})]}, "Не вижу картинку."])
    (h.project_dir / "a.png").write_bytes(PNG)

    async def blind(model):
        return {"thinking": False, "vision": False, "defaults": {}}

    h.llm.model_info = blind
    h.turn("что на a.png?")
    tool = [m for m in h.llm.requests[1]["messages"] if m["role"] == "tool"][0]
    assert "не видит картинок" in tool["content"] and "images" not in tool


def test_view_image_rejects_non_images(harness):
    h = harness([{"calls": [("view_image", {"source": "notes.txt"})]}, "Не картинка."])
    (h.project_dir / "notes.txt").write_text("текст", encoding="utf-8")
    h.turn("посмотри notes.txt")
    assert "не картинка" in [m for m in h.messages() if m["role"] == "tool"][0]["content"]


def test_wiki_note_gets_image_from_view_image(tmp_path):
    services, llm = make(tmp_path, [{"calls": [("view_image", {"source": "cover.png"})]},
                                    {"calls": [("wiki_save_note", {**NOTE, "images": []})]}, "Готово."])
    (services.wiki.root / "cover.png").write_bytes(PNG)

    def save_with_image(messages):
        name = [m for m in messages if m["role"] == "tool"][-1]["content"].split()[1]
        return {"calls": [("wiki_save_note", {**NOTE, "images": [name]})]}

    llm.script[1] = save_with_image
    session, _ = wiki_turn(services, "добавь заметку с обложкой cover.png")
    name = [m for m in services.sessions.messages(session["id"]) if m["role"] == "tool"][0]["images"][0]
    assert f"images/{name}" in services.wiki.read("rag_основы.md")


MEDIA = """---
type: "note"
title: "Список медиа"
description: "Что посмотреть"
tags: ["аниме"]
---

# Список медиа

**Источник:** текст из чата

## Ключевые мысли

- Дни Сакамото — аниме про бывшего киллера
- Барри — сериал про киллера, который хочет стать актёром

## Подробности

### Дни Сакамото
Бывший киллер держит магазин и растит дочь.

### Барри
Чёрная комедия HBO, четыре сезона, премия Peabody.

## Ключевые слова

аниме, сериалы
"""


def test_add_images_keeps_text_and_goes_under_section(tmp_path):
    services, _ = make(tmp_path, [])
    (services.wiki.root / "список_медиа.md").write_text(MEDIA, encoding="utf-8")
    name = services.attachments.save_bytes(PNG)
    result = services.wiki.add_images("список_медиа.md", (name,), "Дни Сакамото")
    text = services.wiki.read("список_медиа.md")
    assert "Остальной текст не менялся" in result
    assert text.replace(f"\n![Дни Сакамото](images/{name})\n", "") == MEDIA
    assert text.index(f"images/{name}") < text.index("### Барри")
    assert (services.wiki.root / "images" / name).is_file()
    other = services.attachments.save_bytes(PNG)
    services.wiki.add_images("список_медиа.md", (other,))
    text = services.wiki.read("список_медиа.md")
    assert text.index("## Картинки") < text.index("## Ключевые слова") and f"images/{other}" in text


def test_save_note_refuses_to_lose_text(tmp_path):
    services, llm = make(tmp_path, [{"calls": [("wiki_save_note", {
        "path": "список_медиа.md", "title": "Список медиа", "description": "Что посмотреть",
        "ideas": ["Дни Сакамото — аниме"], "keywords": ["аниме", "сериалы"]})]}, "Не вышло."])
    (services.wiki.root / "список_медиа.md").write_text(MEDIA, encoding="utf-8")
    session, _ = wiki_turn(services, "добавь обложку")
    tool = [m for m in services.sessions.messages(session["id"]) if m["role"] == "tool"][0]
    assert tool["is_error"] and "wiki_add_images" in tool["content"]
    assert services.wiki.read("список_медиа.md") == MEDIA


def test_add_images_section_with_markup(tmp_path):
    services, _ = make(tmp_path, [])
    (services.wiki.root / "список_медиа.md").write_text(MEDIA, encoding="utf-8")
    name = services.attachments.save_bytes(PNG)
    services.wiki.add_images("список_медиа.md", (name,), "### Барри")
    text = services.wiki.read("список_медиа.md")
    assert text.index("### Барри") < text.index(f"![Барри](images/{name})") < text.index("## Ключевые слова")


def test_search_blocked_is_reported(monkeypatch):
    import asyncio

    import httpx

    from meepo.agent.tools import web

    monkeypatch.setattr(web, "SEARCH_INTERVAL", 0)
    monkeypatch.setattr(web, "SEARCH_RETRY_PAUSE", 0)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(1) or httpx.Response(202, text="<p>anomaly</p>"))
    real = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kwargs: real(transport=transport, **kwargs))
    result = asyncio.run(web.web_search({"query": "барри"}, None))
    assert result.is_error and "ограничил запросы" in result.content and len(calls) == 2
