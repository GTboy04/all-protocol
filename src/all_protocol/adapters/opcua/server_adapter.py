import asyncio
from datetime import datetime, timezone
from time import monotonic

from asyncua import Server, ua
from asyncua.common.callback import CallbackType

from ...application.contracts import ErrorKind
from ...application.contracts import ClientError
from ...application.server_contracts import RequestMetadata
from .server_model import UaServerPlan, UaValuesChanged, value_span


class UaServerAdapter:
    def __init__(self, plan, callbacks):
        if not isinstance(plan, UaServerPlan):
            raise ValueError("需要 OPC UA 模拟服务端方案")
        self.plan, self.callbacks = plan, callbacks
        self.server = None
        self.values = {}
        self._targets = {}
        self._writes = set()
        self._started = {}

    @property
    def endpoint(self):
        return self.plan.endpoint

    def validate_plan(self):
        self.plan.validate(starting=True)

    async def bind(self):
        self.server = server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        application_uri = f"urn:allprotocol:simulator:{self.plan.host}:{self.plan.port}"
        await server.set_application_uri(application_uri)
        await server.get_node(ua.ObjectIds.Server_ServerArray).write_value([application_uri])
        server.set_server_name("AllProtocol 本机 OPC UA 模拟端")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        server.set_identity_tokens([ua.AnonymousIdentityToken])
        index = await server.register_namespace(self.plan.namespace_uri)
        root = await server.nodes.objects.add_object(ua.NodeId("AllProtocol", index), "AllProtocol")
        for point in self.plan.points:
            value = point.encode(point.initial)
            nodeid = point.target.node_id(index)
            node = await root.add_variable(nodeid, point.name or point.target.identifier,
                                           ua.Variant(value, point.target.data_type.variant))
            if point.writable:
                await node.set_writable()
            self._targets[nodeid] = point.target
            self.values[point.target] = value
        server.subscribe_server_callback(CallbackType.PreRead, self._before)
        server.subscribe_server_callback(CallbackType.PreWrite, self._before)
        server.subscribe_server_callback(CallbackType.PostRead, self._read)
        server.subscribe_server_callback(CallbackType.PostWrite, self._written)
        await server.start()

    async def _before(self, event, _service):
        if event.is_external:
            self._started[id(event.request_params)] = monotonic()

    def _record(self, event, service, requests, results):
        if not event.is_external:
            return
        started = self._started.pop(id(event.request_params), monotonic())
        elapsed = round((monotonic() - started) * 1000)
        for item, result in zip(requests, results):
            status = result.StatusCode if isinstance(result, ua.DataValue) else result
            target = self._targets.get(item.NodeId)
            label = target.label if target else item.NodeId.to_string()
            self.callbacks.request(datetime.now(), RequestMetadata(
                "OPC UA 会话", "成功" if status.is_good() else status.name,
                details=(("service", service), ("target", label), ("status_code", status.value)),
                elapsed_ms=elapsed, error=None if status.is_good() else ErrorKind.PROTOCOL))

    async def _read(self, event, service):
        self._record(event, "Read", event.request_params.NodesToRead, event.response_params)

    async def _written(self, event, service):
        self._record(event, "Write", event.request_params.NodesToWrite, event.response_params)
        changes = []
        for item, status in zip(event.request_params.NodesToWrite, event.response_params):
            target = self._targets.get(item.NodeId)
            if target is None or not status.is_good() or item.AttributeId != ua.AttributeIds.Value:
                continue
            value = self.server.read_attribute_value(item.NodeId).Value.Value
            self.values[target] = value
            changes.append((target, value))
            if event.is_external and self.callbacks.written:
                self.callbacks.written(value_span(target))
        if changes:
            self.callbacks.changed(UaValuesChanged(tuple(changes)))

    def read_current(self, target):
        if self.server is None or target not in self.values:
            raise ValueError("需要正在监听的 OPC UA 模拟节点")
        return self.values[target]

    def write_current(self, target, value):
        self.read_current(target)
        try:
            value = target.prepare_write((value,))[0]
        except ClientError as error:
            raise ValueError(str(error)) from None
        nodeid = next(key for key, configured in self._targets.items() if configured == target)
        previous = self.values[target]
        self.values[target] = value
        self.callbacks.changed(UaValuesChanged(((target, value),)))
        if self.callbacks.written:
            self.callbacks.written(value_span(target))

        async def publish():
            try:
                now = datetime.now(timezone.utc)
                status = await self.server.write_attribute_value(nodeid, ua.DataValue(
                    ua.Variant(value, target.data_type.variant), SourceTimestamp=now, ServerTimestamp=now))
                if status is not None:
                    status.check()
            except Exception:
                self.values[target] = previous
                self.callbacks.changed(UaValuesChanged(((target, previous),)))
                self.callbacks.request(datetime.now(), RequestMetadata("本机", "模拟值发布失败",
                    details=(("service", "Write"), ("target", target.label)), error=ErrorKind.INTERNAL, elapsed_ms=0))

        task = asyncio.create_task(publish())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def shutdown(self):
        if self._writes:
            await asyncio.gather(*tuple(self._writes), return_exceptions=True)
        if self.server is not None:
            try:
                if self.server.bserver is not None:
                    await self.server.stop()
                else:
                    await self.server.iserver.stop()
            finally:
                self.server = None
        self.values.clear()
        self._targets.clear()
        self._started.clear()
