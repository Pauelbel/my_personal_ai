"""Файлы и папки проекта: list_dir, read_file (чтение, без карточки) и write_file, edit_file, delete_path,
move_path, make_dir (группа доверия files_write — с карточкой; в карточке записи и правки — diff)."""

import difflib
import os
import re
import shutil
from pathlib import Path

from meepo.agent.tools.base import SKIPPED_DIRS, Tool, ToolContext, ToolResult, inside, relative_to
from meepo.agent.tools.web import images_footer

# Сколько read_file отдаёт за раз: файл больше — оглавлением, а читается кусками строк (offset, limit).
MAX_READ_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_READ_LINES = 400
MAX_WRITE_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 200
DIFF_LINES = 400
OUTLINE_LINES = 150
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
GROUP = "files_write"


def _text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Файл больше {MAX_FILE_BYTES // 1024 // 1024} МБ")
    content = path.read_bytes().decode("utf-8")
    if "\x00" in content:
        raise ValueError("Это не текстовый файл")
    return content


# ---------- чтение ----------

def list_dir(args: dict, ctx: ToolContext) -> ToolResult:
    target = inside(ctx.project, args.get("path") or ".")
    if not target.is_dir():
        raise ValueError("Это не папка")
    depth = args.get("depth", 1)
    lines: list[str] = []

    def walk(folder: Path, level: int) -> None:
        entries = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        for item in entries:
            if item.name in SKIPPED_DIRS or len(lines) >= MAX_LIST_ENTRIES:
                continue
            indent = "  " * level
            if item.is_dir():
                lines.append(f"{indent}{item.name}/")
                if level + 1 < depth:
                    walk(item, level + 1)
            else:
                lines.append(f"{indent}{item.name} ({item.stat().st_size} байт)")

    walk(target, 0)
    if len(lines) >= MAX_LIST_ENTRIES:
        lines.append(f"Показаны первые {MAX_LIST_ENTRIES} элементов — уточните папку")
    return ToolResult("\n".join(lines) or "Папка пуста")


def read_file(args: dict, ctx: ToolContext) -> ToolResult:
    target = inside(ctx.project, args.get("path"))
    if not target.is_file():
        raise ValueError("Это не файл")
    content = _text(target)
    relative = relative_to(ctx.project, target)
    offset, limit = args.get("offset"), args.get("limit")
    if offset is None and limit is None:
        size = len(content.encode("utf-8"))
        if size > MAX_READ_BYTES:
            return ToolResult(_overview(relative, content, size))
        # Картинки Markdown — путями от корня проекта: так их сразу можно открыть через view_image.
        return ToolResult(content + (images_footer(relative, content) if target.suffix.lower() == ".md" else ""))
    return ToolResult(_lines(relative, content, max(offset or 1, 1), max(limit or DEFAULT_READ_LINES, 1)))


def outline(text: str) -> str:
    """Оглавление строками «строка N: заголовок» с отступом по уровню; заголовки внутри блоков кода не считаются."""
    found, fence = [], False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and (match := HEADING.match(line)):
            found.append((number, len(match[1]), match[2].strip()))
    if not found:
        return ""
    # Не влезает в OUTLINE_LINES — мелкие уровни убираются.
    levels = sorted({level for _, level, _ in found})
    while len(levels) > 1 and sum(1 for item in found if item[1] in levels) > OUTLINE_LINES:
        levels.pop()
    top = levels[0]
    shown = [item for item in found if item[1] in levels]
    lines = [f"{'  ' * (level - top)}строка {number}: {title}" for number, level, title in shown[:OUTLINE_LINES]]
    if len(shown) > OUTLINE_LINES:
        lines.append(f"… и ещё {len(shown) - OUTLINE_LINES} заголовков")
    return "\n".join(lines)


def _overview(relative: str, content: str, size: int) -> str:
    """Большой файл: размер, оглавление и как читать дальше — вместо содержимого."""
    lines = content.splitlines()
    contents = outline(content)
    body = (f"Оглавление (номер строки: заголовок):\n{contents}" if contents
            else "Заголовков в файле нет. Начало файла:\n" + "\n".join(lines[:20]))
    return (
        f"Файл {relative} большой: {len(lines)} строк, {size // 1024} КБ — целиком в окно модели не помещается.\n\n"
        f"{body}\n\nЧитай нужные места кусками: read_file с offset (номер строки) и limit (до {DEFAULT_READ_LINES} "
        "строк); найти место по слову — search_files с path этого файла. Работу по всему документу делай скиллом."
    )


def _lines(relative: str, content: str, offset: int, limit: int) -> str:
    """Кусок файла строками offset…: не больше limit строк и не больше MAX_READ_BYTES."""
    lines = content.splitlines()
    if offset > len(lines):
        raise ValueError(f"В файле {len(lines)} строк — offset больше")
    taken, used = [], 0
    for line in lines[offset - 1:offset - 1 + limit]:
        used += len(line.encode("utf-8")) + 1
        if taken and used > MAX_READ_BYTES:
            break
        taken.append(line)
    end = offset + len(taken) - 1
    tail = f"\n\n(дальше — read_file с offset {end + 1})" if end < len(lines) else ""
    return f"{relative}, строки {offset}–{end} из {len(lines)}:\n" + "\n".join(taken) + tail


# ---------- изменения ----------

def diff(before: str, after: str, name: str) -> str:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{name}", f"b/{name}", lineterm=""))
    if len(lines) > DIFF_LINES:
        lines = [*lines[:DIFF_LINES], f"… ещё {len(lines) - DIFF_LINES} строк diff"]
    return "\n".join(lines)


def _write_target(args: dict, ctx: ToolContext) -> tuple[Path, str, str]:
    """Куда пишем, что было и что станет: общее для карточки и для самой записи."""
    content = args["content"]
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"Содержимое больше {MAX_WRITE_BYTES // 1024} КБ")
    target = inside(ctx.project, args.get("path"), must_exist=False)
    if target == ctx.project.resolve() or target.is_dir():
        raise ValueError("По этому пути находится папка")
    before = _text(target) if target.is_file() else ""
    return target, before, content


