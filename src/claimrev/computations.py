"""Generic pluggable computation registry for claimrev core.

Core never embeds domain-specific math or datasets; example/domain plugins register
named controlled computations here.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import NotFoundError, ValidationError
from .models import validate_metric_record


@dataclass(frozen=True)
class ComputationInput:
    """Inputs handed by core to a registered computation."""

    task_id: str
    series_id: str
    batch_id: str
    input_path: Path
    input_sha256: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComputationOutput:
    """Structured output returned by a registered computation and archived by core."""

    algorithm_version: str
    run_scope: dict[str, Any]
    metrics: dict[str, dict[str, Any]]
    summary: dict[str, Any]
    raw_output: dict[str, Any] = field(default_factory=dict)


ComputationFn = Callable[[ComputationInput], ComputationOutput]


@dataclass(frozen=True)
class ComputationSpec:
    name: str
    description: str
    algorithm_version: str
    param_schema: dict[str, Any]
    fn: ComputationFn


class ComputationRegistry:
    """Registry of named controlled computation plugins."""

    def __init__(self) -> None:
        self._specs: dict[str, ComputationSpec] = {}

    def register(self, spec: ComputationSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ComputationSpec:
        if name not in self._specs:
            raise NotFoundError(
                f"Unknown computation: {name!r}",
                details={"computation_name": name, "available": sorted(self._specs.keys())},
            )
        return self._specs[name]

    def list_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "algorithm_version": s.algorithm_version,
                "param_schema": s.param_schema,
            }
            for s in sorted(self._specs.values(), key=lambda x: x.name)
        ]

    def execute(self, name: str, comp_input: ComputationInput) -> ComputationOutput:
        spec = self.get(name)
        out = spec.fn(comp_input)
        if not isinstance(out, ComputationOutput):
            raise ValidationError(f"Computation {name!r} did not return ComputationOutput")
        for key, rec in out.metrics.items():
            validate_metric_record(rec)
            if rec["metric_key"] != key:
                raise ValidationError(
                    f"Metric key mismatch: dict key {key!r} != metric_key {rec['metric_key']!r}"
                )
        return out

    def load_plugin_spec(self, import_spec: str) -> None:
        """Load a plugin module:callable spec (e.g. 'pkg.mod:register') into this registry."""
        if ":" in import_spec:
            mod_name, attr = import_spec.split(":", 1)
        else:
            mod_name, attr = import_spec, "register"
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr)
        fn(self)
