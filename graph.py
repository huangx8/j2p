import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from state import AgentState
from config import config
from jira_client import fetch_jira_ticket, detect_missing_info
from github_client import (
    clone_repo,
    commit_and_push,
    create_pull_request,
    find_existing_pr,
    has_new_comments_since,
    is_pr_merged_or_closed,
    is_pr_draft,
    reply_to_review_comment,
    reply_to_issue_comment,
)
from coding_agent import run_coding_agent


def _prompt_multiline(prompt_text: str) -> str:
    """
    Print *prompt_text* and collect lines from stdin until the user submits
    two consecutive blank lines (or EOF).  Returns the collected text stripped.
    """
    print(f"\n{prompt_text}")
    lines: list[str] = []
    blank_streak = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            blank_streak += 1
            if blank_streak >= 2:
                break
            lines.append(line)
        else:
            blank_streak = 0
            lines.append(line)
    # Remove trailing blank lines collected before the double-Enter
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def pr_check_node(state: AgentState) -> AgentState:
    """
    Check whether an open PR already exists for this ticket's branch,
    using repos already known in state (e.g. on a resumed run) or supplied
    via --repo CLI flags (extra_repos).
    On a fresh run with no repos available, falls through to jira_node.
    """
    branch_name = state["jira_ticket_key"].lower()

    # Collect all repo candidates: persisted ticket repos + CLI extra_repos
    repos_to_check: list[str] = []
    if state.get("jira_ticket") and state["jira_ticket"].get("repos"):
        repos_to_check = list(state["jira_ticket"]["repos"])
    for r in (state.get("extra_repos") or []):
        if r not in repos_to_check:
            repos_to_check.append(r)

    if not repos_to_check:
        return {**state, "status": "init"}

    existing_prs: list[dict] = []
    for repo_full_name in repos_to_check:
        pr_info = find_existing_pr(repo_full_name, branch_name)
        if pr_info:
            print(f"[pr_check_node] Found existing open PR #{pr_info.pr_number} for {repo_full_name}: {pr_info.pr_url}")
            existing_prs.append({
                "repo_full_name": repo_full_name,
                "branch_name": branch_name,
                "pr_number": pr_info.pr_number,
                "pr_url": pr_info.pr_url,
                "head_sha": pr_info.head_sha,
                "is_draft": pr_info.is_draft,
                "known_comment_ids": [],
            })

    if existing_prs:
        return {
            **state,
            "prs": existing_prs,
            "review_iteration": 0,
            "status": "pr_created",
        }

    print(f"[pr_check_node] No existing open PRs found — proceeding with full flow.")
    return {**state, "status": "init"}


def jira_node(state: AgentState) -> AgentState:
    print(f"[jira_node] Fetching ticket {state['jira_ticket_key']} ...")
    ticket = fetch_jira_ticket(state["jira_ticket_key"])
    branch_name = ticket.key.lower()

    # Merge CLI-supplied repos (extra_repos) with those found in the ticket.
    # CLI repos come first and duplicates are removed, preserving order.
    extra_repos: list[str] = list(state.get("extra_repos") or [])
    if extra_repos:
        merged = list(dict.fromkeys(extra_repos + ticket.repos))
        print(f"[jira_node] Merging CLI repos {extra_repos} with ticket repos {ticket.repos} → {merged}")
        ticket.repos = merged

    # After fetching the ticket, check if an open PR already exists for any repo.
    # If so, skip coding entirely and go straight to review watching.
    existing_prs: list[dict] = []
    for repo_full_name in ticket.repos:
        pr_info = find_existing_pr(repo_full_name, branch_name)
        if pr_info:
            print(f"[jira_node] Found existing open PR #{pr_info.pr_number} for {repo_full_name}: {pr_info.pr_url}")
            existing_prs.append({
                "repo_full_name": repo_full_name,
                "branch_name": branch_name,
                "pr_number": pr_info.pr_number,
                "pr_url": pr_info.pr_url,
                "head_sha": pr_info.head_sha,
                "is_draft": pr_info.is_draft,
                "known_comment_ids": [],
            })

    if existing_prs:
        print(f"[jira_node] Existing PRs found — skipping coding, jumping to review watcher.")
        return {
            **state,
            "jira_ticket": {
                "key": ticket.key,
                "summary": ticket.summary,
                "description": ticket.description,
                "repos": ticket.repos,
                "labels": ticket.labels,
            },
            "prs": existing_prs,
            "missing_info": [],
            "review_iteration": 0,
            "status": "pr_created",
            "coding_messages": [],
        }

    missing = detect_missing_info(ticket)

    if missing:
        print(f"[jira_node] Ticket is missing {len(missing)} required field(s) — will prompt user.")
        return {
            **state,
            "jira_ticket": {
                "key": ticket.key,
                "summary": ticket.summary,
                "description": ticket.description,
                "repos": ticket.repos,
                "labels": ticket.labels,
            },
            "missing_info": [
                {"field": m.field, "prompt": m.prompt, "required": m.required}
                for m in missing
            ],
            "prs": [],
            "review_iteration": 0,
            "status": "needs_clarification",
            "coding_messages": [],
        }

    print(f"[jira_node] Found repos: {ticket.repos}")
    return {
        **state,
        "jira_ticket": {
            "key": ticket.key,
            "summary": ticket.summary,
            "description": ticket.description,
            "repos": ticket.repos,
            "labels": ticket.labels,
        },
        "missing_info": [],
        "prs": [],
        "review_iteration": 0,
        "status": "coding",
        "coding_messages": [],
    }


