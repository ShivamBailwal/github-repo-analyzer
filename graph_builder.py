from langgraph.graph import END, StateGraph

from agent_state import AgentState
from backend_agent import backend_agent, route_from_backend
from frontend_agent import frontend_agent, route_from_frontend
from main_agent import agent_node, choose_agent
from repo_clone import clone_node


builder = StateGraph(AgentState)

builder.add_node(
    "create_repo_clone",
    clone_node
)

builder.add_node(
    "agent",
    agent_node
)

builder.add_node(
    "frontend_agent",
    frontend_agent
)

builder.add_node(
    "backend_agent",
    backend_agent
)


builder.add_edge(
    "__start__",
    "create_repo_clone"
)

builder.add_edge(
    "create_repo_clone",
    "agent"
)

builder.add_conditional_edges(
    "agent",
    choose_agent,
    {
        "frontend_agent": "frontend_agent",
        "backend_agent": "backend_agent",
        "END": END
    }
)

builder.add_conditional_edges(
    "frontend_agent",
    route_from_frontend,
    {
        "main_agent": "agent"
    }
)

builder.add_conditional_edges(
    "backend_agent",
    route_from_backend,
    {
        "main_agent": "agent"
    }
)

graph = builder.compile()
