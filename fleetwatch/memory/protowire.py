"""A schema-free Protocol Buffers wire-format reader, standard library only.

It splits a message into (field number, wire type, value) triples: varints as ints, fixed32
and fixed64 as unsigned little-endian ints, length-delimited fields as memoryview slices of
the input (a nested message, a string or raw bytes; the wire format does not say which, so
the caller decides by asking for a path). Groups (wire types 3 and 4) are long deprecated and
are refused, as is anything truncated, so a payload in some other format fails loudly
instead of decoding into nonsense.
"""
VARINT, FIXED64, LENGTH, FIXED32 = 0, 1, 2, 5


class DecodeError(ValueError):
    """The bytes are not a well-formed protobuf message."""


def _varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7
        if shift >= 64:
            raise DecodeError("varint longer than ten bytes")


def fields(buf):
    """Every top-level field of `buf` (bytes or memoryview) as (number, wire type, value)."""
    buf = memoryview(buf)
    out, i, n = [], 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise DecodeError("field number 0")
        if wt == VARINT:
            val, i = _varint(buf, i)
        elif wt == LENGTH:
            size, i = _varint(buf, i)
            if i + size > n:
                raise DecodeError("length-delimited field runs past the end")
            val, i = buf[i:i + size], i + size
        elif wt in (FIXED64, FIXED32):
            width = 8 if wt == FIXED64 else 4
            if i + width > n:
                raise DecodeError("truncated fixed-width field")
            val, i = int.from_bytes(buf[i:i + width], "little"), i + width
        else:
            raise DecodeError("wire type %d" % wt)
        out.append((num, wt, val))
    return out


def every(buf, *path):
    """The values at a field path (field numbers, outermost first); every repeated
    occurrence at each level counts. A level that does not decode yields nothing there."""
    level = [memoryview(buf)]
    for k, num in enumerate(path):
        last = k == len(path) - 1
        nxt = []
        for m in level:
            try:
                fs = fields(m)
            except DecodeError:
                continue
            nxt += [v for f, wt, v in fs if f == num and (last or wt == LENGTH)]
        level = nxt
    return level


def first(buf, *path):
    """The first value at a field path, or None."""
    found = every(buf, *path)
    return found[0] if found else None


def text(buf, *path):
    """The first length-delimited value at a path as UTF-8 text, or None when there is none
    or it is not valid UTF-8."""
    v = first(buf, *path)
    if not isinstance(v, memoryview):
        return None
    try:
        return bytes(v).decode("utf-8")
    except UnicodeDecodeError:
        return None
