"""Клиент модели: параметры из настроек попадают в запрос; сервер, не принявший reasoning_effort, получает запрос без него."""

import asyncio
import json

import httpx

from meepo.llm.client import LLMClient, Reply


def sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks).encode("utf-8") + b"data: [DONE]\n\n"


ANSWER = sse({"choices": [{"delta": {"content": "Готово: файл test_web_ui/google_test_cases.md"}}]})


OFF = {"thinking": False}


def ask(client: LLMClient, options: dict | None = OFF) -> Reply:
    async def run():
        return [item async for item in client.stream("m", [{"role": "user", "content": "привет"}], options=options)][-1]
    return asyncio.run(run())


def test_options_go_to_request():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=ANSWER)

    client = LLMClient("http://model/v1", transport=httpx.MockTransport(handler))
    assert ask(client, {"thinking": False, "temperature": 0.2, "max_tokens": 512}).content.startswith("Готово")
    ask(client, {"thinking": True})
    ask(client, None)
    assert bodies[0]["reasoning_effort"] == "none" and bodies[0]["temperature"] == 0.2 and bodies[0]["max_tokens"] == 512
    # Рассуждения включены или не заданы — решает модель: параметры не отправляются.
    assert all("reasoning_effort" not in body and "temperature" not in body for body in bodies[1:])


def test_model_info_from_ollama():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show" and json.loads(request.content) == {"model": "qwen"}
        return httpx.Response(200, json={"capabilities": ["completion", "tools", "thinking"],
                                         "parameters": "temperature                    1\ntop_k  20\nnum_predict 4096"})

    info = asyncio.run(LLMClient("http://model/v1", transport=httpx.MockTransport(handler)).model_info("qwen"))
    assert info == {"thinking": True, "vision": False, "defaults": {"temperature": 1.0, "max_tokens": 4096}}


def test_model_info_unknown_server():
    client = LLMClient("http://model/v1", transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    assert asyncio.run(client.model_info("m")) == {"thinking": None, "vision": None, "defaults": {}}


def test_server_rejecting_parameter_gets_request_without_it():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "unknown field reasoning_effort"}})
        return httpx.Response(200, content=ANSWER)

    client = LLMClient("http://model/v1", transport=httpx.MockTransport(handler))
    assert ask(client).content.startswith("Готово")
    assert ask(client).content.startswith("Готово")
    # Первый запрос отвергнут и повторён без параметра; следующий сразу идёт без него.
    assert ["reasoning_effort" in body for body in bodies] == [True, False, False]
