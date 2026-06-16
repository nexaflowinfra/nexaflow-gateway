# NexaFlow AI Gateway

NexaFlow AI Gateway is a multi-model AI API gateway. Customers buy a plan, receive an API key, and spend credits based on token usage when they call the chat endpoint.

## Revenue Model

Sell monthly access to the hosted API:

| Plan | Price | Credits | Default Model | Allowed Tiers | Limit |
| --- | ---: | ---: | --- | --- | ---: |
| Starter | $19/mo | 10,000 | gpt-4o-mini | economy | 30 req/min |
| Pro | $49/mo | 50,000 | gpt-4o-mini | economy, standard | 90 req/min |
| Business | $149/mo | 200,000 | gpt-4o-mini | economy, standard, premium | 240 req/min |

Use hosted payment links from a compliant payment processor. After payment, create or upgrade the customer account from the admin API.

Credits are token-based, not request-based:

```text
credits_spent = ceil((prompt_tokens + completion_tokens * 4) / 1000)
```

Output tokens are weighted higher because they are usually more expensive than input tokens. A request always costs at least 1 credit. If a completed request costs more credits than the customer has left, the account can go negative and future requests are blocked until the balance is topped up.

Each successful request also records:

```text
provider
model
prompt_tokens
completion_tokens
provider_cost_usd
revenue_usd
gross_margin_usd
```

This lets the gateway optimize for margin instead of blindly forwarding traffic to one model.

Default routing is profit-priority:

```text
routing_strategy=profit
```

When a request does not specify a model, the router estimates prompt/output tokens, estimates provider cost, estimates plan revenue, and chooses the allowed configured model with the highest expected gross margin. Supported strategies:

| Strategy | Behavior |
| --- | --- |
| profit | Maximize estimated gross margin |
| cost | Choose the lowest estimated provider cost |
| default | Prefer the plan default model |

If the first routed model fails, the gateway automatically tries the next eligible candidate in the same ranked list. The response and usage log include `routing.attempts` so operators can see whether fallback was used.

## Local Setup

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

```env
OPENAI_API_KEY=your-openai-api-key
OPENROUTER_API_KEY=your-openrouter-api-key
ADMIN_KEY=your-strong-admin-key
API_KEY_PEPPER=your-random-api-key-hash-pepper
NEXAFLOW_ALLOW_QUERY_AUTH=false
NEXAFLOW_SITE_URL=https://your-domain.example
NEXAFLOW_APP_NAME=NexaFlow AI Gateway
PAYMENT_LINK_STARTER=https://your-payment-link/starter
PAYMENT_LINK_PRO=https://your-payment-link/pro
PAYMENT_LINK_BUSINESS=https://your-payment-link/business
ENABLE_LEGACY_PAYMENT_WEBHOOK=false
PAYMENT_WEBHOOK_SECRET=your-random-payment-webhook-secret
STRIPE_WEBHOOK_SECRET=whsec_your-stripe-webhook-secret
RESEND_API_KEY=re_your-resend-api-key
FROM_EMAIL=NexaFlow <onboarding@your-domain.com>
NEXAFLOW_AUTO_BACKUP_ENABLED=true
NEXAFLOW_AUTO_BACKUP_INTERVAL_SECONDS=86400
NEXAFLOW_BACKUP_RETENTION_COUNT=14
S3_BACKUP_ENDPOINT_URL=https://your-account-id.r2.cloudflarestorage.com
S3_BACKUP_BUCKET=nexaflow-backups
S3_BACKUP_ACCESS_KEY_ID=your-r2-access-key-id
S3_BACKUP_SECRET_ACCESS_KEY=your-r2-secret-access-key
S3_BACKUP_REGION=auto
S3_BACKUP_PREFIX=production
```

Install and run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The app uses SQLite at `nexaflow.db`. On first startup it migrates existing `clients.json` and `usage_logs.json` into SQLite, then uses the database for runtime reads and writes. Keep JSON files only as migration backups.

Open:

```text
http://127.0.0.1:8000/docs
```

