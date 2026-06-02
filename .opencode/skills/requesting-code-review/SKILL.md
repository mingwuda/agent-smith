---
name: requesting-code-review
description: Proactively initiate code review between tasks. Block progress on critical issues.
triggers: [code review, review changes, review diff, quality check, request review]
tools-required: [delegate_task, git_status, git_diff, git_log, git_show]
---

# Requesting Code Review

> After each development task, proactively initiate a code review. Critical issues block progress; warnings and suggestions are logged.

## When to Use

- After completing a development task, before starting the next one
- When the user explicitly asks for a code review
- Before merging a branch or creating a pull request

## Process

### Step 1: Gather Context

1. Run `git_status` and `git_diff` to see all current changes
2. Read the corresponding plan to understand what the changes were supposed to implement
3. Prepare the review context: list of changed files, full diff content, and plan requirements

### Step 2: Dispatch Review Subagent

Call `delegate_task` with `agent_type: "reviewer"` and provide:
- The complete `git_diff` output
- The plan's requirements for the changes
- A review checklist covering these dimensions:

| Dimension | Focus |
|-----------|-------|
| **Correctness** | Does the code implement the plan's requirements faithfully? |
| **Test coverage** | Are tests present and sufficient? Do they cover edge cases? |
| **Code style** | Does the code follow the project's existing conventions? |
| **Security** | Are there injection risks, privilege escalations, or information leaks? |
| **Performance** | Are there N+1 queries, memory leaks, or obvious bottlenecks? |
| **Dependencies** | Are any new dependencies introduced unnecessarily? |
| **Edge cases** | Is error handling complete? Are boundary conditions covered? |

### Step 3: Evaluate Results

The review subagent returns issues classified by severity:

- 🔴 **Critical (blocking)** — Functional bugs, security vulnerabilities, data loss risks
  → MUST fix before proceeding to the next task
- 🟡 **Warning** — Code style issues, missing tests, minor performance concerns
  → Log for later; fix during current task if quick
- 🟢 **Suggestion** — Alternative approaches, readability improvements
  → Log only; does not block progress

### Step 4: Handle Critical Issues

If any 🔴 Critical issues exist:
1. Feed the issue list to a new `coder` subagent for repair
2. After repair, repeat Steps 1–3
3. If still critical after 3 repair attempts, pause and escalate to the user

### Step 5: Log and Continue

- Record the review outcome (pass / issues found)
- No critical issues → proceed to the next task
- Issues exist → fix before moving on

## Rules

- Reviews must be based on actual `git_diff` output — never review unchanged code
- The reviewer subagent's prompt must contain the full diff and plan requirements
- Keep reviews focused — catch real quality problems, not nitpicks
- If diff is empty (no changes), skip the review
