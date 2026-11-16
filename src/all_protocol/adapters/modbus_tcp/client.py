from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from ...application.contracts import ClientError, ClientEvents, ErrorKind
from .model import ModbusTcpConfig
from .common import ModbusClient


class ModbusTcpClient(ModbusClient):
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

    def _matches_response(self, response, device_id):
        return response.transaction_id == self._transaction_id and response.dev_id == device_id
