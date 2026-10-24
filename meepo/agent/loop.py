"""Цикл хода основного агента: модель → вызовы инструментов (с карточками) → модель … → ответ.

Раунд — один запрос к модели и выполнение её вызовов (не больше MAX_CALLS_PER_ROUND). На лимите раундов —
карточка «продолжить или остановить»; «остановить» — последний запрос без инструментов: итог.
События хода уходят в `emit`: delta, reasoning, tool_call, tool_result, progress, approval, log, retry, done.
retry — guard вернул модель к работе: напечатанный текст запроса уходит в строки хода (keep) или выбрасывается.
Перед каждым запросом контекст сжимается под окно модели (agent/context.py), ответ проверяют guards (agent/guards/).
«Стоп» отменяет задачу хода: написанный текст сохраняется с пометкой «(ответ прерван)».
"""

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from meepo.agent import context
from meepo.agent.guards import registry as guards
from meepo.agent.approvals import Approvals
from meepo.agent.prompt import system_prompt
from meepo.agent.tools.base import Tool, ToolContext, ToolResult, check_arguments, describe_parameters
from meepo.llm.client import LLMEmpty, LLMRepetition, Reply

logger = logging.getLogger(__name__)

MAX_CALLS_PER_ROUND = 4
LOG_CHARS = 160
INTERRUPTED_SUFFIX = "\n\n_(ответ прерван)_"
STOPPED_TEXT = "_(ход остановлен)_"
STOP_NOTE = (
    "Пользователь остановил работу на лимите шагов. Инструменты больше не вызывай. Коротко напиши итог: "
    "что уже сделано (и где лежит результат) и что осталось сделать."
)


class TurnError(Exception):
    """Ход нельзя начать: нет проекта, не выбрана модель."""


@dataclass
class TurnState:
    session_id: str
    request_id: str
    project: dict
    model: str
    window: int
    messages: list[dict]
    max_rounds: int
    emit: Callable[[dict], None]
    goal: str = ""
    browser_mode: str = "inspect"
    rounds: int = 0
    stopped: bool = False
    # Пользователь нажал «Нет» в карточке: текущий ход завершается без ещё одного ответа модели.
    cancelled: bool = False
    # Текст ответа, который модель пишет прямо сейчас: его сохраняет «Стоп».
    partial: str = ""
    spent: float = 0.0
    output_tokens: int = 0
    input_tokens: int | None = None
    log: list[str] = field(default_factory=list)
    # Для guards: какие включены, сколько раз сработал каждый, проверяемый ответ, подсказки (не реплики пользователя),
    # сбой последнего запроса и надо ли повторить его без инструментов.
    guards: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    answer: str = ""
    hints: set = field(default_factory=set)
    failure: Exception | None = None
    without_tools: bool = False
    session: Callable[[], dict] = dict
    # Во сколько раз реальные токены сервера меньше грубой оценки (байты / 4): по русскому тексту оценка завышена.
    # Уточняется по каждому ответу с input_tokens, чтобы не сжимать контекст раньше времени.
    scale: float = 1.0
    # Что известно о пользователе — по реплике, с которой начался ход; в промпт каждого запроса.
    memory: str = ""
    # Видит ли модель картинки (сервер сообщает; None — не сообщает, тогда картинки отправляются).
    vision: bool | None = None

    def note(self, text: str) -> None:
        """Строка лога хода: видна под ответом и сохраняется с ним."""
        self.log.append(text)
        self.emit({"type": "log", "text": text})

    def user_texts(self) -> list[str]:
        return [item["content"] or "" for item in self.messages if item["role"] == "user"
                and item["content"] not in self.hints and item["content"] != STOP_NOTE]


