# phase-validate

The two validators the epic-swarm spec asks for, as a graph. They are model calls, and model calls belong in graphs — a driver that called the runner directly would be the first non-graph thing in the system to do so, and every purity rule the portability suite enforces would stop applying to the two nodes whose judgment the phase boundary rests on. So the driver invokes this the way it invokes any other graph, through `invoke_graphs`, and gets back an opinion rather than a decision.

```mermaid
flowchart LR
    n0["validate_chunk (per task)<br/>step · 1"]
    n1["validate_phase (once)<br/>step · 2"]
    n0 --> n1
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `validate_chunk (per task)` | 1 |
| `validate_phase (once)` | 2 |
