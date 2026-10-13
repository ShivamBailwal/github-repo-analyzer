import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from agent_state import AgentState


SANDBOX_PATH = Path(
    os.path.abspath(
        os.path.join(os.getcwd(), "sandbox")
    )
)

# Every repository gets its own clone folder in here
REPOS_PATH = SANDBOX_PATH / "repos"


def clear_readonly_and_retry(func, path, exc):
    # Git marks its object/pack files read-only, and Windows
    # refuses to delete read-only files
    os.chmod(path, stat.S_IWRITE)
    func(path)


def repo_dir_for(repo_url: str) -> Path:
    # "https://github.com/Owner/Repo.git" -> sandbox/repos/Owner__Repo-1a2b3c
    # The hash keeps e.g. github.com/a/b and gitlab.com/a/b apart.
    normalized = repo_url.strip().rstrip("/").removesuffix(".git").lower()
    parts = [part for part in re.split(r"[/\\:]+", normalized) if part][-2:]
    name = re.sub(r"[^a-z0-9._-]+", "-", "__".join(parts)).strip(".-") or "repo"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:6]

    return REPOS_PATH / f"{name[:60]}-{digest}"


def create_repo_clone(repo_url: str) -> str:

    REPOS_PATH.mkdir(
        parents=True,
        exist_ok=True
    )

    repo_path = repo_dir_for(repo_url)

    # Remove previous clone of this repository
    if repo_path.exists():
        shutil.rmtree(repo_path, onexc=clear_readonly_and_retry)

    result = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            repo_url,
            str(repo_path)
        ],
        capture_output=True,
        text=True,
        timeout=120
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Git clone failed:\n{result.stderr}"
        )

    return str(repo_path)


def clone_node(state: AgentState) -> AgentState:

    existing_repo_path = state.get("repo_path")

    if existing_repo_path and Path(existing_repo_path).exists():
        return state

    repo_path = create_repo_clone(
        state["repo_url"]
    )

    return {
        **state,
        "repo_path": repo_path
    }
