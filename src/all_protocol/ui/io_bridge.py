import asyncio
import logging
from collections.abc import Coroutine
from concurrent.futures import Future
from threading import Thread

from PySide6.QtCore import QObject, Signal

from ..application.session import Session


class IoBridge(QObject):
    event = Signal(object)
    stopped = Signal()

    def __init__(self, create_session=Session, close_session=Session.disconnect):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self.session = create_session(self.event.emit)
        self._close_session = close_session
        self._stopping = False
        self._thread = Thread(target=self._run, name="protocol-io")
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        finally:
            self._loop.close()
            self.stopped.emit()

    def submit(self, operation: Coroutine):
        if self._stopping:
            operation.close()
            return
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        future.add_done_callback(self._report_failure)

    def shutdown(self):
        if self._stopping:
            return
        self._stopping = True
        future = asyncio.run_coroutine_threadsafe(self._close_session(self.session), self._loop)
        future.add_done_callback(self._finish)

    def join(self):
        self.shutdown()
        self._thread.join()

    def _finish(self, future: Future):
        self._report_failure(future)
        self._loop.call_soon_threadsafe(self._loop.stop)

    @staticmethod
    def _report_failure(future: Future):
        if not future.cancelled() and (error := future.exception()) is not None:
            logging.error("Background task failed", exc_info=error)