def clarification_node(state: AgentState) -> AgentState:
    """
    Interactively prompts the user for any information that is missing from the
    JIRA ticket before handing off to the coding node.
    """
    from jira_client import extract_repos_from_text, _qualify_repo, detect_missing_info
    from state import JiraTicket

    ticket: dict = dict(state["jira_ticket"])
    missing_items: list[dict] = list(state.get("missing_info") or [])

    print("\n[clarification_node] Some information is needed before implementation can begin.")

    for item in missing_items:
        field = item["field"]
        answer = _prompt_multiline(item["prompt"]).strip()

        if not answer:
            if item.get("required", True):
                print(f"  ⚠  No answer provided for '{field}'. Skipping (may cause errors later).")
            continue

        if field == "repos":
            extra_repos = extract_repos_from_text(answer)
            if not extra_repos:
                # Accept raw "org/repo" or bare "repo" entries too
                extra_repos = [
                    _qualify_repo(line.strip().rstrip("/"))
                    for line in answer.splitlines()
                    if line.strip()
                ]
            # Filter out any repo names that still have no "/" and no default org
            valid = [r for r in extra_repos if "/" in r]
            invalid = [r for r in extra_repos if "/" not in r]
            if invalid:
                print(f"  ⚠  Could not qualify bare repo name(s) {invalid} — set GITHUB_DEFAULT_ORG or use 'org/repo' format.")
            existing = ticket.get("repos") or []
            ticket["repos"] = list(dict.fromkeys(existing + valid))
            print(f"  ✓  Repos updated: {ticket['repos']}")

        elif field == "required_changes":
            old_desc = ticket.get("description", "").strip()
            separator = "\n\n" if old_desc else ""
            ticket["description"] = (
                old_desc + separator +
                "## Required Changes\n" + answer
            )
            print("  ✓  Required changes added to description.")

        else:
            # Generic fallback: append the answer as an extra section
            old_desc = ticket.get("description", "").strip()
            separator = "\n\n" if old_desc else ""
            ticket["description"] = (
                old_desc + separator +
                f"## {field.replace('_', ' ').title()}\n" + answer
            )
            print(f"  ✓  '{field}' added to description.")

    print(f"[clarification_node] Clarification complete. Repos: {ticket.get('repos')}")

    # Re-check if anything is still missing after the user's answers
    tmp_ticket = JiraTicket(
        key=ticket.get("key", ""),
        summary=ticket.get("summary", ""),
        description=ticket.get("description", ""),
        repos=ticket.get("repos") or [],
        labels=ticket.get("labels") or [],
    )
    still_missing = detect_missing_info(tmp_ticket)
    if still_missing:
        print(f"[clarification_node] Still missing {len(still_missing)} field(s) — looping back for another round.")
        return {
            **state,
            "jira_ticket": ticket,
            "missing_info": [
                {"field": m.field, "prompt": m.prompt, "required": m.required}
                for m in still_missing
            ],
            "status": "needs_clarification",
        }

    # Check for existing open PRs on the (now resolved) repos before coding
    branch_name = ticket["key"].lower()
    existing_prs: list[dict] = []
    for repo_full_name in (ticket.get("repos") or []):
        pr_info = find_existing_pr(repo_full_name, branch_name)
        if pr_info:
            print(f"[clarification_node] Found existing open PR #{pr_info.pr_number} for {repo_full_name}: {pr_info.pr_url}")
            existing_prs.append({
                "repo_full_name": repo_full_name,
                "branch_name": branch_name,
                "pr_number": pr_info.pr_number,
                "pr_url": pr_info.pr_url,
                "head_sha": pr_info.head_sha,
                "is_draft": pr_info.is_draft,
                "known_comment_ids": [],
            })

    if existing_prs:
        return {
            **state,
            "jira_ticket": ticket,
            "missing_info": [],
            "prs": existing_prs,
            "review_iteration": 0,
            "status": "pr_created",
        }

    return {
        **state,
        "jira_ticket": ticket,
        "missing_info": [],
        "status": "coding",
    }


