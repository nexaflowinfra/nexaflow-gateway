# NexaFlow Production Status

Last updated: 2026-05-29

## Live Service

```text
https://api.nexaflowinfra.com
```

Railway service:

```text
nexaflow-gateway
```

## Current Revenue Flow

1. Customer opens pricing or checkout link.
2. Stripe payment link completes checkout.
3. Stripe webhook creates or updates the customer.
4. NexaFlow generates a one-time `nf_...` API key.
5. Resend emails onboarding, Portal, and API usage instructions.
6. Customer uses the Portal to test requests, view credits, rotate keys, and buy/upgrade plans.
7. Usage is logged with credits, provider cost, revenue, and gross margin.
8. Low credits and payment lifecycle issues trigger customer notifications.

## Production Safeguards

- Stripe webhook signature verification is enabled.
- Payment events are idempotent.
- Initial Stripe subscription events are de-duplicated to avoid double credits.
- Customer API keys are stored hashed with pepper.
- Customers can rotate their own API key.
- Admin can rotate and re-send a customer API key.
- Credits and rate limits protect usage.
- Margin guard blocks model routes that fail profitability checks.
- Local SQLite backups are automatic.
- Cloudflare R2 offsite backup upload is configured and tested.
- Generated databases, logs, backups, `.env`, and legacy JSON data files are ignored by Git and Railway deploys.

## Backup Status

Automatic local backups:

```text
enabled=true
interval=86400 seconds
retention=14 backups
```

Offsite R2 backup:

```text
bucket=nexaflow-backups
prefix=production
status=uploaded
```

Verify in the Admin Dashboard:

```text
/admin/dashboard
```

## Remaining Before Real Launch

- Switch Stripe from sandbox to live mode.
- Add live Stripe payment links and live webhook secret.
- Confirm live checkout creates customers and sends email.
- Review and publish Terms, Privacy Policy, Refund Policy, and Acceptable Use Policy.
- Add stronger abuse monitoring and admin revenue dashboards.
- Decide the first application-layer product built on top of NexaFlow Core.
