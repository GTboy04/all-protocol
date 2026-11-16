"""Portable OPC UA addresses use namespace URIs, never session namespace indexes."""

from dataclasses import dataclass
from enum import StrEnum
import math
import struct
from urllib.parse import urlsplit
from uuid import UUID

from asyncua import ua

from ...application.contracts import ClientError, ErrorKind


STANDARD_NAMESPACE = "http://opcfoundation.org/UA/"


class UaType(StrEnum):
    Boolean = "Boolean"
    SByte = "SByte"
    Byte = "Byte"
    Int16 = "Int16"
    UInt16 = "UInt16"
    Int32 = "Int32"
    UInt32 = "UInt32"
    Int64 = "Int64"
    UInt64 = "UInt64"
    Float = "Float"
    Double = "Double"
    String = "String"

    @property
    def floating(self):
        return self in (UaType.Float, UaType.Double)

    @property
    def variant(self):
        return ua.VariantType[self.value]

    def parse(self, text):
        if self == UaType.String:
            return text
        if self == UaType.Boolean:
            if text.lower() not in ("true", "false", "0", "1"):
                raise ValueError("Boolean 请输入 true、false、0 或 1")
            return text.lower() in ("true", "1")
        return float(text) if self.floating else int(text)

    def prepare(self, value):
        valid = False
        if self == UaType.String:
            valid = type(value) is str and len(value.encode("utf-8")) <= 65536
        elif self == UaType.Boolean:
            valid = type(value) is bool or type(value) is int and value in (0, 1)
            value = bool(value) if valid else value
        elif self.floating:
            valid = type(value) in (int, float)
            try:
                value = float(value) if valid else value
                valid = valid and math.isfinite(value)
                if valid and self == UaType.Float:
                    value = struct.unpack("<f", struct.pack("<f", value))[0]
                    valid = math.isfinite(value)
            except (OverflowError, struct.error):
                valid = False
        else:
            bits = {"SByte": 8, "Byte": 8}.get(self.value)
            bits = bits or int(self.value.removeprefix("UInt").removeprefix("Int"))
            signed = self == UaType.SByte or self.value.startswith("Int")
            low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
            valid = type(value) is int and low <= value <= high
        if not valid:
            raise ClientError(ErrorKind.INPUT, f"值超出 {self.value} 标量范围")
        return value


@dataclass(frozen=True, slots=True)
class OpcUaConfig:
    url: str = ""
    security: str = "Basic256Sha256/SignAndEncrypt"
    identity_id: str = ""
    timeout_ms: int = 3000

    @property
    def endpoint(self):
        return self.url

    def validate(self):
        try:
            parsed = urlsplit(self.url)
            valid = (parsed.scheme == "opc.tcp" and bool(parsed.hostname) and parsed.port is not None
                     and 1 <= parsed.port <= 65535 and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment and not any(c.isspace() for c in self.url))
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ClientError(ErrorKind.INPUT, "请输入完整的 opc.tcp://主机:端口/路径，不允许内嵌凭据")
        if self.security not in ("None/None", "Basic256Sha256/SignAndEncrypt"):
            raise ClientError(ErrorKind.INPUT, "不支持的 OPC UA 安全策略")
        if not isinstance(self.identity_id, str) or (self.identity_id and not self.identity_id.isidentifier()):
            raise ClientError(ErrorKind.INPUT, "本机证书配置 ID 无效")
        if type(self.timeout_ms) is not int or not 100 <= self.timeout_ms <= 30000:
            raise ClientError(ErrorKind.INPUT, "超时须为 100–30000 ms")


@dataclass(frozen=True, slots=True)
class UaTarget:
    namespace_uri: str = STANDARD_NAMESPACE
    identifier_type: str = "i"
    identifier: str = "2267"
    data_type: UaType = UaType.Byte
    count: int = 1

    @property
    def label(self):
        return f"{self.namespace_uri} · {self.identifier_type}={self.identifier} · {self.data_type}"

    @property
    def value_type(self):
        return self.data_type

    def validate(self):
        if (not isinstance(self.namespace_uri, str) or not self.namespace_uri.strip()
                or len(self.namespace_uri) > 4096 or not isinstance(self.identifier, str)
                or len(self.identifier) > 4096 or not isinstance(self.data_type, UaType)
                or type(self.count) is not int or self.count != 1):
            raise ClientError(ErrorKind.INPUT, "节点须含命名空间 URI、类型化标识符和一个标量")
        self.node_id(0)

    def node_id(self, index):
        try:
            if self.identifier_type == "i":
                if not self.identifier.isascii() or not self.identifier.isdecimal() or not 0 <= int(self.identifier) <= 0xFFFFFFFF:
                    raise ValueError()
                return ua.NodeId(int(self.identifier), index, ua.NodeIdType.Numeric)
            if self.identifier_type == "s" and self.identifier:
                return ua.NodeId(self.identifier, index, ua.NodeIdType.String)
            if self.identifier_type == "g":
                return ua.NodeId(UUID(self.identifier), index, ua.NodeIdType.Guid)
            if self.identifier_type == "b":
                value = bytes.fromhex(self.identifier)
                if value:
                    return ua.NodeId(value, index, ua.NodeIdType.ByteString)
        except (ValueError, TypeError):
            pass
        raise ClientError(ErrorKind.INPUT, "节点标识符无效；支持 i 数值、s 文本、g GUID、b 十六进制字节")

    def prepare_write(self, values):
        self.validate()
        if len(values) != 1:
            raise ClientError(ErrorKind.INPUT, "每次写入一个 OPC UA 标量")
        return (self.data_type.prepare(values[0]),)


@dataclass(frozen=True, slots=True)
class UaBrowseTarget:
    node: UaTarget

    @property
    def label(self):
        return "浏览 · " + self.node.label


@dataclass(frozen=True, slots=True)
class UaReference:
    name: str
    node_class: str
    namespace_uri: str
    identifier_type: str
    identifier: str
    data_type: str = ""
    writable: bool = False
