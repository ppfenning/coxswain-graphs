# Gate/landing regression, 2026-09-16

Conclusion: the `done` write at `harness/epic.py:1304` fires on a *local stack*
merge, not on landing to main. `run_epic`'s docstring, line 534: "Drive a whole
initiative: every phase, in dependency order, landing nothing" — landing to
main is out of scope for this driver. `state.merged` records only the stack
merge `_execute` performs; once that succeeds and `state.moved[task]` is set,
line 1304 writes `done` with no PR opened. The same blind spot silences the
driver's warning: `exit_summary` (663-675) only names a task "approved but not
landed" when `outcome == approved_not_landed`, and `task_outcome`'s `landed`
argument at 1294 is `task_record["merged"] and not quarantine` — comment
1266-1269 says reconciliation "would see only `merged=True` and report it
`landed`". Occurrence 1: reviewers approved, the stack merge succeeded,
`done` was written, no PR opened, no warning fired.

(a) `harness/epic.py:1304`, `target_state = "done" if state.merged.get(task)
else "approved"`, in `for task, moved in state.moved.items()` (1302-1307),
runs after the batch loop calling `_execute` (1246-1264) — ordered correctly
relative to that call. The defect is scope: `state.merged` means "merged into
the run's stack," never "landed to main."

(b) At both landing sites, `applied, _ = _execute(...)` (1249, 1259), the
second return value is discarded. `task_record["draft"]`/`["merged"]`
(1274-1275) come only from `state.landed`/`state.merged` afterward, never from
`_execute`'s own return — the visible cause of a record with no landing
content.

(c) `gate(gated, assume=ctx.assume)` (1240) runs once per phase over the whole
`batch`. The slot lookup at 1247/1253 shows a `"rebase"` slot beside the merge
path, consistent with `_build_batch` (1223, body outside this fence) placing
more than one entry per task, each a distinct `id(item)` with its own
decision and `gate_diff` (1264) — two entries, two
`decision: a (--assume)` lines. `state.moved`/`state.merged` key only by task
id, not slot, so a second entry's `_execute` can set `merged[task] = True`
while the entry with the real change stays partial — `done` plus
`0 complete, 1 partial`.

(d) `git log --since='2026-09-16 08:26' --until='2026-09-16 08:59'` names one
in-window merge, #99 (`cdea44f`), touching only
`graphs/ops/triage_quarantine.py` and its test. `--stat` on every #95-#101
commit shows only #96 (`db1e709`, 05:49:12, outside the window) touches
`harness/epic.py`, adding `_trace_evidence` to `_build_task` and a comment
near `_trim_phase` — nowhere in 440-700 or 1220-1330. No merged change in
#95-#101 altered the gate/landing sequence.

Named for context only, not inspected: coxswain-tools PRs #148-#153 and the
tools-loop-fixes-3 task's lost record — both in that repository, out of scope
here.
