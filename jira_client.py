import re
import sys
import json
import threading
import subprocess
from config import config
from state import JiraTicket, MissingInfo

REPO_PATTERNS = [
    r"https://github\.com/([\w.-]+/[\w.-]+)",
    r"\[\[([\w.-]+/[\w.-]+)\]\]",
    r"(?:repo|repository)[:\s]+([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)",
    # bare repo name (no slash) — only matched when GITHUB_DEFAULT_ORG is set
    r"(?:repo|repository)[:\s]+([a-zA-Z0-9_.-]+(?!/)[a-zA-Z0-9_.-]*)",
]


def _qualify_repo(repo: str) -> str:
    """
    Ensure *repo* is in "owner/repo" format.
    If it contains no "/" and GITHUB_DEFAULT_ORG is configured,
    prepend the default org.  Otherwise return as-is (and let `gh` error out
    with a clear message rather than silently using a wrong name).
    """
    if "/" not in repo:
        org = config.GITHUB_DEFAULT_ORG.strip()
        if org:
            return f"{org}/{repo}"
        # Can't qualify — return the bare name so the error is visible downstream
    return repo

_SECTION_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)


def _run_claude(prompt: str, cwd: str | None = None) -> str:
    """
    Invoke the local claude CLI with --print and --debug flags (non-interactive).
    Uses --permission-mode bypassPermissions so MCP tools (JIRA, etc.) are
    allowed without interactive prompts.
    Reads stdout/stderr concurrently via threads to prevent pipe deadlocks.
    Raises RuntimeError if the process exits non-zero.
    """
    proc = subprocess.Popen(
        [
            config.CLAUDE_CLI_PATH,
            "--print",
            "--debug",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", "mcp__atlassian",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    # Write prompt to stdin and close it so claude knows input is complete
    proc.stdin.write(prompt)
    proc.stdin.close()

    stdout_lines: list[str] = []

    def _stream_stdout():
        for line in proc.stdout:
            print(line, end="", flush=True)
            stdout_lines.append(line)

    def _stream_stderr():
        for line in proc.stderr:
            print(line, end="", file=sys.stderr, flush=True)

    t_out = threading.Thread(target=_stream_stdout, daemon=True)
    t_err = threading.Thread(target=_stream_stderr, daemon=True)
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode})")

    return "".join(stdout_lines).strip()


def _extract_sections(text: str) -> dict[str, str]:
    """
    Split a markdown description into {section_title_lower: section_body} pairs.
    E.g. "## Required Changes\n..." -> {"required changes": "..."}
    """
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, match in enumerate(matches):
        title = match.group(1).strip().lower()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
        sections[title] = body
    return sections


