"""Guards и сжатие контекста: срабатывает / не срабатывает / лимит раз / выключен."""

import pytest

from meepo.agent import context
from meepo.agent.guards import language, plan, promise
from meepo.agent.loop import TurnError
from meepo.llm.client import LLMEmpty, LLMRepetition

ENGLISH = "Here is the answer to your question about the login page and its validation rules in English."


def settings(h, **values):
    import json
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text(json.dumps(values), encoding="utf-8")


def sent(h, index):
    return h.llm.requests[index]["messages"]


# ---------- language ----------

def test_language_fires_and_answer_is_rewritten(harness):
    h = harness([ENGLISH, "Вот ответ по-русски."])
    h.turn("расскажи про вход")
    assert sent(h, 1)[-1]["content"] == language.HINT
    assert [m["content"] for m in h.messages() if m["role"] == "assistant"] == ["Вот ответ по-русски."]
    assert {"type": "retry", "keep": False} in h.events


def test_language_not_when_user_asked_english(harness):
    h = harness([ENGLISH])
    h.turn("ответь in english, пожалуйста")
    assert len(h.llm.requests) == 1 and h.messages()[-1]["content"] == ENGLISH


def test_language_limit(harness):
    h = harness([ENGLISH] * 4)
    h.turn("расскажи")
    assert len(h.llm.requests) == language.MAX + 1 and h.messages()[-1]["content"] == ENGLISH


def test_language_switched_off(harness):
    h = harness([ENGLISH])
    settings(h, guards={"language": False})
    h.turn("расскажи")
    assert len(h.llm.requests) == 1


# ---------- promise ----------

def test_promise_fires_after_tool_calls(harness):
    h = harness([{"calls": [("list_dir", {})]}, "Теперь проверю файл README.", {"calls": [("list_dir", {})]}, "Итог: папка пуста."])
    h.turn("посмотри проект")
    assert sent(h, 2)[-1]["content"] == promise.HINT
    texts = [m["content"] for m in h.messages() if m["role"] == "assistant" and not m.get("tool_calls")]
    assert texts == ["Теперь проверю файл README.", "Итог: папка пуста."]
    assert {"type": "retry", "keep": True} in h.events


def test_promise_not_on_first_answer_or_summary(harness):
    assert not promise.promises_action("Готово: создано три файла.")
    assert not promise.promises_action("Жду вашего ответа.")
    assert promise.promises_action("Перехожу к корзине:")
    h = harness(["Сейчас проверю."])
    h.turn("привет")
    assert len(h.llm.requests) == 1


def test_promise_continues_instead_of_offering_next_step(harness):
    offer = 'Результатов мало. Хочешь открыть Песочницу? Или попробую поиск "автотесты"?'
    h = harness([{"calls": [("list_dir", {})]}, offer, {"calls": [("list_dir", {})]}, "Нашёл результаты."])
    h.turn("найди результаты")
    assert sent(h, 2)[-1]["content"] == promise.HINT
    assert h.messages()[-1]["content"] == "Нашёл результаты."


def test_promise_allows_question_for_missing_data() -> None:
    assert not promise.promises_action("Какой раздел документа нужно проверить?")


def test_promise_limit(harness):
    h = harness([{"calls": [("list_dir", {})]}, *["Приступаю к проверке."] * 4])
    h.turn("посмотри")
    assert len(h.llm.requests) == 1 + promise.MAX + 1


# ---------- plan ----------

def test_plan_fires_until_steps_done(harness):
    h = harness([
        {"calls": [("plan", {"action": "set", "steps": ["Кейсы", "Ревью"]})]},
        {"calls": [("plan", {"action": "done", "step": 1})]},
        "Кейсы готовы.",
        {"calls": [("plan", {"action": "done", "step": 2})]},
        "Всё готово.",
    ])
    h.turn("кейсы и ревью")
    assert "Сделай пункт 2" in sent(h, 3)[-1]["content"]
    assert h.messages()[-1]["content"] == "Всё готово."


def test_plan_question_ends_turn(harness):
    h = harness([{"calls": [("plan", {"action": "set", "steps": ["Кейсы", "Ревью"]})]}, "Какой раздел взять?"])
    h.turn("кейсы и ревью")
    assert len(h.llm.requests) == 2


def test_plan_limit(harness):
    h = harness([{"calls": [("plan", {"action": "set", "steps": ["Кейсы"]})]}, *["Работа идёт хорошо."] * 5])
    h.turn("кейсы")
    assert len(h.llm.requests) == 1 + plan.MAX + 1


# ---------- retry ----------

