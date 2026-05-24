# NexaFlow AI API

## Endpoint

POST

```text
https://nexaflow-gateway-production.up.railway.app/v1/chat
```

---

## Parameters

Query:

```text
api_key=nexaflow-secret-key
```

---

## JSON Body

```json
{
  "message": "Hello AI"
}
```

---

## Example cURL

```bash
curl -X POST "https://nexaflow-gateway-production.up.railway.app/v1/chat?api_key=nexaflow-secret-key" \
-H "Content-Type: application/json" \
-d "{\"message\":\"Hello\"}"
```