"""Общее для встроенных инструментов: описание инструмента, результат, проверка аргументов, пути внутри проекта.

Инструмент — `Tool`: имя, описание и JSON-схема для модели, функция `run` и группа доверия `group`.
`group` не None — инструмент что-то меняет, и перед запуском пользователь подтверждает его карточкой
(`preview` готовит для карточки diff или другое пояснение). Чтение — `group=None`, без карточки.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

# Служебные папки, которые не показываются в списке и не просматриваются поиском.
SKIPPED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
                ".ruff_cache", ".pytest_cache", ".meepo"}
# Обёртка из Markdown, которую модель тащит в путь из текста задачи: «(`test_results.md`)».
WRAPPERS = "`'\"«»“”"
JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    # Данные для интерфейса: сохраняются с результатом в истории, модели не отправляются.
    meta: dict | None = None
    # Картинки (имена в data/attachments), которые модель увидит рядом с результатом: view_image.
    images: tuple[str, ...] = ()


@dataclass
class ToolContext:
    """Чей ход выполняет инструмент: папка проекта, сессия и куда слать события (прогресс)."""

    project: Path
    session_id: str
    sessions: object
    request_id: str = ""
    emit: Callable[[dict], None] = lambda event: None
    services: dict = field(default_factory=dict)
    goal: str = ""
    browser_mode: str = "inspect"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict, ToolContext], ToolResult | Awaitable[ToolResult]]
    group: str | None = None
    preview: Callable[[dict, ToolContext], dict] | None = None
    # Некоторые варианты одного инструмента требуют подтверждения каждый раз, даже для доверенной группы.
    always_confirm: Callable[[dict], bool] | None = None
    # Возвращает причину блокировки вызова в текущем контексте или None.
    authorize: Callable[[dict, ToolContext], str | None] | None = None
    # Чинит мелкие промахи модели до проверки по схеме: список строкой «a, b», номер строкой «2».
    prepare: Callable[[dict], None] | None = None

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


def check_arguments(schema: dict, arguments: dict) -> str | None:
    """Проверка по схеме: обязательные есть, лишних нет, простые типы и enum совпадают."""
    properties = schema.get("properties") or {}
    missing = [name for name in schema.get("required") or [] if name not in arguments]
    if missing:
        return f"Не хватает параметров: {', '.join(missing)}"
    unknown = [name for name in arguments if name not in properties]
    if unknown and schema.get("additionalProperties") is False:
        return f"Неизвестные параметры: {', '.join(unknown)}"
    for name, value in arguments.items():
        spec = properties.get(name) or {}
        expected = JSON_TYPES.get(spec.get("type"))
        if expected and (not isinstance(value, expected) or (spec.get("type") != "boolean" and isinstance(value, bool))):
            return f"Параметр {name} должен быть типа {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"Параметр {name} — одно из: {', '.join(map(str, spec['enum']))}"
    return None


def describe_parameters(schema: dict) -> str:
    required = set(schema.get("required") or [])
    parts = []
    for name, spec in (schema.get("properties") or {}).items():
        note = f" — {spec['description']}" if spec.get("description") else ""
        parts.append(f"{name} ({spec.get('type', 'any')}{', обязательный' if name in required else ''}){note}")
    return "; ".join(parts) or "нет"


def clean_path(path: str) -> str:
    """Срезает кавычки и непарные скобки по краям пути: файл «README.md`)» модели никогда не нужен."""
    previous = None
    while path != previous:
        previous = path
        path = path.strip().strip(WRAPPERS).rstrip(",;:")
        # Путь, скопированный из «крошек» («docs / api / index.md»): пробелы вокруг «/» — не часть имён.
        path = re.sub(r"\s*/\s*", "/", path)
        if path.endswith(")") and (path[-2:-1] in tuple(WRAPPERS) or path.count(")") > path.count("(")):
            path = path[:-1]
        if path.startswith("(") and (path[1:2] in tuple(WRAPPERS) or path.count("(") > path.count(")")):
            path = path[1:]
    return path


def inside(project: Path, path: object, *, must_exist: bool = True) -> Path:
    """Путь внутри папки проекта; абсолютный путь и выход за папку (в том числе через ссылку) — ошибка."""
    if not isinstance(path, str) or not clean_path(path):
        raise ValueError("Укажите путь относительно папки проекта")
    relative = clean_path(path)
    # «C:/…» на Linux — обычный относительный путь, но модель так просит абсолютный путь Windows.
    if Path(relative).is_absolute() or re.match(r"^[A-Za-z]:", relative):
        raise ValueError("Нужен путь относительно папки проекта, а не абсолютный")
    root = project.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Путь выходит за пределы папки проекта")
    if must_exist and not target.exists():
        raise ValueError(f"Не найдено: {relative}")
    return target


def relative_to(project: Path, target: Path) -> str:
    return target.relative_to(project.resolve()).as_posix() or "."
