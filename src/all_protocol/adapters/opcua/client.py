"""OPC UA client with bounded browsing, explicit certificate pinning and native quality."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
import ipaddress
from time import monotonic
from urllib.parse import urlsplit

from asyncua import Client, ua
from asyncua.client.ua_client import UASocketProtocol
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ...application.contracts import ClientError, ErrorKind, Point, ValueMetadata
from .identity import IdentityStore, fingerprint, load_certificate
from .model import OpcUaConfig, UaBrowseTarget, UaReference, UaTarget, UaType


@dataclass(frozen=True, slots=True)
class BrowsePoint(Point):
    reference: UaReference | None = None


@dataclass(frozen=True, slots=True)
class EndpointDescription:
    url: str
    policy: str
    mode: str
    fingerprint: str
    application_uri: str
    anonymous: bool


class _Transport:
    def __init__(self, transport, owner):
        self.transport, self.owner = transport, owner

    def write(self, data):
        if self.owner._sending_write:
            self.owner._write_sent = True
        self.transport.write(data)
        self.owner.events.packet(True, bytes(data))

    def __getattr__(self, name):
        return getattr(self.transport, name)


class _Protocol(UASocketProtocol):
    def __init__(self, owner, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner

    def connection_made(self, transport):
        super().connection_made(_Transport(transport, self.owner))

    def data_received(self, data):
        self.owner.events.packet(False, bytes(data))
        super().data_received(data)

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if self.owner._online and not self.owner._closing:
            self.owner._online = False
            self.owner.events.disconnected()

    def _send_request(self, request, *args, **kwargs):
        self.owner._sending_write = isinstance(request, ua.WriteRequest)
        try:
            return super()._send_request(request, *args, **kwargs)
        finally:
            self.owner._sending_write = False


def _failure(error, *, connecting=False, write_sent=None):
    if isinstance(error, ClientError):
        if write_sent is not None:
            error.write_sent = write_sent
        return error
    causes, current = [], error
    while current is not None and current not in causes:
        causes.append(current)
        current = current.__cause__ or current.__context__
    if isinstance(error, ua.UaStatusCodeError):
        code = error.code
        status = ua.StatusCode(code)
        return ClientError(ErrorKind.CONNECT if connecting else ErrorKind.PROTOCOL,
                           f"OPC UA {status.name}: {status.doc}", code,
                           write_rejected=write_sent is True, write_sent=write_sent)
    if any(isinstance(item, TimeoutError) for item in causes):
        return ClientError(ErrorKind.TIMEOUT, "OPC UA 请求超时，未自动重试", write_sent=write_sent)
    if any(isinstance(item, (OSError, ConnectionError)) for item in causes):
        return ClientError(ErrorKind.CONNECT if connecting else ErrorKind.DISCONNECTED,
                           "OPC UA 连接失败或已中断", write_sent=write_sent)
    # Do not expose library exception text that might contain local key paths.
    return ClientError(ErrorKind.CONNECT if connecting else ErrorKind.RESPONSE,
                       "OPC UA 连接或响应无效，请检查端点、证书、节点类型与服务端状态", write_sent=write_sent)


class OpcUaClient:
    def __init__(self, config: OpcUaConfig, events, identity_store=None):
        config.validate()
        self.config, self.events = config, events
        self.identities = identity_store or IdentityStore()
        self.client = Client(config.url, timeout=config.timeout_ms / 1000)
        self.client.name = "AllProtocol"
        self.client.description = "AllProtocol OPC UA Client"
        self._online = self._closing = self._sending_write = self._write_sent = False
        self._lock = asyncio.Lock()
        self._namespaces = ()
        self._last_values = {}
        self._subscriptions = set()

        def make_protocol():
            protocol = _Protocol(self, self.client.uaclient._timeout,
                                 security_policy=self.client.uaclient.security_policy)
            protocol.pre_request_hook = self.client.uaclient._pre_request_hook
            self.client.uaclient.protocol = protocol
            return protocol

        # asyncua 1.1 transport factory; the wire codec and request correlation remain in the library.
        self.client.uaclient._make_protocol = make_protocol

    async def _security(self):
        if self.config.security == "None/None":
            return
        identity = self.identities.load(self.config.identity_id)
        try:
            trusted = load_certificate(identity.trusted_server)
            trusted_der = trusted.public_bytes(serialization.Encoding.DER)
            validator = CertificateValidator(CertificateValidatorOptions.EXT_VALIDATION
                                             | CertificateValidatorOptions.PEER_SERVER)
            hostname = urlsplit(self.config.url).hostname

            async def validate(cert, description):
                if cert.public_bytes(serialization.Encoding.DER) != trusted_der:
                    raise ua.UaStatusCodeError(ua.StatusCodes.BadCertificateUntrusted)
                await validator.validate(cert, description)
                san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                try:
                    address = ipaddress.ip_address(hostname)
                except ValueError:
                    # Exact DNS names only; wildcard identities are intentionally not accepted.
                    matched = hostname.lower() in [item.lower() for item in san.get_values_for_type(x509.DNSName)]
                else:
                    matched = address in san.get_values_for_type(x509.IPAddress)
                if not matched:
                    raise ua.UaStatusCodeError(ua.StatusCodes.BadCertificateHostNameInvalid)

            self.client.application_uri = identity.application_uri
            self.client.certificate_validator = validate
            await self.client.set_security(SecurityPolicyBasic256Sha256, identity.certificate, identity.private_key,
                                           server_certificate=trusted_der, mode=ua.MessageSecurityMode.SignAndEncrypt)
        except ClientError:
            raise
        except Exception as error:
            raise _failure(error, connecting=True) from None

    async def connect(self):
        if self._online:
            return
        self._closing = False
        try:
            async with asyncio.timeout(self.config.timeout_ms / 1000):
                await self._security()
                await self.client.connect()
                self._namespaces = tuple(await self.client.get_namespace_array())
                self._online = True
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise _failure(error, connecting=True) from None

    async def close(self):
        self._closing, self._online = True, False
        try:
            if self._subscriptions:
                await asyncio.gather(*(subscription.close() for subscription in tuple(self._subscriptions)))
            # CloseSession cancels asyncua's renewal/watchdog tasks, including partial connects.
            async with asyncio.timeout(2):
                if self.client.uaclient.protocol is not None:
                    await self.client.disconnect()
        except Exception:
            pass
        finally:
            self.client.disconnect_socket()
            self.client.uaclient._subscription_callbacks.clear()
            self._namespaces = ()
            self._last_values.clear()

    @property
    def write_sent(self):
        return self._write_sent

    def function(self, command):
        return "Browse" if isinstance(command.target, UaBrowseTarget) else "Read" if command.values is None else "Write"

    def _node(self, target):
        if not isinstance(target, UaTarget):
            raise ClientError(ErrorKind.INPUT, "需要 OPC UA 节点目标")
        target.validate()
        if not self._online:
            raise ClientError(ErrorKind.DISCONNECTED, "OPC UA 会话未连接")
        try:
            index = self._namespaces.index(target.namespace_uri)
        except ValueError:
            raise ClientError(ErrorKind.INPUT, "服务端未发布目标命名空间 URI") from None
        return self.client.get_node(target.node_id(index))

    async def read(self, target):
        async with self._lock:
            try:
                async with asyncio.timeout(self.config.timeout_ms / 1000):
                    if isinstance(target, UaBrowseTarget):
                        return await self._browse(target.node)
                    node = self._node(target)
                    value = await node.read_data_value(raise_on_bad_status=False)
                    return (self._point(target, value),)
            except Exception as error:
                raise _failure(error) from None

    def _point(self, target, value):
        status = value.StatusCode
        quality = "Good" if status.is_good() else "Bad" if status.is_bad() else "Uncertain"
        metadata = ValueMetadata(status.value, status.name, quality, value.SourceTimestamp, value.ServerTimestamp)
        scalar = value.Value
        if quality == "Good":
            if scalar is None or scalar.is_array or scalar.VariantType != target.data_type.variant:
                raise ClientError(ErrorKind.RESPONSE, f"响应须为 {target.data_type} 标量；请浏览并选择正确类型")
            try:
                result = target.data_type.prepare(scalar.Value)
            except ClientError:
                raise ClientError(ErrorKind.RESPONSE, "服务端返回了超出类型范围或非有限的标量") from None
            self._last_values[target] = result
        else:
            result = self._last_values.get(target, "—")
        return Point(target.label, str(target.data_type), result, metadata=metadata)

    async def subscribe(self, targets, interval_ms, queue_size, notify):
        async with self._lock:
            subscription = UaSubscription(self, targets, notify)
            self._subscriptions.add(subscription)
            try:
                async with asyncio.timeout(self.config.timeout_ms / 1000):
                    await subscription.start(interval_ms, queue_size)
                return subscription
            except BaseException:
                await subscription.close()
                if subscription.creation_started and subscription.subscription_id is None:
                    await self.close()
                    self.events.disconnected()
                raise

    async def write(self, target, values):
        async with self._lock:
            self._write_sent = False
            try:
                values = target.prepare_write(values)
                async with asyncio.timeout(self.config.timeout_ms / 1000):
                    node = self._node(target)
                    if ua.AccessLevel.CurrentWrite not in await node.get_user_access_level():
                        raise ClientError(ErrorKind.INPUT, "节点对当前用户只读，写入未发送")
                    if await node.read_data_type_as_variant_type() != target.data_type.variant:
                        raise ClientError(ErrorKind.INPUT, "节点数据类型与写入类型不一致，写入未发送")
                    await node.write_value(ua.Variant(values[0], target.data_type.variant))
            except Exception as error:
                raise _failure(error, write_sent=self._write_sent) from None

    async def _browse(self, target):
        node = self._node(target)
        params = ua.BrowseParameters()
        desc = ua.BrowseDescription()
        desc.NodeId = node.nodeid
        desc.BrowseDirection = ua.BrowseDirection.Forward
        desc.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
        desc.IncludeSubtypes = True
        desc.ResultMask = ua.BrowseResultMask.All
        params.NodesToBrowse = [desc]
        params.RequestedMaxReferencesPerNode = 100
        result = (await self.client.uaclient.browse(params))[0]
        references = []
        continuation = result.ContinuationPoint
        try:
            while True:
                result.StatusCode.check()
                references.extend(result.References)
                if len(references) > 2000:
                    raise ClientError(ErrorKind.RESPONSE, "此层节点超过 2000 个，请指定更具体的父节点")
                if not continuation:
                    break
                next_params = ua.BrowseNextParameters(ReleaseContinuationPoints=False, ContinuationPoints=[continuation])
                result = (await self.client.uaclient.browse_next(next_params))[0]
                continuation = result.ContinuationPoint
        finally:
            if continuation:
                try:
                    await self.client.uaclient.browse_next(ua.BrowseNextParameters(
                        ReleaseContinuationPoints=True, ContinuationPoints=[continuation]))
                except Exception:
                    pass
        # NamespaceArray can grow during a session. Refresh before converting references.
        self._namespaces = tuple(await self.client.get_namespace_array())
        points = []
        variables = [ref for ref in references if ref.NodeClass == ua.NodeClass.Variable and not getattr(ref.NodeId, "ServerIndex", 0)]
        descriptions = {}
        for start in range(0, len(variables), 50):
            batch = variables[start:start + 50]
            attrs = (ua.AttributeIds.DataType, ua.AttributeIds.ValueRank, ua.AttributeIds.UserAccessLevel)
            reads = ua.ReadParameters(NodesToRead=[ua.ReadValueId(NodeId_=ref.NodeId, AttributeId=attr)
                                                  for ref in batch for attr in attrs])
            values = await self.client.uaclient.read(reads)
            for index, ref in enumerate(batch):
                dtype, rank, access = values[index * 3:index * 3 + 3]
                if (dtype.StatusCode.is_good() and rank.StatusCode.is_good() and dtype.Value and rank.Value
                        and rank.Value.Value == -1 and isinstance(dtype.Value.Value, ua.NodeId)
                        and dtype.Value.Value.NamespaceIndex == 0):
                    try:
                        kind = UaType(ua.VariantType(dtype.Value.Value.Identifier).name)
                    except ValueError:
                        continue
                    writable = bool(access.StatusCode.is_good() and access.Value
                                    and access.Value.Value & (1 << ua.AccessLevel.CurrentWrite))
                    descriptions[ref.NodeId] = kind.value, writable
        for ref in references:
            nodeid = ref.NodeId
            if getattr(nodeid, "ServerIndex", 0) or nodeid.NamespaceIndex >= len(self._namespaces):
                continue  # Remote-server references need a different connection.
            kind = {ua.NodeIdType.TwoByte: "i", ua.NodeIdType.FourByte: "i", ua.NodeIdType.Numeric: "i",
                    ua.NodeIdType.String: "s", ua.NodeIdType.Guid: "g", ua.NodeIdType.ByteString: "b"}[nodeid.NodeIdType]
            identifier = nodeid.Identifier.hex() if kind == "b" else str(nodeid.Identifier)
            uri = getattr(nodeid, "NamespaceUri", None) or self._namespaces[nodeid.NamespaceIndex]
            dtype, writable = descriptions.get(nodeid, ("", False))
            reference = UaReference(ref.DisplayName.Text or ref.BrowseName.Name, ref.NodeClass.name, uri, kind, identifier,
                                    dtype, writable)
            points.append(BrowsePoint(f"{uri} · {kind}={identifier}", reference.name, ref.NodeClass.name,
                                      reference=reference))
        return tuple(points)


async def discover_endpoints(url, timeout_ms=3000):
    config = OpcUaConfig(url, timeout_ms=timeout_ms)
    config.validate()
    client = Client(url, timeout=timeout_ms / 1000)
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            endpoints = await client.connect_and_get_server_endpoints()
        return tuple(EndpointDescription(
            item.EndpointUrl, item.SecurityPolicyUri.rsplit("#", 1)[-1], item.SecurityMode.name,
            fingerprint(x509.load_der_x509_certificate(item.ServerCertificate)) if item.ServerCertificate else "",
            item.Server.ApplicationUri,
            any(token.TokenType == ua.UserTokenType.Anonymous for token in item.UserIdentityTokens),
        ) for item in endpoints)
    except Exception as error:
        raise _failure(error, connecting=True) from None
    finally:
        client.disconnect_socket()


class UaSubscription:
    """One publish stream; callbacks are fenced before any asynchronous deletion."""

    def __init__(self, owner, targets, notify):
        self.owner, self.targets, self.notify = owner, tuple(targets), notify
        self.subscription_id = None
        self.active = True
        self.items = {}
        self.publishing_ms = 0
        self.creation_started = False
        self.health_callback = None
        self.last_publish_at = self.last_notification_at = None
        self._last_publish_tick = monotonic()
        self.max_silence_ms = 30000
        self.health_detail = "等待发布"
        self._health_task = None
        self._status_good = True

    def set_health_callback(self, callback):
        self.health_callback = callback
        self._report_health()

    def _report_health(self):
        alive = self._status_good and (monotonic() - self._last_publish_tick) * 1000 < self.max_silence_ms
        if self.health_callback is not None:
            self.health_callback(alive, self.last_publish_at, self.last_notification_at,
                                 self.max_silence_ms, self.health_detail if alive else "订阅发布异常或超时")

    async def _watch_health(self):
        try:
            while self.active:
                await asyncio.sleep(max(.2, min(5, self.max_silence_ms / 3000)))
                self._report_health()
        except asyncio.CancelledError:
            pass

    async def start(self, interval_ms, queue_size):
        nodes = [self.owner._node(target) for target in self.targets]
        params = ua.CreateSubscriptionParameters()
        params.RequestedPublishingInterval = interval_ms
        params.RequestedMaxKeepAliveCount = 10
        params.RequestedLifetimeCount = 1000
        params.MaxNotificationsPerPublish = 1000
        params.PublishingEnabled = True
        self.creation_started = True
        result = await self.owner.client.uaclient.create_subscription(params, self.publish)
        self.subscription_id = result.SubscriptionId
        self.creation_started = False
        self.publishing_ms = result.RevisedPublishingInterval
        self.max_silence_ms = max(1000, self.publishing_ms * result.RevisedMaxKeepAliveCount * 1.5)
        self._last_publish_tick = monotonic()
        monitored = ua.CreateMonitoredItemsParameters()
        monitored.SubscriptionId = self.subscription_id
        monitored.TimestampsToReturn = ua.TimestampsToReturn.Both
        for handle, node in enumerate(nodes, 1):
            item = ua.MonitoredItemCreateRequest()
            item.ItemToMonitor.NodeId = node.nodeid
            item.ItemToMonitor.AttributeId = ua.AttributeIds.Value
            item.MonitoringMode = ua.MonitoringMode.Reporting
            item.RequestedParameters.ClientHandle = handle
            item.RequestedParameters.SamplingInterval = interval_ms
            item.RequestedParameters.QueueSize = queue_size
            item.RequestedParameters.DiscardOldest = True
            monitored.ItemsToCreate.append(item)
            self.items[handle] = (interval_ms, queue_size)
        results = await self.owner.client.uaclient.create_monitored_items(monitored)
        for handle, item in enumerate(results, 1):
            item.StatusCode.check()
            self.items[handle] = (item.RevisedSamplingInterval, item.RevisedQueueSize)
        self._health_task = asyncio.create_task(self._watch_health())

    async def publish(self, result):
        if not self.active or not self.owner._online:
            return
        self._last_publish_tick = monotonic()
        self.last_publish_at = datetime.now()
        self.health_detail = "订阅保活正常"
        for notification in result.NotificationMessage.NotificationData or ():
            if isinstance(notification, ua.StatusChangeNotification):
                self._status_good = notification.Status.is_good()
                self.health_detail = f"订阅状态 {notification.Status.name}"
                for index in range(len(self.targets)):
                    self.notify(index, None, f"订阅状态 {notification.Status.name}")
                continue
            if not isinstance(notification, ua.DataChangeNotification):
                continue
            for item in notification.MonitoredItems:
                if not self.active or item.ClientHandle not in self.items:
                    continue
                index = item.ClientHandle - 1
                self.last_notification_at = datetime.now()
                self.health_detail = "订阅发布正常"
                sampling, queue = self.items[item.ClientHandle]
                overflow = bool(item.Value.StatusCode.value & 0x80)
                detail = (f"订阅 #{self.subscription_id} · 发布 {self.publishing_ms:g} ms · "
                          f"采样 {sampling:g} ms · 队列 {queue}" + (" · 队列溢出" if overflow else ""))
                try:
                    point = self.owner._point(self.targets[index], item.Value)
                except ClientError as error:
                    self.notify(index, None, str(error))
                else:
                    self.notify(index, point, detail)
        self._report_health()

    async def close(self):
        self.active = False
        if self._health_task is not None:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
            self._health_task = None
        self.owner._subscriptions.discard(self)
        if self.subscription_id is None:
            return
        subscription_id, self.subscription_id = self.subscription_id, None
        try:
            async with asyncio.timeout(1):
                await self.owner.client.uaclient.delete_subscriptions([subscription_id])
        except Exception:
            pass
        finally:
            self.owner.client.uaclient._subscription_callbacks.pop(subscription_id, None)
