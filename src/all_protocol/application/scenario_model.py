"""Portable, station-relative scenario definitions. No Qt or protocol I/O."""
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from .contracts import Value
from .simulation import MAX_TIME_MS

MAX_SOURCE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PointRef:
    area: str
    offset: int

    def __post_init__(self):
        if self.area not in ("HR", "IR", "Coil", "DI") or type(self.offset) is not int or not 0 <= self.offset <= 65535:
            raise ValueError("点位须使用 HR/IR/Coil/DI 和 0–65535 的起始偏移")


@dataclass(frozen=True, slots=True)
class ScenarioValue:
    target: PointRef
    value: Value
    value_source: PointRef | None = None


@dataclass(frozen=True, slots=True)
class SequenceStep:
    at_ms: int
    values: tuple[ScenarioValue, ...]


@dataclass(frozen=True, slots=True)
class ConditionalRule:
    id: str
    source: PointRef | None
    value: Value
    steps: tuple[SequenceStep, ...]
    op: str = "eq"
    completion: str = "hold"
    abs_tol: float | None = None
    rel_tol: float | None = None
    fire_initial: bool = False
    cancel_on_false: bool = False


@dataclass(frozen=True, slots=True)
class ScenarioExtension:
    id: str
    name: str
    rules: tuple[ConditionalRule, ...]
    mode: str = "form"
    source: str = ""
    enabled: bool = True
    sdk_version: int = 1
    # Freeze the codec of every dependency; Unit ID remains relative.
    signatures: tuple[tuple[str, int, str, str, int], ...] = ()
    checked_digest: str = ""

    def copied(self):
        return replace(self, id=str(uuid4()), enabled=False, checked_digest="")


@dataclass(frozen=True, slots=True)
class PointCommit:
    sequence: int
    changes: tuple[tuple[object, Value, Value], ...]


@dataclass(frozen=True, slots=True)
class ScenarioEvent:
    unit_id: int
    extension_id: str
    rule_id: str
    run_id: int
    action: str
    result: str
    step: int | None = None
    planned_ms: int | None = None
    actual_ms: int | None = None
    commit_sequence: int | None = None
    source: str = ""
    source_before: Value | None = None
    source_after: Value | None = None
    session_id: str = ""
    station_generation: int = 0


def extension_record(extension):
    record = asdict(extension)
    # Keep existing fixed-value records and their checked/trusted digests stable.
    for rule in record["rules"]:
        for step in rule["steps"]:
            for item in step["values"]:
                if item["value_source"] is None:
                    item.pop("value_source")
    return record


def extension_format_version(extension):
    return 2 if any(rule.op == "always" or any(item.value_source is not None for step in rule.steps for item in step.values)
                    for rule in extension.rules) else 1


def extension_digest(extension):
    record = extension_record(extension)
    for key in ("id", "name", "enabled", "checked_digest"):
        record.pop(key)
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def decode_rules(records):
    try:
        if not isinstance(records, (list, tuple)) or len(records) > 100:
            raise ValueError("每个从站最多 100 条联动规则")
        return tuple(ConditionalRule(**(rule | {
            "source": PointRef(**rule["source"]) if rule["source"] is not None else None,
            "steps": tuple(SequenceStep(step["at_ms"], tuple(
                ScenarioValue(PointRef(**item["target"]), item["value"],
                              PointRef(**item["value_source"]) if item.get("value_source") is not None else None)
                for item in step["values"]
            )) for step in rule["steps"]),
        })) for rule in records)
    except (KeyError, TypeError) as error:
        raise ValueError("条件联动格式无效") from error


def decode_extension(record):
    try:
        extension = ScenarioExtension(**(record | {
            "rules": decode_rules(record["rules"]),
            "signatures": tuple(tuple(item) for item in record.get("signatures", ())),
        }))
        if (not all(isinstance(s, str) for s in (extension.id, extension.name, extension.source, extension.checked_digest))
                or not extension.id or not extension.name.strip() or extension.mode not in ("form", "python")
                or type(extension.enabled) is not bool or type(extension.sdk_version) is not int or extension.sdk_version != 1
                or len(extension.source.encode()) > MAX_SOURCE_BYTES):
            raise ValueError("扩展名称、模式、版本或源码无效")
        extension_digest(extension)
        if len(json.dumps(extension_record(extension), ensure_ascii=False, allow_nan=False).encode()) > MAX_MANIFEST_BYTES:
            raise ValueError("扩展定义超过 1 MiB")
        return extension
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("场景扩展格式无效") from error


def _finite(value):
    if type(value) not in (int, float, bool) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("条件和动作值必须为有限数值")


def dependency_refs(extension):
    refs = {r.source for r in extension.rules if r.op != "always"}
    for rule in extension.rules:
        for step in rule.steps:
            for item in step.values:
                refs.add(item.target)
                if item.value_source is not None:
                    refs.add(item.value_source)
    return refs


