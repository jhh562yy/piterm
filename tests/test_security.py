"""Security policy tests (no QGIS required)."""

from __future__ import annotations

import os
import tempfile
import types

import pytest

from piterm_qgis_ai.errors import CommandError, ErrorCode
from piterm_qgis_ai.security import DictSettings, SecurityManager


def make_spec(name, risk):
    return types.SimpleNamespace(name=name, risk=risk)


def make_manager(**overrides):
    values = {
        "mode.read_only": False,
        "mode.allow_execute": False,
        "mode.confirm_destructive": False,
        "dangerous.allow_full_python": False,
        "paths.allow": ["*"],
        "paths.deny": [],
        "auth.enabled": False,
        "auth.token": "",
    }
    values.update(overrides)
    return SecurityManager(DictSettings(values))


def test_token_disabled_accepts_anything():
    manager = make_manager()
    assert manager.token_ok(None)
    assert manager.token_ok("whatever")


def test_token_constant_time_compare():
    manager = make_manager(**{"auth.enabled": True, "auth.token": "secret"})
    assert manager.token_ok("secret")
    assert not manager.token_ok("Secret")
    assert not manager.token_ok("")
    assert not manager.token_ok(None)


def test_read_only_blocks_non_read():
    manager = make_manager(**{"mode.read_only": True})
    manager.check_command(make_spec("layers.list", "read"), {})
    with pytest.raises(CommandError) as exc:
        manager.check_command(make_spec("layers.remove", "destructive"), {})
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_execute_scope_gate():
    manager = make_manager()
    with pytest.raises(CommandError) as exc:
        manager.check_command(make_spec("python.exec", "execute"), {})
    assert exc.value.code == ErrorCode.FORBIDDEN
    manager2 = make_manager(**{"mode.allow_execute": True})
    manager2.check_command(make_spec("python.exec", "execute"), {})


def test_full_python_needs_both_switches():
    manager = make_manager(**{"mode.allow_execute": True,
                              "dangerous.allow_full_python": False})
    with pytest.raises(CommandError) as exc:
        manager.check_command(make_spec("python.exec_full", "execute"), {})
    assert exc.value.code == ErrorCode.FORBIDDEN
    manager2 = make_manager(**{"mode.allow_execute": True,
                               "dangerous.allow_full_python": True})
    manager2.check_command(make_spec("python.exec_full", "execute"), {})


def test_confirm_flow():
    manager = make_manager(**{"mode.confirm_destructive": True})
    with pytest.raises(CommandError) as exc:
        manager.check_command(make_spec("features.delete", "destructive"), {})
    assert exc.value.code == ErrorCode.CONFIRM_REQUIRED
    manager.check_command(make_spec("features.delete", "destructive"),
                          {"__confirm": True})


def test_path_allow_and_deny():
    with tempfile.TemporaryDirectory() as tmp:
        allowed = os.path.join(tmp, "work")
        os.makedirs(allowed, exist_ok=True)
        denied = os.path.join(allowed, "secret")
        os.makedirs(denied, exist_ok=True)
        manager = make_manager(**{"paths.allow": [allowed],
                                  "paths.deny": [denied]})
        manager.check_path(os.path.join(allowed, "a.txt"))
        with pytest.raises(CommandError) as exc:
            manager.check_path(os.path.join(denied, "a.txt"))
        assert exc.value.code == ErrorCode.PATH_DENIED
        with pytest.raises(CommandError) as exc:
            manager.check_path(os.path.join(tmp, "outside.txt"))
        assert exc.value.code == ErrorCode.PATH_DENIED


def test_rate_limiter():
    from piterm_qgis_ai.security import RateLimiter
    limiter = RateLimiter(0)
    assert limiter.allow()
    limited = RateLimiter(1)
    assert limited.allow()
    assert not limited.allow()
