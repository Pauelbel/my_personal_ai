"""Раздел «Настройки»: data/settings.json, список моделей сервера и заплатки для маленькой модели."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from meepo.agent.guards import registry as guards
from meepo.llm.client import PARAMETERS, LLMClient, LLMError
from meepo.settings import ROOT
from meepo.settings import UserSettings, default_guards

router = APIRouter(prefix="/api")

# Числовые настройки: допустимые границы.
LIMITS = {"default_context_tokens": (1024, 1_048_576), "max_tool_rounds": (1, 200), "memory_auto_update_messages": (0, 1000)}


class ModelProbe(BaseModel):
    base_url: str
    api_key: str = ""


def check(fields: dict) -> str | None:
    known = UserSettings.__dataclass_fields__
    if unknown := sorted(set(fields) - set(known)):
        return f"Неизвестные настройки: {', '.join(unknown)}"
    for name, value in fields.items():
        if name in LIMITS:
            low, high = LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                return f"{name} — целое число от {low} до {high}"
        elif name == "guards":
            if not isinstance(value, dict) or set(value) - set(default_guards()) or not all(isinstance(on, bool) for on in value.values()):
                return f"guards — {{{', '.join(default_guards())}: true/false}}"
        elif name == "model_options":
            if problem := check_model_options(value):
                return problem
        elif name == "browser_headed":
            if not isinstance(value, bool):
                return "browser_headed — true или false"
        elif not isinstance(value, str):
            return f"{name} — строка"
    if fields.get("llm_kind", "") not in ("", "llama", "openai", "disabled"):
        return "llm_kind — llama или openai"
    base = fields.get("llm_base_url", "")
    if base and not base.startswith(("http://", "https://")):
        return "Адрес сервера модели — http://… или https://…, с /v1 на конце"
    return None


def check_model_options(value) -> str | None:
    """{id модели: {"thinking": bool, параметр из PARAMETERS: число в границах}}."""
    if not isinstance(value, dict) or not all(isinstance(model, str) and isinstance(options, dict) for model, options in value.items()):
        return "model_options — {модель: {параметр: значение}}"
    for options in value.values():
        for name, setting in options.items():
            if name == "thinking":
                if not isinstance(setting, bool):
                    return "thinking — true или false"
                continue
            spec = PARAMETERS.get(name)
            if spec is None:
                return f"Неизвестный параметр модели: {name}"
            kinds = (int,) if spec["type"] == "int" else (int, float)
            if isinstance(setting, bool) or not isinstance(setting, kinds) or not spec["min"] <= setting <= spec["max"]:
                return f"{spec['label']} — число от {spec['min']} до {spec['max']}"
    return None


@router.get("/settings")
def get_settings(request: Request) -> dict:
    services = request.app.state.services
    settings = services.settings()
    disabled = settings.llm_kind == "disabled"
    public = {name: value for name, value in settings.__dict__.items() if name != "llm_api_key"}
    return {**public, "llm_api_key_set": bool(settings.llm_api_key),
            "env_base_url": services.env.llm_base_url, "env_model": services.env.default_model,
            "active_model": "" if disabled else settings.model or services.env.default_model,
            "active_base_url": "" if disabled else settings.llm_base_url or services.env.llm_base_url,
            "configured": not disabled and bool(settings.llm_base_url or services.env.llm_base_url)}


@router.put("/settings")
def put_settings(body: dict, request: Request) -> dict:
    services = request.app.state.services
    if problem := check(body):
        raise HTTPException(400, problem)
    current = services.settings().__dict__
    UserSettings(**{**current, **body}).save(services.settings_path)
    return get_settings(request)


@router.get("/guards")
def list_guards(request: Request) -> list[dict]:
    """Заплатки: название и описание — из docstring файла («Guard «язык»: …»), исходный код — сам файл."""
    enabled = request.app.state.services.settings().guards
    items = []
    for name, module in guards.ALL.items():
        path = Path(module.__file__)
        doc = (module.__doc__ or "").strip()
        title = re.match(r"Guard «([^»]+)»", doc)
        items.append({
            "name": name, "title": title.group(1).capitalize() if title else name, "enabled": enabled.get(name, True),
            "doc": doc, "file": path.relative_to(ROOT).as_posix(), "source": path.read_text(encoding="utf-8"),
        })
    return items


@router.get("/models")
async def list_models(request: Request, base_url: str = "") -> list[str]:
    """Модели сервера: текущего или по адресу base_url — чтобы проверить сервер до сохранения настроек."""
    services = request.app.state.services
    current = (services.settings().llm_base_url or services.env.llm_base_url).rstrip("/")
    if not base_url or base_url.rstrip("/") == current:
        client, own = services.llm(), False
    elif base_url.startswith(("http://", "https://")):
        client, own = LLMClient(base_url, api_key=services.env.llm_api_key, timeout=10,
                                verify_ssl=services.env.llm_verify_ssl), True
    else:
        raise HTTPException(400, "Адрес сервера модели — http://… или https://…, с /v1 на конце")
    try:
        return await client.list_models()
    except LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        if own:
            await client.close()


@router.get("/models/info")
async def model_info(request: Request, model: str) -> dict:
    """Возможности модели текущего сервера и её значения параметров — для блока «Параметры модели»."""
    info = await request.app.state.services.llm().model_info(model)
    parameters = [{"name": name, **{key: value for key, value in spec.items() if key != "ollama"}}
                  for name, spec in PARAMETERS.items()]
    return {**info, "parameters": parameters}


@router.post("/models/probe")
async def probe_models(body: ModelProbe, request: Request) -> list[str]:
    """Проверяет ещё не сохранённое подключение; ключ передаётся в теле и не попадает в URL."""
    if not body.base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "Адрес сервера модели — http://… или https://…, с /v1 на конце")
    env = request.app.state.services.env
    client = LLMClient(body.base_url, api_key=body.api_key, timeout=10, verify_ssl=env.llm_verify_ssl)
    try:
        return await client.list_models()
    except LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        await client.close()