def validate_extensions(station, *, require_checked=True):
    points = {PointRef(str(p.area), p.offset): p for p in station.points}
    occupied, ids, count = [], set(), 0
    for extension in station.extensions:
        decode_extension(extension_record(extension))
        if not extension.rules:
            raise ValueError("扩展至少需要一条条件联动规则")
        if extension.id in ids:
            raise ValueError("扩展 ID 重复")
        ids.add(extension.id)
        refs, rule_ids = set(), set()
        for rule in extension.rules:
            count += 1
            if (not isinstance(rule.id, str) or not rule.id or rule.id in rule_ids
                    or rule.op not in ("eq", "ne", "gt", "ge", "lt", "le", "always") or rule.completion not in ("hold", "release")
                    or type(rule.fire_initial) is not bool or type(rule.cancel_on_false) is not bool):
                raise ValueError("规则 ID、条件或完成策略无效")
            rule_ids.add(rule.id)
            if rule.op != "always":
                source = points.get(rule.source)
                if source is None:
                    raise ValueError(f"Unit {station.unit_id} / {rule.source}：触发点位不存在")
                refs.add(rule.source)
                _finite(rule.value)
                source.encode(rule.value)
                if source.value_type.floating and rule.op in ("eq", "ne"):
                    if rule.abs_tol is None or rule.rel_tol is None:
                        raise ValueError("浮点相等比较必须填写 abs_tol 和 rel_tol")
            for tolerance in (rule.abs_tol, rule.rel_tol):
                if tolerance is not None:
                    _finite(tolerance)
                    if tolerance < 0:
                        raise ValueError("浮点容差不能为负数")
            if not rule.steps or len(rule.steps) > 1000:
                raise ValueError("每条规则需要 1–1000 个动作时刻")
            previous, outputs = -1, set()
            for step in rule.steps:
                if (type(step.at_ms) is not int or not 0 <= step.at_ms <= MAX_TIME_MS
                        or 0 < step.at_ms < 20 or step.at_ms <= previous or not step.values):
                    raise ValueError("动作时刻须严格递增，0 表示立即，正延时至少 20 ms")
                previous = step.at_ms
                seen = set()
                for item in step.values:
                    point = points.get(item.target)
                    if point is None:
                        raise ValueError(f"Unit {station.unit_id} / {item.target}：输出点位不存在")
                    if item.target in seen:
                        raise ValueError("同一步骤重复写入点位")
                    seen.add(item.target)
                    refs.add(item.target)
                    outputs.add(item.target)
                    if item.value_source is None:
                        _finite(item.value)
                        point.encode(item.value)
                    else:
                        value_point = points.get(item.value_source)
                        if value_point is None:
                            raise ValueError(f"Unit {station.unit_id} / {item.value_source}：取值点位不存在")
                        refs.add(item.value_source)
                        # Runtime values are validated again as one atomic batch.
                        point.encode(value_point.decode(value_point.encode(value_point.initial)))
            if extension.enabled:
                for ref in outputs:
                    point = points[ref]
                    if any(ref.area == area and point.offset < end and start < point.addresses.stop
                           for area, start, end in occupied):
                        raise ValueError(f"Unit {station.unit_id} / {ref.area} / {ref.offset}：扩展输出范围冲突")
                    occupied.append((ref.area, point.offset, point.addresses.stop))
        signature = tuple(sorted((ref.area, ref.offset, str(points[ref].value_type), str(points[ref].order),
                                  points[ref].value_type.words) for ref in refs))
        if extension.signatures and extension.signatures != signature:
            raise ValueError(f"{extension.name}：依赖点位类型或字序已改变，请先移除相关扩展引用")
        if require_checked and extension.enabled and extension.checked_digest != extension_digest(extension):
            raise ValueError(f"{extension.name}：配置已变化，请重新检查并应用扩展")
    if count > 100:
        raise ValueError("每个从站最多 100 条联动规则")


def check_extension(station, extension):
    unchecked = replace(extension, signatures=(), checked_digest="")
    validate_extensions(replace(station, extensions=(unchecked,)), require_checked=False)
    refs = dependency_refs(extension)
    points = {PointRef(str(p.area), p.offset): p for p in station.points}
    checked = replace(unchecked, signatures=tuple(sorted(
        (ref.area, ref.offset, str(points[ref].value_type), str(points[ref].order), points[ref].value_type.words)
        for ref in refs)))
    return replace(checked, checked_digest=extension_digest(checked))


def matches(rule, value):
    if rule.op == "always":
        return True
    if type(value) is float and not math.isfinite(value):
        return False
    equal = math.isclose(value, rule.value, abs_tol=rule.abs_tol or 0, rel_tol=rule.rel_tol or 0)
    if type(value) is not float and type(rule.value) is not float:
        equal = value == rule.value
    return {"eq": lambda: equal, "ne": lambda: not equal, "gt": lambda: value > rule.value,
            "ge": lambda: value >= rule.value, "lt": lambda: value < rule.value,
            "le": lambda: value <= rule.value}[rule.op]()
