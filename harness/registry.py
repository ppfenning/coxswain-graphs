"""Graph discovery: how a declared SPEC becomes a subcommand.

Discovery walks the `graphs` package, imports what it finds, and collects the
module-level `SPEC`s — so adding a graph to the CLI is dropping a module into
`graphs/`, not editing a dispatch table in the harness. A dispatch table was
how the last shape worked, and it meant the harness had to know every graph by
name, which is backwards: the harness owns consequences, and which programs
exist is not its business.

The spec TYPES live in `graphs._spec`, not here, so that declaring one never
imports the harness (or, behind it, the substrate). Re-exported for callers.
"""

from __future__ import annotations

import importlib
import pkgutil

from graphs._spec import GraphSpec, Need

__all__ = ["DiscoveryError", "GraphSpec", "Need", "discover"]


class DiscoveryError(Exception):
    """The graphs package could not be turned into a coherent registry."""


def discover(package: str = "graphs") -> dict[str, GraphSpec]:
    """Import every module under `package` and collect the SPECs.

    Import errors are not swallowed: a graph that cannot import is a graph
    that would have failed at run time anyway, and discovery is the earliest
    moment anyone can hear about it.
    """
    root = importlib.import_module(package)
    specs: dict[str, GraphSpec] = {}
    for info in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
        leaf = info.name.rsplit(".", 1)[-1]
        if leaf.startswith("_"):
            continue
        module = importlib.import_module(info.name)
        spec = getattr(module, "SPEC", None)
        if spec is None:
            continue
        if not isinstance(spec, GraphSpec):
            raise DiscoveryError(f"{info.name}.SPEC is {type(spec).__name__}, expected GraphSpec")
        if spec.name in specs:
            raise DiscoveryError(
                f"two graphs register the subcommand '{spec.name}'; "
                "the second would silently shadow the first"
            )
        specs[spec.name] = spec
    if not specs:
        raise DiscoveryError(f"no graph in '{package}' declares a SPEC — nothing to run")
    return specs