Product pages:

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/pricing
http://127.0.0.1:8000/portal
http://127.0.0.1:8000/admin/dashboard
```

The customer portal uses the customer's API key in the browser to show account status, remaining credits, and recent usage. The admin dashboard uses your `ADMIN_KEY` in the browser to call the existing admin APIs.

Deployment notes:

```text
DEPLOYMENT.md
STRIPE_SETUP.md
R2_BACKUP_SETUP.md
```

Use `/admin/deploy-check` before pointing `api.nexaflowinfra.com` at the service.

## Public Endpoints

### Health

```http
GET /health
```

### Plans

```http
GET /plans
```

The response includes plan details and the active credit formula.

### Models

```http
GET /models
```

Returns the provider registry, configured provider status, model tiers, task support, and model cost assumptions.

### Checkout

```http
GET /billing/checkout?plan=starter
```

Redirects to the hosted payment link configured in `.env`. Add `redirect=false` to receive the link and success/cancel URLs as JSON.

### Customer Portal

```http
GET /portal
```

Customers can paste their `nf_...` API key to view their account without needing admin access.

### Customer Account API

```http
GET /customer/me
X-API-Key: customer_api_key
```

```http
GET /customer/usage?limit=25
X-API-Key: customer_api_key
```

```http
GET /customer/billing-links
X-API-Key: customer_api_key
```

```http
POST /customer/rotate-api-key
X-API-Key: current_customer_api_key
```

These endpoints also accept `Authorization: Bearer customer_api_key`. Prefer headers over putting API keys in URLs.

`/customer/billing-links` returns the configured plan checkout URLs so the customer portal can show self-service buy/upgrade buttons.

`/customer/rotate-api-key` returns a new `api_key` once and immediately invalidates the previous key.

### Chat

```http
POST /v1/chat
X-API-Key: customer_api_key
Content-Type: application/json

{
  "message": "Hello AI",
  "task": "chat",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "routing_strategy": "profit"
}
```

Query-string auth is disabled by default to keep API keys out of browser history and proxy logs. Use `X-API-Key` or `Authorization: Bearer customer_api_key`. If an old integration needs temporary migration time, set `NEXAFLOW_ALLOW_QUERY_AUTH=true` briefly and rotate the affected keys after migration.

`provider` and `model` are optional. If omitted, the router uses `routing_strategy` to choose an allowed configured model that supports the task.

Response fields include `credits_spent`, `remaining_credits`, token usage, provider/model, routing score, fallback attempts, and request-level gross margin.

When a request leaves the customer below the low-credit threshold, the gateway sends one email warning and records it in admin notifications. The threshold is 10% of plan credits, with a minimum of 100 credits. A successful top-up or renewal resets the warning so the next low-credit cycle can notify again.

## Admin Endpoints

Pass the admin key as `X-Admin-Key` or `admin_key`.

### Create Client

```http
POST /admin/clients
X-Admin-Key: your-admin-key
Content-Type: application/json

{
  "client_id": "customer_001",
  "plan": "starter",
  "billing_email": "customer@example.com"
}
```

The API key is returned once. Store it securely.

### List Clients

```http
GET /admin/clients
X-Admin-Key: your-admin-key
```

### Update Client

```http
PATCH /admin/clients/customer_001
X-Admin-Key: your-admin-key
Content-Type: application/json

{
  "plan": "pro",
  "status": "active"
}
```

### Top Up Credits

```http
POST /admin/clients/customer_001/topup
X-Admin-Key: your-admin-key
Content-Type: application/json

{
  "amount": 10000
}
```

### Usage Stats

```http
GET /admin/usage-stats
X-Admin-Key: your-admin-key
```

### Customer Notifications

```http
GET /admin/notifications
X-Admin-Key: your-admin-key
```

Shows low-credit email notices and delivery status.

## Legacy Payment Webhook

`/webhooks/payment` is a legacy platform-neutral endpoint and is disabled by default. Prefer the signed Stripe endpoint below for production. Only enable this endpoint for a trusted internal integration by setting `ENABLE_LEGACY_PAYMENT_WEBHOOK=true`, and never expose it to an unverified checkout provider without adding provider-specific signature verification, timestamp tolerance, amount checks, and replay protection.

```http
POST /webhooks/payment
X-Webhook-Secret: your-random-payment-webhook-secret
Content-Type: application/json

