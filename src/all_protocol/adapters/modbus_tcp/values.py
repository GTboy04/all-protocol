from __future__ import annotations

import math
import struct
from enum import StrEnum

from ...application.contracts import Value


class DataType(StrEnum):
    BOOL = "Bool"
    UINT16 = "UInt16"
    INT16 = "Int16"
    UINT32 = "UInt32"
    INT32 = "Int32"
    UINT64 = "UInt64"
    INT64 = "Int64"
    FLOAT32 = "Float32"
    FLOAT64 = "Float64"

    @property
    def words(self):
        return 1 if self == DataType.BOOL else struct.calcsize(self._format) // 2

    @property
    def _format(self):
        return ">" + {
            DataType.UINT16: "H", DataType.INT16: "h",
            DataType.UINT32: "I", DataType.INT32: "i",
            DataType.UINT64: "Q", DataType.INT64: "q",
            DataType.FLOAT32: "f", DataType.FLOAT64: "d",
        }[self]

    @property
    def floating(self):
        return self in (DataType.FLOAT32, DataType.FLOAT64)

    def parse(self, text: str) -> Value:
        try:
            return float(text) if self.floating else int(text, 10)
        except ValueError as error:
            raise ValueError(f"请输入有效的 {self} 数值") from error

    def encode(self, value: Value, order: RegisterOrder) -> tuple[int, ...]:
        if self == DataType.BOOL:
            if type(value) not in (bool, int) or value not in (0, 1):
                raise ValueError("Bool 的值必须是 0 或 1")
            return (int(value),)
        if self.floating:
            if type(value) not in (int, float):
                raise ValueError(f"请输入有效的 {self} 数值")
        elif type(value) not in (int, bool):
            raise ValueError(f"{self} 的值必须是整数")
        try:
            if self.floating and not math.isfinite(value):
                raise ValueError(f"{self} 的值必须是有限数值")
            packed = struct.pack(self._format, value)
        except (struct.error, OverflowError) as error:
            raise ValueError(f"数值超出 {self} 的范围") from error
        return order.arrange(struct.unpack(f">{self.words}H", packed))

    def decode(self, registers: tuple[int, ...], order: RegisterOrder) -> Value:
        if self == DataType.BOOL:
            return int(registers[0])
        packed = struct.pack(f">{self.words}H", *order.arrange(registers))
        return struct.unpack(self._format, packed)[0]


class RegisterOrder(StrEnum):
    BIG = "big"
    SWAP_WORDS = "swap_words"
    SWAP_BYTES = "swap_bytes"
    LITTLE = "little"

    @property
    def label(self):
        return {
            RegisterOrder.BIG: "高字在前 · 高字节在前",
            RegisterOrder.SWAP_WORDS: "低字在前 · 高字节在前",
            RegisterOrder.SWAP_BYTES: "高字在前 · 低字节在前",
            RegisterOrder.LITTLE: "低字在前 · 低字节在前",
        }[self]

    def arrange(self, registers: tuple[int, ...]) -> tuple[int, ...]:
        if self in (RegisterOrder.SWAP_WORDS, RegisterOrder.LITTLE):
            registers = registers[::-1]
        if self in (RegisterOrder.SWAP_BYTES, RegisterOrder.LITTLE):
            registers = tuple((word & 0xFF) << 8 | word >> 8 for word in registers)
        return registers
