from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ExceptionResponse
from ...application.contracts import ClientError, Command, ErrorKind, Point, Target, Value
from .model import Area, modbus_target


class ModbusClient:
    """Transport-independent Modbus value and response validation."""
    def function(self, command: Command) -> str:
        return f"0x{modbus_target(command.target).validate(command.values):02X}"

    async def _exchange(self, operation, *args, code: int, device_id: int, **kwargs):
        if not self._client.connected:
            raise ClientError(ErrorKind.DISCONNECTED, "连接已关闭")
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
        if not self._matches_response(response, device_id):
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
