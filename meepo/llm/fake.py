"""Фейковая модель для тестов: отдаёт заранее заданные ответы по очереди, без сервера.

Ответ в сценарии:
- строка — текстовый ответ;
- словарь {"content": "...", "calls": [("write_file", {"path": "a.md", "content": "x"}), ...]} — вызовы инструментов;
- исключение — будет брошено вместо ответа (LLMEmpty, LLMRepetition, …);
- функция messages → одно из перечисленного: ответ, зависящий от запроса.
Каждый запрос запоминается в `requests`: сообщения и были ли предложены инструменты.
"""

import json
from collections.abc import AsyncIterator

from meepo.llm.client import Reply


class FakeLLM:
    def __init__(self, script: list, window: int | None = 8192) -> None:
        self.script = list(script)
        self.window = window
        self.requests: list[dict] = []
        self.base_url = "http://fake/v1"
        self.api_key = ""

    async def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                     response_format: dict | None = None, options: dict | None = None) -> AsyncIterator:
        self.requests.append({"model": model, "messages": [dict(item) for item in messages], "tools": tools,
                              "response_format": response_format, "options": options})
        if not self.script:
            raise AssertionError("Фейковой модели не хватило ответов сценария")
        item = self.script.pop(0)
        if callable(item):
            # Ответ, который зависит от запроса: например, операции памяти со ссылкой на id сообщения.
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            item = {"content": item}
        content = item.get("content", "")
        if content:
            yield "delta", content
        calls = [
            {"id": f"call-{len(self.requests)}-{index}", "name": name,
             "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}
            for index, (name, arguments) in enumerate(item.get("calls", []))
        ]
        # Вход — грубо по размеру запроса, как считал бы сервер: так калибровка окна в цикле остаётся около 1.
        size = len(json.dumps([messages, tools or []], ensure_ascii=False).encode("utf-8")) // 4
        yield Reply(content, calls, "", input_tokens=size, output_tokens=10)

    async def list_models(self) -> list[str]:
        return ["fake"]

    async def model_info(self, model: str) -> dict:
        return {"thinking": True, "vision": True, "defaults": {"temperature": 1.0}}

    async def context_length(self, model: str) -> int | None:
        return self.window

    async def close(self) -> None:
        pass
