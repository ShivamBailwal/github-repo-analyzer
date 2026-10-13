# GitHub Repo Analyzer

A multi-agent system that investigates problems in a GitHub repository. You give it a repository URL and describe what's going wrong. It clones the repo, sends frontend and backend specialist agents to read the actual code, and returns an evidence-based answer.

Built with [LangGraph](https://github.com/langchain-ai/langgraph), Google Gemini, and the [MCP filesystem server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem).

## How it works

```
__start__ → create_repo_clone → Main Agent ──► END
                                  ▲   │
                                  │   ├──► Frontend Agent ─┐
                                  │   └──► Backend Agent ──┤
                                  └────────────────────────┘
```

1. **Clone**: the repository is shallow-cloned into `sandbox/repos/<owner>__<repo>-<hash>/`.
2. **Main Agent** (router): reads your request, the conversation so far, and any existing specialist reports. It then routes to a specialist or ends with a final answer. It doesn't read the code itself.
3. **Frontend Agent / Backend Agent**: investigate with read-only tools: the MCP filesystem server, rooted at that single clone, plus a regex `search_code` tool. Each returns a structured report (relevant files, evidence, probable cause, confidence) to the Main Agent.
4. Reports persist across turns, so follow-up questions can be answered without investigating again. A run stops after at most 4 Main Agent decisions.

## Requirements

- Python 3.12+
- Node.js (for `npx`, which runs the MCP filesystem server)
- Git
- A [Gemini API key](https://aistudio.google.com/apikey)
- A [LangSmith API key](https://smith.langchain.com/) (tracing is enabled in `settings.py`)

## Setup

```bash
git clone https://github.com/ShivamBailwal/github-repo-analyzer.git
cd github-repo-analyzer

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your keys:

```
GEMINI_API_KEY=your-gemini-api-key
LANGSMITH_API_KEY=your-langsmith-api-key
```

## Usage

### Web UI

```bash
python server.py
```

Open http://127.0.0.1:8000, enter a repository (for example `github.com/owner/repo`), and describe the problem. The UI streams each agent step and tool call as it happens, and lists the reports on file and the files read.

Conversations are saved as threads that survive restarts:

- `data/checkpoints.sqlite`: LangGraph agent state per thread (messages, reports, repo path).
- `data/threads.sqlite`: thread titles and each turn's trace events.

Set the `PORT` environment variable to use a port other than 8000.

### Command line

```bash
python main.py
```

Enter a repository URL, then ask questions. Type `exit` to quit.

### Notebook

`agent.ipynb` runs the same graph interactively and can render it with `visualize_graph.py`.

## Project structure

| File | Purpose |
| --- | --- |
| `graph_builder.py` | Builds the LangGraph `StateGraph` |
| `agent_state.py` / `initial_state.py` | Shared graph state |
| `main_agent.py` | Main Agent: routing and final answer |
| `frontend_agent.py` / `backend_agent.py` | Specialist investigators |
| `mcp_tools.py` | Read-only tools, sandboxed to a single clone |
| `repo_clone.py` | Clones repositories into `sandbox/repos/` |
| `model.py` | Gemini model configuration |
| `settings.py` | Loads `.env`, LangSmith tracing, Windows event loop policy |
| `server.py` / `threads_store.py` / `web/` | Web UI server, thread store, frontend |
| `main.py` / `chat_loop.py` | Command-line chat |

## Safety

Agents are told never to modify files. Their tools only reach the clone being investigated, never the sandbox root or other cloned repositories.
