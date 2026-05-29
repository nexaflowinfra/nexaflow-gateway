# Stripe Setup for NexaFlow

This step connects real payments to NexaFlow. It may require Stripe account verification, business information, tax details, bank details, and Stripe transaction fees.

## Products and Prices

Create three monthly recurring products in Stripe:

| Product | Price | Billing | Metadata |
| --- | ---: | --- | --- |
| NexaFlow Starter | USD 19 | Monthly recurring | `plan=starter`, `mode=subscription` |
| NexaFlow Pro | USD 49 | Monthly recurring | `plan=pro`, `mode=subscription` |
| NexaFlow Business | USD 149 | Monthly recurring | `plan=business`, `mode=subscription` |

If you create top-up links, use:

```text
mode=topup
credits=10000
plan=starter
```

For normal public payment links, `client_id` can be omitted. NexaFlow will create one from the customer email. For controlled B2B onboarding, you may include:

```text
client_id=customer_001
```

## Payment Links

For each product price, create a Stripe Payment Link. Enable customer email collection.

After creating links, put them in Railway variables:

```env
PAYMENT_LINK_STARTER=https://buy.stripe.com/...
PAYMENT_LINK_PRO=https://buy.stripe.com/...
PAYMENT_LINK_BUSINESS=https://buy.stripe.com/...
```

## Webhook

Create a Stripe webhook endpoint:

```text
https://api.nexaflowinfra.com/webhooks/stripe
```

Subscribe to:

```text
checkout.session.completed
invoice.paid
invoice.payment_failed
customer.subscription.updated
customer.subscription.deleted
charge.refunded
charge.dispute.created
```

Copy the endpoint signing secret. It starts with:

```text
whsec_
```

Set it in Railway:

```env
STRIPE_WEBHOOK_SECRET=whsec_...
```

## Validation

After setting the real webhook secret and payment links, call:

```http
GET https://api.nexaflowinfra.com/admin/deploy-check
X-Admin-Key: your-admin-key
```

Expected remaining payment checks:

```text
STRIPE_WEBHOOK_SECRET=true
PAYMENT_LINKS=true
```

## Important

Do not place Stripe secret keys or webhook secrets in GitHub. Put them only in Railway Variables.
