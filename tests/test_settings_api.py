"""Настройки: сохранение и проверка значений."""

import json

from fastapi.testclient import TestClient

from meepo.app import create_app
from meepo.llm.fake import FakeLLM
from meepo.settings import Env


def test_settings_roundtrip_and_validation(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="env-model"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/settings").json()["active_model"] == "env-model"
        saved = client.put("/api/settings", json={"model": "m", "guards": {"plan": False}, "max_tool_rounds": 10}).json()
        assert saved["active_model"] == "m" and saved["guards"]["plan"] is False and saved["guards"]["language"] is True
        assert client.put("/api/settings", json={"max_tool_rounds": 0}).status_code == 400
        assert client.put("/api/settings", json={"guards": {"magic": True}}).status_code == 400
        assert client.put("/api/settings", json={"llm_base_url": "ftp://x"}).status_code == 400
        assert client.put("/api/settings", json={"unknown": 1}).status_code == 400
        assert client.get("/api/models").json() == ["fake"]


def test_model_options_validation_and_info(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="m"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        options = {"m": {"thinking": False, "temperature": 0.3, "max_tokens": 1024}}
        assert client.put("/api/settings", json={"model_options": options}).json()["model_options"] == options
        for bad in ({"m": {"temperature": 3}}, {"m": {"max_tokens": 1.5}}, {"m": {"thinking": "no"}},
                    {"m": {"magic": 1}}, {"m": []}):
            assert client.put("/api/settings", json={"model_options": bad}).status_code == 400
        info = client.get("/api/models/info", params={"model": "m"}).json()
        assert info["thinking"] is True and info["defaults"] == {"temperature": 1.0}
        assert "temperature" in [item["name"] for item in info["parameters"]]
        assert all("ollama" not in item and item["hint"] for item in info["parameters"])


def test_model_connection_key_and_reset(tmp_path):
    app = create_app(Env(data_path=tmp_path, llm_base_url="http://env/v1", default_model="env-model"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        saved = client.put("/api/settings", json={
            "llm_kind": "openai", "llm_base_url": "https://models.example/v1",
            "llm_api_key": "secret", "model": "model-a",
        }).json()
        assert saved["configured"] is True and saved["llm_api_key_set"] is True
        assert "llm_api_key" not in saved
        assert json.loads((tmp_path / "settings.json").read_text())["llm_api_key"] == "secret"

        reset = client.put("/api/settings", json={
            "llm_kind": "disabled", "llm_base_url": "", "llm_api_key": "", "model": "",
        }).json()
        assert reset["configured"] is False
        assert reset["active_base_url"] == "" and reset["active_model"] == ""


def test_guards_list_with_source(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="m"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.put("/api/settings", json={"guards": {"language": False, "promise": True, "plan": True, "retry": True}})
        guards = {item["name"]: item for item in client.get("/api/guards").json()}
    assert set(guards) == {"language", "promise", "plan", "retry"}
    language = guards["language"]
    assert language["title"] == "Язык" and language["enabled"] is False and guards["plan"]["enabled"] is True
    assert language["file"] == "meepo/agent/guards/language.py"
    assert "def check(state)" in language["source"] and language["doc"].startswith("Guard «язык»")


def test_models_probe_other_server(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="m"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/models", params={"base_url": "ftp://x"}).status_code == 400
        # Порт 9 закрыт: сервер модели недоступен — понятная ошибка, а не падение.
        response = client.get("/api/models", params={"base_url": "http://127.0.0.1:9/v1"})
        assert response.status_code == 503 and "Не удалось подключиться" in response.json()["detail"]


def test_tools_by_theme_with_browser_window(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="m"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.put("/api/settings", json={"browser_headed": False})
        themes = {theme["id"]: theme for theme in client.get("/api/tools").json()}
    assert list(themes)[:4] == ["files", "search", "command", "browser"]
    files = {tool["name"]: tool for tool in themes["files"]["tools"]}
    assert files["read_file"]["group"] is None and files["write_file"]["group"] == "files_write"
    browser = themes["browser"]
    assert browser["title"] == "Браузер" and browser["settings"] == {"browser_headed": False}
    assert "open" in browser["tools"][0]["parameters"]["action"]
    assert [item["file"] for item in browser["files"]] == ["meepo/agent/tools/browser.py", "meepo/agent/tools/snapshot.js"]
    assert "settings" not in themes["files"]
