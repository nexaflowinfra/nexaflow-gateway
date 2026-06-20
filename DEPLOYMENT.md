# Deployment Guide

This guide prepares NexaFlow for `nexaflowinfra.com` without starting paid services.

## Recommended Domain Layout

```text
nexaflowinfra.com          product site
www.nexaflowinfra.com      redirect to apex
api.nexaflowinfra.com      FastAPI backend
admin.nexaflowinfra.com    optional admin dashboard
```

The current app can serve the product site and API from one deployment. A simple first launch can use:

```text
https://api.nexaflowinfra.com/
https://api.nexaflowinfra.com/pricing
https://api.nexaflowinfra.com/admin/dashboard
```

## Required Production Environment Variables

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ADMIN_KEY=...
API_KEY_PEPPER=...
PAYMENT_WEBHOOK_SECRET=...
STRIPE_WEBHOOK_SECRET=...
NEXAFLOW_SITE_URL=https://api.nexaflowinfra.com
NEXAFLOW_APP_NAME=NexaFlow AI Gateway
NEXAFLOW_DB_PATH=/data/nexaflow.db
META_WEBHOOK_VERIFY_TOKEN=...
META_APP_SECRET=...
PAYMENT_LINK_STARTER=...
PAYMENT_LINK_PRO=...
PAYMENT_LINK_BUSINESS=...
RESEND_API_KEY=...
FROM_EMAIL=NexaFlow <onboarding@your-domain.com>
S3_BACKUP_ENDPOINT_URL=...
S3_BACKUP_BUCKET=...
S3_BACKUP_ACCESS_KEY_ID=...
S3_BACKUP_SECRET_ACCESS_KEY=...
S3_BACKUP_REGION=auto
S3_BACKUP_PREFIX=production
```

At least one model provider key is required. For multi-model routing, keep both OpenAI and OpenRouter configured.

## Meta Message Webhook

For Facebook Messenger, Instagram DM, and WhatsApp Cloud API auto-receive:

1. Set `NEXAFLOW_SITE_URL` to the public HTTPS domain, for example `https://api.nexaflowinfra.com`.
2. Set `META_WEBHOOK_VERIFY_TOKEN` in Railway. Use the same value in the Meta webhook callback verification form.
3. Set `META_APP_SECRET` in Railway. NexaFlow uses it to verify `X-Hub-Signature-256` on every Meta webhook request.
4. In Meta Developer, use this callback URL:

```text
https://api.nexaflowinfra.com/webhooks/meta
```

5. In the merchant channel setup page, save the correct Meta account ID and change the channel status to `Connected - auto receive live`.

Only channels with status `connected`, `official_api_requested` mode, a saved external Meta account ID, and data-processing acknowledgement can create real buyer records from Meta webhooks. Keep channels in `requested` while setting up or testing permissions.

## Persistent Storage

SQLite is fine for early paid pilots, but the database file must live on a persistent disk.

Set:

```env
NEXAFLOW_DB_PATH=/data/nexaflow.db
```

Then configure your deployment platform so `/data` is persistent. Without persistent storage, customer credits and usage logs can disappear on redeploy.

## Startup Command

The included `Procfile` starts the app with:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Platforms like Railway and Render usually provide `$PORT` automatically.

## DNS Plan

Your DNS is managed by Cloudflare. Add a backend subdomain after choosing a deployment platform:

```text
Type: CNAME
Name: api
Target: your-platform-target.example
Proxy: DNS only at first
```

After the platform verifies TLS, you can decide whether to enable Cloudflare proxy.

Existing records found during local checks:

```text
nexaflowinfra.com      A      76.76.21.21
www.nexaflowinfra.com  CNAME  cname.vercel-dns.com
```

The apex already points to Vercel. If you want this FastAPI app to own the apex too, either deploy the app on a platform and repoint the apex, or keep apex on Vercel and point only `api.nexaflowinfra.com` to the backend.

## Deployment Readiness Check

After deployment, call:

```http
GET /admin/deploy-check
X-Admin-Key: your-admin-key
```

It reports missing provider keys, payment links, webhook secret, HTTPS site URL, email delivery, and persistent database path.
It also reports the plan/model margin guard so unsafe pricing can be caught before launch.

## Backups

Create an on-volume SQLite backup from the admin dashboard or:

```http
POST /admin/backups
X-Admin-Key: your-admin-key
```

For off-platform disaster recovery, configure a Cloudflare R2 or S3-compatible bucket:

```env
S3_BACKUP_ENDPOINT_URL=https://your-account-id.r2.cloudflarestorage.com
S3_BACKUP_BUCKET=nexaflow-backups
S3_BACKUP_ACCESS_KEY_ID=...
S3_BACKUP_SECRET_ACCESS_KEY=...
S3_BACKUP_REGION=auto
S3_BACKUP_PREFIX=production
```

When these variables are present, each new backup is uploaded to object storage after the local backup is created.

## Payment Webhook URL

Once deployed, configure your payment provider webhook to:

```text
https://api.nexaflowinfra.com/webhooks/payment
```

Do this only after you are ready to create or connect a real payment account. That may involve KYC, payment fees, or platform costs.

For Stripe Checkout, use:

```text
https://api.nexaflowinfra.com/webhooks/stripe
```

Set Checkout metadata for each price/payment link:

```text
plan=starter
mode=subscription
client_id={{customer identifier}}
billing_email={{customer email}}
```
