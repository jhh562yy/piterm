"""Wire codec tests (no QGIS required)."""

from __future__ import annotations

import gzip
import json
import struct

import pytest

from piterm_qgis_ai.constants import MAX_MESSAGE_SIZE
from piterm_qgis_ai.wire import (HEADER_STRUCT, MessageDecoder, dumps,
                                 encode_frame, encode_message, encode_text,
                                 json_safe, loads)


def test_frame_roundtrip():
    payload = {"type": "system.ping", "params": {}, "id": 1}
    frame = encode_frame(payload)
    decoder = MessageDecoder()
    messages = decoder.feed(frame)
    assert len(messages) == 1
    mode, message = messages[0]
    assert mode == "frame"
    assert message == payload


def test_text_lines_roundtrip_and_mixed():
    decoder = MessageDecoder()
    first = encode_text({"type": "a"})
    second = encode_text({"type": "b"})
    messages = decoder.feed(first + second)
    assert [m[1]["type"] for m in messages] == ["a", "b"]
    assert decoder.mode == "text"


def test_feed_split_across_chunks():
    frame = encode_frame({"type": "x", "params": {"v": "y" * 1000}})
    decoder = MessageDecoder()
    out = []
    for i in range(0, len(frame), 7):
        out.extend(decoder.feed(frame[i:i + 7]))
    assert len(out) == 1
    assert out[0][1]["params"]["v"] == "y" * 1000


def test_gzip_body_auto_detected():
    body = gzip.compress(dumps({"type": "big", "data": "z" * 100}).encode())
    frame = HEADER_STRUCT.pack(len(body)) + body
    decoder = MessageDecoder()
    messages = decoder.feed(frame)
    assert messages[0][1]["type"] == "big"


def test_bad_frame_length_is_fatal_not_crashing():
    frame = struct.pack(">I", MAX_MESSAGE_SIZE + 1) + b"{}"
    decoder = MessageDecoder()
    messages = decoder.feed(frame)
    assert messages == []
    assert decoder.fatal is not None


def test_json_parse_error_is_reported_not_fatal():
    body = b"{not json"
    frame = HEADER_STRUCT.pack(len(body)) + body
    decoder = MessageDecoder()
    messages = decoder.feed(frame)
    assert messages[0][0] == "error"
    assert decoder.fatal is None


def test_json_safe_replaces_nonfinite():
    payload = {"a": float("nan"), "b": [float("inf"), 1.5],
               "c": {"d": float("-inf")}}
    safe = json_safe(payload)
    assert safe["a"] is None
    assert safe["b"][0] is None
    assert safe["c"]["d"] is None
    assert safe["b"][1] == 1.5


def test_dumps_is_strict_json():
    text = dumps({"v": float("nan")})
    assert "NaN" not in text
    assert loads(text)["v"] is None


def test_bytes_become_base64_marker():
    text = dumps({"blob": b"\x00\x01"})
    assert loads(text)["blob"]["__b64__"] == "AAE="
