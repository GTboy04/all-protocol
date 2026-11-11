import pytest

from all_protocol.application.binary import bit_pattern


@pytest.mark.parametrize("kind,value,width,encoded", [
    ("Bool", False, 1, 0), ("Bool", True, 1, 1), ("Byte", 128, 8, 128),
    ("UInt16", 0x8001, 16, 0x8001), ("Int16", -2, 16, 0xFFFE),
    ("Int", -32768, 16, 0x8000), ("Word", 65535, 16, 65535),
    ("UInt32", 1 << 31, 32, 1 << 31), ("DInt", -1, 32, 0xFFFFFFFF),
    ("DWord", 0xFFFFFFFF, 32, 0xFFFFFFFF),
    ("UInt64", (1 << 64) - 1, 64, (1 << 64) - 1),
    ("Int64", -(1 << 63), 64, 1 << 63),
    ("Real", 1.0, 32, 0x3F800000), ("Float32", -0.0, 32, 0x80000000),
    ("Float64", -2.5, 64, 0xC004000000000000),
])
def test_typed_bit_patterns(kind, value, width, encoded):
    pattern = bit_pattern(value, kind)
    assert (pattern.width, pattern.value) == (width, encoded)
    assert int(pattern.binary.replace(" ", ""), 2) == encoded
    assert len(pattern.binary.replace(" ", "")) == width
    assert pattern.bit(0) == encoded & 1
    assert pattern.bit(width - 1) == encoded >> (width - 1)
    with pytest.raises(IndexError):
        pattern.bit(width)


@pytest.mark.parametrize("value,kind", [
    (None, "UInt16"), (-1, "UInt16"), (65536, "UInt16"), ("3", "Int16"),
    (3.5, "Int16"), (float("nan"), "Float32"), (float("inf"), "Real"),
    (3, "Bool"), (1.0, ""), (1, "Unknown"),
])
def test_missing_invalid_or_untyped_values_have_no_invented_bits(value, kind):
    assert bit_pattern(value, kind) is None