def _strip_placeholder_lines(text: str) -> str:
    """Remove unfilled template placeholder lines like '[Replace with ...]'."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^\[.+\]$", stripped) or stripped in ("", "N/A"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_repos_from_text(text: str) -> list[str]:
    """Extract GitHub repo full names from any free-form text."""
    repos: set[str] = set()
    if not text:
        return []
    for pattern in REPO_PATTERNS:
        for match in re.findall(pattern, text, re.IGNORECASE):
            repo = match.rstrip(".git").strip("/")
            repo = _qualify_repo(repo)
            repos.add(repo)
    return list(repos)


def build_agent_prompt(sections: dict[str, str], summary: str, description: str) -> str:
    """
    Build a structured prompt for the coding agent from parsed template sections.
    Falls back to the raw description if no template sections are detected.
    """
    if not sections:
        return description

    known_headings = {
        "summary", "background / context", "background", "context",
        "affected repositories", "repositories", "repos",
        "required changes", "changes required", "changes",
        "acceptance criteria", "acceptance", "criteria",
        "technical notes", "technical", "notes", "implementation notes",
        "test instructions", "testing", "tests",
        "dependencies / blockers", "dependencies", "blockers",
    }

    parts = [f"## Ticket Summary\n{summary}"]

    for heading in ("background / context", "background", "context"):
        if heading in sections and sections[heading]:
            parts.append(f"## Background\n{_strip_placeholder_lines(sections[heading])}")
            break

    changes_parts = []
    for heading in ("required changes", "changes required", "changes"):
        if heading in sections:
            body = _strip_placeholder_lines(sections[heading])
            if body:
                changes_parts.append(body)
            break

    for heading, body in sections.items():
        if heading not in known_headings and body:
            changes_parts.append(f"### {heading.title()}\n{_strip_placeholder_lines(body)}")

    if changes_parts:
        parts.append("## Required Changes\n" + "\n\n".join(changes_parts))

    for heading in ("acceptance criteria", "acceptance", "criteria"):
        if heading in sections and sections[heading]:
            parts.append(f"## Acceptance Criteria\n{_strip_placeholder_lines(sections[heading])}")
            break

    for heading in ("technical notes", "technical", "notes", "implementation notes"):
        if heading in sections and sections[heading]:
            parts.append(f"## Technical Notes\n{_strip_placeholder_lines(sections[heading])}")
            break

    for heading in ("test instructions", "testing", "tests"):
        if heading in sections and sections[heading]:
            parts.append(f"## Test Instructions\n{_strip_placeholder_lines(sections[heading])}")
            break

    return "\n\n".join(parts)


def fetch_jira_ticket(ticket_key: str) -> JiraTicket:
    """
    Fetch a JIRA ticket via the Claude Code CLI (which has JIRA MCP access).
    Asks Claude to return the ticket fields as JSON, then parses the result.
    """
    prompt = (
        f"Use the atlassian MCP to fetch ticket {ticket_key}. "
        f"Return ONLY a JSON object with exactly these keys: "
        f"summary, description, labels (array of strings), assignee (string or null). "
        f"No explanation, no markdown fences - raw JSON only."
    )
    raw = _run_claude(prompt)

    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise RuntimeError(f"Could not parse JSON from claude output:\n{raw}")
    data = json.loads(json_match.group())

    summary = data.get("summary", "")
    description = data.get("description", "")
    labels = data.get("labels", [])
    assignee = data.get("assignee")

    sections = _extract_sections(description)

    repos: list[str] = []
    for heading in ("affected repositories", "repositories", "repos"):
        if heading in sections:
            repos = extract_repos_from_text(sections[heading])
            break
    if not repos:
        repos = extract_repos_from_text(description)

    repos = list(set(repos))
    agent_description = build_agent_prompt(sections, summary, description)

    return JiraTicket(
        key=ticket_key,
        summary=summary,
        description=agent_description,
        repos=repos,
        labels=labels,
        assignee=assignee,
    )



def detect_missing_info(ticket: JiraTicket) -> list[MissingInfo]:
    """
    Inspect a parsed JiraTicket and return a list of MissingInfo items for any
    information that is required before the coding agent can start work.
    """
    missing: list[MissingInfo] = []

    if not ticket.repos:
        org_hint = f" or bare name (e.g. myrepo → {config.GITHUB_DEFAULT_ORG}/myrepo)" if config.GITHUB_DEFAULT_ORG else ""
        missing.append(MissingInfo(
            field="repos",
            prompt=(
                "No GitHub repositories were found in the ticket description.\n"
                f"Please enter one or more repos, one per line — accepted formats:\n"
                f"  • Full URL : https://github.com/myorg/myrepo\n"
                f"  • owner/repo: myorg/myrepo\n"
                f"  • Bare name{org_hint}\n"
                "Press Enter twice when done:"
            ),
            required=True,
        ))

    # Check whether the description contains any meaningful change instructions
    description_text = ticket.description.strip()
    change_keywords = [
        "required changes", "changes required", "changes",
        "todo", "task", "implement", "add", "remove", "update", "fix", "refactor",
        "- [ ]", "* [ ]",
    ]
    has_changes = any(kw in description_text.lower() for kw in change_keywords)
    if not has_changes or len(description_text) < 30:
        missing.append(MissingInfo(
            field="required_changes",
            prompt=(
                "The ticket description does not contain clear implementation instructions.\n"
                "Please describe what changes need to be made (you can use plain text or\n"
                "bullet points). Press Enter twice when done:"
            ),
            required=True,
        ))

    return missing