def test_retry_same_request_then_without_tools(harness):
    h = harness([LLMEmpty("пусто"), LLMRepetition("цикл"), "Ответ."])
    h.turn("привет")
    assert h.llm.requests[1]["tools"] and h.llm.requests[2]["tools"] is None
    assert h.messages()[-1]["content"] == "Ответ."
    assert any("Guard retry: Модель зациклилась — повторяю без инструментов" in line for line in h.messages()[-1]["log"])


def test_retry_gives_up_with_error(harness):
    h = harness([LLMEmpty("пусто")] * 3)
    with pytest.raises(TurnError, match="пустой ответ"):
        h.turn("привет")
    assert len(h.llm.requests) == 3


def test_retry_switched_off(harness):
    h = harness([LLMEmpty("пусто")])
    settings(h, guards={"retry": False})
    with pytest.raises(TurnError):
        h.turn("привет")
    assert len(h.llm.requests) == 1


# ---------- сжатие контекста ----------

def test_fit_replaces_oldest_results_then_drops_messages():
    big = "x" * 4000
    messages = [{"role": "system", "content": "промпт"}, {"role": "user", "content": "старый вопрос"},
                {"role": "assistant", "content": "старый ответ"}, {"role": "user", "content": "прочитай файлы"}]
    for number in range(4):
        call = {"id": f"c{number}", "name": "read_file", "arguments": f'{{"path": "docs/{number}.md"}}'}
        messages += [{"role": "assistant", "content": None, "tool_calls": [call]},
                     {"role": "tool", "tool_call_id": call["id"], "name": "read_file", "about": context.about(call), "content": big}]
    fitted, report = context.fit(messages, 3000)
    assert fitted[5]["content"] == "[результат read_file docs/0.md убран из контекста — при необходимости прочитай снова]"
    assert fitted[7]["content"].startswith("[результат read_file docs/1.md")
    assert fitted[-1]["content"] == big and fitted[-3]["content"] == big
    assert report == "заменено результатов: 2"
    fitted, report = context.fit(messages, 1500)
    assert "старый вопрос" not in [m["content"] for m in fitted] and "прочитай файлы" in [m["content"] for m in fitted]
    assert "выпало сообщений" in report
    # Вызов и его результат выпадают только вместе: у каждого результата остаётся свой вызов.
    ids = {call["id"] for m in fitted for call in m.get("tool_calls") or []}
    assert all(m["tool_call_id"] in ids for m in fitted if m["role"] == "tool")


def test_old_snapshots_go_first():
    snapshot = "Страница: x\n\nЭлементы (ref роль «имя» — локатор Playwright):\n" + "e1 button «Ок»\n" * 300
    calls = [{"id": f"s{n}", "name": "browser", "arguments": '{"action": "snapshot"}'} for n in range(2)]
    read = {"id": "r", "name": "read_file", "arguments": '{"path": "a.md"}'}
    messages = [{"role": "system", "content": "п"}, {"role": "user", "content": "проверь"}]
    for call, content in ((calls[0], snapshot), (read, "y" * 3000), (calls[1], snapshot)):
        messages += [{"role": "assistant", "content": None, "tool_calls": [call]},
                     {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "about": context.about(call), "content": content}]
    fitted, _ = context.fit(messages, context.total_tokens(messages) - 500)
    assert fitted[3]["content"].startswith("[результат browser snapshot убран") and "сними snapshot заново" in fitted[3]["content"]
    assert fitted[5]["content"] == "y" * 3000


def test_turn_compacts_inside_small_window(harness):
    script = [{"calls": [("read_file", {"path": f"{n}.md"})]} for n in range(4)] + ["Прочитал."]
    # Окно подобрано так, чтобы старые результаты заменялись маркерами, но сами группы ещё не выпадали целиком.
    h = harness(script, window=11800)
    for n in range(4):
        (h.project_dir / f"{n}.md").write_text(f"файл {n} " + "текст " * 400, encoding="utf-8")
    h.turn("прочитай все файлы")
    last = h.llm.requests[-1]["messages"]
    assert any(m["content"] == "[результат read_file 0.md убран из контекста — при необходимости прочитай снова]" for m in last)
    assert any("Сжатие контекста" in line for line in h.messages()[-1]["log"])
    assert h.services.sessions.get(h.sid)["context_window"] == 11800


def test_huge_paste_goes_to_file(harness):
    h = harness(["Принял."], window=8192)
    text = "Требование. " * 3000
    h.turn(text)
    saved = h.messages()[0]["content"]
    assert ".meepo/inbox/" in saved and len(saved) < 3000
    assert list((h.project_dir / ".meepo" / "inbox").glob("*.md"))[0].read_text(encoding="utf-8") == text