{
  "event_id": "checkout_123",
  "event_type": "payment.succeeded",
  "provider": "stripe",
  "client_id": "customer_001",
  "plan": "starter",
  "billing_email": "customer@example.com",
  "amount_usd": 19,
  "currency": "USD",
  "mode": "subscription"
}
```

Supported successful event types:

```text
payment.succeeded
checkout.session.completed
invoice.paid
```

Behavior:

- New `client_id`: creates a customer, grants plan credits, and returns the API key once.
- Existing `client_id` with `subscription`: upgrades/renews the plan and adds included credits.
- Existing `client_id` with `topup`: adds credits without changing the plan.
- Repeated `event_id`: ignored safely through idempotency.

## Stripe Checkout Webhook

Stripe can call a dedicated endpoint:

```text
https://api.nexaflowinfra.com/webhooks/stripe
```

Required Stripe Checkout metadata:

```text
client_id=customer_001
plan=starter
mode=subscription
billing_email=customer@example.com
```

Optional:

```text
credits=10000
```

Supported Stripe event types:

```text
checkout.session.completed
invoice.paid
invoice.payment_failed
customer.subscription.updated
customer.subscription.deleted
charge.refunded
charge.dispute.created
```

The endpoint verifies the `Stripe-Signature` header with `STRIPE_WEBHOOK_SECRET`, maps Stripe metadata or payment amount into NexaFlow's internal payment event, and then creates or tops up the customer. Initial subscription events are de-duplicated so `checkout.session.completed` and the first `invoice.paid` do not double-credit the same customer. Failed payments, refunds, disputes, and subscription cancellation events automatically pause or cancel customer access.

## Automatic API Key Delivery

When a payment creates a new customer, NexaFlow generates a one-time API key. Configure email delivery before selling publicly:

```env
RESEND_API_KEY=re_your-resend-api-key
FROM_EMAIL=NexaFlow <onboarding@your-domain.com>
```

With these variables set, the webhook automatically emails the customer their `client_id`, API key, plan, credits, and API docs URL. Without them, the webhook still creates the customer but reports `delivery.status=pending_email_setup`.

The same email configuration is used for low-credit alerts after customer API usage.

Fulfillment emails include the customer portal link, a header-authenticated `curl` test request, and the pricing page so paid users can self-test and self-upgrade without manual support.

Stripe lifecycle events also create customer notifications. Payment failures, refunds, disputes, and subscription cancellations pause or cancel access and email the customer with portal/pricing links when `RESEND_API_KEY` and `FROM_EMAIL` are configured.

## Production Checklist

- Use strong `ADMIN_KEY` and `API_KEY_PEPPER` values.
- Keep automatic SQLite backups enabled and verify `/admin/backups` shows recent backups.
- Configure `S3_BACKUP_*` variables for Cloudflare R2 or S3 before relying on the system for production disaster recovery.
- Use the Admin Dashboard `Test Offsite` button after setting R2 variables. It uploads a tiny probe file before any customer database backup is uploaded.
- Configure payment links for all paid plans.
- Configure `RESEND_API_KEY` and `FROM_EMAIL` so paid customers receive API keys without manual work.
- Keep `ALLOW_UNPROFITABLE_MODEL_ROUTES` unset in production so the gateway blocks model routes that fail margin guard checks.
- Keep customer payment records in your payment provider dashboard.
- Do not commit `.env`, logs, or generated API keys.
- Do not commit `nexaflow.db`; it contains customer and usage data.
- Rotate any API keys that were previously committed in plaintext.
- For larger production traffic, move from local SQLite to managed Postgres.
- Keep credits token-based. Do not sell high-volume `gpt-4.1` usage as unlimited fixed request counts.
- Keep `MODEL_CATALOG` prices current or override them with `MODEL_CATALOG_JSON` before relying on margin reports.
- Direct-connect high-volume providers over time; use OpenRouter for breadth and fallback, not as the only long-term supplier.
- Add a privacy policy, terms of service, refund policy, and abuse policy before selling publicly.
- Review OpenAI usage policies and your local business/tax obligations.
