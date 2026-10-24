"""Цикл хода на фейковой модели: инструменты, карточки, доверие, папка проекта, лимит раундов."""

import asyncio

from meepo.agent.approvals import INTERRUPTED, reset_pending
from meepo.agent.loop import STOP_NOTE
from meepo.agent.turns import TurnHub

YES = {"action": "approve"}
NO = {"action": "reject"}


def test_text_answer_ends_turn(harness):
    h = harness(["Привет! Чем помочь?"])
    events = h.turn("привет")
    assert events[-1]["type"] == "done"
    assert [m["role"] for m in h.messages()] == ["user", "assistant"]
    assert h.messages()[-1]["content"] == "Привет! Чем помочь?"
    assert h.services.sessions.get(h.sid)["title"] == "привет"


def test_read_tool_runs_without_card(harness):
    h = harness([{"calls": [("read_file", {"path": "a.md"})]}, "В файле написано «текст»."])
    (h.project_dir / "a.md").write_text("текст", encoding="utf-8")
    h.turn("что в a.md?")
    assert h.cards() == []
    tool = [m for m in h.messages() if m["role"] == "tool"][0]
    assert tool["content"] == "текст" and not tool.get("is_error")
    # Результат инструмента ушёл модели во втором запросе.
    assert h.llm.requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call-1-0", "name": "read_file",
                                                 "about": "read_file a.md", "content": "текст"}


def test_write_needs_card_with_diff(harness):
    h = harness([{"calls": [("write_file", {"path": "notes.md", "content": "привет"})]}, "Файл создан."])
    h.turn("создай файл notes.md с текстом привет", [YES])
    card = h.cards()[0]
    assert card["tool"] == "write_file" and card["group"] == "files_write"
    assert "+привет" in card["diff"] and card["title"] == "Создать файл notes.md"
    assert (h.project_dir / "notes.md").read_text(encoding="utf-8") == "привет"
    assert h.services.sessions.get(h.sid)["pending"] is None


def test_rejected_card_does_nothing(harness):
    h = harness([{"calls": [("write_file", {"path": "notes.md", "content": "x"})]}])
    h.turn("создай notes.md", [NO])
    assert not (h.project_dir / "notes.md").exists()
    tool = [m for m in h.messages() if m["role"] == "tool"][0]
    assert tool["is_error"] and tool["content"] == "Действие отменено через карточку"
    assert h.messages()[-1]["content"] == "Действие отменено."
    assert len(h.llm.requests) == 1


def test_reply_text_goes_to_model(harness):
    h = harness([
        {"calls": [("write_file", {"path": "notes.md", "content": "x"})]},
        {"calls": [("write_file", {"path": "todo.md", "content": "x"})]},
        "Готово.",
    ])
    h.turn("создай файл", [{"action": "reply", "text": "назови его todo.md"}, YES])
    assert not (h.project_dir / "notes.md").exists()
    assert (h.project_dir / "todo.md").exists()
    second = h.llm.requests[1]["messages"]
    assert "назови его todo.md" in second[-2]["content"]
    assert second[-1] == {"role": "user", "content": "назови его todo.md"}


def test_session_trust_skips_next_cards(harness):
    h = harness([
        {"calls": [("write_file", {"path": "a.md", "content": "1"})]},
        {"calls": [("make_dir", {"path": "docs"}), ("write_file", {"path": "docs/b.md", "content": "2"})]},
        "Готово.",
    ])
    h.turn("создай файлы", [{"action": "approve", "trust": True}])
    assert len(h.cards()) == 1
    assert h.services.sessions.get(h.sid)["trusted"] == ["files_write"]
    assert (h.project_dir / "docs" / "b.md").read_text(encoding="utf-8") == "2"


def test_path_outside_project_is_error_for_model(harness, tmp_path):
    h = harness([
        {"calls": [("write_file", {"path": "../evil.md", "content": "x"}), ("read_file", {"path": "/etc/passwd"})]},
        "Не получилось.",
    ])
    h.turn("запиши файл снаружи")
    # Карточка не показывалась: ошибку исправляет модель, а не пользователь.
    assert h.cards() == []
    assert not (tmp_path / "evil.md").exists()
    results = [m for m in h.messages() if m["role"] == "tool"]
    assert all(m["is_error"] for m in results)
    assert "за пределы папки проекта" in results[0]["content"]
    assert "не абсолютный" in results[1]["content"]


