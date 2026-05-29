# NexaFlow Gateway - 2026-05-27 Local Change Snapshot

This file records the product changes completed today so the project can be opened later in VS Code and reviewed from one place.

## Product Status

- FastAPI gateway is implemented in `main.py`.
- Railway deployment is connected to `https://api.nexaflowinfra.com`.
- Production SQLite storage is configured through `NEXAFLOW_DB_PATH=/data/nexaflow.db`.
- Railway persistent volume has been created and mounted at `/data`.
- OpenAI and OpenRouter provider support is present.
- Stripe webhook code is present, but final Stripe Dashboard setup still requires real Stripe products, payment links, and a real `whsec_...` signing secret.

## Main Product Features Added

- Multi-provider model gateway with OpenAI and OpenRouter.
- Model catalog with route selection and fallback behavior.
- Token/credit billing logic.
- Plan-based access control.
- Usage logging with cost, revenue, and margin tracking.
- Admin client creation, top-up, client inspection, usage stats, and deployment check endpoints.
- Public landing, pricing, plans, models, checkout, and health endpoints.
- Generic payment webhook endpoint.
- Stripe webhook endpoint with signature verification.
- Local and deployment documentation.

## Important Files

- `main.py` - application source code.
- `test_app.py` - local tests for API, billing, routing, admin, and webhook behavior.
- `.env.example` - environment variable template.
- `README.md` - product and usage overview.
- `DEPLOYMENT.md` - Railway deployment notes.
- `STRIPE_SETUP.md` - Stripe setup checklist.
- `LEGAL.md` - compliance and operating notes.
- `Procfile` and `runtime.txt` - Railway runtime files.
- `.railwayignore` and `.gitignore` - deployment and local ignore rules.

## Local VS Code Usage

Open this folder in VS Code:

`C:\Users\Aureus Zi Loong\Documents\Codex\2026-05-27\chatgpt\nexaflow-gateway-main`

The real `.env` file contains private API keys and should not be uploaded or shared.

## Next Stripe Step

Before payment can become fully live:

1. Create Stripe products and payment links.
2. Add metadata to each payment link:
   - `plan=starter`, `mode=subscription`
   - `plan=pro`, `mode=subscription`
   - `plan=business`, `mode=subscription`
3. Create Stripe webhook endpoint:
   - `https://api.nexaflowinfra.com/webhooks/stripe`
4. Subscribe to:
   - `checkout.session.completed`
   - `invoice.paid`
5. Put the real `STRIPE_WEBHOOK_SECRET=whsec_...` and payment links into Railway variables.

## Safety Note

This snapshot should preserve the source code and operating documents. Do not share `.env`, Railway tokens, OpenAI keys, OpenRouter keys, Stripe secrets, or admin keys.
