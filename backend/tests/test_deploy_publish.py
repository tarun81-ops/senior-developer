"""Phase 5, P5.2: what leaves the machine, and what stops it (D37). Offline."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.deploy.publish import MAX_FILE_BYTES, build_publish_set

# Shaped like real keys, but not real: built at runtime so this file itself
# never holds a key-shaped literal a scanner would flag.
FAKE = {
    "Google API key": "AIza" + "S" * 35,
    "OpenAI/OpenRouter key": "sk-or-v1-" + "a" * 40,
    "Groq key": "gsk_" + "b" * 40,
    "GitHub token": "ghp_" + "c" * 36,
    "Render API key": "rnd_" + "d" * 24,
    "AWS access key": "AKIA" + "E" * 16,
    "Private key block": "-----BEGIN RSA PRIVATE KEY-----",
}


def make(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def test_a_clean_project_publishes_its_source_only(tmp_path: Path) -> None:
    result = build_publish_set(
        make(
            tmp_path,
            {
                "index.html": "<h1>hi</h1>",
                "src/app.js": "console.log(1)",
                ".env.example": "API_KEY=put-yours-here\n",
                "node_modules/x/index.js": "x",
                ".venv/pyvenv.cfg": "x",
                "__pycache__/a.pyc": "x",
                ".git/HEAD": "x",
            },
        )
    )
    assert not result.blocked
    assert [f for f, _ in result.files] == [".env.example", "index.html", "src/app.js"]
    assert result.total_bytes == sum(size for _, size in result.files)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".env.production",
        "prod.env",
        "server.pem",
        "tls.key",
        "id_rsa",
        "id_ed25519.pub",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "service-account-prod.json",
    ],
)
def test_secret_files_are_excluded_by_name(tmp_path: Path, name: str) -> None:
    result = build_publish_set(make(tmp_path, {"index.html": "", name: "whatever"}))
    assert name not in [f for f, _ in result.files]
    assert (name, "a secrets file by its name") in result.excluded


@pytest.mark.parametrize(("kind", "value"), list(FAKE.items()))
def test_a_key_shaped_string_blocks_the_deploy_without_echoing_it(
    tmp_path: Path, kind: str, value: str
) -> None:
    result = build_publish_set(
        make(
            tmp_path,
            {
                "index.html": "<h1>hi</h1>",
                "src/config.js": f"// setup\nconst key = '{value}';\n",
            },
        )
    )
    assert result.blocked
    assert [(f.path, f.line, f.kind) for f in result.findings] == [("src/config.js", 2, kind)]
    # the report can be shown and logged: the value is nowhere in it
    assert value not in repr(result.findings)


def test_ordinary_code_does_not_trip_the_scan(tmp_path: Path) -> None:
    result = build_publish_set(
        make(
            tmp_path,
            {
                "main.py": (
                    "import os\nAPI_KEY = os.environ['GEMINI_API_KEY']\n"
                    "task = 'sk-short'\nsecret_key = 'change-me'\nprint('AIza')\n"
                ),
                "README.md": "Set GROQ_API_KEY and GITHUB_TOKEN in your environment.\n",
            },
        )
    )
    assert not result.blocked, result.findings


def test_binary_and_oversized_files(tmp_path: Path) -> None:
    result = build_publish_set(
        make(
            tmp_path,
            {
                "index.html": "",
                "logo.png": b"\x89PNG\x00\x00"
                + FAKE["Google API key"].encode(),  # binary: not scanned
                "dump.sql": "x" * (MAX_FILE_BYTES + 1),
            },
        )
    )
    assert not result.blocked
    assert "logo.png" in [f for f, _ in result.files]
    assert ("dump.sql", "larger than 1024 KB") in result.excluded


def test_empty_or_missing_projects_are_blocked(tmp_path: Path) -> None:
    assert build_publish_set(tmp_path / "missing").problem == "the project folder does not exist"
    only_secrets = build_publish_set(make(tmp_path / "p", {".env": "X=1"}))
    assert only_secrets.blocked and only_secrets.problem == "no files would be published"


def test_a_link_pointing_outside_the_project_is_excluded(tmp_path: Path) -> None:
    project = make(tmp_path / "p", {"index.html": ""})
    secret = tmp_path / "outside.txt"
    secret.write_text(FAKE["GitHub token"], encoding="utf-8")
    try:
        os.symlink(secret, project / "leak.txt")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin on Windows")
    result = build_publish_set(project)
    assert ("leak.txt", "points outside the project") in result.excluded
    assert not result.findings  # it was never read


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_a_junction_pointing_outside_the_project_is_never_published(tmp_path: Path) -> None:
    """Junctions need no special rights on Windows, so they are the real risk there."""
    import _winapi

    project = make(tmp_path / "p", {"index.html": ""})
    outside = make(tmp_path / "outside", {"secrets.txt": FAKE["Groq key"]})
    _winapi.CreateJunction(str(outside), str(project / "escape"))
    result = build_publish_set(project)
    published = [f for f, _ in result.files]
    assert not any(f.startswith("escape/") for f in published), published
    assert not result.findings  # the outside file was never read
