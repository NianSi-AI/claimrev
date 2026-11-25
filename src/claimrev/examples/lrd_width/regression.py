"""Width-luminosity regression helpers.

Faithfully ported from NianSi-AI/lrd-width-luminosity
(commit 8e892e19b58189b239cc9d4a5fb0d813072e4b37, lrdwidth/regression.py).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import stats
from .stats import diffq, quant


def ols_sd(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Standard deviation of residuals about an OLS line, and its slope."""
    c = np.polyfit(x, y, 1)
    r = y - np.polyval(c, x)
    return (float(np.std(r, ddof=1)), float(c[0]))


def fit(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], tuple[np.ndarray, np.ndarray]]:
    """Width-luminosity fit of one sample."""
    x = np.array([r["x"] for r in rows], dtype=float)
    y = np.array([r["y"] for r in rows], dtype=float)
    C = np.array([r["cov"] for r in rows], dtype=float)
    b, s, p, _ = stats.post2(x, y, C[:, 0, 0], C[:, 1, 1], C[:, 0, 1])
    B = b[:, None]
    S = s[None, :]
    sw = 0.0
    swr = 0.0
    for xi, yi, ci in zip(x, y, C):
        V = S * S + ci[1, 1] + B * B * ci[0, 0] - 2.0 * B * ci[0, 1]
        sw = sw + 1.0 / V
        swr = swr + (yi - B * xi) / V
    a = float((p * swr / sw).sum())
    sd, sl = ols_sd(x, y)
    pm_s = p.sum(0)
    pm_b = p.sum(1)
    return (
        {
            "N": len(rows),
            "sigma": quant(s, pm_s),
            "sigma_upper95": quant(s, pm_s, qs=(0.95,))[0],
            "slope": quant(b, pm_b),
            "a": a,
            "ols_sd": sd,
            "ols_slope": sl,
        },
        (s, pm_s),
    )


def pair(L: list[dict[str, Any]], O: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit target (LRD) and comparison sources and return both plus quantiles of scatter difference (LRD - other)."""
    rl, ml = fit(L)
    ro, mo = fit(O)
    return {
        "LRD": rl,
        "other": ro,
        "diff": diffq(ml[0], ml[1], mo[1]),
    }
