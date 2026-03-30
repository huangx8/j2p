from typing import TypedDict, Optional, List
from dataclasses import dataclass, field


@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str
    repos: List[str]  # extracted GitHub repo full names e.g. ["org/repo1", "org/repo2"]
    labels: List[str] = field(default_factory=list)
    assignee: Optional[str] = None


@dataclass
class PRInfo:
    repo_full_name: str
    pr_number: int
    pr_url: str
    branch_name: str
    head_sha: str
    is_draft: bool = False


@dataclass
class ReviewComment:
    pr_number: int
    comment_id: int
    author: str
    body: str
    path: Optional[str] = None        # file path for inline comments
    line: Optional[int] = None        # line number for inline comments
    in_reply_to_id: Optional[int] = None  # set for inline reply comments


@dataclass
class MissingInfo:
    """Describes a single piece of information that must be supplied interactively."""
    field: str          # internal field name, e.g. "repos" or "required_changes"
    prompt: str         # human-readable question shown to the user
    required: bool = True


class AgentState(TypedDict, total=False):
    # Input
    jira_ticket_key: str

    # JIRA data
    jira_ticket: Optional[dict]

    # Per-repo working state (keyed by repo full name)
    current_repo: Optional[str]
    branch_name: Optional[str]
    local_repo_path: Optional[str]

    # PR tracking
    prs: Optional[List[dict]]           # list of PRInfo dicts
    review_comments: Optional[List[dict]]  # latest unresolved review comments

    # Iteration control
    review_iteration: int
    # "init" | "needs_clarification" | "coding" | "pr_pending" | "pr_created" | "reviewing" | "done" | "error"
    status: str
    error_message: Optional[str]

    # Clarification support
    missing_info: Optional[List[dict]]  # list of MissingInfo dicts (serialised)

    # LLM conversation history for coding context
    coding_messages: Optional[List[dict]]

    # Repos explicitly supplied via CLI (override / supplement ticket repos)
    extra_repos: Optional[List[str]]

