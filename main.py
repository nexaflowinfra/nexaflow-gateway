from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import secrets
from datetime import datetime

load_dotenv()

with open("clients.json", "r") as file:
    clients = json.load(file)

app = FastAPI()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

class ChatRequest(BaseModel):
    message: str

class CreateClientRequest(BaseModel):
    client_id: str
    credits: int = 100000

@app.post("/create-client")
def create_client(req: CreateClientRequest):

    new_api_key = secrets.token_urlsafe(24)

    clients[req.client_id] = {
        "api_key": new_api_key,
        "credits": req.credits
    }

    with open("clients.json", "w") as file:
        json.dump(clients, file, indent=2)

    return {
        "client_id": req.client_id,
        "api_key": new_api_key,
        "credits": req.credits
    }

@app.get("/client-info")
def client_info(api_key: str):

    for client_id in clients:

        if api_key == clients[client_id]["api_key"]:

            return {
                "client_id": client_id,
                "credits": clients[client_id]["credits"],
                "api_key": api_key
            }

    return {
        "error": "Invalid API key"
    }
    
@app.get("/admin/clients")
def admin_clients(admin_key: str):

    if admin_key != os.getenv("ADMIN_KEY"):
        return {
            "error": "Unauthorized"
        }

    return clients

@app.post("/topup-credits")
def topup_credits(api_key: str, amount: int):

    for client_id in clients:

        if api_key == clients[client_id]["api_key"]:

            clients[client_id]["credits"] += amount

            with open("clients.json", "w") as file:
                json.dump(clients, file, indent=2)

            return {
                "client_id": client_id,
                "new_credits": clients[client_id]["credits"]
            }

    return {
        "error": "Invalid API key"
    }

@app.get("/usage-history")
def usage_history(api_key: str):

    for client_id in clients:
        if api_key == clients[client_id]["api_key"]:

            with open("usage_logs.json", "r") as file:
                logs = json.load(file)

            client_logs = []

            for log in logs:
                if log["client_id"] == client_id:
                    client_logs.append(log)

            return {
                "client_id": client_id,
                "total_requests": len(client_logs),
                "logs": client_logs
            }

    return {
        "error": "Invalid API key"
    }

@app.post("/v1/chat")
def chat(req: ChatRequest, api_key: str):

    valid_client = False

    for client_id in clients:
        if api_key == clients[client_id]["api_key"]:
            valid_client = True
            break

    if not valid_client:
        return {
            "error": "Invalid API key"
        }


    client_data = clients[client_id]

    if client_data["credits"] <= 0:
        return {
            "error": "Insufficient credits"
     }

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": req.message}
        ]
    )

    client_data["credits"] -= 1

    with open("usage_logs.json", "r") as file:
        logs = json.load(file)

    logs.append({
        "client_id": client_id,
        "message": req.message,
        "timestamp": datetime.now().isoformat()
    })

    with open("usage_logs.json", "w") as file:
        json.dump(logs, file, indent=2)

    with open("clients.json", "w") as file:
        json.dump(clients, file, indent=2)

    return {
        "reply": response.choices[0].message.content,
        "remaining_credits": client_data["credits"]
    }