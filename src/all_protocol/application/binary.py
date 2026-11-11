"""Read-only bit representations of decoded scalar values (not wire byte order)."""
from dataclasses import dataclass
import math
import struct


_FORMATS = {
    "Byte": "B", "UInt16": "H", "Int16": "h", "Word": "H", "Int": "h",
    "UInt32": "I", "Int32": "i", "DWord": "I", "DInt": "i",
    "UInt64": "Q", "Int64": "q", "Float32": "f", "Real": "f", "Float64": "d",
}


@dataclass(frozen=True, slots=True)
class BitPattern:
    value: int
    width: int
    kind: str

    @property
    def binary(self):
        digits = f"{self.value:0{self.width}b}"
        return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))

    @property
    def hexadecimal(self):
        return f"0x{self.value:0{(self.width + 3) // 4}X}"

    def bit(self, index):
        if not 0 <= index < self.width:
            raise IndexError(index)
        return (self.value >> index) & 1


def target_kind(target):
    return str(getattr(target, "value_type", getattr(target, "data_type", "")))


def bit_pattern(value, kind=""):
    """Preserve signed two's complement and typed IEEE-754 widths; never guess floats."""
    if value is None:
        return None
    if kind == "Bool" or type(value) is bool:
        return BitPattern(int(value), 1, "Bool") if value in (0, 1) else None
    code = _FORMATS.get(str(kind))
    if code is None:
        return None
    if code in "fd":
        if type(value) not in (int, float) or not math.isfinite(value):
            return None  # A decoded NaN cannot preserve its original payload bits.
    elif type(value) is not int:
        return None
    try:
        packed = struct.pack(">" + code, value)
    except (struct.error, OverflowError):
        return None
    return BitPattern(int.from_bytes(packed, "big"), len(packed) * 8, str(kind))


def binary_text(value, kind):
    pattern = bit_pattern(value, kind)
    return pattern.binary if pattern else "—"
