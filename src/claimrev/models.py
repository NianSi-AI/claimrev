"""Canonical serialization, hashing, scope normalization, and metric validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ValidationError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_value(val: Any) -> Any:
    if isinstance(val, dict):
        return {str(k).strip(): _normalize_value(v) for k, v in sorted(val.items(), key=lambda kv: str(kv[0]).strip())}
    if isinstance(val, list):
        items = [_normalize_value(x) for x in val]
        if all(isinstance(x, str) for x in items):
            return sorted(items)
        return items
    if isinstance(val, bool):
        return val
    if isinstance(val, float) and val.is_integer():
        return int(val)
    if isinstance(val, str):
        return val.strip()
    return val


def normalize_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Normalize a structured claim scope for deterministic equality and diffing."""
    if not isinstance(scope, dict):
        raise ValidationError("scope must be a JSON object", details={"got_type": type(scope).__name__})
    return _normalize_value(scope)


def diff_scopes(old_scope: dict[str, Any], new_scope: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Return dotted-path differences between two normalized scopes."""
    old_n = normalize_scope(old_scope)
    new_n = normalize_scope(new_scope)
    diffs: dict[str, dict[str, Any]] = {}
    all_keys = sorted(set(old_n.keys()) | set(new_n.keys()))
    for k in all_keys:
        path = f"{prefix}.{k}" if prefix else k
        in_old = k in old_n
        in_new = k in new_n
        if in_old and not in_new:
            diffs[path] = {"old": old_n[k], "new": None}
        elif in_new and not in_old:
            diffs[path] = {"old": None, "new": new_n[k]}
        else:
            ov = old_n[k]
            nv = new_n[k]
            if isinstance(ov, dict) and isinstance(nv, dict):
                diffs.update(diff_scopes(ov, nv, prefix=path))
            elif ov != nv:
                diffs[path] = {"old": ov, "new": nv}
    return diffs


REQUIRED_METRIC_FIELDS = {
    "metric_key",
    "label",
    "value",
    "display_value",
    "unit",
    "stat_meaning",
    "sample_id",
    "sample_n",
    "algorithm_version",
}


def validate_metric_record(rec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rec, dict):
        raise ValidationError("metric record must be a dict")
    missing = sorted(REQUIRED_METRIC_FIELDS - set(rec.keys()))
    if missing:
        raise ValidationError(
            f"metric record missing required fields: {missing}",
            details={"missing_fields": missing, "metric_key": rec.get("metric_key")},
        )
    if not isinstance(rec["stat_meaning"], str) or not rec["stat_meaning"].strip():
        raise ValidationError("metric stat_meaning must be a non-empty string")
    if not isinstance(rec["unit"], str):
        raise ValidationError("metric unit must be a string")
    return rec
