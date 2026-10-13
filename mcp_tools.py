import fnmatch
import os
import re

import settings
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from repo_clone import REPOS_PATH, SANDBOX_PATH


sandbox_path = str(SANDBOX_PATH)

os.makedirs(sandbox_path, exist_ok=True)


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
MAX_MATCHES = 100
MAX_FILE_BYTES = 1_000_000


def resolve_repo_root(repo_path: str) -> str:
    # Tools may only ever see a single clone folder directly inside
    # sandbox/repos, never the sandbox itself or another repository.
    root = os.path.realpath(repo_path or "")
    repos = os.path.realpath(REPOS_PATH)

    if os.path.normcase(os.path.dirname(root)) != os.path.normcase(repos) or not os.path.isdir(root):
        raise ValueError(f"Repository tools are limited to clones in {repos}; got {repo_path!r}")

    return root


def make_search_code(repo_root: str):

    @tool
    def search_code(pattern: str, file_glob: str = "*") -> str:
        """Search file CONTENTS in the cloned repository for a regex pattern.

        Use this to find where a function, variable, route, error message,
        or keyword is defined or used. Returns matches as
        "relative/path:line_number: line text". Then use read_file to read
        the relevant files in full.

        Args:
            pattern: Python regular expression (case-insensitive).
            file_glob: Optional filename filter, e.g. "*.py" or "*.tsx".
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return f"Invalid regex: {error}"

        if not os.path.isdir(repo_root):
            return "Repository has not been cloned yet."

        matches = []

        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

            for name in files:
                if not fnmatch.fnmatch(name, file_glob):
                    continue

                path = os.path.join(root, name)
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue

                try:
                    with open(path, encoding="utf-8", errors="strict") as file:
                        for line_number, line in enumerate(file, 1):
                            if regex.search(line):
                                relative = os.path.relpath(path, repo_root)
                                matches.append(f"{relative}:{line_number}: {line.strip()[:200]}")

                                if len(matches) >= MAX_MATCHES:
                                    matches.append(f"... stopped after {MAX_MATCHES} matches; refine the pattern.")
                                    return "\n".join(matches)
                except (UnicodeDecodeError, OSError):
                    # Skip binary or unreadable files
                    continue

        return "\n".join(matches) if matches else "No matches found."

    return search_code


async def get_mcp_tools(repo_path: str):

    repo_root = resolve_repo_root(repo_path)

    # The filesystem server only allows the directories it is started
    # with, so rooting it at this clone keeps other repositories out.
    mcp_client = MultiServerMCPClient(
        {
            "filesystem": {
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    repo_root,
                ],
                "transport": "stdio",
            }
        }
    )

    tools = await mcp_client.get_tools(
        server_name="filesystem"
    )

    return [*tools, make_search_code(repo_root)]
