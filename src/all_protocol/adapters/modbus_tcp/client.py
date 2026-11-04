from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

from ...application.contracts import (
    ClientError, ClientEvents, Command, ErrorKind, Point, Target, Value,
)
from .model import Area, ModbusTcpConfig, modbus_target


class ModbusTcpClient:
    def __init__(self, config: object, events: ClientEvents):
        if not isinstance(config, ModbusTcpConfig):
            raise ClientError(ErrorKind.INPUT, "连接配置与 Modbus TCP 不匹配")
        config.validate()
        self._events = events
        self._received = False
        self._transaction_id = 0
        self._client = AsyncModbusTcpClient(
            config.host, port=config.port, timeout=config.timeout_ms / 1000,
            retries=0, reconnect_delay=0, trace_packet=self._trace_packet,
            trace_connect=self._trace_connect,
        )

    def _trace_packet(self, sending: bool, data: bytes) -> bytes:
        if sending:
            self._transaction_id = int.from_bytes(data[:2], "big")
        else:
            self._received = True
        self._events.packet(sending, data)
        return data

    def _trace_connect(self, connected: bool):
        if not connected:
            self._events.disconnected()

    async def connect(self):
        try:
            connected = await self._client.connect()
        except (OSError, ModbusException) as error:
            raise ClientError(ErrorKind.CONNECT, str(error)) from error
        if not connected or not self._client.connected:
            raise ClientError(ErrorKind.CONNECT, "无法建立 TCP 连接，请检查主机、端口和网络")

    async def close(self):
        self._client.close()

    def function(self, command: Command) -> str:
        return f"0x{modbus_target(command.target).validate(command.values):02X}"

    async def _exchange(self, operation, *args, code: int, device_id: int, **kwargs):
        if not self._client.connected:
            raise ClientError(ErrorKind.DISCONNECTED, "TCP 连接已关闭")
        self._received = False
        try:
            response = await operation(*args, device_id=device_id, **kwargs)
        except (ConnectionException, OSError) as error:
            raise ClientError(ErrorKind.DISCONNECTED, str(error)) from error
        except (ModbusIOException, TimeoutError) as error:
            kind = ErrorKind.RESPONSE if self._received else ErrorKind.TIMEOUT
            if not self._client.connected:
                kind = ErrorKind.DISCONNECTED
            detail = "收到字节但未得到有效响应" if kind == ErrorKind.RESPONSE else str(error)
            raise ClientError(kind, detail) from error
        except ModbusException as error:
            raise ClientError(ErrorKind.RESPONSE, str(error)) from error
        if response.transaction_id != self._transaction_id or response.dev_id != device_id:
            raise ClientError(ErrorKind.RESPONSE, "响应事务 ID 或 Unit ID 与请求不一致")
        if response.function_code not in (code, code | 0x80):
            raise ClientError(ErrorKind.RESPONSE, "响应功能码与请求不一致")
        if isinstance(response, ExceptionResponse):
            raise ClientError(ErrorKind.PROTOCOL, "设备返回 Modbus 异常", response.exception_code,
                              write_rejected=response.exception_code in (1, 2, 3))
        if response.isError():
            raise ClientError(ErrorKind.RESPONSE, str(response))
        return response

    async def read(self, target: Target) -> tuple[Point, ...]:
        point = modbus_target(target)
        code = point.validate()
        operation = {
            Area.HR: self._client.read_holding_registers, Area.IR: self._client.read_input_registers,
            Area.COIL: self._client.read_coils, Area.DI: self._client.read_discrete_inputs,
        }[point.area]
        response = await self._exchange(operation, point.offset, count=point.count,
                                        code=code, device_id=point.unit_id)
        if point.area in (Area.HR, Area.IR):
            raw = response.registers
            expected = point.count
            width = 4
        else:
            raw = response.bits
            expected = ((point.count + 7) // 8) * 8
            width = 2
        if len(raw) != expected:
            raise ClientError(ErrorKind.RESPONSE, "响应数据长度与请求数量不一致")
        return tuple(
            Point(str(address), f"{point.area}[{address}]", int(value), f"0x{value:0{width}X}")
            for address, value in enumerate(raw[:point.count], start=point.offset)
        )

    async def write(self, target: Target, values: tuple[Value, ...]):
        point = modbus_target(target)
        code = point.validate(values)
        if point.area == Area.HR:
            operation = self._client.write_register if point.count == 1 else self._client.write_registers
            payload = [int(value) for value in values]
        else:
            operation = self._client.write_coil if point.count == 1 else self._client.write_coils
            payload = [bool(value) for value in values]
        response = await self._exchange(operation, point.offset, payload[0] if point.count == 1 else payload,
                                        code=code, device_id=point.unit_id)
        if response.address != point.offset:
            raise ClientError(ErrorKind.RESPONSE, "写入回执地址与请求不一致")
        if point.count == 1:
            echo = response.registers if point.area == Area.HR else response.bits
            if not echo or echo[0] != payload[0]:
                raise ClientError(ErrorKind.RESPONSE, "写入回执值与请求不一致")
        elif response.count != point.count:
            raise ClientError(ErrorKind.RESPONSE, "写入回执数量与请求不一致")
