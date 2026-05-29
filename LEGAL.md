# Legal Commercialization Notes

This project is designed for lawful API resale and managed AI access. It is not legal, tax, or financial advice.

## What This System Should Sell

- Paid access to your hosted NexaFlow AI Gateway.
- Monthly plans with included credits.
- Optional manual credit top-ups for verified customers.
- Business onboarding for customers that need higher limits.

## Minimum Pages Before Public Sales

- Terms of Service: define acceptable use, account suspension, uptime limits, refunds, and liability limits.
- Privacy Policy: explain what data is collected, why it is processed, retention, subprocessors, and contact details.
- Refund Policy: define trial, cancellation, and refund windows.
- Acceptable Use Policy: prohibit illegal activity, abuse, spam, malware, credential theft, and evasion of safety systems.

## Operating Rules

- Only create or upgrade accounts after payment is verified.
- Keep payment processing inside a compliant provider such as Stripe, Paddle, Lemon Squeezy, PayPal, or a local equivalent.
- Do not store card data in this app.
- Do not expose customer API keys in admin list responses.
- Rotate keys that were previously committed to the repository.
- Maintain records for taxes, invoices, refunds, and customer support.

## Suggested Launch Flow

1. Customer chooses a plan at `/plans`.
2. Customer opens `/billing/checkout?plan=starter`.
3. Customer pays through the hosted payment page.
4. Admin verifies payment in the provider dashboard.
5. Admin creates the customer with `POST /admin/clients`.
6. Customer receives the API key and integration instructions.
7. Usage is tracked through `/admin/usage-stats` and `/usage-history`.
