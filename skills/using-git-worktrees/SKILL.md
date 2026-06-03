---
name: using-git-worktrees
description: Create isolated Git worktrees for safe parallel development on separate branches
triggers: [worktree, isolate, branch, sandbox, git worktree]
tools-required: [git_status, git_command, run_python, read_file, write_file]
---

# Using Git Worktrees

> Create isolated development workspaces with Git Worktree. Develop on a separate branch without touching the main working tree.

## When to Use

- Before starting a new development task (feature or bug fix)
- When isolation from the main working tree is desired
- Before running experiments that should not affect the stable workspace

## Process

### Step 0: Detect Worktree

1. Check if already inside a worktree: `git rev-parse --is-inside-work-tree`  
   (This is informational — proceed either way.)
2. If inside a worktree and no new isolation is needed, skip the remaining steps.

### Step 1: Prepare

1. Run `git_status` to confirm the current working tree is clean
2. If there are uncommitted changes, ask the user to commit or stash them first
3. Determine a branch name (`feature/<task-name>` or `fix/<bug-name>`)

### Step 2: Create Worktree

1. Use `git_command` to create the worktree:
   ```
   git_command: "worktree add ../desktop-agent-<branch-name> -b <branch-name>"
   ```
   Or with a specific path:
   ```
   git_command: "worktree add ../desktop-agent-feature-xyz feature/new-feature"
   ```
2. Confirm the worktree was created successfully with `git_status` inside the worktree

### Step 3: Setup Worktree

- Run the project's baseline tests inside the worktree to confirm the environment is healthy
- Record the worktree path for subsequent subagent executions
- Set the subagent's `cwd` to the worktree path when calling `delegate_task`

### Step 4: Develop in Worktree

- All code changes happen inside the worktree
- The main working tree (original branch) remains clean and unaffected
- Subagents dispatched for development tasks should have their working directory set to the worktree path

### Step 5: Cleanup

After development is complete and reviewed:

- **Keep the branch**: Merge back into main, or create a PR
- **Discard the branch**: Delete both the worktree and the branch

Cleanup commands:
```bash
git worktree remove <worktree-path>
git branch -D <branch-name>
```

## Rules

- Never make destructive modifications directly on the main branch via the worktree
- Worktree path should be placed adjacent to the repository (e.g., `../desktop-agent-feature-xyz`)
- Run a baseline test before starting development to catch environment issues early
- Confirm the worktree points to the correct base commit before branching
