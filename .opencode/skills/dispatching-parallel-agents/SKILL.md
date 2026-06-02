---
name: dispatching-parallel-agents
description: Dispatch multiple independent subagents concurrently to speed up development
triggers: [parallel, concurrent, speed up, run in parallel, independent tasks]
tools-required: [delegate_task, git_status, git_diff, run_python]
---

# Dispatching Parallel Agents

> When multiple plan tasks have no interdependencies, dispatch them concurrently. Each subagent receives a fully isolated context.

## When to Use

- The plan contains multiple independent tasks
- Tasks do not modify the same files
- No task depends on another task's output

## Process

### Step 1: Dependency Analysis

1. Read the plan and list all pending tasks
2. Analyze dependencies:
   - **File dependency**: Do two tasks modify the same file(s)?
   - **Data dependency**: Does task B need task A's output as input?
3. Partition tasks into:
   - **Parallel groups** — tasks with no mutual dependencies
   - **Serial chains** — tasks with dependency relationships

### Step 2: Dispatch Parallel Group

For each parallel group:

1. Call `delegate_task` for every task in the group simultaneously (or in quick succession)
2. Each subagent prompt must be fully self-contained
3. Ensure no two subagents are modifying the same file at the same time

Example:
```
Parallel Group: [Refactor CSS, Add backend endpoint, Write integration tests]
→ Dispatch 3 coder subagents concurrently (max 3-4 per group)
```

### Step 3: Collect and Verify

1. Wait for all subagents in the group to complete
2. Collect their results
3. Check for conflicts (should be none if dependency analysis was correct)
4. Run the test suite to confirm integration is sound

### Step 4: Continue to Serial Tasks

After the parallel group completes, execute any serial tasks that depend on the group's results.

## Rules

- Never let two subagents modify the same file concurrently
- If uncertain about a dependency, err on the side of serial execution
- Parallelism does not skip review — each subagent's output must still pass `requesting-code-review`
- Limit parallel groups to 3-4 subagents to avoid excessive context pressure
- If any subagent in the group fails, the entire parallel group should be re-evaluated
