# triage-propose

Alerts arrive as an argument rather than being fetched here. A graph that reads a queue reads the world, and the contract puts both the filesystem and the clock on the far side of the graph boundary for the same reason: a graph that cannot be replayed cannot be debugged after the fact.

```mermaid
flowchart LR
    n0["fetch<br/>step · 1"]
    n1["classify<br/>step · 2"]
    n2["verify<br/>step · 3"]
    n3["emit<br/>step · 4"]
    n0 --> n1
    n1 --> n2
    n2 --> n3
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `fetch` | 1 |
| `classify` | 2 |
| `verify` | 3 |
| `emit` | 4 |
