"""Push local changes directly to GitHub (main) using dulwich without needing Git installed."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from dulwich import porcelain
from dulwich.repo import Repo

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

REMOTE_URL = "https://github.com/Shashank1887/winwin-delta-trader.git"
BRANCH = "main"

EXCLUDED_PREFIXES = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
)
EXCLUDED_FILES = {
    ".env",
    "delta_trades_log.csv",
    ".streamlit/secrets.toml",
}


def _collect_repo_files(root: Path) -> List[str]:
    """Collect tracked/eligible project files while strictly excluding secrets and caches."""
    files: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in EXCLUDED_FILES:
            continue
        parts = rel.split("/")
        if any(p in EXCLUDED_PREFIXES for p in parts):
            continue
        if rel.endswith((".pyc", ".pyo")):
            continue
        files.append(rel)
    return sorted(files)


def sync_and_push(commit_message: str) -> None:
    """Stage eligible files, commit if changed, and push to GitHub main."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "GITHUB_TOKEN missing in .env. Add GITHUB_TOKEN=<your_github_pat> to .env first."
        )

    files = _collect_repo_files(BASE_DIR)
    repo = Repo(str(BASE_DIR))
    try:
        porcelain.add(repo, paths=files)
        status = porcelain.status(repo)
        has_changes = any(status.staged.values())
        if has_changes:
            commit_sha = porcelain.commit(
                repo,
                message=commit_message.encode("utf-8"),
                author=b"Shashank1887 <shashank1887@users.noreply.github.com>",
                committer=b"Shashank1887 <shashank1887@users.noreply.github.com>",
            )
            print(f"Committed {commit_sha.decode()[:7]}: {commit_message}")
        else:
            print("No new file changes to commit; pushing current HEAD if ahead.")

        auth_url = REMOTE_URL.replace("https://", f"https://x-access-token:{token}@")
        porcelain.push(repo, auth_url, refspecs=[f"refs/heads/{BRANCH}:refs/heads/{BRANCH}"])
        print(f"Pushed to {REMOTE_URL} ({BRANCH}). Streamlit Cloud will auto-redeploy shortly.")
    finally:
        repo.close()


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]).strip() or "Update trading app"
    sync_and_push(msg)
