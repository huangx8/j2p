#!/usr/bin/env python3
"""
main.py — Entry point for j2p agent.

Usage:
    python main.py --ticket PROJ-123
    python main.py --ticket PROJ-123 --thread-id my-run-id
    python main.py --ticket PROJ-123 --repo org/repo1,org/repo2
"""

import argparse
import uuid

from config import config
from graph import app
from jira_client import _qualify_repo


def main():
    parser = argparse.ArgumentParser(
        description="j2p: Automatically implement JIRA tickets as GitHub PRs using Claude claude-sonnet-4-5."
    )
    parser.add_argument(
        "--ticket",
        required=True,
        help="JIRA ticket key, e.g. PROJ-123",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="LangGraph thread ID for resuming a previous run (optional).",
    )
    parser.add_argument(
        "--repo",
        dest="repos",
        default="",
        metavar="OWNER/REPO[,OWNER/REPO,...]",
        help=(
            "Comma-separated list of GitHub repositories to target, "
            "e.g. myorg/repo1,myorg/repo2. "
            "When provided these repos are used instead of (or merged with) "
            "any repos found in the JIRA ticket description."
        ),
    )
    args = parser.parse_args()

    # Split comma-separated repos and qualify any bare names
    extra_repos = [
        _qualify_repo(r.strip())
        for r in args.repos.split(",")
        if r.strip()
    ]

    # Validate config
    config.validate()

    thread_id = args.thread_id or str(uuid.uuid4())
    print(f"\n{'='*60}")
    print("  j2p Agent")
    print(f"  Ticket  : {args.ticket}")
    print(f"  Claude  : {config.CLAUDE_CLI_PATH}")
    print(f"  Thread  : {thread_id}")
    if extra_repos:
        print(f"  Repos   : {', '.join(extra_repos)}")
    print(f"{'='*60}\n")

    initial_state = {
        "jira_ticket_key": args.ticket,
        "jira_ticket": None,
        "current_repo": None,
        "branch_name": None,
        "local_repo_path": None,
        "prs": None,
        "review_comments": None,
        "review_iteration": 0,
        "status": "init",
        "error_message": None,
        "missing_info": None,
        "coding_messages": None,
        "extra_repos": extra_repos or None,
    }

    config_obj = {"configurable": {"thread_id": thread_id}}

    try:
        final_state = app.invoke(initial_state, config=config_obj)
    except KeyboardInterrupt:
        print(f"\n{'='*60}")
        print("  Interrupted — exiting.")
        print(f"  Resume later with: --ticket {args.ticket} --thread-id {thread_id}")
        print(f"{'='*60}\n")
        return

    print(f"\n{'='*60}")
    print(f"  Final status: {final_state.get('status')}")

    if final_state.get("status") == "error":
        print(f"  Error: {final_state.get('error_message')}")
    else:
        prs = final_state.get("prs") or []
        for pr in prs:
            print(f"  PR: {pr.get('pr_url', 'N/A')}  [{pr['repo_full_name']}]")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

