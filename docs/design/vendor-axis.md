# Vendor axis

Status: design. Specifies the target shape only; no code in this repo implements it yet.

## 1. The runner contract

A graph never constructs a client and never names a model. It calls one seam:

```
run(role, tier, prompt, schema, tools, ceiling) -> NodeResult
```

`role` says what the node needs done; the cartridge maps it to a skill. `tier` says how much
capability it wants (`cheap`, `standard`, `deep`); a provider profile maps it to a model. `schema`
and `tools` bound the structured output and tool surface. `ceiling` is the dollar cap for the call.
The contract raises three things and nothing else: `RunnerError` when a node could not be run or
came back unusable, `BudgetStop` when a ceiling stopped it without that being a real failure, and
`LimitStop` when the account's own session limit stopped it. A graph catches these three; it never
inspects a provider's raw error shape.

## 2. Claude-specific behaviours, not universal ones

Today's Claude Code runner does three things the contract above does not promise:

- **Session resume on a budget stop.** `BudgetStop` carries a `session` so a later phase can resume
  the same context instead of starting the node over. Not every provider keeps a resumable session.
- **The `--strict-mcp-config`/`--setting-sources` isolation.** Each session starts with no MCP
  servers, plugins, hooks, or user settings, so a node pays only for tools its profile grants. A
  provider with no config layer has nothing to strip.
- **The limit banner.** `LimitStop` parses a Claude account banner's raw text into `detail`. Another
  provider has no equivalent banner to parse.

None of these are assumed by the contract. Each becomes an optional capability a profile may or may
not declare; a role that depends on one must ask for it, and the shell decides whether the resolved
runner can honor the ask.

## 3. The capability declaration

A provider profile declares what it can do and what it runs, per tier:

```yaml
provider_profile:
  local-oss:
    capabilities:
      structured_output: true
      tool_use: true
      resume: false
      streaming: false
      max_context: 32000
    tiers:
      cheap: local/mixtral-8x7b
      standard: anthropic/claude-standard
      deep: anthropic/claude-deep
```

`capabilities` is a flat map: `structured_output`, `tool_use`, `resume`, `streaming` are booleans;
`max_context` is a token count. `tiers` is today's field for the per-tier model map (required by
`runner/anthropic_runner.py`); the capability declaration sits beside it, unchanged.

## 4. Per-tier runner resolution

`provider_profile` becomes a map from tier to profile name, so `cheap`, `standard`, and `deep` may
each resolve to a different runner. Today's config gives a single profile value, not a map; the
shell treats that single value as the all-tiers default, so an unmigrated config keeps its current
behavior with no edit required.

## 5. The fallback rule

A role declares the capabilities it needs. When the profile resolved for a node's tier lacks a
capability the role needs, the shell falls up exactly one tier and retries resolution there. The
shell ledgers the event under the key `capability_fallback`, recording: the role, the requested
tier, the tier it fell up to, and the missing capability. A fallback is never silent.

## 6. Worked example

`cheap` is routed to `local-oss`, a local OpenAI-compatible endpoint (the profile in §3), which
declares `resume: false` and `streaming: false`. A `build` role needs `resume`, so that it can
recover a partial patch after a mid-run stop rather than restart. Resolution at `cheap` finds
`resume` missing, falls up to `standard`, and ledgers:

```json
{
  "event": "capability_fallback",
  "role": "build",
  "requested_tier": "cheap",
  "resolved_tier": "standard",
  "missing_capability": "resume"
}
```

`build` runs on `standard`'s model instead of `local-oss`'s. Every other role on the graph that
does not need `resume` stays on `cheap`.
