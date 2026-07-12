"""Regression tests: dashboard starts from any cwd without npm when dist exists.

Issue: a manually launched ``hermes dashboard --host 0.0.0.0 --insecure``
worked from the repository root (because npm was in the shell PATH and the
web UI could be rebuilt), but the same command failed from ``/`` with a
minimal launchd-style environment with:

    Web UI frontend not built and npm is not available.

Root cause: ``cmd_dashboard`` unconditionally called ``_build_web_ui``,
which compares source mtimes against the dist and triggers a rebuild
whenever a source/meta file is newer. The rebuild is fine in a developer
shell, but is impossible under launchd/systemd/supervisors that lack npm.

Fix: when the package-relative dist ``hermes_cli/web_dist/index.html``
already exists, use it automatically. Only rebuild when explicitly
requested with ``--rebuild`` (or when no dist exists at all). The dist
path is always derived from ``PROJECT_ROOT`` (package-relative), never from
``os.getcwd()``, so the dashboard starts from any working directory.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.main import PROJECT_ROOT, _dashboard_web_dist_dir, _web_ui_build_needed


@pytest.fixture()
def main_mod():
    import hermes_cli.main as main
    return main


def _args(**over):
    base = {
        "host": "127.0.0.1",
        "port": 0,
        "no_open": True,
        "open_profile": None,
        "skip_build": False,
        "rebuild": False,
        "headless_backend": False,
        "tui": False,
    }
    base.update(over)
    return types.SimpleNamespace(**base)


def _wire_common(main_mod, monkeypatch):
    """Mock heavy dependencies so cmd_dashboard can be exercised in isolation."""
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "default"
    )
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_quietly", lambda: None)
    monkeypatch.setitem(sys.modules, "fastapi", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "hermes_logging",
        types.SimpleNamespace(setup_logging=lambda **_k: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: None),
    )
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.start_background_mcp_discovery",
        lambda **_k: None,
    )


def test_dashboard_web_dist_dir_is_package_relative():
    """The dist directory is always under the Hermes package root, never
    derived from the current working directory."""
    original_cwd = os.getcwd()
    try:
        os.chdir("/tmp")
        dist_dir = _dashboard_web_dist_dir()
        assert dist_dir == PROJECT_ROOT / "hermes_cli" / "web_dist"
        assert dist_dir.is_absolute()
    finally:
        os.chdir(original_cwd)


def test_web_ui_build_needed_is_independent_of_cwd():
    """The staleness check resolves paths against PROJECT_ROOT, not cwd."""
    original_cwd = os.getcwd()
    try:
        os.chdir("/")
        # The result should be the same whether run from / or from PROJECT_ROOT.
        result_from_root = _web_ui_build_needed(PROJECT_ROOT / "web")
    finally:
        os.chdir(original_cwd)

    result_from_project = _web_ui_build_needed(PROJECT_ROOT / "web")
    assert result_from_root == result_from_project


@pytest.mark.skipif(
    not (PROJECT_ROOT / "hermes_cli" / "web_dist" / "index.html").exists(),
    reason="pre-built web dist not present; cannot test no-npm startup",
)
def test_dashboard_uses_existing_dist_without_rebuild(main_mod, monkeypatch, capsys):
    """When the pre-built dist exists, cmd_dashboard does not invoke npm and
    sets HERMES_WEB_DIST to the absolute package-relative dist path."""
    _wire_common(main_mod, monkeypatch)
    monkeypatch.delenv("HERMES_WEB_DIST", raising=False)

    started = []
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.web_server",
        types.SimpleNamespace(start_server=lambda **k: started.append(k)),
    )
    builds = []
    monkeypatch.setattr(
        main_mod, "_build_web_ui", lambda *a, **k: builds.append((a, k)) or True
    )

    # Simulate a launchd-style minimal PATH with no npm.
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    original_cwd = os.getcwd()
    try:
        os.chdir("/")
        main_mod.cmd_dashboard(_args())
    finally:
        os.chdir(original_cwd)

    assert len(started) == 1
    assert builds == []  # no npm build attempted
    out = capsys.readouterr().out
    assert "Using pre-built web dist" in out
    assert os.environ["HERMES_WEB_DIST"] == str(PROJECT_ROOT / "hermes_cli" / "web_dist")


def test_dashboard_rebuilds_when_explicitly_requested(main_mod, monkeypatch, capsys):
    """When --rebuild is passed, cmd_dashboard attempts a fresh build even if a
    dist already exists."""
    _wire_common(main_mod, monkeypatch)
    monkeypatch.delenv("HERMES_WEB_DIST", raising=False)

    started = []
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.web_server",
        types.SimpleNamespace(start_server=lambda **k: started.append(k)),
    )
    builds = []
    monkeypatch.setattr(
        main_mod, "_build_web_ui", lambda *a, **k: builds.append((a, k)) or True
    )

    main_mod.cmd_dashboard(_args(rebuild=True))

    assert len(started) == 1
    assert len(builds) == 1
    assert builds[0][1].get("fatal") is True
    assert builds[0][1].get("dist_dir") == PROJECT_ROOT / "hermes_cli" / "web_dist"


def test_dashboard_tries_build_when_dist_missing(main_mod, monkeypatch, tmp_path):
    """If the package-relative dist is missing, the default behavior still tries to
    build once so first-time users get a usable dashboard."""
    _wire_common(main_mod, monkeypatch)
    monkeypatch.delenv("HERMES_WEB_DIST", raising=False)

    # Point the web dist at a non-existent temp directory so the existing dist
    # is not found.
    fake_dist = tmp_path / "nonexistent_dist"
    monkeypatch.setattr(main_mod, "_dashboard_web_dist_dir", lambda: fake_dist)
    monkeypatch.setattr(
        main_mod, "_build_web_ui", lambda *a, **k: True
    )  # pretend build succeeds
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.web_server",
        types.SimpleNamespace(start_server=lambda **k: None),
    )

    main_mod.cmd_dashboard(_args())

    assert os.environ["HERMES_WEB_DIST"] == str(fake_dist)


def test_serve_headless_still_skips_build_and_spa(main_mod, monkeypatch):
    """Regression: the `serve` headless subcommand must not start building or
    serving the SPA."""
    _wire_common(main_mod, monkeypatch)
    monkeypatch.delenv("HERMES_WEB_DIST", raising=False)
    monkeypatch.setenv("HERMES_SERVE_HEADLESS", "0")

    builds = []
    monkeypatch.setattr(
        main_mod, "_build_web_ui", lambda *a, **k: builds.append((a, k)) or True
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.web_server",
        types.SimpleNamespace(start_server=lambda **k: None),
    )

    main_mod.cmd_dashboard(_args(headless_backend=True, skip_build=False, rebuild=False))

    assert os.environ["HERMES_SERVE_HEADLESS"] == "1"
    assert builds == []
