import asyncio

import settings
from chat_loop import chat_loop


if __name__ == "__main__":
    asyncio.run(chat_loop())
