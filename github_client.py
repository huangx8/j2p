import json
import time
import shutil
import subprocess
from pathlib import Path
from config import config
from state import PRInfo, ReviewComment

_TRANSIENT_ERRORS = ("Bad Gateway", "Service Unavailable", "rate limit", "timeout", "connection reset")


def _run_gh(*args: str, cwd: str | None = None, retries: int = 3, retry_delay: float = 10.0) -> str:
    """Run a `gh` CLI command, return stdout. Retries on transient network errors."""
    last_error: RuntimeError | None = None
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            [config.GH_CLI_PATH, *args],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        err = result.stderr.strip()
        if any(t in err for t in _TRANSIENT_ERRORS) and attempt < retries:
            print(f"[gh] Transient error (attempt {attempt}/{retries}), retrying in {retry_delay}s: {err[:120]}")
            time.sleep(retry_delay)
            last_error = RuntimeError(f"gh command failed: gh {' '.join(args)}\n{err}")
            continue
        raise RuntimeError(f"gh command failed: gh {' '.join(args)}\n{err}")
    raise last_error


def _run_git(*args: str, cwd: str) -> str:
    """Run a `git` command inside cwd. Raises RuntimeError on failure."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed: git {' '.join(args)}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Repo cloning / checkout
# ---------------------------------------------------------------------------

def _find_local_repo(repo_full_name: str) -> Path | None:
    """
    Look for an existing local checkout under LOCAL_REPOS_DIR.

    Supports two layouts:
      - org-scoped : <LOCAL_REPOS_DIR>/<org>/<repo>   (mirrors GitHub structure)
      - flat       : <LOCAL_REPOS_DIR>/<repo>          (just the repo name)

    Returns the Path if a valid git repo is found, otherwise None.
    """
    if not config.LOCAL_REPOS_DIR:
        return None

    base = Path(config.LOCAL_REPOS_DIR).expanduser()
    repo_name = repo_full_name.split("/")[-1]
    candidates = [
        base / repo_full_name,   # org-scoped: myorg/myrepo
        base / repo_name,        # flat:       myrepo
    ]
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def clone_repo(repo_full_name: str, branch_name: str) -> str:
    """
    Prepare a local working copy of *repo_full_name* on *branch_name*.

    Strategy:
    1. If LOCAL_REPOS_DIR is set and the repo is found there, use that checkout
       in-place (fetch + create/reset the feature branch from the base branch).
    2. Otherwise, clone via `gh repo clone` into WORKSPACE_DIR as before.

    Returns the absolute local path.
    """
    local = _find_local_repo(repo_full_name)

    if local:
        print(f"[clone_repo] Using existing local checkout: {local}")
        _run_git("fetch", "origin", cwd=str(local))

        # Check if the feature branch already exists on the remote (i.e. a PR is open).
        # If so, check it out from origin so we don't discard existing PR commits.
        remote_branches = _run_git("branch", "-r", cwd=str(local))
        branch_exists_on_remote = f"origin/{branch_name}" in remote_branches

        if branch_exists_on_remote:
            # Resume from the existing remote branch tip
            print(f"[clone_repo] Branch {branch_name} exists on remote — resuming from origin/{branch_name}")
            try:
                _run_git("checkout", "-b", branch_name, "--track", f"origin/{branch_name}", cwd=str(local))
            except RuntimeError:
                _run_git("checkout", branch_name, cwd=str(local))
                _run_git("reset", "--hard", f"origin/{branch_name}", cwd=str(local))
        else:
            # Remote branch is gone — start completely fresh from base.
            # Delete the stale local branch if it exists so we don't accidentally
            # reuse old commits that were part of a now-closed/deleted PR.
            print(f"[clone_repo] Branch {branch_name} not on remote — starting fresh from {config.GITHUB_BASE_BRANCH}")
            _run_git("checkout", config.GITHUB_BASE_BRANCH, cwd=str(local))
            _run_git("reset", "--hard", f"origin/{config.GITHUB_BASE_BRANCH}", cwd=str(local))
            # Force-delete the stale local branch if present
            try:
                _run_git("branch", "-D", branch_name, cwd=str(local))
                print(f"[clone_repo] Deleted stale local branch {branch_name}")
            except RuntimeError:
                pass  # branch didn't exist locally — that's fine
            _run_git("checkout", "-b", branch_name, cwd=str(local))

        return str(local)

    # ── Fallback: fresh clone into WORKSPACE_DIR ────────────────────────────
    print(f"[clone_repo] No local checkout found for {repo_full_name}, cloning ...")
    workspace = Path(config.WORKSPACE_DIR)
    repo_dir = workspace / repo_full_name.replace("/", "_") / branch_name

    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    _run_gh("repo", "clone", repo_full_name, str(repo_dir))
    _run_git("checkout", config.GITHUB_BASE_BRANCH, cwd=str(repo_dir))
    _run_git("checkout", "-b", branch_name, cwd=str(repo_dir))

    return str(repo_dir)


def commit_and_push(local_repo_path: str, branch_name: str, commit_message: str) -> str | None:
    """
    Stage all changes, commit, and push to remote. Returns HEAD sha, or None if
    there was nothing to commit (no changes made by the coding agent).
    """
    _run_git("add", "-A", cwd=local_repo_path)

    status = _run_git("status", "--porcelain", cwd=local_repo_path)
    if not status:
        print(f"[commit_and_push] No changes to commit on {branch_name} — skipping push.")
        return None

    _run_git("commit", "-m", commit_message, cwd=local_repo_path)
    _run_git("push", "origin", branch_name, cwd=local_repo_path)

    return _run_git("rev-parse", "HEAD", cwd=local_repo_path)


# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------

def find_existing_pr(repo_full_name: str, branch_name: str) -> PRInfo | None:
    """
    Look for an open PR on *repo_full_name* whose head branch is *branch_name*.
    Returns a PRInfo if found, or None if no open PR exists for that branch.
    """
    try:
        pr_json = _run_gh(
            "pr", "view", branch_name,
            "--repo", repo_full_name,
            "--json", "number,headRefOid,url,state,isDraft",
        )
    except RuntimeError:
        return None

    pr_data = json.loads(pr_json)
    if pr_data.get("state", "").upper() != "OPEN":
        return None

    return PRInfo(
        repo_full_name=repo_full_name,
        pr_number=pr_data["number"],
        pr_url=pr_data["url"],
        branch_name=branch_name,
        head_sha=pr_data["headRefOid"],
        is_draft=pr_data.get("isDraft", False),
    )


def _has_closed_pr(repo_full_name: str, branch_name: str) -> bool:
    """Return True if there is a CLOSED (not merged) PR for the branch."""
    try:
        pr_json = _run_gh(
            "pr", "view", branch_name,
            "--repo", repo_full_name,
            "--json", "state",
        )
    except RuntimeError:
        return False
    return json.loads(pr_json).get("state", "").upper() == "CLOSED"


def create_pull_request(
    repo_full_name: str,
    branch_name: str,
    title: str,
    body: str,
    local_repo_path: str | None = None,
) -> PRInfo:
    """
    Create a draft GitHub PR using `gh pr create`.
    Closed PRs are ignored — if one exists the remote branch may have been
    deleted when it was closed, so we re-push from the local checkout before
    creating a fresh draft PR.
    Returns PRInfo.
    """
    # When a closed PR exists, GitHub may have deleted the remote branch.
    # Re-push the local branch so `gh pr create` can find the commits.
    if _has_closed_pr(repo_full_name, branch_name):
        if local_repo_path:
            print(f"[create_pull_request] Closed PR found for {repo_full_name}:{branch_name} — re-pushing branch to allow fresh PR.")
            _run_git("push", "origin", branch_name, cwd=local_repo_path)
        else:
            raise RuntimeError(
                f"Closed PR exists for {repo_full_name}:{branch_name} but no local_repo_path provided to re-push."
            )

    try:
        _run_gh(
            "pr", "create",
            "--repo", repo_full_name,
            "--head", branch_name,
            "--base", config.GITHUB_BASE_BRANCH,
            "--title", title,
            "--body", body,
            "--draft",
        )
    except RuntimeError as e:
        # PR may already exist — that's fine, proceed to fetch its metadata
        if "already exists" not in str(e):
            raise

    # Fetch PR metadata (number, head sha, url, draft status) via gh pr view
    pr_json = _run_gh(
        "pr", "view", branch_name,
        "--repo", repo_full_name,
        "--json", "number,headRefOid,url,isDraft",
    )
    pr_data = json.loads(pr_json)

    return PRInfo(
        repo_full_name=repo_full_name,
        pr_number=pr_data["number"],
        pr_url=pr_data["url"],
        branch_name=branch_name,
        head_sha=pr_data["headRefOid"],
        is_draft=pr_data.get("isDraft", False),
    )


def update_pull_request_body(repo_full_name: str, pr_number: int, body: str) -> None:
    _run_gh(
        "pr", "edit", str(pr_number),
        "--repo", repo_full_name,
        "--body", body,
    )


# ---------------------------------------------------------------------------
# PR Review Comments
# ---------------------------------------------------------------------------

def get_pr_review_comments(repo_full_name: str, pr_number: int) -> list[ReviewComment]:
    """
    Fetch all review comments + general PR comments via `gh api`.
    Returns a combined list of ReviewComment objects.
    """
    comments: list[ReviewComment] = []

    # Inline review comments (pull request review threads)
    review_json = _run_gh(
        "api",
        f"repos/{repo_full_name}/pulls/{pr_number}/comments",
        "--paginate",
    )
    for c in json.loads(review_json):
        comments.append(ReviewComment(
            pr_number=pr_number,
            comment_id=c["id"],
            author=c["user"]["login"],
            body=c["body"],
            path=c.get("path"),
            line=c.get("original_line"),
            in_reply_to_id=c.get("in_reply_to_id"),
        ))

    # General issue comments on the PR
    issue_json = _run_gh(
        "api",
        f"repos/{repo_full_name}/issues/{pr_number}/comments",
        "--paginate",
    )
    for c in json.loads(issue_json):
        comments.append(ReviewComment(
            pr_number=pr_number,
            comment_id=c["id"],
            author=c["user"]["login"],
            body=c["body"],
        ))

    return comments


def reply_to_review_comment(
    repo_full_name: str, pr_number: int, comment_id: int, reply: str
) -> None:
    """Reply to an inline pull request review comment."""
    body = reply if reply == "No code changes required" else f"Addressed in {reply}"
    _run_gh(
        "api", "--method", "POST",
        f"repos/{repo_full_name}/pulls/{pr_number}/comments/{comment_id}/replies",
        "--field", f"body={body}",
    )


def reply_to_issue_comment(
    repo_full_name: str, pr_number: int, reply: str
) -> None:
    """Post a follow-up issue comment on the PR."""
    body = reply if reply == "No code changes required" else f"Addressed in {reply}"
    _run_gh(
        "api", "--method", "POST",
        f"repos/{repo_full_name}/issues/{pr_number}/comments",
        "--field", f"body={body}",
    )


def has_new_comments_since(
    repo_full_name: str, pr_number: int, known_comment_ids: set[int]
) -> tuple[bool, list[ReviewComment]]:
    """
    Returns (has_new, new_comments_list).
    Filters out:
    - Comments already in known_comment_ids (i.e. already addressed)
    - j2p's own reply/acknowledgement comments
    """
    all_comments = get_pr_review_comments(repo_full_name, pr_number)

    _OWN_PREFIXES = ("Addressed in ", "No code changes required")
    new = [
        c for c in all_comments
        if c.comment_id not in known_comment_ids
        and not any(c.body.startswith(p) for p in _OWN_PREFIXES)
    ]
    return len(new) > 0, new


def is_pr_merged_or_closed(repo_full_name: str, pr_number: int) -> tuple[bool, str]:
    """
    Returns (is_terminal, state) where state is 'MERGED', 'CLOSED', or 'OPEN'.
    is_terminal is True when the PR is no longer open (merged or closed).
    """
    state = _run_gh(
        "pr", "view", str(pr_number),
        "--repo", repo_full_name,
        "--json", "state",
        "--jq", ".state",
    )
    state = state.upper()
    return state in ("CLOSED", "MERGED"), state


def is_pr_draft(repo_full_name: str, pr_number: int) -> bool:
    """Returns True if the PR is still in draft state."""
    result = _run_gh(
        "pr", "view", str(pr_number),
        "--repo", repo_full_name,
        "--json", "isDraft",
        "--jq", ".isDraft",
    )
    return result.strip().lower() == "true"