def test_bad_arguments_go_back_to_model(harness):
    h = harness([{"calls": [("write_file", {"path": "a.md"}), ("nope", {})]}, "Исправлюсь."])
    h.turn("создай")
    first, second = [m for m in h.messages() if m["role"] == "tool"]
    assert "Не хватает параметров: content" in first["content"]
    assert "Инструмента «nope» нет" in second["content"]


def test_round_limit_card_stop_gives_summary(harness):
    call = {"calls": [("list_dir", {})]}
    h = harness([call, call, "Итог: посмотрел папку дважды."])
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text('{"max_tool_rounds": 2}', encoding="utf-8")
    h.turn("смотри папку", [NO])
    assert h.cards()[0]["kind"] == "limit"
    last = h.llm.requests[-1]
    assert last["tools"] is None and last["messages"][-1]["content"] == STOP_NOTE
    assert h.messages()[-1]["content"] == "Итог: посмотрел папку дважды."


def test_round_limit_continue(harness):
    call = {"calls": [("list_dir", {})]}
    h = harness([call, call, call, "Готово."])
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text('{"max_tool_rounds": 2}', encoding="utf-8")
    h.turn("смотри папку", [YES])
    assert len(h.llm.requests) == 4 and h.llm.requests[-1]["tools"]


def test_edit_file_and_plan(harness):
    h = harness([
        {"calls": [("plan", {"action": "set", "steps": ["Правка"]})]},
        {"calls": [("edit_file", {"path": "a.md", "old_text": "старое", "new_text": "новое"})]},
        {"calls": [("plan", {"action": "done", "step": "1"})]},
        "Готово.",
    ])
    (h.project_dir / "a.md").write_text("строка: старое\n", encoding="utf-8")
    h.turn("поправь", [YES])
    assert (h.project_dir / "a.md").read_text(encoding="utf-8") == "строка: новое\n"
    assert "-строка: старое" in h.cards()[0]["diff"]
    # План был в системном промпте, пока пункт не отмечен.
    assert "1. [ ] Правка" in h.llm.requests[1]["messages"][0]["content"]
    assert h.services.sessions.get(h.sid)["plan"][0]["done"] is True


def test_stop_saves_partial_text_and_clears_card(harness):
    h = harness([{"calls": [("write_file", {"path": "a.md", "content": "x"})]}])

    async def scenario():
        hub = TurnHub()
        turn = hub.start(h.sid, lambda emit: h.services.agent.run(h.sid, "создай", "r", emit))
        while not any(event["type"] == "approval" for event in turn.events):
            await asyncio.sleep(0.01)
        assert h.services.sessions.get(h.sid)["pending"]["tool"] == "write_file"
        hub.stop(h.sid)
        await turn.task
        return turn.events

    events = asyncio.run(scenario())
    assert events[-1] == {"type": "done", "stopped": True}
    assert h.services.sessions.get(h.sid)["pending"] is None
    assert h.messages()[-1]["content"] == "_(ход остановлен)_"


def test_restart_resets_pending_card(harness):
    h = harness([])
    h.services.sessions.update(h.sid, pending={"id": "x", "kind": "tool"})
    reset_pending(h.services.sessions)
    assert h.services.sessions.get(h.sid)["pending"] is None
    assert h.messages()[-1]["content"] == INTERRUPTED


def test_model_options_from_settings_reach_request(harness):
    h = harness(["Ответ.", "Ещё ответ."])
    h.turn("привет")
    model = h.llm.requests[0]["model"]
    assert h.llm.requests[0]["options"] == {}
    h.services.settings_path.write_text(f'{{"model_options": {{"{model}": {{"thinking": false, "temperature": 0.2}}}}}}',
                                        encoding="utf-8")
    h.turn("ещё")
    assert h.llm.requests[1]["options"] == {"thinking": False, "temperature": 0.2}
