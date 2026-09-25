"""What a generated project is, and where it may be deployed (D35, D36).

Pure filesystem inspection: nothing here talks to the network. The target
follows what the app needs (D11 amendment):

* a **static site**, meaning a plain ``index.html`` or a Vite build, goes to
  **GitHub Pages**;
* a **Python backend**, meaning exactly one FastAPI or Flask ``app``, goes to
  **Render**.

Anything else is **refused with a reason** rather than guessed: full-stack
projects, Node servers, several apps, nothing recognizable. Those are later
steps (D36).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# the leaf module: importing the backend.core.workspace package first would
# trip its existing import cycle with backend.core.orchestrator
from backend.core.workspace.sandbox import slugify

#: Folders that are never part of what gets inspected or published.
IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

#: ``app = FastAPI(...)`` / ``app = Flask(__name__)`` at the top level of a module.
_APP = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<kind>FastAPI|Flask)\(", re.MULTILINE)
_NODE_SERVERS = ("express", "fastify", "koa", "@hapi/hapi", "next", "@nestjs/core")


@dataclass(frozen=True)
class Detection:
    """The outcome for one project folder. ``kind == "unsupported"`` is a refusal."""

    kind: str  # "static" | "python" | "unsupported"
    reason: str
    target: str | None = None  # "github-pages" | "render"
    framework: str | None = None  # "html" | "vite" | "fastapi" | "flask"
    #: static: the command the Pages workflow runs, and what it publishes
    build_command: str | None = None
    publish_dir: str | None = None
    #: python: what Render runs
    start_command: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def deployable(self) -> bool:
        return self.kind != "unsupported"


def repo_name(project: str) -> str:
    """The stable name for a workspace folder: its repo, its Render service (D35)."""
    return f"sda-{slugify(project)}"


def detect(project_dir: Path) -> Detection:
    """Classify ``workspace/<project>/``. Never raises for a bad project; refuses."""
    root = Path(project_dir)
    if not root.is_dir() or not any(_files(root)):
        return _refuse("the project folder is empty, so there is nothing to deploy")

    package = _read_package_json(root)
    static = _static(root, package)
    python = _python(root)
    node_server = package is not None and _is_node_server(package)

    if python is not None and python.kind == "unsupported":
        return python
    if python is not None and (static is not None or node_server):
        return _refuse(
            "this is a full-stack project (a frontend and a Python backend together); "
            "deploying those is not supported yet"
        )
    if python is not None:
        return python
    if node_server:
        return _refuse(
            "this is a Node server; only static sites and Python backends deploy for now"
        )
    if static is not None:
        return static
    return _refuse(
        "no static site (index.html or a Vite build) and no FastAPI/Flask app were found"
    )


# -- static --------------------------------------------------------------------
def _static(root: Path, package: dict | None) -> Detection | None:
    if package is not None:
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        scripts = package.get("scripts", {})
        if "vite" in deps and "build" in scripts:
            return Detection(
                kind="static",
                target="github-pages",
                framework="vite",
                reason="a Vite app (package.json with vite and a build script)",
                # Pages serves the site under /<repo>/. A relative base makes the
                # built asset URLs work there whatever the repo is called.
                build_command="(npm ci || npm install) && npm run build -- --base=./",
                publish_dir="dist",
            )
        return None
    if (root / "index.html").is_file():
        return Detection(
            kind="static",
            target="github-pages",
            framework="html",
            reason="a plain static site (index.html, no build step)",
            publish_dir=".",
        )
    return None


# -- python --------------------------------------------------------------------
def _python(root: Path) -> Detection | None:
    requirements = _requirements_text(root)
    apps = [
        (path, match["name"], match["kind"])
        for path in _files(root, suffix=".py")
        if not _is_test_file(path, root)
        for match in _APP.finditer(_read(path))
    ]
    if not apps:
        return None
    if len(apps) > 1:
        where = ", ".join(f"{p.relative_to(root).as_posix()}:{n}" for p, n, _ in apps)
        return _refuse(f"found {len(apps)} web apps ({where}); expected exactly one")
    path, name, kind = apps[0]
    if not requirements:
        return _refuse(
            f"{kind} app found, but no requirements.txt or pyproject.toml lists its dependencies"
        )
    if kind.lower() not in requirements.lower():
        return _refuse(f"{kind} app found, but {kind.lower()} is not in the project's requirements")
    module = ".".join(path.relative_to(root).with_suffix("").parts)
    install = (
        "pip install -r requirements.txt"
        if (root / "requirements.txt").is_file()
        else "pip install ."
    )
    if kind == "FastAPI":
        server, start = "uvicorn", f"uvicorn {module}:{name} --host 0.0.0.0 --port $PORT"
    else:
        server, start = "gunicorn", f"gunicorn {module}:{name} --bind 0.0.0.0:$PORT"
    return Detection(
        kind="python",
        target="render",
        framework=kind.lower(),
        reason=f"a {kind} backend ({module}:{name})",
        # the server is installed explicitly: generated requirements often omit it
        build_command=f"{install} && pip install {server}",
        start_command=start,
    )


def _requirements_text(root: Path) -> str:
    return "\n".join(
        _read(root / name)
        for name in ("requirements.txt", "pyproject.toml")
        if (root / name).is_file()
    )


def _is_test_file(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return (
        "tests" in parts
        or "test" in parts
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


# -- helpers -------------------------------------------------------------------
def _is_node_server(package: dict) -> bool:
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    return any(name in deps for name in _NODE_SERVERS)


def _read_package_json(root: Path) -> dict | None:
    path = root / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(_read(path))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _files(root: Path, *, suffix: str | None = None):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).parts
        if any(part in IGNORED_DIRS for part in rel[:-1]) or not path.is_file():
            continue
        if suffix is None or path.suffix == suffix:
            yield path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _refuse(reason: str) -> Detection:
    return Detection(kind="unsupported", reason=reason)
