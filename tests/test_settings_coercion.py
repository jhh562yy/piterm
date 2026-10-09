"""Settings coercion pins (no QGIS required).

QgsSettings round-trips Python booleans as the *strings* ``"true"``/``"false"``,
so ``bool(settings.value(key, False))`` is True for a stored ``False``. That made
``mode.allow_execute`` / ``mode.read_only`` / ``mode.confirm_destructive``
impossible to switch off (defect D20). Every settings read now goes through
``coerce_value``.
"""

from __future__ import annotations

import pytest

from piterm_qgis_ai.security import (DictSettings, SecurityConfig, SecurityManager,
                                     _as_bool, coerce_value)
from piterm_qgis_ai.errors import CommandError, ErrorCode


# ---------------------------------------------------------------------------
# coerce_value / _as_bool
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("False", False), ("FALSE", False), (" false ", False),
    ("0", False), ("no", False), ("off", False), ("", False), ("disabled", False),
    ("none", False), ("null", False),
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("enabled", True), ("2", True), ("anything", True),
    (False, False), (True, True), (0, False), (1, True), (None, False),
])
def test_as_bool_parses_qgssettings_strings(raw, expected):
    assert _as_bool(raw) is expected


def test_coerce_bool_key_stored_false_stays_false():
    # the exact round-trip QgsSettings performs
    assert coerce_value("mode.allow_execute", "false") is False
    assert coerce_value("mode.read_only", "false") is False
    assert coerce_value("mode.confirm_destructive", "false") is False
    assert coerce_value("dangerous.allow_full_python", "false") is False
    assert coerce_value("mode.allow_execute", "true") is True


def test_coerce_ports_and_numbers():
    assert coerce_value("mode.frame_port", "9887") == 9887
    assert coerce_value("mode.http_port", 9889) == 9889
    assert coerce_value("mode.frame_port", "") is None
    assert coerce_value("mode.frame_port", None) is None
    assert coerce_value("mode.frame_port", "not-a-port") is None
    assert coerce_value("limits.max_clients", "32") == 32
    assert coerce_value("limits.max_clients", "junk") == 16
    assert coerce_value("limits.rate_per_sec", "2.5") == 2.5
    assert coerce_value("limits.rate_per_sec", "junk") == 0.0


def test_coerce_strings_and_lists():
    assert coerce_value("mode.host", None) == ""
    assert coerce_value("mode.host", "0.0.0.0") == "0.0.0.0"
    assert coerce_value("paths.allow", "a;b ") == ["a", "b"]
    assert coerce_value("paths.allow", ["a", 2]) == ["a", "2"]
    assert coerce_value("paths.allow", "") == []
    assert coerce_value("paths.deny", '["x", "y"]') == ["x", "y"]


def test_coerce_unknown_key_is_passthrough():
    assert coerce_value("nope.nope", "whatever") == "whatever"


# ---------------------------------------------------------------------------
# SecurityConfig (policy actually honours "false")
# ---------------------------------------------------------------------------
def config_with(**values):
    return SecurityConfig(DictSettings(values))


def test_policy_switches_off_after_string_roundtrip():
    cfg = config_with(**{
        "mode.read_only": "false",
        "mode.allow_execute": "false",
        "mode.confirm_destructive": "false",
        "dangerous.allow_full_python": "false",
    })
    assert cfg.read_only is False
    assert cfg.allow_execute is False
    assert cfg.confirm_destructive is False
    assert cfg.allow_full_python is False


def test_policy_switches_on_after_string_roundtrip():
    cfg = config_with(**{
        "mode.read_only": "true",
        "mode.allow_execute": "true",
        "mode.confirm_destructive": "true",
    })
    assert cfg.read_only is True
    assert cfg.allow_execute is True
    assert cfg.confirm_destructive is True


def test_execute_can_be_disabled_end_to_end():
    """Before the fix a stored 'false' left python.exec enabled forever."""
    values = {
        "mode.read_only": False,
        "mode.allow_execute": "false",
        "mode.confirm_destructive": False,
        "dangerous.allow_full_python": False,
        "paths.allow": ["*"],
        "paths.deny": [],
        "auth.enabled": False,
        "auth.token": "",
    }
    manager = SecurityManager(DictSettings(values))
    import types
    spec = types.SimpleNamespace(name="python.exec", risk="execute")
    with pytest.raises(CommandError) as exc:
        manager.check_command(spec, {})
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_read_only_mode_from_string_blocks_writes():
    values = {
        "mode.read_only": "true",
        "mode.allow_execute": False,
        "mode.confirm_destructive": False,
        "dangerous.allow_full_python": False,
        "paths.allow": ["*"],
        "paths.deny": [],
        "auth.enabled": False,
        "auth.token": "",
    }
    manager = SecurityManager(DictSettings(values))
    import types
    write = types.SimpleNamespace(name="layers.remove", risk="destructive")
    with pytest.raises(CommandError) as exc:
        manager.check_command(write, {})
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_audit_path_and_token_coerced():
    cfg = config_with(**{"audit.path": None, "auth.token": 12345})
    assert cfg.audit_path == ""
    assert cfg.token == "12345"
