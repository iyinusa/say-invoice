from __future__ import annotations

from pathlib import Path

import pytest

from app import settings as settings_mod
from app.db import ensure_sqlite_dir
from app.settings import Settings


def _which(available: set[str]):  # type: ignore[no-untyped-def]
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_configured_mcp_command_wins_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_mod.shutil, "which", _which({"npx", "node"}))
    s = Settings(paypal_mcp_command="npx -y @paypal/mcp@1.8.1")
    assert s.paypal_mcp_argv() == ["npx", "-y", "@paypal/mcp@1.8.1"]


def test_docker_image_falls_back_to_the_bundled_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A local .env says npx, but the image only has node and the pre-installed server.
    bundled = tmp_path / "index.js"
    bundled.write_text("")
    monkeypatch.setattr(settings_mod, "BUNDLED_PAYPAL_MCP", bundled)
    monkeypatch.setattr(settings_mod.shutil, "which", _which({"node"}))
    s = Settings(paypal_mcp_command="npx -y @paypal/mcp@1.8.1")
    assert s.paypal_mcp_argv() == ["node", str(bundled)]


def test_sqlite_folder_is_created(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "data" / "si.db"
    ensure_sqlite_dir(f"sqlite+aiosqlite:///{db}")
    assert db.parent.is_dir()
    ensure_sqlite_dir("sqlite+aiosqlite:///:memory:")  # no-op
    ensure_sqlite_dir("postgresql+asyncpg://u:p@host/db")  # no-op
