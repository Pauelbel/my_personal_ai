"""Настройки: `.env` задаёт запуск (адрес модели, порт, папка данных), `data/settings.json` — выбор из UI.

`.env` читается один раз при старте; переменные окружения важнее файла. `settings.json` пишет раздел
«Настройки» интерфейса: всё, чего в нём нет, берётся из значений по умолчанию ниже.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env_file(path: Path) -> dict[str, str]:
    """Строки `КЛЮЧ=значение`; пустые строки и комментарии `#` пропускаются, кавычки вокруг значения снимаются."""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True)
class Env:
    """Параметры запуска из `.env`. Меняются правкой файла и перезапуском сервера."""

    llm_base_url: str = ""
    llm_api_key: str = ""
    default_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_verify_ssl: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    data_path: Path = ROOT / "data"

    @classmethod
    def load(cls, env_file: Path = ROOT / ".env") -> "Env":
        raw = {**read_env_file(env_file), **os.environ}

        def get(name: str, default):
            value = raw.get(name.upper())
            if value is None or value == "":
                return default
            if isinstance(default, bool):
                return value.lower() in ("1", "true", "yes", "on")
            if isinstance(default, Path):
                path = Path(value)
                return path if path.is_absolute() else ROOT / path
            return type(default)(value)

        return cls(**{name: get(name, getattr(cls, name)) for name in cls.__dataclass_fields__})


def default_guards() -> dict[str, bool]:
    return {"language": True, "promise": True, "plan": True, "retry": True}


@dataclass
class UserSettings:
    """Выбор пользователя из раздела «Настройки»; хранится в `data/settings.json`."""

    llm_kind: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    model: str = ""
    default_context_tokens: int = 8192
    max_tool_rounds: int = 40
    guards: dict[str, bool] = field(default_factory=default_guards)
    browser_headed: bool = True
    memory_auto_update_messages: int = 10
    # Параметры по модели: {id модели: {"thinking": bool, "temperature": 0.7, …}}; чего нет — решает модель.
    model_options: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "UserSettings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        known = {name: raw[name] for name in cls.__dataclass_fields__ if name in raw}
        settings = cls(**known)
        # Новый guard, которого ещё нет в файле, включён по умолчанию.
        settings.guards = {**default_guards(), **settings.guards}
        return settings

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