def write_preview(args: dict, ctx: ToolContext) -> dict:
    target, before, after = _write_target(args, ctx)
    name = relative_to(ctx.project, target)
    action = "Перезаписать" if target.exists() else "Создать"
    return {"title": f"{action} файл {name}", "diff": diff(before, after, name)}


def write_file(args: dict, ctx: ToolContext) -> ToolResult:
    target, _, content = _write_target(args, ctx)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")
    name = relative_to(ctx.project, target)
    return ToolResult(f"Файл {name} {'перезаписан' if existed else 'создан'}", meta={"path": name})


def _edited(args: dict, ctx: ToolContext) -> tuple[Path, str, str]:
    target = inside(ctx.project, args.get("path"))
    if not target.is_file():
        raise ValueError("Это не файл")
    content = _text(target)
    # Модель пишет переводы строк как \n; в файле с CRLF ищем и вставляем в его формате.
    newline = "\r\n" if "\r\n" in content else "\n"
    old, new = (args[key].replace("\r\n", "\n").replace("\n", newline) for key in ("old_text", "new_text"))
    if not old:
        raise ValueError("old_text не должен быть пустым")
    count = content.count(old)
    if count == 0:
        raise ValueError("Фрагмент old_text не найден в файле; сверьтесь с read_file")
    if count > 1:
        raise ValueError(f"Фрагмент old_text встречается {count} раз(а); добавьте соседние строки")
    after = content.replace(old, new, 1)
    if len(after.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"После правки файл больше {MAX_WRITE_BYTES // 1024} КБ")
    return target, content, after


def edit_preview(args: dict, ctx: ToolContext) -> dict:
    target, before, after = _edited(args, ctx)
    name = relative_to(ctx.project, target)
    return {"title": f"Изменить файл {name}", "diff": diff(before, after, name)}


def edit_file(args: dict, ctx: ToolContext) -> ToolResult:
    target, _, after = _edited(args, ctx)
    target.write_text(after, encoding="utf-8", newline="")
    name = relative_to(ctx.project, target)
    return ToolResult(f"Файл {name} изменён", meta={"path": name})


def _deletable(args: dict, ctx: ToolContext) -> Path:
    target = inside(ctx.project, args.get("path"))
    if target == ctx.project.resolve():
        raise ValueError("Папку проекта целиком удалить нельзя")
    return target


