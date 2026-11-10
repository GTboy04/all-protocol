from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
import math
import struct

from ...application.contracts import ClientError, ErrorKind, Point, Value


MAX_OFFSET = 0xFFFFFF // 8
MAX_BYTES = 200


class Area(IntEnum):
    I = 0x81
    Q = 0x82
    M = 0x83
    DB = 0x84


class DataType(StrEnum):
    BOOL = "Bool"
    BYTE = "Byte"
    INT = "Int"
    WORD = "Word"
    DINT = "DInt"
    DWORD = "DWord"
    REAL = "Real"

    @property
    def codec(self) -> struct.Struct:
        return CODECS[self]

    @property
    def width(self) -> int:
        return self.codec.size

    @property
    def address_code(self) -> str:
        return {1: "B", 2: "W", 4: "D"}[self.width]


CODECS = {
    DataType.BOOL: struct.Struct(">?"),
    DataType.BYTE: struct.Struct(">B"),
    DataType.INT: struct.Struct(">h"),
    DataType.WORD: struct.Struct(">H"),
    DataType.DINT: struct.Struct(">i"),
    DataType.DWORD: struct.Struct(">I"),
    DataType.REAL: struct.Struct(">f"),
}


@dataclass(frozen=True, slots=True)
class S7Config:
    host: str
    port: int = 102
    rack: int = 0
    slot: int = 1
    timeout_ms: int = 3000

    @property
    def endpoint(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port} · Rack {self.rack} / Slot {self.slot}"

    @property
    def remote_tsap(self) -> int:
        return 0x0100 | (self.rack << 5) | self.slot

    def validate(self):
        if not isinstance(self.host, str) or not self.host or any(
            char.isspace() or char in "[]/" or ord(char) < 32 for char in self.host
        ):
            raise ClientError(ErrorKind.INPUT, "请输入主机名或 IP 地址")
        for value, minimum, maximum, name in (
            (self.port, 1, 65535, "端口"), (self.rack, 0, 7, "Rack"),
            (self.slot, 0, 31, "Slot"), (self.timeout_ms, 100, 30000, "超时（ms）"),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ClientError(ErrorKind.INPUT, f"{name}范围为 {minimum}–{maximum}")


@dataclass(frozen=True, slots=True)
class S7Target:
    area: Area
    db: int
    offset: int
    data_type: DataType
    count: int = 1
    bit: int = 0

    def merged_with(self, other):
        if (not isinstance(other, S7Target) or other.count != 1 or self.area != other.area or self.db != other.db
                or self.data_type != other.data_type or self.data_type == DataType.BOOL
                or self.offset + self.byte_count != other.offset):
            return None
        merged = replace(self, count=self.count + 1)
        try:
            merged.validate()
        except ClientError:
            return None
        return merged

    @property
    def byte_count(self) -> int:
        return self.count * self.data_type.width

    @property
    def writable(self) -> bool:
        return self.area != Area.I

    def address(self, index: int = 0) -> str:
        offset = self.offset + index * self.data_type.width
        prefix = f"DB{self.db}.DB" if self.area == Area.DB else self.area.name
        if self.data_type == DataType.BOOL:
            suffix = "X" if self.area == Area.DB else ""
            return f"{prefix}{suffix}{offset}.{self.bit}"
        return f"{prefix}{self.data_type.address_code}{offset}"

    @property
    def label(self) -> str:
        addresses = self.address()
        if self.count > 1:
            addresses += f"–{self.address(self.count - 1)}"
        return f"{addresses} · {self.data_type} × {self.count}"

    def validate(self):
        if not isinstance(self.area, Area) or not isinstance(self.data_type, DataType):
            raise ClientError(ErrorKind.INPUT, "无效的 S7 区域或类型")
        minimum_db, maximum_db = (1, 65535) if self.area == Area.DB else (0, 0)
        if type(self.db) is not int or not minimum_db <= self.db <= maximum_db:
            raise ClientError(ErrorKind.INPUT, "DB 区使用 1–65535，其他区域使用 0")
        if type(self.count) is not int or self.count < 1 or self.byte_count > MAX_BYTES:
            raise ClientError(ErrorKind.INPUT, f"单次访问范围为 1–{MAX_BYTES} 字节")
        if type(self.offset) is not int or not 0 <= self.offset <= MAX_OFFSET - self.byte_count + 1:
            raise ClientError(ErrorKind.INPUT, "S7 字节地址超出范围")
        if type(self.bit) is not int or not 0 <= self.bit <= 7:
            raise ClientError(ErrorKind.INPUT, "位索引范围为 0–7")
        if self.data_type == DataType.BOOL and self.count != 1:
            raise ClientError(ErrorKind.INPUT, "Bool 每次访问一个点")
        if self.data_type != DataType.BOOL and self.bit != 0:
            raise ClientError(ErrorKind.INPUT, "位索引仅用于 Bool")

    def encode(self, values: tuple[Value, ...]) -> bytes:
        if not self.writable:
            raise ClientError(ErrorKind.INPUT, "I 区只读")
        return self.pack_values(values)

    def prepare_write(self, values: tuple[Value, ...]) -> tuple[Value, ...]:
        payload = self.encode(values)
        if self.data_type == DataType.REAL:
            return tuple(value for value, in self.data_type.codec.iter_unpack(payload))
        return values

    def pack_values(self, values: tuple[Value, ...]) -> bytes:
        self.validate()
        if len(values) != self.count:
            raise ClientError(ErrorKind.INPUT, f"请输入 {self.count} 个值")
        try:
            encoded = bytearray()
            for value in values:
                match self.data_type:
                    case DataType.BOOL:
                        if type(value) not in (bool, int) or value not in (0, 1):
                            raise ValueError
                    case DataType.REAL:
                        if type(value) not in (int, float) or not math.isfinite(value):
                            raise ValueError
                    case _:
                        if type(value) is not int:
                            raise ValueError
                encoded.extend(self.data_type.codec.pack(value))
            return bytes(encoded)
        except (ValueError, OverflowError, struct.error) as error:
            raise ClientError(ErrorKind.INPUT, f"写入值超出 {self.data_type} 的取值范围") from error

    def decode(self, payload: bytes) -> tuple[Point, ...]:
        label = self.label
        if self.data_type == DataType.BOOL:
            value = bool(payload[0] & (1 << self.bit))
            return (Point(self.address(), label, value, payload.hex(" ").upper()),)
        width = self.data_type.width
        points = []
        for index, (value,) in enumerate(self.data_type.codec.iter_unpack(payload)):
            start = index * width
            raw = payload[start:start + width].hex(" ").upper()
            points.append(Point(self.address(index), label, value, raw))
        return tuple(points)
