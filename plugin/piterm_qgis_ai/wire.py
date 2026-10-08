"""Wire codec for the Piterm Bridge: framing, text mode, gzip, JSON safety.

Two interchangeable modes on the same socket:

1. **Binary frames** (default, used by the MCP server and CLI):
   4-byte big-endian uint32 length + UTF-8 JSON body. The body may be gzip
   (``1f 8b`` magic) when the sender opted in.

2. **Text lines** (``curl``/netcat/skill scripts friendly):
   one JSON object per line, ``\\n`` delimited. Detected automatically by the
   first byte being ``{``. Responses mirror the request mode.

The decoder never blocks and never trusts the peer: every length is range
checked, malformed input is reported as a structured error while the
connection stays alive (except for unrecoverable length garbage).

Stdlib-only; importable outside QGIS for tests.
"""

from __future__ import annotations

import gzip
import json
import math
import struct

from .constants import GZIP_THRESHOLD, MAX_MESSAGE_SIZE

HEADER_STRUCT = struct.Struct(">I")
GZIP_MAGIC = b"\x1f\x8b"
_TEXT_OPEN = 0x7B  # '{'

MODE_UNKNOWN = "unknown"
MODE_FRAME = "frame"
MODE_TEXT = "text"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def json_safe(value):
    """Recursively replace non-finite floats with None.

    Empty layers (extent of a layer with no features) and nodata raster stats
    legitimately produce NaN/Infinity, which ``json.dumps`` emits as bare
    tokens that strict JSON parsers reject.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def dumps(obj):
    return json.dumps(json_safe(obj), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False, default=_json_default)


def _json_default(o):
    if isinstance(o, (bytes, bytearray, memoryview)):
        import base64
        return {"__b64__": base64.b64encode(bytes(o)).decode("ascii")}
    if isinstance(o, set):
        return sorted(o)
    raise TypeError("Object of type %s is not JSON serializable" % type(o).__name__)


def loads(data):
    return json.loads(data)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def encode_body(obj, gzip_ok=False, threshold=GZIP_THRESHOLD):
    """Return (body_bytes, compressed_flag)."""
    data = dumps(obj).encode("utf-8")
    if gzip_ok and len(data) >= threshold:
        return gzip.compress(data, 6), True
    return data, False


def encode_frame(obj, gzip_ok=False, threshold=GZIP_THRESHOLD):
    body, _ = encode_body(obj, gzip_ok, threshold)
    return HEADER_STRUCT.pack(len(body)) + body


def encode_text(obj):
    """One NDJSON line (never compressed: curl users want readable bytes)."""
    return dumps(obj).encode("utf-8") + b"\n"


def encode_message(obj, mode=MODE_FRAME, gzip_ok=False, threshold=GZIP_THRESHOLD):
    if mode == MODE_TEXT:
        return encode_text(obj)
    return encode_frame(obj, gzip_ok, threshold)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

class MessageDecoder:
    """Incremental decoder accepting both frame and text modes."""

    def __init__(self, max_message=MAX_MESSAGE_SIZE):
        self.buf = bytearray()
        self.mode = MODE_UNKNOWN
        self.max_message = max_message
        # Set when the stream is unrecoverably corrupt (bad frame length).
        self.fatal = None
        self.messages = 0
        self.bytes_in = 0

    def feed(self, data):
        """Feed bytes; returns a list of ("frame"|"text"|"error", payload)."""
        if data:
            self.buf.extend(data)
            self.bytes_in += len(data)
        out = []
        while True:
            if self.fatal:
                break
            if self.mode == MODE_UNKNOWN:
                if not self.buf:
                    break
                if self.buf[0] == _TEXT_OPEN:
                    self.mode = MODE_TEXT
                elif len(self.buf) >= 4:
                    length = HEADER_STRUCT.unpack_from(self.buf, 0)[0]
                    if length == 0 or length > self.max_message:
                        self.fatal = "invalid frame length %d (max %d)" % (
                            length, self.max_message)
                        break
                    self.mode = MODE_FRAME
                else:
                    break

            if self.mode == MODE_TEXT:
                idx = self.buf.find(b"\n")
                if idx < 0:
                    if len(self.buf) > self.max_message:
                        self.fatal = "text line exceeds %d bytes" % self.max_message
                    break
                line = bytes(self.buf[:idx])
                del self.buf[:idx + 1]
                if line.strip():
                    out.append(self._decode(line, "text"))
                continue

            # frame mode
            if len(self.buf) < 4:
                break
            length = HEADER_STRUCT.unpack_from(self.buf, 0)[0]
            if length == 0 or length > self.max_message:
                self.fatal = "invalid frame length %d (max %d)" % (length, self.max_message)
                break
            if len(self.buf) < 4 + length:
                break
            body = bytes(self.buf[4:4 + length])
            del self.buf[:4 + length]
            out.append(self._decode(body, "frame"))
        return out

    def _decode(self, body, mode):
        if body[:2] == GZIP_MAGIC:
            try:
                body = gzip.decompress(body)
            except Exception as exc:  # noqa: BLE001 - report as parse error
                return ("error", {"message": "gzip decode failed: %s" % exc})
        try:
            text = body.decode("utf-8")
            obj = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            return ("error", {"message": "JSON decode failed: %s" % exc})
        if not isinstance(obj, dict):
            return ("error", {"message": "message must be a JSON object, got %s"
                              % type(obj).__name__})
        self.messages += 1
        return (mode, obj)

    def reset(self):
        self.buf.clear()
        self.mode = MODE_UNKNOWN
        self.fatal = None