def coding_node(state: AgentState) -> AgentState:
    ticket = state.get("jira_ticket") or {}
    existing_prs: list[dict] = state.get("prs") or []

    # Build per-repo review feedback from review comments
    review_comments_all: list[dict] = state.get("review_comments") or []
    is_review_round = state.get("review_iteration", 0) > 0 and bool(review_comments_all)

    # Group comments by repo so each worker only sees its own feedback
    comments_by_repo: dict[str, list[dict]] = {}
    for c in review_comments_all:
        comments_by_repo.setdefault(c.get("repo", ""), []).append(c)

    # Derive repos and branch
    if ticket.get("repos"):
        repos: list[str] = ticket["repos"]
        branch_name: str = ticket["key"].lower()
    else:
        repos = list(dict.fromkeys(p["repo_full_name"] for p in existing_prs))
        branch_name = existing_prs[0]["branch_name"] if existing_prs else state["jira_ticket_key"].lower()

    # Lazily fetch ticket when bypassed by pr_check_node fast-path
    if not ticket and is_review_round:
        print("[coding_node] jira_ticket not in state — fetching ticket for coding context ...")
        try:
            fetched = fetch_jira_ticket(state["jira_ticket_key"])
            ticket = {
                "key": fetched.key,
                "summary": fetched.summary,
                "description": fetched.description,
                "repos": fetched.repos,
                "labels": fetched.labels,
            }
        except Exception as e:
            print(f"[coding_node] Warning: could not fetch ticket: {e}. Proceeding with empty context.")
            ticket = {"key": state["jira_ticket_key"], "summary": "", "description": "", "repos": repos, "labels": []}

    # Snapshot existing PR map for read-only lookups inside threads
    existing_pr_map: dict[str, dict] = {p["repo_full_name"]: p for p in existing_prs}

    # Each worker returns either an updated pr dict or raises
    def _process_repo(repo_full_name: str) -> dict:
        print(f"[coding_node] [{repo_full_name}] Starting ...")

        # If a PR already exists and this is not a review round, skip coding
        if repo_full_name in existing_pr_map and not is_review_round:
            existing_pr = find_existing_pr(repo_full_name, branch_name)
            if existing_pr:
                print(f"[coding_node] [{repo_full_name}] Open PR #{existing_pr.pr_number} already exists — skipping.")
                return {
                    "repo_full_name": repo_full_name,
                    "branch_name": branch_name,
                    "pr_number": existing_pr.pr_number,
                    "pr_url": existing_pr.pr_url,
                    "head_sha": existing_pr.head_sha,
                    "is_draft": existing_pr.is_draft,
                    "known_comment_ids": list(existing_pr_map[repo_full_name].get("known_comment_ids") or []),
                }

        local_path = clone_repo(repo_full_name, branch_name)

        # Build review feedback string scoped to this repo only
        repo_comments = comments_by_repo.get(repo_full_name, [])
        repo_review_feedback: str | None = None
        if is_review_round and repo_comments:
            lines = []
            for c in repo_comments:
                location = f" (in `{c['path']}` line {c['line']})" if c.get("path") else ""
                lines.append(f"- **{c['author']}**{location}: {c['body']}")
            repo_review_feedback = "\n".join(lines)

        commit_summary, _ = run_coding_agent(
            repo_path=local_path,
            ticket_summary=ticket.get("summary", ""),
            ticket_description=ticket.get("description", ""),
            review_feedback=repo_review_feedback,
            conversation_history=[],
        )

        sha = commit_and_push(local_path, branch_name, commit_summary)

        if sha:
            print(f"[coding_node] [{repo_full_name}] Pushed {sha[:8]}")
        elif commit_summary == "NO_CHANGES_NEEDED":
            print(f"[coding_node] [{repo_full_name}] No code changes needed.")
        else:
            print(f"[coding_node] [{repo_full_name}] No changes to commit.")

        # Build the updated pr entry from the existing one (if any)
        pr_entry: dict = {
            **(existing_pr_map.get(repo_full_name) or {}),
            "repo_full_name": repo_full_name,
            "branch_name": branch_name,
            "local_path": local_path,
            "commit_summary": commit_summary,
        }
        if sha:
            pr_entry["head_sha"] = sha

        # Reply to addressed review comments
        if repo_review_feedback and repo_comments and "pr_number" in pr_entry:
            reply_text = sha[:8] if sha else "No code changes required"
            known_ids = set(pr_entry.get("known_comment_ids") or [])
            for c in repo_comments:
                try:
                    if c.get("path"):
                        reply_to_review_comment(
                            repo_full_name, pr_entry["pr_number"],
                            c["comment_id"], reply_text,
                        )
                    else:
                        reply_to_issue_comment(
                            repo_full_name, pr_entry["pr_number"],
                            reply_text,
                        )
                    known_ids.add(c["comment_id"])
                    print(f"[coding_node] [{repo_full_name}] Replied to comment #{c['comment_id']} with '{reply_text}'")
                except Exception as e:
                    print(f"[coding_node] [{repo_full_name}] Warning: could not reply to comment #{c['comment_id']}: {e}")
            pr_entry["known_comment_ids"] = list(known_ids)

        return pr_entry

    # Run all repos concurrently
    print(f"[coding_node] Processing {len(repos)} repo(s) concurrently ...")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(len(repos), 1)) as executor:
        future_to_repo = {executor.submit(_process_repo, repo): repo for repo in repos}
        for future in as_completed(future_to_repo):
            repo = future_to_repo[future]
            exc = future.exception()
            if exc:
                print(f"[coding_node] [{repo}] ERROR: {exc}")
            else:
                results[repo] = future.result()

    # Merge: repos in order, then any existing PR entries for repos not in this run
    seen: set[str] = set(repos)
    updated_prs: list[dict] = [results[r] for r in repos if r in results]
    for p in existing_prs:
        if p["repo_full_name"] not in seen:
            updated_prs.append(p)

    return {
        **state,
        "prs": updated_prs,
        "coding_messages": [],
        "status": "pr_pending",
    }


