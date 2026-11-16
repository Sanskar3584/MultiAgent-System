# Agent Operations Runbook

Agents in the mesh coordinate through the blackboard. A worker claims a task before starting it and completes it with its output, so no task is processed twice.

## Verification policy

No agent output is accepted on self-report. Every task output passes deterministic validators first (tests, schemas, citation checks) and an independent reviewer agent second. A task gets at most three attempts; after that it is marked failed and escalated to a human on the escalations channel.

## Budgets

Each agent run is capped at a fixed number of tool steps. Repeating the identical tool call three times in a row is treated as a loop and the run is stopped.
