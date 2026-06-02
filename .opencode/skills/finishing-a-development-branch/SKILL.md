---
name: finishing-a-development-branch
description: Final verification, branch fate decision, and cleanup after all development tasks are complete
triggers: [finish branch, cleanup, merge, complete, wrap up, finalize]
tools-required: [git_status, git_diff, git_log, run_python, delegate_task]
---

# Finishing a Development Branch

> After all tasks are done and reviewed, verify tests, decide the branch's fate, and clean up temporary worktrees.

## When to Use

- All plan tasks are marked completed
- All code reviews have passed
- It is time to finalize and merge (or archive) the development branch

## Process

### Step 1: Final Verification

1. Run the full test suite — all tests must pass
2. Run lint or type checks if the project has them configured
3. Run `git_status` to confirm no leftover uncommitted changes
4. Run `git_log` to review commit history for clarity

### Step 2: Present Options

Show the user four options in a table:

| Option | Action | Use Case |
|--------|--------|----------|
| **A. Merge to main** | `git merge <branch>` | Development is complete, ready for deployment |
| **B. Create PR** | `git push origin <branch>` + create PR | Needs team review before merging |
| **C. Keep branch** | No action | Not yet decided, will continue later |
| **D. Discard branch** | Delete worktree + delete branch | Completely wrong direction, start over |

### Step 3: Execute Choice

Carry out the user's selected option.

### Step 4: Cleanup

If merging or discarding:
- Remove the worktree: `git worktree remove <worktree-path>`
- Confirm removal with `ls` or `git worktree list`

If keeping the branch, leave the worktree in place.

### Step 5: Summary

Provide a final report:
- List of completed tasks
- Test results (pass/fail counts)
- Branch final status (merged / PR created / kept / discarded)
- Any remaining issues or follow-up recommendations

## Rules

- Never merge without running the full test suite first
- Never decide the branch's fate autonomously — present options and let the user choose
- Before discarding, confirm with the user that they truly want to throw away the work
- Make sure all committed changes are pushed before cleanup if there is any chance they will be needed later
