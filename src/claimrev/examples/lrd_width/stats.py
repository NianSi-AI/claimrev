"""Grid posteriors and summary statistics.

Faithfully ported from NianSi-AI/lrd-width-luminosity
(commit 8e892e19b58189b239cc9d4a5fb0d813072e4b37, lrdwidth/stats.py).
"""

from __future__ import annotations

import math

import numpy as np

ALGORITHM_COMMIT = "8e892e19b58189b239cc9d4a5fb0d813072e4b37"
ALGORITHM_VERSION = f"lrdwidth@{ALGORITHM_COMMIT[:12]}"

_erf = np.vectorize(math.erf)


def ndtr(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(z / math.sqrt(2.0)))


def quant(
    grid: np.ndarray,
    pm: np.ndarray,
    qs: tuple[float, ...] = (0.025, 0.16, 0.5, 0.84, 0.975),
) -> list[float]:
    c = np.cumsum(pm) / pm.sum()
    h = grid[1] - grid[0]
    edges = np.concatenate([[grid[0] - h / 2.0], grid + h / 2.0])
    cdf = np.concatenate([[0.0], c])
    return [float(np.interp(v, cdf, edges)) for v in qs]


def post2(
    x: np.ndarray,
    y: np.ndarray,
    cxx: np.ndarray,
    cyy: np.ndarray,
    cxy: np.ndarray,
    b_lo: float = -1.0,
    b_hi: float = 1.0,
    nb: int = 801,
    s_lo: float = 0.0,
    s_hi: float = 0.6,
    ns: int = 601,
    a_lo: float = 2.0,
    a_hi: float = 4.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """y = a + b x + N(0, s^2); per-source covariance; a uniform on [a_lo, a_hi], integrated analytically."""
    b = b_lo + (np.arange(nb) + 0.5) * (b_hi - b_lo) / nb
    s = s_lo + (np.arange(ns) + 0.5) * (s_hi - s_lo) / ns
    B = b[:, None]
    S = s[None, :]
    sw = 0.0
    swr = 0.0
    swr2 = 0.0
    slv = 0.0
    for xi, yi, a, c, d in zip(x, y, cxx, cyy, cxy):
        V = S * S + c + B * B * a - 2.0 * B * d
        R = yi - B * xi
        w = 1.0 / V
        sw = sw + w
        swr = swr + w * R
        swr2 = swr2 + w * R * R
        slv = slv + np.log(V)
    ah = swr / sw
    asd = 1.0 / np.sqrt(sw)
    norm = ndtr((a_hi - ah) / asd) - ndtr((a_lo - ah) / asd)
    lp = -0.5 * (slv + swr2 - swr * swr / sw) - 0.5 * np.log(sw) + np.log(np.maximum(norm, 1e-300))
    lp -= lp.max()
    p = np.exp(lp)
    p /= p.sum()
    return (b, s, p, ah)


def diffq(s: np.ndarray, p1: np.ndarray, p2: np.ndarray) -> list[float]:
    n = len(s)
    d = np.convolve(p1, p2[::-1])
    g = (np.arange(2 * n - 1) - (n - 1)) * (s[1] - s[0])
    return quant(g, d)