def delete_preview(args: dict, ctx: ToolContext) -> dict:
    target = _deletable(args, ctx)
    name = relative_to(ctx.project, target)
    if target.is_dir():
        count = sum(len(files) for _, _, files in os.walk(target))
        return {"title": f"Удалить папку {name}", "text": f"Вместе со всем содержимым: файлов — {count}."}
    return {"title": f"Удалить файл {name}"}


def delete_path(args: dict, ctx: ToolContext) -> ToolResult:
    target = _deletable(args, ctx)
    name = relative_to(ctx.project, target)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return ToolResult(f"Удалено: {name}")


def _move(args: dict, ctx: ToolContext) -> tuple[Path, Path]:
    source = inside(ctx.project, args.get("path"))
    target = inside(ctx.project, args.get("new_path"), must_exist=False)
    if source == ctx.project.resolve():
        raise ValueError("Папку проекта переместить нельзя")
    if target.exists():
        raise ValueError(f"Уже существует: {relative_to(ctx.project, target)}")
    return source, target


def move_preview(args: dict, ctx: ToolContext) -> dict:
    source, target = _move(args, ctx)
    return {"title": f"Переместить {relative_to(ctx.project, source)} → {relative_to(ctx.project, target)}"}


def move_path(args: dict, ctx: ToolContext) -> ToolResult:
    source, target = _move(args, ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    return ToolResult(f"Перемещено: {relative_to(ctx.project, source)} → {relative_to(ctx.project, target)}")


def _new_dir(args: dict, ctx: ToolContext) -> Path:
    target = inside(ctx.project, args.get("path"), must_exist=False)
    if target.exists():
        raise ValueError(f"Уже существует: {relative_to(ctx.project, target)}")
    return target


def make_dir(args: dict, ctx: ToolContext) -> ToolResult:
    target = _new_dir(args, ctx)
    target.mkdir(parents=True)
    return ToolResult(f"Папка {relative_to(ctx.project, target)} создана")


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


PATH = {"type": "string", "description": "Путь относительно папки проекта"}

TOOLS = [
    Tool("list_dir", "Показать содержимое папки проекта деревом; служебные папки (.git, .venv, node_modules) пропускаются.",
         _schema({"path": {"type": "string", "description": "Папка относительно проекта; корень — ."},
                  "depth": {"type": "integer", "description": "Глубина дерева, по умолчанию 1"}}, []),
         list_dir),
    Tool("read_file", "Прочитать текстовый файл проекта. Большой файл (больше 64 КБ) целиком не отдаётся: без offset "
         "придёт его оглавление с номерами строк, затем читай нужные места кусками — offset и limit.",
         _schema({"path": PATH,
                  "offset": {"type": "integer", "description": "С какой строки читать (с 1)"},
                  "limit": {"type": "integer", "description": f"Сколько строк, по умолчанию {DEFAULT_READ_LINES}"}},
                 ["path"]),
         read_file),
    Tool("write_file", "Создать или полностью перезаписать текстовый файл проекта. Пользователь подтверждает запись.",
         _schema({"path": PATH, "content": {"type": "string", "description": "Полное новое содержимое файла"}},
                 ["path", "content"]),
         write_file, GROUP, write_preview),
    Tool("edit_file", "Заменить фрагмент текста в файле проекта. old_text должен встречаться в файле ровно один раз — "
         "добавьте соседние строки, чтобы фрагмент стал уникальным. Пользователь подтверждает правку.",
         _schema({"path": PATH, "old_text": {"type": "string", "description": "Точный фрагмент, который заменить"},
                  "new_text": {"type": "string", "description": "Новый текст; пустая строка удаляет фрагмент"}},
                 ["path", "old_text", "new_text"]),
         edit_file, GROUP, edit_preview),
    Tool("delete_path", "Удалить файл или папку проекта. Пользователь подтверждает удаление.",
         _schema({"path": PATH}, ["path"]), delete_path, GROUP, delete_preview),
    Tool("move_path", "Переместить или переименовать файл или папку проекта. Пользователь подтверждает.",
         _schema({"path": PATH, "new_path": {"type": "string", "description": "Новый путь относительно проекта"}},
                 ["path", "new_path"]),
         move_path, GROUP, move_preview),
    Tool("make_dir", "Создать папку в проекте. Пользователь подтверждает.",
         _schema({"path": PATH}, ["path"]), make_dir, GROUP,
         lambda args, ctx: {"title": f"Создать папку {relative_to(ctx.project, _new_dir(args, ctx))}"}),
]
