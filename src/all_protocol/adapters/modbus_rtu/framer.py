from pymodbus.framer.rtu import FramerRTU


class StrictRtuFramer(FramerRTU):
    """Consume one ADU at a time, retaining a following frame in the same USB read.

    PyModbus 3.16.2's RTU decode returns the entire buffer length even when only
    the first frame belongs to a foreign station, losing the following response.
    """
    def decode(self, data):
        if len(data) < 4:
            return 0, 0, 0, b""
        try:
            size = self.decoder.calculateRtuFrameSize(data)
        except IndexError:
            return 0, 0, 0, b""
        if not size:
            return len(data), 0, 0, b""
        if len(data) < size:
            return 0, data[0], 0, b""
        frame = data[:size]
        if not self.check_CRC(frame[:-2], int.from_bytes(frame[-2:], "big")):
            return size, 0, 0, b""
        return size, frame[0], 0, frame[1:-2]
