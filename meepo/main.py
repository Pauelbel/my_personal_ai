"""Запуск сервера: `python -m meepo.main`."""

import os
import sys

import uvicorn

from meepo.app import create_app
from meepo.settings import Env


def run() -> None:
    env = Env.load()
    app = create_app(env)
    # Открытые потоки хода не держат остановку дольше нескольких секунд — иначе перезапуск ждал бы их конца.
    server = uvicorn.Server(uvicorn.Config(app, host=env.host, port=env.port, timeout_graceful_shutdown=3))
    app.state.server = server
    server.run()
    # Кнопка «Перезапустить» в шапке: сервер остановился штатно — тот же процесс запускается заново со свежим кодом.
    if app.state.restart:
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


if __name__ == "__main__":
    run()
