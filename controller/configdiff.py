"""Pure helpers over workflow config dicts: what changed, how far a distribution moved, 3-way merge."""
from __future__ import annotations

import copy
from typing import Any

ALLOC_DIMS = ("welcome_message", "voice", "caller_id")


def _norm(d: dict[str, float]) -> dict[str, float]:
    t = sum(d.values()) or 1.0
    return {k: v / t for k, v in d.items()}


def effective_dist(cfg: dict, cohort: str, dim: str) -> dict[str, float]:
    a = cfg.get("allocations", {})
    return _norm(a.get(cohort, {}).get(dim) or a.get("default", {}).get(dim, {}))


def changed_dims(base: dict, new: dict) -> set[str]:
    out: set[str] = set()
    if base.get("cohorts", []) != new.get("cohorts", []):
        out.add("cohorts")
    for dim in ALLOC_DIMS:
        for c in set(base.get("allocations", {})) | set(new.get("allocations", {})):
            if effective_dist(base, c, dim) != effective_dist(new, c, dim):
                out.add(dim)
    if base.get("prompt_version") != new.get("prompt_version"):
        out.add("prompt_version")
    if base.get("schedule", {}) != new.get("schedule", {}):
        out.add("schedule")
    return out


def max_shift(base: dict, new: dict) -> float:
    """Largest total-variation distance any cohort's distribution moves on any dimension."""
    m = 0.0
    cohorts = set(base.get("allocations", {})) | set(new.get("allocations", {}))
    for c in cohorts:
        for dim in ALLOC_DIMS:
            p, q = effective_dist(base, c, dim), effective_dist(new, c, dim)
            m = max(m, 0.5 * sum(abs(p.get(k, 0) - q.get(k, 0)) for k in set(p) | set(q)))
    return m


def take_dim(dst: dict, src: dict, dim: str) -> None:
    """Copy one changed dimension from src into dst (in place on dst, which the caller owns)."""
    if dim == "cohorts":
        dst["cohorts"] = copy.deepcopy(src.get("cohorts", []))
    elif dim in ALLOC_DIMS:
        for c, dists in src.get("allocations", {}).items():
            if dim in dists:
                dst.setdefault("allocations", {}).setdefault(c, {})[dim] = copy.deepcopy(dists[dim])
    else:
        dst[dim] = copy.deepcopy(src.get(dim))


def merge(current: dict, parts: list[tuple[dict, set[str]]]) -> dict:
    """Overlay each proposal's changed dims onto the *current* config (disjoint dims assumed by caller)."""
    out: dict[str, Any] = copy.deepcopy(current)
    for cfg, dims in parts:
        for d in dims:
            take_dim(out, cfg, d)
    return out
