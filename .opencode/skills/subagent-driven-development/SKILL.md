---
name: subagent-driven-development
description: Delegate plan tasks to subagents with two-stage review — compliance then code quality
triggers: [execute plan, implement plan, develop, subagent, delegate task]
tools-required: [delegate_task, read_file, write_file, git_status, git_diff, run_python]
---

# Subagent-Driven Development

> Execute plan tasks via delegated subagents. Each subagent receives an isolated context, completes its task, then passes through two stages of review before being marked done.

## When to Use

- A plan has been created and approved
- Tasks can be isolated and delegated
- Testing and verification are required

## Announce

"I'm using the Subagent-Driven Development skill to implement this plan."

## Prerequisites

- An approved plan file must exist and have been read
- `delegate_task` must be available (agent_type: `coder` / `reviewer` / `debugger`)
- Git working tree should be clean before starting

## Process

### Step 1: Load and Review Plan

1. Read the plan file using `read_file`
2. Critically review the plan — identify risks, missing steps, unclear requirements
3. If concerns arise: raise them with the user before starting
4. If no concerns: create a task tracking list and begin execution

### Step 2: Execute Tasks via Subagents

For each task in the plan:

1. Mark the task as in_progress
2. Call `delegate_task` with `agent_type: "coder"` and a fully self-contained prompt containing:
   - The complete task description (every step, file path, code requirement)
   - Only the project context relevant to this task
   - Clear completion criteria
3. After the subagent finishes, inspect its output and the actual code changes
4. Enter the two-stage review:

**Stage One — Compliance Review**
- Call `delegate_task` with `agent_type: "reviewer"` to verify the output matches the plan's specification for this task
- If review fails: feed the issues to a new `coder` subagent for rework (max 3 retries)

**Stage Two — Code Quality Review**
- Call `delegate_task` with `agent_type: "reviewer"` to check code style, security, performance, unnecessary dependencies
- If review fails: feed issues to a new `coder` subagent for fixes (max 3 retries)

5. After both stages pass, mark the task as completed

### Step 3: Verify Between Tasks

- After each completed task, run the project's test suite with `run_python` or the appropriate test command
- Check for regressions with `git_status` and `git_diff`
- If regressions are found, pause and report to the user

### Step 4: Report Completion

When all tasks are done, provide a summary:
- Completed tasks with their status
- Any remaining issues or known concerns
- Test results

## Rules

- Every subagent prompt must be **fully self-contained** — do not assume the subagent can see the main conversation history
- Never write "based on your findings" in a subagent prompt — include concrete file paths, line numbers, and specific changes
- If a task fails after 3 retries, pause and ask the user for guidance
- Read-only tasks can run in parallel; write operations on the same file must be serialized
- Use `git_status` between tasks to detect unintended side effects
