from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

class ChatRequest(BaseModel):
    message: str

@app.post("/v1/chat")
def chat(req: ChatRequest):

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": req.message}
        ]
    )

    return {
        "reply": response.choices[0].message.content
    }