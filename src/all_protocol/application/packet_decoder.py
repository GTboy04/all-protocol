from dataclasses import dataclass, field


@dataclass(slots=True)
class PacketField:
    name: str
    value: str = ""
    spans: tuple[tuple[int, int], ...] = ()
    children: list["PacketField"] = field(default_factory=list)


class ParseError(ValueError):
    def __init__(self, offset: int, message: str):
        super().__init__(message)
        self.offset = offset


class _Cursor:
    def __init__(self, data: bytes, positions: list[int], fields: list[PacketField], origin=0):
        self.data = data
        self.positions = positions
        self.fields = fields
        self.offset = 0
        self.origin = origin

    def _position(self, offset):
        if offset < len(self.positions):
            return self.positions[offset]
        return self.positions[-1] + 1 if self.positions else self.origin

    def error(self, message, offset=None):
        raise ParseError(self._position(self.offset if offset is None else offset), message)

    def spans(self, start, end):
        ranges = []
        for offset in self.positions[start:end]:
            if ranges and ranges[-1][1] == offset:
                ranges[-1] = (ranges[-1][0], offset + 1)
            else:
                ranges.append((offset, offset + 1))
        return tuple(ranges)

    @property
    def remaining(self):
        return len(self.data) - self.offset

    def take(self, name, size, display=str):
        start = self.offset
        end = start + size
        if end > len(self.data):
            self.error(f"{name} 残缺，需 {size} 字节", len(self.data))
        self.offset = end
        data = self.data[start:end]
        self.fields.append(PacketField(name, display(int.from_bytes(data, "big")) if display else "",
                                       self.spans(start, end)))
        return data

    def number(self, name, size=1, *, hex_value=False):
        display = (lambda value: f"0x{value:0{size * 2}X}") if hex_value else str
        return int.from_bytes(self.take(name, size, display), "big")

    def section(self, name, size):
        start = self.offset
        data = self.take(name, size, None)
        return _Cursor(data, self.positions[start:self.offset], self.fields[-1].children, self._position(start))

    def finish(self):
        if self.remaining:
            self.error("存在多余字节")


def _modbus(data, request, fields):
    offset = 0
    while offset < len(data):
        frame = PacketField("Modbus TCP ADU")
        fields.append(frame)
        header = _Cursor(data[offset:offset + 6], list(range(offset, min(offset + 6, len(data)))), frame.children)
        header.number("事务 ID", 2, hex_value=True)
        if header.number("协议 ID", 2, hex_value=True):
            header.error("协议 ID 应为 0", 2)
        length = header.number("长度", 2)
        if not 2 <= length <= 254:
            header.error("MBAP 长度超出范围", 4)
        end = min(offset + 6 + length, len(data))
        frame.spans = ((offset, end),)
        body = _Cursor(data[offset + 6:end], list(range(offset + 6, end)), frame.children)
        if not body.remaining:
            raise ParseError(end, "缺少 Unit ID 和功能码")
        body.number("Unit ID")
        code = body.number("功能码", hex_value=True)
        if not request and code & 0x80:
            body.number("异常码", hex_value=True)
        elif code in (1, 2, 3, 4, 5, 6, 15, 16):
            if request or code in (5, 6, 15, 16):
                body.number("起始地址", 2)
                if code in (5, 6):
                    body.number("写入值", 2, hex_value=True)
                else:
                    body.number("数量", 2)
            if (not request and code in (1, 2, 3, 4)) or (request and code in (15, 16)):
                size = body.number("数据字节数")
                body.take("数据", size, None)
        else:
            body.take("PDU 数据", body.remaining, None)
        if end < offset + 6 + length:
            body.error("报文短于 MBAP 声明长度", body.offset + body.remaining)
        body.finish()
        offset = end


