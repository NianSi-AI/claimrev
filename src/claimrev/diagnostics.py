"""Isolated diagnostic script runner for agent-authored verification & counterexamples.

Allows an agent to write and run novel Python checks on an archived data batch
when facing an unfamiliar critique, while the host archives the exact script source
(`DIAGNOSTIC_CODE`) and verified output (`METRICS_JSON`) with SHA-256 provenance.

Trust boundary note: In single-account local mode, execution isolation uses a fresh
temporary directory + timeout + immutable host-archived inputs/outputs (documented
as product-interface enforcement, not OS-container sandboxing).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .models import validate_metric_record


@dataclass(frozen=True)
class DiagnosticRunResult:
    script_source: str
    exit_code: int
    duration_sec: float
    stdout: str
    stderr: str
    metrics: dict[str, dict[str, Any]]
    summary: dict[str, Any]


def execute_diagnostic_script(
    *,
    script_source: str,
    input_path: Path,
    series_id: str,
    batch_id: str,
    input_sha256: str,
    timeout_sec: int = 30,
) -> DiagnosticRunResult:
    """Execute an agent-authored Python diagnostic script in a fresh temp directory.

    Contract passed to script via environment variables:
      - CLAIMREV_DATA_PATH: absolute path to the verified archived input data file (read-only by convention)
      - CLAIMREV_SERIES_ID, CLAIMREV_BATCH_ID, CLAIMREV_INPUT_SHA256
      - CLAIMREV_OUTPUT_JSON: path where the script must write JSON:
          {"metrics": {<key>: {metric_key, label, value, display_value, unit, stat_meaning, sample_id, sample_n}},
           "summary": {...}}
    """
    if not script_source or not script_source.strip():
        raise ValidationError("Diagnostic script_source must be non-empty")
    if timeout_sec < 1 or timeout_sec > 120:
        raise ValidationError("timeout_sec must be between 1 and 120")

    with tempfile.TemporaryDirectory(prefix="claimrev_diag_") as tmpdir:
        tmp = Path(tmpdir)
        script_file = tmp / "diagnostic.py"
        output_file = tmp / "metrics.json"
        script_file.write_text(script_source, encoding="utf-8")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp),
            "TMPDIR": str(tmp),
            "PYTHONNOUSERSITE": "1",
            "CLAIMREV_DATA_PATH": str(input_path),
            "CLAIMREV_SERIES_ID": series_id,
            "CLAIMREV_BATCH_ID": batch_id,
            "CLAIMREV_INPUT_SHA256": input_sha256,
            "CLAIMREV_OUTPUT_JSON": str(output_file),
        }

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(script_file)],
                cwd=str(tmp),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(
                f"Diagnostic script timed out after {timeout_sec}s",
                details={"timeout_sec": timeout_sec, "stdout": (exc.stdout or "")[:2000]},
            )
        elapsed = round(time.monotonic() - t0, 3)

        if proc.returncode != 0:
            raise ValidationError(
                f"Diagnostic script exited with code {proc.returncode}",
                details={
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout[:4000],
                    "stderr": proc.stderr[:4000],
                },
            )

        if not output_file.is_file():
            raise ValidationError(
                "Diagnostic script succeeded (exit 0) but did not write CLAIMREV_OUTPUT_JSON",
                details={"stdout": proc.stdout[:4000], "stderr": proc.stderr[:2000]},
            )

        try:
            out_data = json.loads(output_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"CLAIMREV_OUTPUT_JSON is not valid JSON: {exc}",
                details={"stdout": proc.stdout[:2000]},
            )

        if not isinstance(out_data, dict):
            raise ValidationError("CLAIMREV_OUTPUT_JSON must be a JSON object")

        raw_metrics = out_data.get("metrics")
        if not isinstance(raw_metrics, dict) or not raw_metrics:
            raise ValidationError(
                "CLAIMREV_OUTPUT_JSON must include a non-empty 'metrics' dict",
                details={"keys": sorted(out_data.keys())},
            )

        validated_metrics: dict[str, dict[str, Any]] = {}
        for key, rec in raw_metrics.items():
            if not isinstance(rec, dict):
                raise ValidationError(f"metrics[{key!r}] must be an object")
            enriched = dict(rec)
            enriched.setdefault("metric_key", key)
            enriched.setdefault("algorithm_version", "agent_diagnostic_script")
            enriched.setdefault("sample_id", f"{series_id}:{batch_id}")
            validate_metric_record(enriched)
            if enriched["metric_key"] != key:
                raise ValidationError(
                    f"Metric key mismatch: dict key {key!r} != metric_key {enriched['metric_key']!r}"
                )
            validated_metrics[key] = enriched

        summary = out_data.get("summary")
        if not isinstance(summary, dict):
            summary = {}

        return DiagnosticRunResult(
            script_source=script_source,
            exit_code=proc.returncode,
            duration_sec=elapsed,
            stdout=proc.stdout[:8000],
            stderr=proc.stderr[:4000],
            metrics=validated_metrics,
            summary=summary,
        )
