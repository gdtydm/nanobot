---
name: source-code-deep-dive
description: Investigate a codebase's architecture, actual execution and state flow, design tradeoffs, and extension points with source evidence. Use for deep code comprehension or planning changes to an existing system.
---

# Source code deep dive

Build a verifiable model of the current checkout that answers the user's question and supports their intended extension. Scope the investigation to the requested mechanism; map the wider architecture only as needed.

1. Identify the repository, current revision and relevant uncommitted changes. Look for existing relevant research and reuse it after checking its evidence against the current code. Map entry points, components, dependencies, and tests; give a useful reading order when it helps.
2. Trace at least one concrete path from the real entry point through calls, dispatch and boundaries to the observable outcome. Follow relevant control, data and state changes, including ownership, persistence, side effects, error paths and important alternatives. Resolve dynamic dispatch through registrations, configuration and call sites rather than guessing from names. State what remains untraced.
3. Explain why the design may have been chosen and its tradeoffs using code, tests, documentation or history where available. Separate documented intent from inference; do not present an unverified rationale as the author's intent.
4. For an extension request, identify existing extension points, affected modules and contracts, compatibility risks, implementation options and focused validation. Connect each recommendation to observed behavior and highlight unknowns.
5. Cite concrete file paths and line numbers for consequential claims; distinguish observed facts, inference and open questions. Report the revision and relevant local changes so findings can be revisited. Use diagrams or examples when they clarify the mechanism.

Use targeted tests, instrumentation or a small reproducible run when useful and authorized; state what was actually run and its limitations. Avoid mutations or external side effects beyond the request. DeepWiki can provide an optional orientation or leads, but verify claims against the current checkout and continue without it if unavailable. Save or update research notes only when requested, preserving existing work and recording the revision and evidence.