def _s7_pdu(data, positions, fields):
    pdu = PacketField("S7comm PDU")
    fields.append(pdu)
    header = _Cursor(data, positions, pdu.children)
    pdu.spans = header.spans(0, len(data))
    if header.number("协议 ID", hex_value=True) != 0x32:
        header.error("S7 协议 ID 应为 0x32", 0)
    kind = header.number("ROSCTR", hex_value=True)
    if kind not in (1, 2, 3, 7):
        header.error("未知 ROSCTR", 1)
    header.number("冗余 ID", 2, hex_value=True)
    header.number("PDU 引用号", 2, hex_value=True)
    param_length = header.number("参数长度", 2)
    data_length = header.number("数据长度", 2)
    if kind in (2, 3):
        header.number("错误类别", hex_value=True)
        header.number("错误代码", hex_value=True)
    params = header.section("参数", param_length)
    payload = header.section("数据", data_length)
    header.finish()
    if not params.remaining or kind == 7:
        return
    function = params.number("功能码", hex_value=True)
    if function == 0xF0:
        params.number("保留")
        params.number("主叫 Max AmQ", 2)
        params.number("被叫 Max AmQ", 2)
        params.number("PDU 容量", 2)
    elif function in (4, 5):
        count = params.number("项目数")
        if kind == 1:
            for index in range(count):
                item = params.section(f"地址项 {index + 1}", 12)
                item.number("变量规范", hex_value=True)
                item.number("地址长度")
                item.number("语法 ID", hex_value=True)
                item.number("传输类型", hex_value=True)
                item.number("元素数", 2)
                item.number("DB 编号", 2)
                item.number("区域", hex_value=True)
                item.number("位地址", 3)
        if kind == 3 and function == 5:
            for index in range(count):
                payload.number(f"写入状态 {index + 1}", hex_value=True)
        elif (kind == 3 and function == 4) or (kind == 1 and function == 5):
            for index in range(count):
                payload.number(f"项目 {index + 1} · 状态 / 保留", hex_value=True)
                transport = payload.number("数据类型", hex_value=True)
                length = payload.number("数据长度", 2)
                size = (length + 7) // 8 if transport in (4, 5) or (kind == 3 and transport == 3) else length
                payload.take("值", size, None)
                if index + 1 < count and size % 2:
                    payload.number("对齐填充", hex_value=True)
        params.finish()
        payload.finish()


def _s7(data, fields):
    offset = 0
    pdu = bytearray()
    positions = []
    while offset < len(data):
        frame = PacketField("TPKT / COTP")
        fields.append(frame)
        header_end = min(offset + 4, len(data))
        header = _Cursor(data[offset:header_end], list(range(offset, header_end)), frame.children)
        if header.number("TPKT 版本") != 3:
            header.error("TPKT 版本应为 3", 0)
        if header.number("保留"):
            header.error("TPKT 保留字节应为 0", 1)
        length = header.number("TPKT 长度", 2)
        if length < 7:
            header.error("TPKT 长度小于最小帧", 2)
        end = min(offset + length, len(data))
        frame.spans = ((offset, end),)
        body = _Cursor(data[offset + 4:end], list(range(offset + 4, end)), frame.children)
        if not body.remaining:
            raise ParseError(end, "缺少 COTP 头")
        size = body.number("COTP 头长度")
        cotp = body.section("COTP 头", size)
        kind = cotp.number("TPDU 类型", hex_value=True)
        if kind in (0xE0, 0xD0):
            cotp.number("目标引用", 2, hex_value=True)
            cotp.number("来源引用", 2, hex_value=True)
            cotp.number("协议类别", hex_value=True)
            while cotp.remaining:
                code = cotp.number("参数类型", hex_value=True)
                count = cotp.number("参数长度")
                cotp.number({0xC0: "TPDU 大小指数", 0xC1: "来源 TSAP", 0xC2: "目标 TSAP"}.get(code, "参数值"),
                            count, hex_value=True)
        elif kind == 0xF0:
            end_flag = cotp.number("EOT / TPDU 编号", hex_value=True)
            if end == offset + length:
                pdu.extend(body.data[body.offset:])
                positions.extend(body.positions[body.offset:])
                if end_flag & 0x80:
                    if not positions:
                        raise ParseError(end, "缺少 S7 PDU")
                    _s7_pdu(bytes(pdu), positions, fields)
                    pdu.clear()
                    positions.clear()
        if end < offset + length:
            raise ParseError(end, "报文短于 TPKT 声明长度")
        offset = end
    if positions:
        raise ParseError(len(data), "COTP 分段缺少结束帧")


def decode_packet(protocol: str, data: bytes, *, request: bool) -> list[PacketField]:
    fields = []
    try:
        if not data:
            raise ParseError(0, "空报文")
        if protocol == "modbus-tcp":
            _modbus(data, request, fields)
        elif protocol == "s7":
            _s7(data, fields)
        else:
            fields.append(PacketField("原始字节", spans=((0, len(data)),)))
    except ParseError as error:
        spans = ((error.offset, error.offset + 1),) if error.offset < len(data) else ()
        fields.append(PacketField("解析失败", f"偏移 {error.offset} · {error}", spans))
    return fields
