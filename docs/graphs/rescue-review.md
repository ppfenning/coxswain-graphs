# rescue-review

Takes a ticket, a patch and the evidence rows the harness produced by running the checks, and returns a verdict shaped like a lifecycle result so the driver reads it the same way. There is no plan node, no build node and no fix loop: one review round, and the verdict is final.

```mermaid
flowchart LR
    n0["handoff<br/>step · 1"]
    n1["review_charter<br/>step · 2"]
    n2["review_adversary<br/>step · 3"]
    n3["arbitrate<br/>step · 4"]
    n4["emit<br/>step · 5"]
    n0 --> n1
    n1 --> n2
    n2 --> n3
    n3 --> n4
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `handoff` | 1 |
| `review_charter` | 2 |
| `review_adversary` | 3 |
| `arbitrate` | 4 |
| `emit` | 5 |
