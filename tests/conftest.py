"""Общая обвязка тестов: сервисы Мипо на фейковой модели во временной папке данных."""

import asyncio

import pytest

from meepo.app import Services
from meepo.llm.fake import FakeLLM
from meepo.settings import Env


class Harness:
    """Проект, сессия и ход на фейковой модели. decisions — ответы на карточки по порядку."""

    def __init__(self, tmp_path, script, window=16384) -> None:
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir(exist_ok=True)
        self.llm = FakeLLM(script, window)
        self.services = Services(Env(data_path=tmp_path / "data", default_model="fake"), self.llm)
        self.project = self.services.projects.create("Проект", str(self.project_dir))
        self.session = self.services.sessions.create(self.project["id"])
        self.events: list[dict] = []

    @property
    def sid(self) -> str:
        return self.session["id"]

    def turn(self, text: str, decisions=()) -> list[dict]:
        return asyncio.run(self._turn(text, list(decisions)))

    async def _turn(self, text: str, decisions: list) -> list[dict]:
        events: list[dict] = []

        def emit(event: dict) -> None:
            events.append(event)
            if event["type"] == "approval":
                decision = decisions.pop(0)
                asyncio.get_running_loop().call_soon(self.services.approvals.decide, self.sid, decision)

        await self.services.agent.run(self.sid, text, "req-1", emit)
        # Фоновое обновление памяти после хода дожидаемся здесь: иначе цикл событий закроется раньше.
        await asyncio.gather(*self.services.memory_updater._tasks)
        self.events += events
        return events

    def messages(self) -> list[dict]:
        return self.services.sessions.messages(self.sid)

    def cards(self) -> list[dict]:
        return [event["card"] for event in self.events if event["type"] == "approval"]


@pytest.fixture
def harness(tmp_path):
    return lambda script, **kwargs: Harness(tmp_path, script, **kwargs)
