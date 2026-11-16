---
name: source-code-deep-dive
description: Investigate an unfamiliar codebase or a nanobot subsystem end-to-end: architecture, verified call chains, data and state flow, design trade-offs, and safe extension points. Use for source-code learning, reverse engineering, deep debugging, architecture reviews, or planning an implementation without editing code first.
---

# Source Code Deep Dive

Develop a correct mental model of a real code path before proposing changes. Explain **what it does, how it runs, why the boundaries exist, and how to extend it safely**. Default to read-only exploration. Do not modify source code, run destructive commands, or create notes/files unless the user specifically asks.

## Investigation contract

- Start from the user's **question or scenario**, not a whole-repository dump. If the question is broad, select one representative end-to-end path and name the scope.
- Treat the **currently checked-out repository and commit** as the implementation source of truth. Never substitute upstream docs, generated wikis, or file-name guesses for inspected code.
- Distinguish claims as **VERIFIED** (direct source/test/runtime evidence), **INFERRED** (plausible interpretation), or **UNKNOWN** (unresolved).
- For each important claim, supply an exact `path:line` or a tight line range where feasible. Explain when references are approximate or version-dependent.
- Preserve existing `AGENTS.md` and repository instructions; this skill supplements, never overrides, them. Respect read/access boundaries and do not expose secrets.

## Phase 1 — Map the minimum relevant architecture

1. Read `AGENTS.md`; consult `.agent/navigation.md` to identify the owning package, tests, and relevant architecture pages. Use `.agent/design.md` when evaluating architectural boundaries and `.agent/workflow.md` for verification evidence.
2. Inspect the entry point, dependency wiring, public contracts, and ownership boundaries in the selected path. Prefer `rg`, targeted file reads, and test discovery over reading large files wholesale.
3. Produce a compact module map: component -> responsibility -> incoming/outgoing dependencies -> key files. Identify the runtime entry, state owner, and boundaries with channels, providers, tools, and persistence.
4. Optionally invoke `$deepwiki` for a fast **orientation only**, if a corresponding public repository is indexed and the resource is available. For the `gdtydm/nanobot` fork, upstream `HKUDS/nanobot` wiki material is not evidence about fork-specific behavior.

## Phase 2 — Trace a concrete execution path

- Follow **real callers and callees** from input to terminal output, including registration/dispatch, async boundaries, event or queue crossings, and lifecycle cleanup.
- At each relevant step identify: input type/value -> transformation -> output -> state mutation -> next owner.
- Inspect branches, retries, cancellation, errors, concurrency, iteration and context budgets, persistence, and recovery if relevant.
- Keep a short trace table or Mermaid sequence diagram, with paths/lines on the most important transitions. Do not invent a call edge because modules have similar names.
- For nanobot's agent behavior, investigate the applicable paths in `nanobot/agent/loop.py`, `nanobot/agent/runner.py`, `nanobot/agent/context.py`, `nanobot/agent/context_governance.py`, and related session/tools/providers *only as needed*. Confirm all edges from code; this list is navigation, not a predetermined trace.

## Phase 3 — Explain mechanisms and design choices

- Identify key structures and invariants, state ownership and lifetime, configuration and dependency injection, tool/model contracts, and the internal/external boundaries.
- Explain why a design **may** exist, including competing alternatives and costs. Verify author intent only when supported by code comments, design docs, issues, or commit history; otherwise mark it **INFERRED**.
- Use a small concrete input/output example to demonstrate any non-obvious transformation or state transition.
- Prefer causal explanations over line-by-line paraphrase.

## Phase 4 — Extension assessment

When the user has an extension in mind:

1. Find existing hooks, adapters, registries, plugins, tools, skills, or MCP interfaces before touching core execution.
2. Compare at least two plausible placements if the seam is not obvious: responsibilities, coupling, state ownership, compatibility, performance, observability, and regression exposure.
3. Respect `.agent/design.md`: keep `AgentLoop` / `AgentRunner` core edits minimal and justified; prefer behavior at owning edges when appropriate.
4. Identify specific files/contracts and focused existing tests that would need attention.
5. Propose a smallest viable experiment or change plan. **Do not implement it unless requested.**

## Phase 5 — Verify selectively

- Use existing focused tests and runtime traces when the user authorizes execution and the environment permits it. State exactly what ran; never claim tests passed based on reading them.
- Prefer one reproducible scenario that proves the uncertain call/state edge over a broad test suite.
- Record contradictory evidence and unresolved questions. Reassess prior notes after code changes.

## Response format

1. **Conclusion and scope** — what is established and which version/files were inspected.
2. **Architecture map** — responsible components, boundaries, and entry point.
3. **Execution trace** — numbered calls or a sequence diagram, key data/state transitions, error paths.
4. **Design rationale** — verified constraints vs inferred trade-offs.
5. **Extension plan** — seams, affected contracts, risks, and tests (if applicable).
6. **Evidence and open questions** — source `path:line` anchors; next 1–3 useful investigation targets.

Be concise by default; go deep on mechanisms the user asks about. If asked to retain the learning, write a focused repository note **only with the user's permission**, noting the inspected commit and evidence.
