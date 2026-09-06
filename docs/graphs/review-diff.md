# review-diff

Takes a diff, a repo and a ref — never a run's own build branch — and returns the same verdict-and-findings shape a build's own review step produces. Nothing is written to the repo and nothing is pushed.

```mermaid
flowchart LR
    n0["review_charter<br/>step · 1"]
    n1["review_adversary<br/>step · 2"]
    n2["verify_evidence<br/>step · 3"]
    n3["arbitrate<br/>step · 4"]
    n0 --> n1
    n1 --> n2
    n2 --> n3
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `review_charter` | 1 |
| `review_adversary` | 2 |
| `verify_evidence` | 3 |
| `arbitrate` | 4 |
