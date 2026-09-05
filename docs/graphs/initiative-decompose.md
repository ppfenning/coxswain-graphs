# initiative-decompose

The front of the pipeline, and the step that makes everything after it parallelisable. An idea arrives as prose; what comes out is phases, tasks, and the dependency edges between them — and one `item_create` proposal per task.

```mermaid
flowchart LR
    n0["decompose<br/>step · 1"]
    n1["adversary<br/>step · 2"]
    n2["emit<br/>step · 3"]
    n0 --> n1
    n1 --> n2
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `decompose` | 1 |
| `adversary` | 2 |
| `emit` | 3 |
