from IPython.display import Markdown, display
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

from graph_builder import graph
from initial_state import initial_state


# Basic notebook chat layer
chat_state = initial_state.copy()

def show_response(final_report: str):
    try:
        shell_name = get_ipython().__class__.__name__
    except NameError:
        shell_name = ""

    if shell_name == "ZMQInteractiveShell":
        display(Markdown(final_report))
    else:
        print(f"\nAI:\n{final_report}\n")


def set_repo_url(repo_url: str):
    global chat_state

    chat_state = {
        **chat_state,
        "repo_url": repo_url.strip(),
        "repo_path": "",
        "frontend_report": "",
        "backend_report": "",
        "final_report": "",
        "route": "",
        "investigation_counter": 0
    }

    return chat_state


async def ask(user_input: str):
    global chat_state

    user_input = user_input.strip()

    if not user_input:
        return chat_state

    chat_state = {
        **chat_state,
        "task": user_input,
        "messages": add_messages(
            chat_state.get("messages", []),
            [HumanMessage(content=user_input)]
        ),
        "investigation_counter": 0,
        "route": "",
        "final_report": ""
    }

    result = await graph.ainvoke(chat_state)
    final_report = result.get("final_report", "").strip()

    if not final_report:
        final_report = "The investigation finished, but the Main Agent did not produce a final answer."

    chat_state = {
        **result,
        "messages": add_messages(
            result.get("messages", []),
            [AIMessage(content=final_report)]
        )
    }

    show_response(final_report)

    return chat_state


async def chat_loop():
    while not chat_state.get("repo_url"):
        repo_url = input("Repository URL: ").strip()

        if repo_url.lower() in {"exit", "quit"}:
            return

        if repo_url:
            set_repo_url(repo_url)

    while True:
        user_input = input("\nYou: ").strip()

        if user_input.lower() in {"exit", "quit"}:
            break

        if not user_input:
            continue

        await ask(user_input)
