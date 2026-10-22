"""Tests for cross platform path resolution."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenmon_pkg import paths as P


def test_default_db_path_respects_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("TOKENMON_DB", str(target))
    assert P.default_db_path() == target


def test_default_db_path_is_platform_specific(monkeypatch):
    monkeypatch.delenv("TOKENMON_DB", raising=False)
    path = P.default_db_path()
    assert path.suffix == ".db"
    assert path.is_absolute()
    if P.IS_WINDOWS:
        assert "tokenmon" in str(path).lower()
    elif P.IS_MACOS:
        assert "Application Support" in str(path)
    else:
        assert ".local" in str(path) or "tokenmon" in str(path)


def test_tool_override_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENMON_CODEX_HOME", str(tmp_path))
    resolved = P.codex_paths()
    assert resolved.roots == [tmp_path]
    assert resolved.found is True


def test_missing_tool_reports_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENMON_CODEX_HOME", str(tmp_path / "nope"))
    resolved = P.codex_paths()
    assert resolved.roots == []
    assert resolved.found is False


def test_expand_user_and_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENMON_TEST_DIR", str(tmp_path))
    assert P._expand("$TOKENMON_TEST_DIR") == tmp_path
    assert P._expand("~/x").is_absolute()


def test_resolve_all_covers_every_tool():
    resolved = P.resolve_all()
    assert set(resolved) == set(P.TOOL_ORDER)
    assert len(P.TOOL_ORDER) == 6


def test_extra_homes_detects_sessions_subdir(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENMON_CODEX_HOME", str(tmp_path))
    (tmp_path / "account_go" / "sessions" / "2026").mkdir(parents=True)
    (tmp_path / "not_a_home").mkdir()
    extras = P.codex_extra_homes()
    assert tmp_path / "account_go" in extras
    assert tmp_path / "not_a_home" not in extras


def test_ensure_parent_creates_directory(tmp_path):
    target = tmp_path / "a" / "b" / "c.db"
    assert P.ensure_parent(target) == target
    assert target.parent.is_dir()


def test_appdata_helpers_are_absolute():
    assert P._appdata("x").is_absolute()
    assert P._localappdata("x").is_absolute()
    assert P.xdg_config("x").is_absolute()
    assert P.xdg_data("x").is_absolute()
    assert P.macos_app_support("x").is_absolute()
