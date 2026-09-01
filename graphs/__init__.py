"""Portable agent graphs. Sequence lives here; who it works for does not.

Namespaced by function, one package per concern:

    delivery/   work that produces work — initiative-decompose, lifecycle-propose
    ops/        keeping a running system honest — triage-propose, epic-reconcile

`_contract.py` (the rules every graph must satisfy) and `_spec.py` (how a
graph declares itself to a harness) are shared across all of them and live at
this level. Cartridges do NOT live here, deliberately: a cartridge is the
domain axis, and grouping one with the graphs would re-couple exactly what
the portability suite exists to keep apart.
"""
