from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    repo_url: str
    repo_path: str

    # Current user request
    task: str

    # Conversation history
    messages: Annotated[list[BaseMessage], add_messages]

    current_agent: str
    investigation_counter: int

    # Main Agent routing decision
    route: str

    # Persistent specialist findings
    frontend_report: str
    backend_report: str

    # Final user-facing answer
    final_report: str
