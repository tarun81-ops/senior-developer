"""Phase 5, P5.1: what a project is, and which runs may deploy (D35, D36). Offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.deploy import deploy_refusal, detect, repo_name


def make(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


VITE_PACKAGE = json.dumps(
    {
        "name": "calc",
        "scripts": {"build": "vite build"},
        "devDependencies": {"vite": "^8.0.0"},
    }
)
FASTAPI_APP = "from fastapi import FastAPI\n\napp = FastAPI()\n"


# -- static sites -> GitHub Pages ------------------------------------------------
def test_a_plain_html_site_goes_to_pages_as_is(tmp_path: Path) -> None:
    found = detect(make(tmp_path, {"index.html": "<h1>hi</h1>", "style.css": "h1{}"}))
    assert (found.kind, found.target, found.framework) == ("static", "github-pages", "html")
    assert found.build_command is None and found.publish_dir == "."


def test_a_vite_app_is_built_by_the_pages_workflow(tmp_path: Path) -> None:
    found = detect(
        make(tmp_path, {"package.json": VITE_PACKAGE, "index.html": "", "src/main.js": ""})
    )
    assert (found.kind, found.target, found.framework) == ("static", "github-pages", "vite")
    assert found.publish_dir == "dist"
    # relative base: the built site works under /<repo>/ whatever it is called
    assert "--base=./" in found.build_command


# -- Python backends -> Render ---------------------------------------------------
def test_a_fastapi_backend_goes_to_render(tmp_path: Path) -> None:
    found = detect(
        make(
            tmp_path,
            {
                "requirements.txt": "fastapi>=0.110\n",
                "app/main.py": FASTAPI_APP,
                "tests/test_main.py": "from app.main import app\n",
            },
        )
    )
    assert (found.kind, found.target, found.framework) == ("python", "render", "fastapi")
    assert found.start_command == "uvicorn app.main:app --host 0.0.0.0 --port $PORT"
    assert found.build_command == "pip install -r requirements.txt && pip install uvicorn"


def test_a_flask_backend_uses_gunicorn_and_its_own_app_name(tmp_path: Path) -> None:
    found = detect(
        make(
            tmp_path,
            {
                "pyproject.toml": '[project]\ndependencies = ["flask"]\n',
                "server.py": "from flask import Flask\napi = Flask(__name__)\n",
            },
        )
    )
    assert found.framework == "flask"
    assert found.start_command == "gunicorn server:api --bind 0.0.0.0:$PORT"
    assert found.build_command == "pip install . && pip install gunicorn"


def test_apps_in_tests_and_tool_folders_are_ignored(tmp_path: Path) -> None:
    found = detect(
        make(
            tmp_path,
            {
                "requirements.txt": "fastapi\n",
                "main.py": FASTAPI_APP,
                "tests/test_app.py": FASTAPI_APP,  # a test's throwaway app
                ".venv/lib/site.py": FASTAPI_APP,
                "node_modules/x/app.py": FASTAPI_APP,
            },
        )
    )
    assert found.kind == "python" and found.start_command.startswith("uvicorn main:app")


# -- refusals, each with a reason --------------------------------------------------
@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({}, "empty"),
        ({"README.md": "# notes"}, "no static site"),
        ({"notes.txt": "x", "calc.py": "def add(a, b): return a + b\n"}, "no static site"),
        (
            {"requirements.txt": "fastapi\n", "a.py": FASTAPI_APP, "b.py": FASTAPI_APP},
            "found 2 web apps",
        ),
        ({"main.py": FASTAPI_APP}, "no requirements.txt or pyproject.toml"),
        (
            {"requirements.txt": "httpx\n", "main.py": FASTAPI_APP},
            "fastapi is not in the project's requirements",
        ),
        (
            {
                "requirements.txt": "fastapi\n",
                "api/main.py": FASTAPI_APP,
                "web/package.json": "{}",
                "package.json": VITE_PACKAGE,
            },
            "full-stack",
        ),
        (
            {"requirements.txt": "fastapi\n", "main.py": FASTAPI_APP, "index.html": ""},
            "full-stack",
        ),
        (
            {"package.json": json.dumps({"dependencies": {"express": "^5"}}), "server.js": ""},
            "Node server",
        ),
    ],
)
def test_unsupported_projects_are_refused_with_a_reason(
    tmp_path: Path, files: dict[str, str], reason: str
) -> None:
    found = detect(make(tmp_path / "p", files) if files else tmp_path / "p")
    assert found.kind == "unsupported" and not found.deployable
    assert reason in found.reason, found.reason
    assert found.target is None


def test_a_broken_package_json_is_not_a_crash(tmp_path: Path) -> None:
    found = detect(make(tmp_path, {"package.json": "{not json", "index.html": ""}))
    assert found.kind == "unsupported"


def test_each_folder_has_one_stable_name() -> None:
    assert repo_name("calc") == "sda-calc"
    assert repo_name("My Calc App!") == "sda-my-calc-app"
    assert repo_name("calc") == repo_name("calc")  # same folder, same target


# -- eligibility (D35) --------------------------------------------------------------
GOOD = {
    "status": "succeeded",
    "options": {"dry_run": False, "no_apply": False, "no_run_tests": False},
    "tests": {"command": "python -m pytest -q", "ok": True, "exit_code": 0, "timed_out": False},
    "result": {"reason": "ok"},
}


def test_a_succeeded_run_with_passing_tests_may_deploy() -> None:
    assert deploy_refusal(GOOD) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "failed"}, "the run failed"),
        ({"status": "cancelled"}, "the run cancelled"),
        ({"status": "running"}, "the run running"),
        ({"status": "failed", "result": {"reason": "interrupted"}}, "interrupted"),
        ({"options": {**GOOD["options"], "dry_run": True}}, "dry run"),
        ({"options": {**GOOD["options"], "no_apply": True}}, "never written"),
        ({"options": {**GOOD["options"], "no_run_tests": True}}, "tests were skipped"),
        ({"tests": None}, "no tests ran"),
        ({"tests": {**GOOD["tests"], "ok": False, "exit_code": 1}}, "failed"),
        ({"tests": {**GOOD["tests"], "timed_out": True}}, "timed out"),
    ],
)
def test_every_other_run_is_refused_with_why(change: dict, reason: str) -> None:
    refusal = deploy_refusal({**GOOD, **change})
    assert refusal is not None and reason in refusal, refusal
