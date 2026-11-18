## Implementation scope

- When the user provides a reference project, read the implementation for the corresponding step before making changes. Follow its relevant behavior unless the task calls for a change.
- Add or reuse a blocking check only if it enforces a user requirement, an applicable rule, or a concrete dependency of the current step. If an unnecessary check blocks the task, remove it from that path and continue.
- Use existing results, logs, or a targeted check to investigate such a failure. Do not rerun a full dataset or pipeline just to diagnose an unnecessary check.
