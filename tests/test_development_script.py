from __future__ import annotations

from pathlib import Path


def test_windows_development_script_uses_safe_local_environment() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = (project_root / "scripts" / "dev.ps1").read_text(encoding="utf-8")

    assert ".env.development.example" in script
    assert '$env:CRAWLING_ENABLED = "false"' in script
    assert '$env:CODEX_ENABLED = "false"' in script
