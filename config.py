import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # GitHub — authenticated via local `gh` CLI (no token needed)
    GH_CLI_PATH: str = os.getenv("GH_CLI_PATH", "gh")
    GITHUB_BASE_BRANCH: str = os.getenv("GITHUB_BASE_BRANCH", "main")
    # Default GitHub org/owner to prepend when a bare repo name (no "/") is encountered.
    # E.g. set to "myorg" so "myrepo" becomes "myorg/myrepo".
    GITHUB_DEFAULT_ORG: str = os.getenv("GITHUB_DEFAULT_ORG", "")

    # Claude Code CLI — path to the locally installed `claude` binary
    # Claude Code already has JIRA + Anthropic access configured.
    CLAUDE_CLI_PATH: str = os.getenv("CLAUDE_CLI_PATH", "claude")

    # JIRA — only the server URL is needed (for building browse links in PRs)
    JIRA_SERVER: str = os.getenv("JIRA_SERVER", "")

    # Agent
    # Directory where repos are already checked out locally.
    # If set, the agent will look for <LOCAL_REPOS_DIR>/<repo-name> before cloning.
    # Supports both flat layout (~/repos/myrepo) and org-scoped layout (~/repos/myorg/myrepo).
    LOCAL_REPOS_DIR: str = os.getenv("LOCAL_REPOS_DIR", "")
    # Fallback clone directory used when a repo is not found under LOCAL_REPOS_DIR.
    WORKSPACE_DIR: str = os.getenv("WORKSPACE_DIR", "/tmp/j2p_workspace")
    PR_POLL_INTERVAL_SECONDS: int = int(os.getenv("PR_POLL_INTERVAL_SECONDS", "60"))
    MAX_REVIEW_ITERATIONS: int = int(os.getenv("MAX_REVIEW_ITERATIONS", "10"))

    def validate(self):
        import shutil
        required = {
            "JIRA_SERVER": self.JIRA_SERVER,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
        for cli, env_var in [(self.CLAUDE_CLI_PATH, "CLAUDE_CLI_PATH"), (self.GH_CLI_PATH, "GH_CLI_PATH")]:
            if not shutil.which(cli):
                raise EnvironmentError(
                    f"CLI not found: '{cli}'. Install it or set {env_var} in .env"
                )


config = Config()