def pr_node(state: AgentState) -> AgentState:
    ticket = state.get("jira_ticket") or {}
    ticket_key = ticket.get("key") or state["jira_ticket_key"]
    pr_title = f"[{ticket_key}] {ticket.get('summary', ticket_key)}"
    pr_body = (
        f"## JIRA Ticket\n[{ticket_key}]({config.JIRA_SERVER}/browse/{ticket_key})\n\n"
        f"## Summary\n{ticket.get('summary', '')}\n\n"
        f"## Description\n{ticket.get('description', '')}\n\n"
        "---\n_This PR was automatically generated by j2p._"
    )

    prs_in = list(state.get("prs") or [])

    def _ensure_pr(pr_data: dict) -> dict:
        repo = pr_data["repo_full_name"]
        branch = pr_data["branch_name"]

        if "pr_number" in pr_data:
            print(f"[pr_node] [{repo}] PR #{pr_data['pr_number']} already exists.")
            return pr_data

        existing = find_existing_pr(repo, branch)
        if existing:
            print(f"[pr_node] [{repo}] Found existing PR #{existing.pr_number}: {existing.pr_url}")
            return {
                **pr_data,
                "pr_number": existing.pr_number,
                "pr_url": existing.pr_url,
                "head_sha": existing.head_sha,
                "is_draft": existing.is_draft,
                "known_comment_ids": [],
            }

        print(f"[pr_node] [{repo}] Creating draft PR ...")
        pr_info = create_pull_request(
            repo_full_name=repo,
            branch_name=branch,
            title=pr_title,
            body=pr_body,
            local_repo_path=pr_data.get("local_path"),
        )
        print(f"[pr_node] [{repo}] Draft PR created: {pr_info.pr_url}")
        return {
            **pr_data,
            "pr_number": pr_info.pr_number,
            "pr_url": pr_info.pr_url,
            "head_sha": pr_info.head_sha,
            "is_draft": pr_info.is_draft,
            "known_comment_ids": [],
        }

    print(f"[pr_node] Creating/verifying {len(prs_in)} PR(s) concurrently ...")
    results_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(len(prs_in), 1)) as executor:
        future_to_repo = {executor.submit(_ensure_pr, pr_data): pr_data["repo_full_name"] for pr_data in prs_in}
        for future in as_completed(future_to_repo):
            repo = future_to_repo[future]
            exc = future.exception()
            if exc:
                print(f"[pr_node] [{repo}] ERROR creating PR: {exc}")
            else:
                results_map[repo] = future.result()

    # Preserve input order
    updated_prs = [results_map[p["repo_full_name"]] for p in prs_in if p["repo_full_name"] in results_map]

    return {**state, "prs": updated_prs, "status": "pr_created"}


