"""The leaf registry: every competition (leaf) installed in this kit.

A leaf is a package ``vector_miner.skills.<skill_py>.<kind>`` whose ``__init__`` defines ``ID``, ``SKILL`` and ``KIND``
(``ID == f"{SKILL}-{KIND}"``) and imports nothing else. Each leaf has a module ``cli`` with
``main(argv=None, prog="vector-miner") -> int``.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import types

KINDS = ("expert", "policy", "contribution")
ENV = "VECTOR_MINER_LEAF"


def _is_leaf(m: types.ModuleType, skill_py: str, kind: str) -> bool:
    skill, k, leaf_id = (getattr(m, n, None) for n in ("SKILL", "KIND", "ID"))
    return (all(isinstance(x, str) for x in (skill, k, leaf_id)) and k == kind and kind in KINDS
            and skill.replace("-", "_") == skill_py and leaf_id == f"{skill}-{k}")


def leaves() -> dict[str, types.ModuleType]:
    """{leaf id: leaf package}, sorted by id."""
    found: dict[str, types.ModuleType] = {}
    for s in pkgutil.iter_modules(__path__):
        if not s.ispkg:
            continue
        skill = importlib.import_module(f"{__name__}.{s.name}")
        for k in pkgutil.iter_modules(getattr(skill, "__path__", [])):
            if not k.ispkg or k.name not in KINDS:
                continue
            m = importlib.import_module(f"{skill.__name__}.{k.name}")
            if _is_leaf(m, s.name, k.name):
                found[m.ID] = m
    return dict(sorted(found.items()))


def leaf(leaf_id: str) -> types.ModuleType:
    """The leaf package of this id; KeyError("unknown leaf X (installed: a, b)") otherwise."""
    have = leaves()
    if leaf_id not in have:
        raise KeyError(f"unknown leaf {leaf_id} (installed: {', '.join(have) or 'none'})")
    return have[leaf_id]


def default() -> str | None:
    """$VECTOR_MINER_LEAF, else the only installed leaf, else None."""
    env = os.environ.get(ENV, "").strip()
    if env:
        return env
    have = leaves()
    return next(iter(have)) if len(have) == 1 else None
