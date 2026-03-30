<!--
  j2p JIRA Description Template
  ══════════════════════════════
  Sections marked [REQUIRED] must be present — the agent cannot run without them.
  Sections marked [OPTIONAL] can be deleted entirely with no impact on the agent.
  The more optional detail you provide, the better the code output will be.
-->


## Affected Repositories
<!-- [REQUIRED] — the agent extracts repo URLs from this section to know what to clone.
     Without this section (or valid GitHub URLs anywhere in the description), the
     agent will abort with "No GitHub repos found".
     Use full URLs, one per line. -->
- https://github.com/your-org/repo-one
- https://github.com/your-org/repo-two


## Required Changes
<!-- [REQUIRED] — this is the core task prompt sent to Claude Code.
     Without this section the agent will have no instructions and produce no changes.
     Use per-repo sub-headings (### repo-name) with concrete, specific task items. -->

### repo-one
- [ ] [Describe change 1 — e.g. "Add a `timeout time.Duration` field to `HTTPClient` struct in `internal/httpclient/client.go` with a default of 30s"]
- [ ] [Describe change 2 — e.g. "Update `NewHTTPClient()` constructor to accept a `WithTimeout(d time.Duration)` functional option"]
- [ ] [Describe change 3 — e.g. "Propagate timeout to all `http.NewRequestWithContext` call sites in `internal/services/`"]

### repo-two
- [ ] [Describe change 1]


## Summary
<!-- [OPTIONAL] — provides extra context in the prompt but the ticket title is already used.
     Safe to delete. -->
[Replace with a clear, concise description of the change]


## Background / Context
<!-- [OPTIONAL] — helps Claude understand the "why" behind the change.
     Safe to delete, but recommended for non-trivial changes. -->
[Replace with relevant background, or delete this section]


## Acceptance Criteria
<!-- [OPTIONAL] — included in the prompt as a success checklist for Claude.
     Safe to delete, but helps Claude self-verify its output. -->
- [ ] [e.g. `go build ./...` passes with no errors]
- [ ] [e.g. `go vet ./...` reports no issues]
- [ ] [e.g. Unit tests added in `_test.go` files covering the new behaviour]
- [ ] [e.g. No breaking changes to exported interfaces]


## Technical Notes
<!-- [OPTIONAL] — guides Claude on language version, style, and scope constraints.
     Safe to delete, but strongly recommended to avoid off-pattern code. -->
- Language/framework: [e.g. Go 1.23, `github.com/your-org/repo-one`]
- Style guide: [e.g. follow existing patterns in `internal/httpclient/`, use `context.Context` as first arg]
- Out of scope: [e.g. do not modify `api/proto/` generated files]


## Test Instructions
<!-- [OPTIONAL] — included in the prompt so Claude knows how to verify the change.
     Safe to delete. -->
```bash
# Example:
go test ./internal/httpclient/... -v -run TestHTTPClient
go vet ./...
```


## Dependencies / Blockers
<!-- [OPTIONAL] — not parsed by the agent at all. For human reference only.
     Safe to delete. -->
- Blocked by: [PROJ-000 or N/A]
