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
        self._partial_response = b""
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
            if not data.startswith(self._partial_response):
                self._flush_partial_response()
            # The packet hook sees the receive buffer again when another TCP segment arrives.
            if len(data) < 8 or (data[2:4] == b"\0\0" and len(data) < 6 + int.from_bytes(data[4:6], "big")):
                self._partial_response = data
                return data
            self._partial_response = b""
        self._events.packet(sending, data)
        return data

    def _flush_partial_response(self):
        if self._partial_response:
            self._events.packet(False, self._partial_response)
            self._partial_response = b""

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
        self._partial_response = b""
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
        finally:
            self._flush_partial_response()
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
        size = point.register_count
        response = await self._exchange(operation, point.offset, count=size,
                                        code=code, device_id=point.unit_id)
        if point.area in (Area.HR, Area.IR):
            raw = response.registers
            expected = size
            width = 4
        else:
            raw = response.bits
            expected = ((size + 7) // 8) * 8
            width = 2
        if len(raw) != expected:
            raise ClientError(ErrorKind.RESPONSE, "响应数据长度与请求数量不一致")
        points = []
        words = point.value_type.words
        for index in range(0, size, words):
            registers = tuple(raw[index:index + words])
            address = point.offset + index
            value = point.value_type.decode(registers, point.order)
            hex_value = "".join(f"{word:0{width}X}" for word in registers)
            points.append(Point(str(address), f"{point.area}[{address}]", value, f"0x{hex_value}"))
        return tuple(points)

    async def write(self, target: Target, values: tuple[Value, ...]):
        point = modbus_target(target)
        code = point.validate(values)
        single = point.register_count == 1
        registers = point.encode_values(values)
        if point.area == Area.HR:
            operation = self._client.write_register if single else self._client.write_registers
            payload = list(registers)
        else:
            operation = self._client.write_coil if single else self._client.write_coils
            payload = list(map(bool, registers))
        response = await self._exchange(operation, point.offset, payload[0] if single else payload,
                                        code=code, device_id=point.unit_id)
        if response.address != point.offset:
            raise ClientError(ErrorKind.RESPONSE, "写入回执地址与请求不一致")
        if single:
            echo = response.registers if point.area == Area.HR else response.bits
            if not echo or echo[0] != payload[0]:
                raise ClientError(ErrorKind.RESPONSE, "写入回执值与请求不一致")
        elif response.count != point.register_count:
            raise ClientError(ErrorKind.RESPONSE, "写入回执数量与请求不一致")
