"""The per-role specs the fast path knows. Later tasks add entries."""

from __future__ import annotations

from collections.abc import Mapping

from runner.system_one import RoleSpec

__all__ = ["role_specs"]


def role_specs() -> Mapping[str, RoleSpec]:
    return {}
