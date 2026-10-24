"""Сборка FastAPI: сервисы, роуты и статика интерфейса."""

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from meepo.agent.approvals import Approvals, reset_pending
from meepo.agent.loop import Agent
from meepo.agent.tools import command, files, memory, plan, search, skills, web
from meepo.agent.tools.browser import TOOLS as BROWSER_TOOLS
from meepo.agent.tools.browser import BrowserPool
from meepo.agent.turns import TurnHub
from meepo.api import chat
from meepo.api import files as files_api
from meepo.api import memory as memory_api
from meepo.api import wiki as wiki_api
from meepo.api import settings as settings_api
from meepo.api import skills as skills_api
from meepo.api import tools as tools_api
from meepo.llm.client import LLMClient
from meepo.memory.store import MemoryStore
from meepo.memory.updater import MemoryUpdater
from meepo.settings import Env, UserSettings
from meepo.skills.env import SkillsEnv
from meepo.skills.history import SkillHistory
from meepo.skills.registry import SkillRegistry
from meepo.skills.secrets import SecretStore
from meepo.storage.attachments import AttachmentStore
from meepo.storage.projects import ProjectStore
from meepo.storage.sessions import SessionStore
from meepo.wiki import tools as wiki_tools
from meepo.wiki.actions import WikiActions
from meepo.wiki.agent import WikiAgent
from meepo.wiki.service import WikiService

UI_DIR = Path(__file__).resolve().parent / "ui"
BUILTIN_SKILLS = Path(__file__).resolve().parent / "skills" / "builtin"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class Services:
    """Всё, что живёт столько же, сколько сервер. llm — фейковая модель в тестах; иначе клиент по настройкам."""

    def __init__(self, env: Env, llm=None) -> None:
        self.env = env
        self.settings_path = env.data_path / "settings.json"
        self.sessions = SessionStore(env.data_path)
        self.projects = ProjectStore(env.data_path)
        self.approvals = Approvals(self.sessions)
        self.turns = TurnHub()
        self.skills = SkillRegistry(env.data_path / "skills", BUILTIN_SKILLS)
        self.skills_env = SkillsEnv(env.data_path / "skills-env")
        self.secrets = SecretStore(env.data_path / "secrets")
        self.history = SkillHistory(env.data_path / "skills")
        self.browsers = BrowserPool(lambda: self.settings().browser_headed)
        self.attachments = AttachmentStore(env.data_path / "attachments")
        self.wiki = WikiService(env.data_path / "wiki", attachments=self.attachments.path)
        self.wiki_actions = WikiActions(env.data_path / "wiki-actions.md")
        self.memory = MemoryStore(env.data_path / "memory")
        self.memory_updater = MemoryUpdater(
            self.memory, self.sessions, self.llm,
            lambda: self.settings().model or ("" if self.settings().llm_kind == "disabled" else env.default_model),
            lambda: self.settings().memory_auto_update_messages)
        self._fixed_llm = llm
        self._client: LLMClient | None = None
        self.agent = Agent(
            sessions=self.sessions, projects=self.projects, approvals=self.approvals, llm=self.llm,
            settings=self.settings,
            tools=[*files.TOOLS, *search.TOOLS, *plan.TOOLS, *skills.TOOLS, *command.TOOLS, *BROWSER_TOOLS, *web.TOOLS,
                   *memory.TOOLS],
            default_model=env.default_model,
            services={"registry": self.skills, "env": self.skills_env, "secrets": self.secrets,
                      "history": self.history, "llm_info": self.llm_info, "browsers": self.browsers,
                      "memory": self.memory, "memory_updater": self.memory_updater, "attachments": self.attachments},
        )
        self.wiki_agent = WikiAgent(
            env.data_path / "wiki", sessions=self.sessions, projects=self.projects, approvals=self.approvals,
            llm=self.llm, settings=self.settings, tools=wiki_tools.TOOLS, default_model=env.default_model,
            services={"wiki": self.wiki, "attachments": self.attachments},
        )

    def settings(self) -> UserSettings:
        return UserSettings.load(self.settings_path)

    def llm_info(self) -> dict[str, str]:
        """Модель сессии для скриптов скиллов: MEEPO_LLM_URL, MEEPO_LLM_MODEL, MEEPO_LLM_KEY."""
        settings = self.settings()
        disabled = settings.llm_kind == "disabled"
        return {"url": "" if disabled else settings.llm_base_url or self.env.llm_base_url,
                "model": "" if disabled else settings.model or self.env.default_model,
                "key": "" if disabled else settings.llm_api_key or self.env.llm_api_key}

    def llm(self):
        if self._fixed_llm is not None:
            return self._fixed_llm
        settings = self.settings()
        disabled = settings.llm_kind == "disabled"
        base_url = "" if disabled else settings.llm_base_url or self.env.llm_base_url
        api_key = "" if disabled else settings.llm_api_key or self.env.llm_api_key
        # Сервер модели сменили в настройках — клиент пересоздаётся (старый закроется при остановке сервера).
        if self._client is None or self._client.base_url != base_url.rstrip("/") or self._client.api_key != api_key:
            self._client = LLMClient(base_url, api_key=api_key, timeout=self.env.llm_timeout_seconds,
                                     verify_ssl=self.env.llm_verify_ssl)
        return self._client

    async def close(self) -> None:
        await self.turns.close()
        await self.browsers.close_all()
        if self._client is not None:
            await self._client.close()


def create_app(env: Env | None = None, llm=None) -> FastAPI:
    env = env or Env.load()
    services = Services(env, llm)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reset_pending(services.sessions)
        services.skills.install_builtin()
        services.memory.initialize()
        services.wiki.initialize()
        yield
        await services.close()

    app = FastAPI(title="Meepo", version="2.0.0", lifespan=lifespan)
    app.state.services = services
    # uvicorn.Server ставит meepo.main; без него (тесты, чужой запуск) перезапуск из интерфейса недоступен.
    app.state.server = None
    app.state.restart = False

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Чужой Host — DNS rebinding; изменяющий запрос со сторонней страницы — CSRF. Агент пишет файлы и
        # запускает команды, поэтому сервер слушается только своих страниц.
        if urlsplit(f"//{request.headers.get('host', '')}").hostname not in LOCAL_HOSTS | {env.host}:
            return PlainTextResponse("Недопустимый заголовок Host", status_code=400)
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin and urlsplit(origin).hostname not in LOCAL_HOSTS | {env.host}:
            return PlainTextResponse("Запрос со сторонней страницы отклонён", status_code=403)
        response = await call_next(request)
        # HTML и модули должны обновляться вместе, иначе браузер смешивает версии интерфейса.
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(chat.router)
    app.include_router(files_api.router)
    app.include_router(settings_api.router)
    app.include_router(skills_api.router)
    app.include_router(tools_api.router)
    app.include_router(memory_api.router)
    app.include_router(wiki_api.router)

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    @app.post("/api/restart", status_code=204)
    def restart() -> None:
        """Остановить сервер штатно и запустить заново (meepo.main); идущие ходы обрываются."""
        if app.state.server is None:
            raise HTTPException(409, "Сервер запущен не через meepo.main — перезапустите его вручную")
        app.state.restart = True
        app.state.server.should_exit = True

    return app
