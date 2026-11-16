"""Portable business meanings attached to bits of a decoded scalar value."""

from dataclasses import asdict, dataclass

from .application.binary import bit_pattern


@dataclass(frozen=True, slots=True)
class BitDefinition:
    bit: int
    name: str = ""
    zero: str = ""
    one: str = ""

    def meaning(self, value):
        return self.one if value else self.zero


def bit_width(kind):
    pattern = bit_pattern(0, str(kind))
    return pattern.width if pattern else 0


def validate_bit_definitions(definitions, kind):
    if not isinstance(definitions, tuple):
        raise ValueError("Bit 定义必须是元组")
    width, seen = bit_width(kind), set()
    if definitions and not width:
        raise ValueError(f"当前类型 {kind} 不支持 Bit 定义")
    for item in definitions:
        if not isinstance(item, BitDefinition):
            raise ValueError("无效的 Bit 定义")
        if type(item.bit) is not int or not 0 <= item.bit < width:
            raise ValueError(f"Bit 编号必须在 0–{width - 1} 之间；当前类型为 {kind}")
        if item.bit in seen:
            raise ValueError(f"bit {item.bit} 定义重复")
        if any(not isinstance(value, str) for value in (item.name, item.zero, item.one)):
            raise ValueError("Bit 名称及 0/1 含义必须是文本")
        if not any(value.strip() for value in (item.name, item.zero, item.one)):
            raise ValueError(f"bit {item.bit} 定义不能为空")
        seen.add(item.bit)


def bit_definition_records(definitions):
    return [asdict(item) for item in definitions]


def decode_bit_definitions(data):
    if not isinstance(data, list) or len(data) > 64:
        raise ValueError("Bit 定义必须是最多 64 项的列表")
    result = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"bit", "name", "zero", "one"}:
            raise ValueError("Bit 定义须包含 bit、name、zero、one")
        result.append(BitDefinition(**item))
    return tuple(result)
