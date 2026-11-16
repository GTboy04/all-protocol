from dataclasses import dataclass
from ipaddress import ip_address

from ...application.contracts import ClientError
from ...bit_definitions import BitDefinition, validate_bit_definitions
from ...application.simulation import SimulationPoint, SimulationRule, WriteSpan, compile_rule
from .model import UaTarget, UaType


SIMULATOR_NAMESPACE = "urn:allprotocol:simulator"


def value_span(target):
    return WriteSpan((target.namespace_uri, target.identifier_type, target.identifier), 0, 1)


@dataclass(frozen=True, slots=True)
class UaServerPoint:
    target: UaTarget
    initial: int | float | bool | str = 0
    name: str = ""
    writable: bool = True
    simulation: SimulationRule | None = None
    bit_definitions: tuple[BitDefinition, ...] = ()

    def encode(self, value):
        try:
            return self.target.prepare_write((value,))[0]
        except ClientError as error:
            raise ValueError(str(error)) from None


@dataclass(frozen=True, slots=True)
class UaServerPlan:
    host: str = "127.0.0.1"
    port: int = 4840
    points: tuple[UaServerPoint, ...] = ()
    security: str = "Disabled"
    namespace_uri: str = SIMULATOR_NAMESPACE

    @property
    def endpoint(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"opc.tcp://{host}:{self.port}/allprotocol/"

    def validate(self, *, starting=False):
        try:
            if not ip_address(self.host).is_loopback:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("首版 OPC UA 模拟端仅允许本机回环地址") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("端口须为 1–65535")
        if self.security not in ("Disabled", "None/None"):
            raise ValueError("模拟端安全配置无效")
        if starting and self.security != "None/None":
            raise ValueError("请显式选择「仅本机无加密匿名模拟」后启动")
        if (not isinstance(self.namespace_uri, str) or not self.namespace_uri.strip()
                or len(self.namespace_uri) > 4096 or self.namespace_uri == "http://opcfoundation.org/UA/"):
            raise ValueError("模拟命名空间 URI 无效")
        if starting and not self.points:
            raise ValueError("启动前至少配置一个 OPC UA 标量节点")
        if len(self.points) > 1000:
            raise ValueError("模拟端最多 1000 个节点")
        seen = set()
        for point in self.points:
            point.encode(point.initial)
            validate_bit_definitions(point.bit_definitions, str(point.target.data_type))
            if point.target.namespace_uri != self.namespace_uri:
                raise ValueError("节点命名空间须与模拟端命名空间一致")
            if not isinstance(point.name, str) or type(point.writable) is not bool:
                raise ValueError("节点名称或读写权限无效")
            key = point.target.node_id(1)
            if key in seen or (point.target.identifier_type == "s" and point.target.identifier == "AllProtocol"):
                raise ValueError("节点标识符重复或占用了模拟根对象")
            seen.add(key)
        for point in self.simulation_points(configured_only=True):
            compile_rule(point, point.rule)

    def simulation_points(self, *, configured_only=False):
        for point in self.points:
            if configured_only and point.simulation is None:
                continue
            yield SimulationPoint(point.target, point.name or point.target.label, point.initial, point.encode,
                                  value_span(point.target), point.target.data_type.floating,
                                  point.target.data_type == UaType.Boolean, point.simulation)


@dataclass(frozen=True, slots=True)
class UaValuesChanged:
    values: tuple
