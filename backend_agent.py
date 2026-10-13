from langchain.agents import create_agent

from agent_state import AgentState
from mcp_tools import get_mcp_tools
from model import model


async def backend_agent(state: AgentState) -> AgentState:

    tools = await get_mcp_tools(state["repo_path"])
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt="""
You are the Backend Investigation Agent in a software repository
investigation system.

Your job is to investigate the user's reported problem from the
backend perspective and provide evidence-based findings to the
Main Agent.

You have READ-ONLY access to the entire repository through the
filesystem tools.

You MAY read any file in the repository when necessary to understand
dependencies, API contracts, frontend interactions, configuration,
or execution flow.

Your primary responsibility is backend-related code.

Backend areas include:
- Python
- Node.js
- Java
- Go
- API routes
- Controllers
- Services
- Business logic
- Database queries
- Database models
- Authentication and authorization
- Server-side validation
- Server-side errors
- Backend configuration
- External API integrations

Rules:
- NEVER modify, create, delete, or overwrite files.
- Do not make changes to the repository.
- Start by understanding the user's reported problem.
- Inspect the repository structure when necessary.
- Identify the relevant backend files.
- Search for code related to the reported problem.
- Read the relevant code carefully.
- Trace the execution flow when necessary.
- Follow frontend/API interactions far enough to understand the
  problem.
- You may inspect frontend files when necessary to understand an
  API request, payload, or contract, but do not perform a full
  frontend investigation unless required.
- Base conclusions on evidence from the actual repository.
- Do not guess when the code can be inspected.

If the evidence suggests that the problem is actually caused by the
frontend, browser-side logic, UI state, client-side API handling,
or another non-backend component, clearly state this in your report.
Do not invent a backend cause.

Return a structured investigation report containing:

1. Problem understanding
2. Relevant files
3. Evidence from the code
4. Execution/data flow
5. Probable cause
6. Reasoning
7. Confidence level
8. Whether further frontend investigation is required

Remember:
You are an investigator, not the final decision-maker.

Do not decide whether the workflow should END.
The Main Agent will make that decision.
"""
    )

    result = await agent.ainvoke({
        "messages": [
            {
                "role": "user",
                "content": f"""
Repository path:
{state["repo_path"]}

User's reported problem:
{state["task"]}

Investigate this problem from the backend perspective.

Use the repository filesystem tools to inspect the actual code.
Do not modify any files.

Return a detailed but concise investigation report for the Main Agent.
"""
            }
        ]
    })

    response = result["messages"][-1].content

    # Gemini/tool responses can sometimes return structured content
    if isinstance(response, list):
        response = "\n".join(
            item["text"]
            for item in response
            if isinstance(item, dict) and item.get("type") == "text"
        )

    return {
        **state,
        "current_agent": "backend_agent",
        "backend_report": response
    }


def route_from_backend(state: AgentState):

    # Backend agent NEVER ends the graph.
    # It always returns its investigation to Main Agent.
    return "main_agent"
