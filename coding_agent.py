"""
coding_agent.py

Delegates all code changes to the locally installed Claude Code CLI.
Claude Code runs inside the cloned repo directory and uses its own built-in
file tools (read, write, edit, bash) — no Anthropic API key required.
"""

import re
import sys
import threading
import subprocess
from config import config


def run_coding_agent(
    repo_path: str,
    ticket_summary: str,
    ticket_description: str,
    review_feedback: str | None = None,
    conversation_history: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """
    Invoke the Claude Code CLI inside `repo_path` to implement the required changes.

    Claude Code has its own file system tools and runs autonomously — we just
    supply the task prompt and let it work.

    Returns:
        (commit_summary, conversation_history)
        conversation_history is always [] because Claude Code manages its own
        session state internally; we don't need to replay it.
    """
    if review_feedback:
        prompt = (
            "You are working inside a Git repository. "
            "A reviewer has left feedback on the pull request for this change. "
            "Review ALL of the feedback below carefully.\n\n"
            f"## PR Review Feedback\n{review_feedback}\n\n"
            "If any feedback requires code changes, make those changes now.\n"
            "- If you made changes, output ONLY a one-line git commit message "
            "summarising what you changed, prefixed with 'COMMIT: '.\n"
            "- If the feedback is informational, already addressed, or requires "
            "no code changes (e.g. questions, acknowledgements, out-of-scope), "
            "output exactly: NO_CHANGES_NEEDED"
        )
    else:
        prompt = (
            "You are working inside a Git repository. "
            "Implement ALL of the changes described in the JIRA ticket below. "
            "Explore the codebase first, then make the necessary file changes.\n\n"
            f"## JIRA Ticket: {ticket_summary}\n\n"
            f"{ticket_description}\n\n"
            "After making all changes, output ONLY a one-line git commit message "
            "summarising what you changed, prefixed with 'COMMIT: '."
        )

    print(f"[coding_agent] Running Claude Code in {repo_path} ...")
    proc = subprocess.Popen(
        [
            config.CLAUDE_CLI_PATH,
            "--print",
            "--debug",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repo_path,
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
        raise RuntimeError(f"Claude Code CLI failed (exit {proc.returncode})")

    output = "".join(stdout_lines).strip()

    # Claude signals that the review comment needs no code change
    if "NO_CHANGES_NEEDED" in output:
        return "NO_CHANGES_NEEDED", []

    # Extract the commit message from the output
    commit_summary = "chore: automated changes from JIRA ticket"
    match = re.search(r"COMMIT:\s*(.+)", output)
    if match:
        commit_summary = match.group(1).strip()

    # Return empty history — Claude Code manages its own session internally
    return commit_summary, []
