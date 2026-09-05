# coxswain

One node, one role. Given a docket describing what is on hand right now — the graph registry, the intake queue, the ready set, the ledger's shape — decide which graphs to run next, and say why. That decision is judgment, exactly like every other decision this codebase hands to a model, so it lives in a graph like every other one: pure, replayable, no disk, no clock.

```mermaid
flowchart LR
    n0["dispatch<br/>step · 1"]
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `dispatch` | 1 |
