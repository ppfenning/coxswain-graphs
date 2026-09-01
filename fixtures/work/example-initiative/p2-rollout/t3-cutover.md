---
id: t3-cutover
phase: p2-rollout
state: todo
needs: [t1-schema-probe, t2-bench-harness]
surfaces: [production_write_path]
title: Cut the reader path over
---

Flip the reader to the new path once the probe and the benchmark both exist.
Blocked until both dependencies are done — that edge is real: this task
consumes their outputs.
