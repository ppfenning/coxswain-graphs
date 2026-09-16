# triage

The triage role classifies a stranded item against `facts` and must cite the record; `emit` builds the write by class — a `ticket_amend` proposal, an `item_create` proposal, a `decompose` handoff, or a `notify` proposal — and refuses to repeat an already-fired `(class, diagnosis)` pair on the same item, escalating to a person instead.

```mermaid
flowchart LR
    n0["facts<br/>step · 1"]
    n1["triage<br/>step · 2"]
    n2["emit<br/>step · 3"]
    n0 --> n1
    n1 --> n2
    style n0 fill:#f96,stroke:#333,stroke-width:2px
```

| Node | Step |
|---|---|
| `facts` | 1 |
| `triage` | 2 |
| `emit` | 3 |