class Agent:
    def __init__(self, *, sessions, projects, approvals: Approvals, llm: Callable, settings: Callable,
                 tools: list[Tool], default_model: str = "", services: dict | None = None) -> None:
        self.sessions = sessions
        self.projects = projects
        self.approvals = approvals
        self.llm = llm
        self.settings = settings
        self.tools = {tool.name: tool for tool in tools}
        self.default_model = default_model
        # Сервисы для инструментов (скиллы, окружение, секреты); registry даёт список скиллов в промпт.
        self.services = services or {}

    # ---------- то, чем агенты различаются (агент вики переопределяет, см. wiki/agent.py) ----------

    # Огромную вставку пользователя — в файл папки хода.
    stash_pastes = True

    def workspace(self, session: dict | None) -> dict:
        """Где работают инструменты хода: {"name", "path"} — проект сессии."""
        if session and session.get("project_id") is None:
            folder = self.sessions.sessions_dir.parent / "chat-workspaces" / session["id"]
            folder.mkdir(parents=True, exist_ok=True)
            return {"name": "Чат", "path": str(folder), "no_project": True}
        project = self.projects.get(session["project_id"]) if session else None
        if project is None:
            raise TurnError("Сессия не привязана к проекту: выберите проект")
        return project

    def select_history(self, history: list[dict]) -> list[dict]:
        return history

    def system(self, state: "TurnState", session: dict) -> str:
        # Промпт пересобирается каждый раунд: план и список скиллов могли измениться посреди хода.
        registry = self.services.get("registry")
        skills = registry.prompt_list() if registry else ""
        return system_prompt(state.project, session["plan"], skills=skills, memory=state.memory,
                             browser_mode=state.browser_mode)

    def memory_context(self, text: str) -> str:
        memory = self.services.get("memory")
        return memory.prompt_context(text) if memory else ""

    def finished(self, session_id: str) -> None:
        if updater := self.services.get("memory_updater"):
            updater.after_turn(session_id)

    # ---------- ход ----------

    async def run(self, session_id: str, text: str, request_id: str, emit: Callable[[dict], None],
                  attachments: tuple[str, ...] = (), browser_mode: str = "inspect") -> None:
        """attachments — имена картинок, присланных с репликой (чат вики)."""
        session = self.sessions.get(session_id)
        project = self.workspace(session)
        settings = self.settings()
        model = settings.model or ("" if settings.llm_kind == "disabled" else self.default_model)
        if not model:
            raise TurnError("Модель не выбрана: укажите DEFAULT_MODEL в .env или выберите модель в настройках")
        window = await self.llm().context_length(model) or settings.default_context_tokens
        if self.stash_pastes:
            text = context.stash_paste(Path(project["path"]), text, window - context.reserve(window))
        self.sessions.add_message(session_id, "user", text, attachments=list(attachments))
        history = self.select_history(self.sessions.messages(session_id))
        messages = context.build("", history, self._image)
        state = TurnState(session_id, request_id, project, model, window, messages,
                          settings.max_tool_rounds, emit, guards=dict(settings.guards),
                          session=lambda: self.sessions.get(session_id), goal=text, browser_mode=browser_mode)
        state.note(f"Цель хода: {text}")
        state.note("Браузер: " + ("взаимодействие с подтверждением" if browser_mode == "interact" else "только изучение"))
        state.memory = self.memory_context(text)
        state.vision = (await self.llm().model_info(model)).get("vision")
        ctx = ToolContext(Path(project["path"]), session_id, self.sessions, request_id, emit, self.services,
                          goal=text, browser_mode=browser_mode)
        try:
            await self._loop(state, ctx)
        except asyncio.CancelledError:
            self._save_stopped(state)
            raise

    async def _loop(self, state: TurnState, ctx: ToolContext) -> None:
        while True:
            if state.rounds >= state.max_rounds and not state.stopped:
                await self._limit(state)
            tools = None if state.stopped else [tool.schema() for tool in self.tools.values()]
            reply = await self._ask(state, tools)
            if reply.tool_calls and tools:
                await self._run_calls(state, ctx, reply)
                state.rounds += 1
                if state.cancelled:
                    self._finish(state, "Действие отменено.", Reply())
                    return
                continue
            text = reply.content
            if reply.tool_calls:
                # Работу остановили на лимите: инструменты не предлагались, а модель всё равно их зовёт.
                note = f"Работа остановлена после {state.rounds} шагов. Напишите «продолжай» — продолжу с этого места."
                text = f"{text}\n\n{note}" if text else note
            state.answer = text
            if found := guards.answer_hint(state):
                self._nudge(state, reply, *found)
                continue
            self._finish(state, text, reply)
            return

    def _nudge(self, state: TurnState, reply: Reply, name: str, hint: str) -> None:
        """Guard вернул модель к работе. Ответ не по-русски выбрасывается; остальные остаются в чате промежуточной репликой."""
        state.counts[name] = state.counts.get(name, 0) + 1
        state.hints.add(hint)
        state.note(f"Guard {name}: {guards.LOG[name]}")
        keep = name != "language"
        if keep:
            self.sessions.add_message(state.session_id, "assistant", reply.content, reasoning=reply.reasoning)
        state.emit({"type": "retry", "keep": keep})
        state.messages += [{"role": "assistant", "content": reply.content}, {"role": "user", "content": hint}]

    async def _ask(self, state: TurnState, tools: list[dict] | None) -> Reply:
        """Запрос к модели; пустой ответ или зацикливание — guard retry: повтор, затем без инструментов, затем ошибка."""
        while True:
            try:
                reply = await self._request(state, None if state.without_tools else tools)
            except (LLMEmpty, LLMRepetition) as exc:
                state.failure = exc
                hint = guards.retry.check(state) if state.guards.get("retry", True) else None
                if hint is None:
                    looped = isinstance(exc, LLMRepetition)
                    raise TurnError(("Модель зациклилась и повторяла одно и то же" if looped else "Модель вернула пустой ответ")
                                    + ". Попробуйте переформулировать или начать новую сессию") from None
                state.counts["retry"] = state.counts.get("retry", 0) + 1
                state.note(f"Guard retry: {hint}")
                state.emit({"type": "retry", "keep": False})
                continue
            state.counts["retry"], state.without_tools = 0, False
            return reply

    async def _request(self, state: TurnState, tools: list[dict] | None) -> Reply:
        state.messages[0] = {"role": "system", "content": self.system(state, self.sessions.get(state.session_id))}
        messages = state.messages + ([{"role": "user", "content": STOP_NOTE}] if state.stopped else [])
        schemas = context.estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
        budget = int((state.window - context.reserve(state.window)) / state.scale) - schemas
        messages, compacted = context.fit(messages, budget)
        if compacted:
            state.note(f"Сжатие контекста под окно {state.window}: {compacted}")
        state.note(f"Запрос к модели {state.model} · раунд {state.rounds + 1}" + ("" if tools else " · без инструментов"))
        started = time.perf_counter()
        first = None
        state.partial = ""
        reply = None
        options = self.settings().model_options.get(state.model, {})
        async for item in self.llm().stream(state.model, messages, tools, options=options):
            # Скорость генерации считается от первого токена: ожидание ответа сервера в неё не входит.
            first = first or time.perf_counter()
            if isinstance(item, Reply):
                reply = item
            elif item[0] == "delta":
                state.partial += item[1]
                state.emit({"type": "delta", "text": item[1]})
            elif item[1]:
                state.emit({"type": "reasoning", "text": item[1]})
        elapsed = time.perf_counter() - started
        state.spent += elapsed
        state.output_tokens += reply.output_tokens or 0
        state.input_tokens = reply.input_tokens
        tokens = f", токенов: вход {reply.input_tokens or '?'}, выход {reply.output_tokens or '?'}"
        calls = f"; вызовы: {', '.join(call['name'] for call in reply.tool_calls)}" if reply.tool_calls else ""
        state.note(f"Модель ответила за {elapsed:.1f} с{tokens}{calls}")
        estimated = context.total_tokens(messages) + schemas
        if reply.input_tokens and estimated:
            state.scale = min(max(reply.input_tokens / estimated, 0.25), 1.5)
        used = reply.input_tokens or int(estimated * state.scale)
        generating = time.perf_counter() - (first or started)
        speed = round(reply.output_tokens / generating, 1) if reply.output_tokens and generating > 0.05 else None
        self.sessions.update(state.session_id, context_tokens=used, context_window=state.window, tokens_per_second=speed)
        return reply

    async def _run_calls(self, state: TurnState, ctx: ToolContext, reply: Reply) -> None:
        calls = reply.tool_calls
        self.sessions.add_message(state.session_id, "assistant", reply.content, tool_calls=calls, reasoning=reply.reasoning)
        state.messages.append({"role": "assistant", "content": reply.content or None, "tool_calls": calls})
        state.partial = ""
        replies = []
        for index, call in enumerate(calls):
            state.emit({"type": "tool_call", "call": call})
            if state.cancelled:
                result = ToolResult("Ход остановлен после отмены действия", True)
            elif index >= MAX_CALLS_PER_ROUND:
                result = ToolResult(f"Не больше {MAX_CALLS_PER_ROUND} вызовов за раунд — повтори этот вызов отдельно", True)
            else:
                result, answer = await self._call(state, ctx, call)
                if answer:
                    replies.append(answer)
            if result.images and state.vision is False:
                result = replace(result, content=f"{result.content}\n\n{context.NO_VISION}")
            self.sessions.add_message(state.session_id, "tool", result.content, tool_call_id=call["id"],
                                      tool_name=call["name"], is_error=result.is_error, meta=result.meta,
                                      images=list(result.images))
            message = {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                       "about": context.about(call), "content": result.content}
            if result.images and state.vision is not False:
                message["images"] = [str(path) for name in result.images if (path := self._image(name))]
            state.messages.append(message)
            state.emit({"type": "tool_result", "call_id": call["id"], "name": call["name"],
                        "content": result.content, "is_error": result.is_error, "meta": result.meta})
        for text in replies:
            # Ответ на карточку — обычная реплика пользователя: она идёт после результатов вызовов.
            self.sessions.add_message(state.session_id, "user", text, card_reply=True)
            state.messages.append({"role": "user", "content": text})

    def _image(self, name: str) -> Path | None:
        store = self.services.get("attachments")
        return store.path(name) if store else None

    async def _call(self, state: TurnState, ctx: ToolContext, call: dict) -> tuple[ToolResult, str]:
        """Результат вызова и текст пользователя, если он ответил на карточку словами."""
        tool = self.tools.get(call["name"])
        if tool is None:
            return ToolResult(f"Инструмента «{call['name']}» нет. Доступны: {', '.join(self.tools)}", True), ""
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return ToolResult("Аргументы должны быть JSON-объектом", True), ""
        if tool.prepare:
            tool.prepare(arguments)
        # Неполный вызов пользователю не показываем: модель сначала исправит его сама.
        if problem := check_arguments(tool.parameters, arguments):
            return ToolResult(f"{problem}. Параметры {tool.name}: {describe_parameters(tool.parameters)}. "
                              "Исправь и вызови снова.", True), ""
        if tool.authorize and (problem := tool.authorize(arguments, ctx)):
            return ToolResult(problem, True), ""
        session = self.sessions.get(state.session_id)
        always_confirm = bool(tool.always_confirm and tool.always_confirm(arguments))
        if tool.group and (always_confirm or tool.group not in session["trusted"]):
            try:
                preview = tool.preview(arguments, ctx) if tool.preview else {}
            except (ValueError, OSError, UnicodeError) as exc:
                return ToolResult(f"{exc}. Исправь и вызови снова.", True), ""
            decision = await self.approvals.ask(state.session_id, {
                "kind": "tool", "tool": tool.name, "group": tool.group, "arguments": arguments,
                "allow_trust": not always_confirm, "goal": state.goal, "browser_mode": state.browser_mode, **preview,
            }, state.emit)
            state.note(f"Решение по {tool.name}: {decision['action']}" + (" · доверие на сессию" if decision["trust"] else ""))
            if decision["trust"] and not always_confirm:
                self.sessions.trust(state.session_id, tool.group)
            if decision["action"] == "reject":
                state.cancelled = True
                return ToolResult("Действие отменено через карточку", True), ""
            if decision["action"] == "reply":
                return ToolResult(f"Пользователь не разрешил вызов и ответил: «{decision['text']}». "
                                  "Учти ответ: доработай действие или спроси, как поступить.", True), decision["text"]
            if "selected" in decision:
                # Карточка со списком: номера пунктов, которые отметил пользователь, — модель подставить их не может.
                arguments = {**arguments, "selected": decision["selected"]}
        return await self._execute(state, ctx, tool, arguments), ""

    async def _execute(self, state: TurnState, ctx: ToolContext, tool: Tool, arguments: dict) -> ToolResult:
        state.note(f"Вызов {tool.name} {_shorten(json.dumps(arguments, ensure_ascii=False))}")
        started = time.perf_counter()
        try:
            if inspect.iscoroutinefunction(tool.run):
                result = await tool.run(arguments, ctx)
            else:
                result = await asyncio.to_thread(tool.run, arguments, ctx)
        except (ValueError, OSError, UnicodeError) as exc:
            result = ToolResult(str(exc), True)
        except Exception as exc:  # noqa: BLE001 — сбой одного инструмента не роняет ход: модель получает ошибку
            logger.exception("Инструмент %s упал", tool.name)
            result = ToolResult(f"Внутренняя ошибка инструмента: {type(exc).__name__}: {exc}", True)
        elapsed = time.perf_counter() - started
        # Время работы инструментов входит в сводку хода; ожидание решения по карточке — нет.
        state.spent += elapsed
        outcome = f"ошибка — {_shorten(result.content)}" if result.is_error else f"готово, {len(result.content)} символов"
        state.note(f"{tool.name}: {outcome} за {elapsed:.1f} с")
        return result

    async def _limit(self, state: TurnState) -> None:
        step = self.settings().max_tool_rounds
        decision = await self.approvals.ask(state.session_id, {
            "kind": "limit", "title": f"Сделано шагов: {state.rounds} — продолжить работу?",
            "text": f"Это лимит хода. «Да» даст ещё {step} шагов; «Нет» — агент напишет итог: что сделано и что осталось.",
        }, state.emit)
        if decision["action"] == "approve":
            state.max_rounds += step
        else:
            state.stopped = True

    def _finish(self, state: TurnState, text: str, reply: Reply) -> None:
        stats = {"seconds": round(state.spent, 1), "rounds": state.rounds + 1,
                 "input_tokens": state.input_tokens, "output_tokens": state.output_tokens or None}
        message = self.sessions.add_message(state.session_id, "assistant", text, reasoning=reply.reasoning,
                                            stats=stats, log=state.log)
        state.emit({"type": "done", "message": message})
        self.finished(state.session_id)

    def _save_stopped(self, state: TurnState) -> None:
        text = state.partial.strip()
        self.sessions.add_message(state.session_id, "assistant", text + INTERRUPTED_SUFFIX if text else STOPPED_TEXT,
                                  log=state.log)


def _shorten(text: str, limit: int = LOG_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
