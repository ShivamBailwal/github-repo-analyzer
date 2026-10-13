import settings
from langchain_google_genai import ChatGoogleGenerativeAI


model = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash-lite"
)
