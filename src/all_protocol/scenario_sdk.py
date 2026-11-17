"""Scenario definition SDK v1. build(scene) creates data, never runtime callbacks."""
from dataclasses import dataclass

from .application.scenario_model import ConditionalRule, PointRef, ScenarioValue, SequenceStep


@dataclass(frozen=True)
class Step:
    at_ms: int
    values: dict


class _Sequence:
    def __init__(self, scene, fields):
        self.scene, self.fields = scene, fields

    def sequence(self, steps):
        # Merge simultaneous batches, rejecting duplicate outputs rather than choosing a winner.
        merged = []
        for step in steps:
            values = tuple(ScenarioValue(target, 0, value) if isinstance(value, PointRef) else ScenarioValue(target, value)
                           for target, value in step.values.items())
            if merged and merged[-1].at_ms == step.at_ms:
                merged[-1] = SequenceStep(step.at_ms, merged[-1].values + values)
            else:
                merged.append(SequenceStep(step.at_ms, values))
        self.scene.rules.append(ConditionalRule(**self.fields, steps=tuple(merged)))


class Scene:
    def __init__(self):
        self.rules = []

    def point(self, area, offset):
        return PointRef(area, offset)

    def always(self, *, id, completion="hold"):
        return _Sequence(self, dict(id=id, source=None, value=0, op="always", completion=completion))

    def when_equal(self, *, id, source, value, edge="enter", busy="ignore", completion="hold",
                   abs_tol=None, rel_tol=None, fire_initial=False, cancel_on_false=False):
        if edge != "enter" or busy != "ignore":
            raise ValueError("SDK v1 支持 edge='enter' 和 busy='ignore'")
        return _Sequence(self, dict(id=id, source=source, value=value, completion=completion,
                                   abs_tol=abs_tol, rel_tol=rel_tol, fire_initial=fire_initial,
                                   cancel_on_false=cancel_on_false))
