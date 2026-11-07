"""Small, deterministic statistics (no scipy: one less dependency to defend)."""
from __future__ import annotations

import math


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def two_prop_z(x1: int, n1: int, x2: int, n2: int) -> float:
    """z for p1 - p2 (pooled variance). 0 when undefined."""
    if n1 == 0 or n2 == 0:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (x1 / n1 - x2 / n2) / se if se > 0 else 0.0


def chi2_sf(x: float, df: int) -> float:
    """Survival function via Wilson-Hilferty (accurate for df >= ~5; plenty for a gating test)."""
    if df <= 0:
        return 1.0
    z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return norm_sf(z)


def interaction_chi2(table: dict[str, dict[int, tuple[int, int]]]) -> tuple[float, int]:
    """Does the success rate depend on segment beyond what the column (hour) explains?
    table[segment][hour] = (n, successes). Returns (chi2, df)."""
    hours = sorted({h for t in table.values() for h in t})
    col = {h: (sum(t.get(h, (0, 0))[0] for t in table.values()), sum(t.get(h, (0, 0))[1] for t in table.values()))
           for h in hours}
    x = 0.0
    for t in table.values():
        for h in hours:
            n, s = t.get(h, (0, 0))
            p = col[h][1] / col[h][0] if col[h][0] else 0
            v = n * p * (1 - p)
            if v > 0:
                x += (s - n * p) ** 2 / v
    return x, (len(table) - 1) * (len(hours) - 1)
