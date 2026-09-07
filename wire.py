"""Binary primitives of the DayZ hive protocol.

Everything is little-endian, including the MessagePack-like PackInt tags.
"""

import struct


def hash37(text):
    """Case-insensitive engine hash: h = h * 37 + tolower(c)."""
    h = 0
    for ch in text.encode("utf-8", "replace"):
        if 0x41 <= ch <= 0x5A:
            ch += 0x20
        h = (h * 37 + ch) & 0xFFFFFFFF
    return h


def hash37_signed(text):
    h = hash37(text)
    return h - 0x1_0000_0000 if h >= 0x8000_0000 else h


class Writer:
    """Builds a response body."""

    def __init__(self):
        self.buf = bytearray()

    def __len__(self):
        return len(self.buf)

    def bytes(self):
        return bytes(self.buf)

    def raw(self, data):
        self.buf += data
        return self

    def u8(self, v):
        self.buf.append(v & 0xFF)
        return self

    def boolean(self, v):
        self.buf.append(1 if v else 0)
        return self

    def u16(self, v):
        self.buf += struct.pack("<H", v & 0xFFFF)
        return self

    def i32(self, v):
        self.buf += struct.pack("<I", int(v) & 0xFFFFFFFF)
        return self

    def f32(self, v):
        self.buf += struct.pack("<f", v)
        return self

    def small_string(self, text):
        """uint8 length + raw bytes, no terminator."""
        raw = text.encode("utf-8", "replace")[:255]
        self.buf.append(len(raw))
        self.buf += raw
        return self

    def string(self, text):
        """int32 length + raw bytes, no terminator."""
        raw = text.encode("utf-8", "replace")
        self.buf += struct.pack("<i", len(raw))
        self.buf += raw
        return self

    def pack_int(self, v):
        """Variable-length integer: 1 raw byte, or 0xD0/0xD1/0xD2 + int8/int16/int32."""
        v = int(v)
        if -32 <= v <= 127:
            self.buf.append(v & 0xFF)
        elif -128 <= v <= 127:
            self.buf += b"\xD0" + struct.pack("<b", v)
        elif -32768 <= v <= 32767:
            self.buf += b"\xD1" + struct.pack("<h", v)
        else:
            self.buf += b"\xD2" + struct.pack("<i", v)
        return self

    # Self-typed values used by the global variables block.

    def var_int(self, v):
        return self.pack_int(v)

    def var_float(self, v):
        self.buf += b"\xCA" + struct.pack("<f", float(v))
        return self

    def var_string(self, text):
        raw = text.encode("utf-8", "replace")
        if len(raw) < 32:
            self.buf.append(0xA0 | len(raw))
        elif len(raw) < 256:
            self.buf += b"\xD4" + bytes([len(raw)])
        else:
            self.buf += b"\xD5" + struct.pack("<H", len(raw))
        self.buf += raw
        return self


def pack_int_bytes(v):
    """Number of bytes pack_int() would use for v."""
    w = Writer()
    w.pack_int(v)
    return len(w)


class Reader:
    """Reads a request body."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def remaining(self):
        return len(self.data) - self.pos

    def raw(self, n):
        out = self.data[self.pos:self.pos + n]
        if len(out) != n:
            raise EOFError("read past end of body")
        self.pos += n
        return out

    def u8(self):
        return self.raw(1)[0]

    def boolean(self):
        return self.raw(1)[0] != 0

    def u16(self):
        return struct.unpack("<H", self.raw(2))[0]

    def i32(self):
        return struct.unpack("<i", self.raw(4))[0]

    def small_string(self):
        n = self.u8()
        return self.raw(n).decode("utf-8", "replace")

    def string(self):
        n = self.i32()
        return self.raw(n).decode("utf-8", "replace")

    def pack_int(self):
        tag = self.u8()
        if tag < 0x80:
            return tag
        if tag >= 0xE0:
            return tag - 0x100
        if tag == 0xD0:
            return struct.unpack("<b", self.raw(1))[0]
        if tag == 0xD1:
            return struct.unpack("<h", self.raw(2))[0]
        if tag == 0xD2:
            return struct.unpack("<i", self.raw(4))[0]
        return 0


def lz4_store(payload):
    """Valid LZ4 block made of literals only (no compression, no dependency)."""
    out = bytearray()
    n = len(payload)
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += payload
    return bytes(out)


def hive_compress(payload):
    """LZ4 block followed by the int32 uncompressed size."""
    return lz4_store(payload) + struct.pack("<i", len(payload))


def lz4_decompress(src, expected=None):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n or (expected is not None and len(dst) >= expected):
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("LZ4 offset is zero")
        match = token & 0x0F
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(dst) - offset
        if start < 0:
            raise ValueError("LZ4 offset out of range")
        for k in range(match):
            dst.append(dst[start + k])
    return bytes(dst)


def hive_decompress(body):
    """Inverse of hive_compress()."""
    if len(body) < 4:
        return b""
    size = struct.unpack("<i", body[-4:])[0]
    return lz4_decompress(body[:-4], size)
