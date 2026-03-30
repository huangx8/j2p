# j2p — JIRA to PR

An AI agent that reads a JIRA ticket, implements the required code changes using the **locally installed Claude Code CLI
**, opens a draft GitHub PR via the **local `gh` CLI**, and continuously addresses PR review comments until the PR is
merged or closed.

No API tokens or API keys are required — all authentication is handled by your locally installed tooling.

## How it works

```
                        ┌──────────────────────────────────────┐
                        │  existing open PR found?             │
                        ▼  yes                                 │
[pr_check_node] ──────────────────► [review_watcher_node]      │
    │ no                                 │                     │
    ▼                                    │ new comments        │
[jira_node] ── existing PR? ─────────────┘                     │
    │ no                                                       │
    ▼                                                          │
[clarification_node]  ← prompts user if repos/description      │
    │                   missing from ticket (loops until done) │
    ▼                                                          │
[coding_node]  ← claude CLI implements changes in each repo    │
    │                                                          │
    ▼                                                          │
[pr_node]  ← creates draft PR via gh CLI                       │
    │                                                          │
    └──────────────────────────────────────────────────────────┘
```

**Review loop:** after a PR is created, `review_watcher_node` polls for new comments. When comments arrive,
`coding_node` re-runs to address them, commits, pushes, and replies to each comment with the fixing commit SHA. Stops
when the PR is marked ready (no longer draft), closed, merged, or `MAX_REVIEW_ITERATIONS` is reached.

## Workflow

1. **pr_check_node** — if repos are already known (via `--repo` or a prior run), immediately checks for open PRs and
   skips to review watching if found.
2. **jira_node** — invokes `claude --print` to fetch the ticket via its built-in JIRA MCP integration. Parses the
   description to extract target repos and build a focused coding prompt. Also detects existing open PRs and
   short-circuits to review watching if found.
3. **clarification_node** — if repos or change instructions are missing from the ticket, interactively prompts the user.
   Loops until all required information is provided.
4. **coding_node** — invokes `claude --print` inside each repo. Claude Code explores the codebase and implements all
   required changes, then outputs a commit message.
5. **pr_node** — commits, pushes the branch, and creates a **draft** PR via `gh pr create`.
6. **review_watcher_node** — polls the PR for new review comments. Feeds them back to `coding_node` for fixes. Replies
   to each addressed comment with the commit SHA so it is not re-processed on the next run.

## Prerequisites

| Tool         | Purpose                           | Install                               |
|--------------|-----------------------------------|---------------------------------------|
| `claude`     | Code implementation + JIRA access | [Claude Code](https://claude.ai/code) |
| `gh`         | GitHub PR management              | `brew install gh`                     |
| `git`        | Branch / commit / push            | pre-installed on macOS                |
| Python 3.11+ | Orchestration (LangGraph)         | `brew install python`                 |

```bash
# One-time auth
gh auth login
claude mcp list   # verify atlassian entry is present
```

## Setup

```bash
git clone <this-repo> && cd j2p
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
```

## Configuration

| Variable                   | Required | Default              | Description                                                                                                                                                                              |
|----------------------------|----------|----------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `JIRA_SERVER`              | ✅        | —                    | e.g. `https://yourcompany.atlassian.net`                                                                                                                                                 |
| `CLAUDE_CLI_PATH`          |          | `claude`             | Path to `claude` binary if not on `$PATH`                                                                                                                                                |
| `GH_CLI_PATH`              |          | `gh`                 | Path to `gh` binary if not on `$PATH`                                                                                                                                                    |
| `GITHUB_BASE_BRANCH`       |          | `main`               | Base branch PRs are opened against                                                                                                                                                       |
| `GITHUB_DEFAULT_ORG`       |          | —                    | Org prepended to bare repo names — e.g. `myorg` turns `myrepo` into `myorg/myrepo`                                                                                                       |
| `LOCAL_REPOS_DIR`          |          | —                    | Path to a directory of existing local checkouts. Supports flat (`~/repos/myrepo`) and org-scoped (`~/repos/myorg/myrepo`) layouts. When set, repos are used in-place instead of cloning. |
| `WORKSPACE_DIR`            |          | `/tmp/j2p_workspace` | Fallback clone directory when repo not found under `LOCAL_REPOS_DIR`                                                                                                                     |
| `PR_POLL_INTERVAL_SECONDS` |          | `60`                 | How often to poll for new PR review comments                                                                                                                                             |
| `MAX_REVIEW_ITERATIONS`    |          | `10`                 | Max coding/review cycles before stopping                                                                                                                                                 |

## Usage

```bash
source .venv/bin/activate

# Basic
python main.py --ticket PROJ-123

# Override / supply repos explicitly (skips repo extraction from ticket)
python main.py --ticket PROJ-123 --repo myorg/repo1 --repo myorg/repo2

# Bare names are auto-qualified using GITHUB_DEFAULT_ORG
python main.py --ticket PROJ-123 --repo repo1 --repo repo2

# Resume a previous run
python main.py --ticket PROJ-123 --thread-id <thread-id>
```

When repos or change instructions are missing from the ticket, the agent will pause and prompt for them interactively
before starting.

## JIRA Ticket

Tickets do not need to follow a strict template. The agent extracts:

- **Repos** — from any GitHub URL (`https://github.com/org/repo`), `org/repo` reference, or bare repo name in the
  description. Can also be supplied via `--repo` on the command line.
- **Change instructions** — from the full ticket description sent as a prompt to Claude Code.

For best results, use `JIRA_DESCRIPTION_TEMPLATE.md` as your ticket description. Required sections:

| Section                    | Required            | Notes                                     |
|----------------------------|---------------------|-------------------------------------------|
| `## Affected Repositories` | ✅ (or use `--repo`) | GitHub URLs, one per line                 |
| `## Required Changes`      | ✅                   | Per-repo sub-headings with task items     |
| `## Acceptance Criteria`   | optional            | Helps Claude self-verify output           |
| `## Technical Notes`       | optional            | Language, style constraints, out-of-scope |
| `## Test Instructions`     | optional            | Commands Claude should run to verify      |

## Project Structure

```
j2p/
├── main.py                       # CLI entry point (--ticket, --repo, --thread-id)
├── graph.py                      # LangGraph workflow (nodes + routing)
├── state.py                      # AgentState TypedDict + dataclasses
├── config.py                     # Environment config
├── coding_agent.py               # Delegates code changes to claude CLI
├── jira_client.py                # Fetches JIRA tickets via claude CLI (JIRA MCP)
├── github_client.py              # Clone, commit, PR, and review via gh CLI + git
├── requirements.txt
├── .env.example
└── JIRA_DESCRIPTION_TEMPLATE.md  # Optional ticket description template
```
