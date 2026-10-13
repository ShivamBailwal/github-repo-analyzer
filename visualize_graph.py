from IPython.display import Image, display

from graph_builder import graph


display(
    Image(
        graph.get_graph().draw_mermaid_png()
    )
)
