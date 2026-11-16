"""Bound server traffic queues and keep journal I/O off the GUI thread."""
from collections import deque
from dataclasses import dataclass
from threading import Condition, Thread

from PySide6.QtCore import QTimer, Signal, Slot

from ..application.server_contracts import RequestMetadata, ServerLifecycle
from ..application.journal import VISIBLE_LOG_ROWS
from .io_bridge import IoBridge


@dataclass(frozen=True)
class ServerEventBatch:
    events: tuple
    rotated: bool = False
    clipped: bool = False
    error: str = ""


def _trim_requests(queue, limit):
    # Control/lifecycle events must survive a flood of polling requests.
    if len(queue) <= limit:
        return False
    excess = sum(isinstance(event.payload, RequestMetadata) for event in queue) - limit
    if excess <= 0:
        return False
    kept = deque()
    for event in queue:
        if excess and isinstance(event.payload, RequestMetadata):
            excess -= 1
        else:
            kept.append(event)
    queue.clear()
    queue.extend(kept)
    return True


class ServerIoBridge(IoBridge):
    events = Signal(object)
    PENDING_REQUESTS = 4096
    BATCH_SIZE = 256
    FLUSH_SECONDS = 0.05

    def __init__(self, create_session, close_session, storage):
        self._storage = storage
        self._condition = Condition()
        self._pending = deque()
        self._pending_requests = 0
        self._ready = deque()
        self._writer_stopping = False
        self._rotated = self._clipped = False
        self._journal_error = ""
        self._writer = Thread(target=self._write_events, name="server-journal")
        self._writer.start()
        super().__init__(create_session, close_session)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._deliver)
        self._timer.start()
        self.stopped.connect(self._stop_delivering)

    def _emit(self, event):
        if not isinstance(event.payload, (RequestMetadata, ServerLifecycle)):
            self.event.emit(event)
            return
        with self._condition:
            self._pending.append(event)
            if isinstance(event.payload, RequestMetadata):
                self._pending_requests += 1
            if self._pending_requests > self.PENDING_REQUESTS:
                controls = []
                while self._pending:
                    oldest = self._pending.popleft()
                    if isinstance(oldest.payload, RequestMetadata):
                        self._pending_requests -= 1
                        break
                    controls.append(oldest)
                self._pending.extendleft(reversed(controls))
                self._journal_error = "请求过多，部分请求日志未保存"

    def _write_events(self):
        while True:
            with self._condition:
                if not self._writer_stopping and len(self._pending) < self.BATCH_SIZE:
                    self._condition.wait(self.FLUSH_SECONDS)
                if not self._pending and self._writer_stopping:
                    return
                events = tuple(self._pending.popleft() for _ in range(min(len(self._pending), self.BATCH_SIZE)))
                self._pending_requests -= sum(isinstance(event.payload, RequestMetadata) for event in events)
            if not events:
                continue
            logs = tuple(event for event in events if isinstance(event.payload, (RequestMetadata, ServerLifecycle)))
            rotations = ()
            error = ""
            try:
                if logs:
                    rotations = self._storage.append_events(logs)
            except (OSError, ValueError) as failure:
                error = f"保存请求日志失败 · {failure}"
            # A rotation invalidates earlier visible journal rows, but not controls.
            last_rotation = max((i for i, rotated in enumerate(rotations) if rotated), default=-1)
            if last_rotation >= 0:
                cutoff = [i for i, event in enumerate(events)
                          if isinstance(event.payload, (RequestMetadata, ServerLifecycle))][last_rotation]
                events = tuple(event for i, event in enumerate(events)
                               if i >= cutoff or not isinstance(event.payload, (RequestMetadata, ServerLifecycle)))
            with self._condition:
                if last_rotation >= 0:
                    self._ready = deque(event for event in self._ready
                                        if not isinstance(event.payload, (RequestMetadata, ServerLifecycle)))
                    self._rotated = True
                self._ready.extend(events)
                self._clipped |= _trim_requests(self._ready, VISIBLE_LOG_ROWS)
                if error:
                    self._journal_error = error

    @Slot()
    def _stop_delivering(self):
        self._timer.stop()
        self._deliver()

    @Slot()
    def _deliver(self):
        with self._condition:
            batch = ServerEventBatch(tuple(self._ready), self._rotated, self._clipped, self._journal_error)
            self._ready.clear()
            self._rotated = self._clipped = False
            self._journal_error = ""
        if batch.events or batch.error:
            self.events.emit(batch)

    def _cleanup(self):
        with self._condition:
            self._writer_stopping = True
            self._condition.notify_all()
        self._writer.join()