def review_watcher_node(state: AgentState) -> AgentState:
    max_iterations = config.MAX_REVIEW_ITERATIONS
    poll_interval = config.PR_POLL_INTERVAL_SECONDS
    prs = state.get("prs") or []

    print(f"[review_watcher] Watching {len(prs)} PR(s) for review comments ...")
    print("[review_watcher] Press Ctrl+C at any time to stop watching and exit.")

    if state.get("review_iteration", 0) >= max_iterations:
        print("[review_watcher] Max review iterations reached. Finishing.")
        return {**state, "status": "done"}

    try:
        while True:
            all_terminal = True
            new_comments_found: list[dict] = []

            for pr_data in prs:
                repo_full_name = pr_data["repo_full_name"]
                pr_number = pr_data["pr_number"]

                # Check merged / closed
                is_terminal, pr_state = is_pr_merged_or_closed(repo_full_name, pr_number)
                if is_terminal:
                    if pr_state == "MERGED":
                        print(f"[review_watcher] PR #{pr_number} merged. ✓")
                    else:
                        print(f"[review_watcher] PR #{pr_number} was closed without merging.")
                    continue

                # Track draft→ready promotion but always monitor comments regardless of draft state
                still_draft = is_pr_draft(repo_full_name, pr_number)
                if pr_data.get("is_draft", False) and not still_draft:
                    print(f"[review_watcher] PR #{pr_number} is now ready for review — continuing to watch for comments.")
                    pr_data["is_draft"] = False

                all_terminal = False
                known_ids = set(pr_data.get("known_comment_ids") or [])
                has_new, new_comments = has_new_comments_since(repo_full_name, pr_number, known_ids)

                if has_new:
                    for c in new_comments:
                        new_comments_found.append({
                            "repo": repo_full_name,
                            "pr_number": c.pr_number,
                            "comment_id": c.comment_id,
                            "author": c.author,
                            "body": c.body,
                            "path": c.path,
                            "line": c.line,
                        })
                    # Do NOT add to known_comment_ids here — only mark known after coding addresses them

            if all_terminal:
                print("[review_watcher] All PRs are merged, closed, or ready for review. Done.")
                return {**state, "prs": prs, "status": "done"}

            if new_comments_found:
                print(f"[review_watcher] {len(new_comments_found)} new comment(s). Triggering update.")
                return {
                    **state,
                    "prs": prs,
                    "review_comments": new_comments_found,
                    "review_iteration": state.get("review_iteration", 0) + 1,
                    "status": "reviewing",
                }

            print(f"[review_watcher] No new comments. Sleeping {poll_interval}s ...")
            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n[review_watcher] Interrupted by user (Ctrl+C). Exiting cleanly.")
        return {**state, "prs": prs, "status": "done"}


def route_after_pr_check(state: AgentState) -> Literal["review_watcher_node", "jira_node"]:
    if state["status"] == "pr_created":
        return "review_watcher_node"
    return "jira_node"


def route_after_jira(state: AgentState) -> Literal["clarification_node", "coding_node", "review_watcher_node", "__end__"]:
    if state["status"] == "error":
        return END
    if state["status"] == "needs_clarification":
        return "clarification_node"
    if state["status"] == "pr_created":
        return "review_watcher_node"
    return "coding_node"


def route_after_clarification(state: AgentState) -> Literal["clarification_node", "coding_node", "review_watcher_node"]:
    if state["status"] == "needs_clarification":
        return "clarification_node"
    if state["status"] == "pr_created":
        return "review_watcher_node"
    return "coding_node"


def route_after_review_watcher(state: AgentState) -> Literal["coding_node", "__end__"]:
    if state["status"] == "reviewing":
        return "coding_node"
    return END


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("pr_check_node", pr_check_node)
    graph.add_node("jira_node", jira_node)
    graph.add_node("clarification_node", clarification_node)
    graph.add_node("coding_node", coding_node)
    graph.add_node("pr_node", pr_node)
    graph.add_node("review_watcher_node", review_watcher_node)

    graph.set_entry_point("pr_check_node")

    graph.add_conditional_edges("pr_check_node", route_after_pr_check)
    graph.add_conditional_edges("jira_node", route_after_jira)
    graph.add_conditional_edges("clarification_node", route_after_clarification)
    graph.add_edge("coding_node", "pr_node")
    graph.add_edge("pr_node", "review_watcher_node")
    graph.add_conditional_edges("review_watcher_node", route_after_review_watcher)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)



app = build_graph()

