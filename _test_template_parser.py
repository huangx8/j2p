from jira_client import _extract_sections, extract_repos_from_text, build_agent_prompt

sample = (
    "\n## Summary\n"
    "Add request timeout support to the HTTP client.\n\n"
    "## Background / Context\n"
    "In production, outbound HTTP calls to third-party APIs occasionally hang\n"
    "indefinitely because no timeout is set on the underlying `http.Client`.\n"
    "This causes goroutine leaks and cascading failures under load.\n\n"
    "## Affected Repositories\n"
    "- https://github.com/myorg/backend\n"
    "- https://github.com/myorg/shared-libs\n\n"
    "## Required Changes\n\n"
    "### backend\n"
    "- [ ] Add a `timeout time.Duration` field to the `HTTPClient` struct in `internal/httpclient/client.go`\n"
    "- [ ] Update `NewHTTPClient()` to accept a `WithTimeout(d time.Duration)` functional option\n"
    "- [ ] Set a default timeout of 30s when no option is provided\n"
    "- [ ] Propagate the timeout via `http.Client{Timeout: c.timeout}` in all call sites inside `internal/services/`\n\n"
    "### shared-libs\n"
    "- [ ] Export `const DefaultHTTPTimeout = 30 * time.Second` from `pkg/defaults/defaults.go`\n"
    "- [ ] Update `pkg/httpclient/` to import and use the new constant\n\n"
    "## Acceptance Criteria\n"
    "- [ ] `go build ./...` passes with no errors\n"
    "- [ ] `go vet ./...` reports no issues\n"
    "- [ ] Unit tests added in `internal/httpclient/client_test.go` covering default and custom timeouts\n"
    "- [ ] No breaking changes to the exported `HTTPClient` interface\n\n"
    "## Technical Notes\n"
    "- Language/framework: Go 1.23, module `github.com/myorg/backend`\n"
    "- Style guide: use functional options pattern, `context.Context` as first arg on all methods\n"
    "- Follow existing patterns in `internal/httpclient/`\n"
    "- Out of scope: do not modify `api/proto/` generated files\n\n"
    "## Test Instructions\n"
    "go test ./internal/httpclient/... -v -run TestHTTPClient\n"
    "go vet ./...\n"
)

sections = _extract_sections(sample)
print("=== Parsed sections ===")
for k, v in sections.items():
    print(f"  [{k}]: {v[:80].replace(chr(10), ' ')} ...")

repos = extract_repos_from_text(sections.get("affected repositories", ""))
print("\n=== Extracted repos ===")
for r in repos:
    print(f"  {r}")

prompt = build_agent_prompt(sections, "Add request timeout support to the HTTP client", sample)
print("\n=== Agent prompt ===")
print(prompt)
