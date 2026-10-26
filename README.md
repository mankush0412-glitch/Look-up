# Telegram Lookup Bot + Paid Plans + Public API

This project includes Telegram lookup features, switchable Manual/Razorpay payments, API access, order history, configurable welcome/contact/payment settings, fixed-amount UPI QR fallback payments, and a public FastAPI service.

## Plans

- Credits: ₹1 = 1 credit, minimum purchase ₹10
- Bot Unlimited: ₹50 for 24 hours
- API Unlimited: ₹300 for 30 days
- API monthly request count is unlimited, with technical rate limits for service protection.

## Purchase Flow

Payment mode is selected from `/setting` → **💳 Payment Settings**. Only one mode is active for new purchases at a time.

### Razorpay mode

1. User opens **View Plans** or `/plans`.
2. User selects credits, 24-hour bot pass, or API plan.
3. API plan users accept the API Terms.
4. Bot creates a unique fixed-amount, single-use Razorpay UPI QR.
5. User scans the QR and pays the exact amount.
6. Bot verifies pending payments directly with Razorpay via authenticated outbound API calls, without any webhook URL or callback; the user can also tap **Check Payment**. The bot checks the QR, payment ID, captured status, currency, and exact order amount.
7. Paid orders automatically add credits, activate the 24-hour pass, or issue API access. No screenshot/admin approval is required.

### Manual mode

1. Bot generates an exact-amount UPI QR.
2. User pays and submits screenshot/document/UTR.
3. Admin receives Approve / Reject buttons.
4. Approval activates the purchased product.

Switching Razorpay ON disables manual QR/proof submission for new purchases. Existing pending manual orders remain approvable. Users can view purchases from **Order History** or `/orders`.

## Public API

Production Base URL:

```text
https://api.teamsecret.in
```

### Mobile Lookup

```text
GET /api/lookup?number=9876543210
X-API-Key: USER_API_KEY
```

### ID Lookup

```text
GET /api/id?id=123456789012
X-API-Key: USER_API_KEY
```

ID lookup accepts an exact 12-digit ID.

Interactive FastAPI documentation:

```text
https://api.teamsecret.in/docs
```

Full user documentation is included in `API_DOCUMENTATION.md` and is sent by the bot through `/apidocs` as a Markdown file.

## API Keys

API keys are generated when an API plan is approved. Only hashes are stored in MongoDB.

The **My API** section (`/myapi`) shows plan status, expiry, key prefix, and Base URL. If a user loses the full key, **Regenerate API Key** revokes the old key and creates a new one without changing the existing plan expiry.

The secret key is sent in a standalone Telegram message so navigation callbacks cannot replace it.

## Required Environment Variables

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
API_URL=https://YOUR_UPSTREAM_LOOKUP_API
API_KEY=YOUR_UPSTREAM_API_KEY
ADMIN_IDS=123456789
MONGO_URI=mongodb+srv://...
MONGO_DB_NAME=telegram_lookup_bot

PAYMENT_UPI_ID=yourupi@bank
PAYMENT_ACCOUNT_NAME=Your Name

PUBLIC_ACCESS=true
RATE_LIMIT_PER_MINUTE=10
API_USER_RATE_LIMIT_PER_MINUTE=60
ENABLE_PUBLIC_API=true
PUBLIC_API_URL=https://api.teamsecret.in
API_PORT=8000
LOG_CHAT_ID=

AUTO_CLOUDFLARE_TUNNEL=false
```

Razorpay credentials can be configured in `/setting` → **💳 Payment Settings**, or optionally as the **pair** `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in a private `.env` file (the environment pair takes priority). Then select Razorpay payment mode in `/setting`. See `RAZORPAY_SETUP.md` for the no-webhook payment flow and the tests.

The payment process never needs an inbound URL, webhook, callback, public domain or Cloudflare tunnel. `python3 bot.py` with `AUTO_CLOUDFLARE_TUNNEL=false` runs Telegram polling and verifies Razorpay over outbound HTTPS. The separate FastAPI lookup service and its domain/tunnel are **not used for payments**; they remain optional for hosted API-plan customers. `start.sh` can still launch that separate lookup API tunnel when needed. For safety, Git auto-pull in `start.sh` is now opt-in (`AUTO_GIT_PULL=true`) so launching the modified ZIP does not overwrite these edits.

Quick verification checks (no live credentials needed):

```bash
python3 -m unittest discover -s tests -v
```

## VPS Start

The included `start.sh` is designed for the current VPS setup. It:

- safely checks Git updates,
- installs Python requirements,
- falls back to `--break-system-packages` where required,
- installs `cloudflared` when missing,
- starts the bot/API on port `8000`,
- waits for the local API health check,
- starts the named Cloudflare Tunnel in a `screen` session.

Run:

```bash
chmod +x start.sh
./start.sh
```

The Cloudflare credentials file expected by the current setup is:

```text
/root/.cloudflared/53b91e72-ca6d-4742-98f0-af79e05475aa.json
```

Do not commit this credentials JSON to GitHub.

## Main Commands

- `/start` — main menu
- `/lookup <number>` — Telegram mobile lookup
- `/id <12-digit-id>` or `/record <12-digit-id>` — Telegram ID lookup
- `/balance` — credits / plan status
- `/plans` or `/buycredits` — purchase plans
- `/orders` — purchase history
- `/myapi` — API plan/key management
- `/apidocs` — download API documentation (`.md`)
- `/referral` — referral information
- `/ping` — status
- `/setting` or `/settings` — admin settings panel

## Admin Settings

The admin settings panel supports:

- force-join channels,
- SRC contact bot configuration,
- welcome image upload/removal,
- **Payment Settings**: switch Manual/Razorpay mode, set Razorpay Key ID/Secret, business/display name, configure the payment-log group, and test Razorpay QR/Payment Link API access,
- API statistics,
- current settings overview.

Razorpay credentials are entered through the bot. The Key Secret is accepted only in the bot's private DM, encrypted before it is stored in MongoDB, and never displayed back in the settings UI. `start.sh` generates `APP_ENCRYPTION_KEY` in `.env` if it is missing. Copy the same `.env` securely during server migrations; if that key is lost, re-enter the Razorpay Key Secret from `/setting`. No webhook setup is required: QR and Payment Link payments are verified through the Razorpay API.

## Buy SRC

The main menu includes **🧾 Buy SRC**. Admins can configure its destination bot at runtime:

```text
/setcontactbot @YourContactBot
/contactbot
```

## Razorpay QR and Payment Link Payments

When Razorpay mode is enabled, after choosing a plan users select either **Pay with UPI QR** (unique fixed-amount single-use QR) or **Pay with Payment Link** (Razorpay-hosted Pay Now URL). Each link is unique to one order, has no partial payments, and is verified with Razorpay's authenticated APIs. No webhook/callback URL is required. Captured INR payment, exact expected amount and order association must match before activation. Configure payment logs from `/setting` → **Payment Settings → Payment Log Group**. See `RAZORPAY_SETUP.md` and `PAYMENT_NO_WEBHOOK.md`.

## Fixed-Amount UPI QR

The purchase flow generates a QR containing the selected exact payment amount. The UPI ID is not printed as text in bot messages.

After payment, the user submits a screenshot/document or UTR for manual approval.

A standard UPI QR contains the payee VPA internally, so it can still be obtained by decoding the QR.

## Date and Time

Database expiry logic is UTC-safe. User-facing timestamps are displayed in **India Standard Time (IST / Asia/Kolkata)**.

## Security

Keep Telegram bot tokens, upstream API keys, user API keys, `.env`, `APP_ENCRYPTION_KEY`, and Cloudflare tunnel credentials private. `.gitignore` protects `.env` from normal Git operations; do not force-add it or commit secrets to repositories. During server migration, securely copy `.env` along with the Cloudflare tunnel credentials.

## Admin API statistics
Admins can use `/apistats` or open `/setting` → **📊 API Stats**. The dashboard shows active/expired/revoked key counts, active API users, request totals for today/current month/all time, rate-limit hits, and per-key prefix/user/expiry/last-used/usage data. Full API keys are intentionally never stored after issuance, so the dashboard shows only key prefixes.

## Automatic plan notifications
The bot runs a background notification worker every 15 minutes. Users are notified when an API plan has about 3 days remaining, about 1 day remaining, and when it expires. Users are also notified when a 24-hour unlimited bot pass expires. Notification markers are stored in MongoDB so restarts do not resend the same reminder for the same plan expiry.

## Admin Payment Stats

Open `/setting` → **Payment Settings** → **Payment Stats** to view total/today/month revenue, order status counts, Manual vs Razorpay split, and paginated payment history. Payment history shows order ID, user ID, plan, amount, method, status, IST timestamps, and Razorpay payment/RRN identifiers when available.


## Payment hardening

- Duplicate manual UTR/transaction IDs are rejected using a normalized hash with a unique database index.
- Razorpay payment IDs are unique per order, preventing one captured payment from activating multiple purchases.
- Credit, 24-hour pass, and API-plan activation are idempotent per order, so stale/retried `processing` orders do not grant the purchase twice.
- Stale processing orders recover automatically after restart.
- Background payment and expiry-notification workers use MongoDB leases to reduce duplicate processing during brief multi-server overlap.
- API keys created by a purchase can be recovered across a crash only until Telegram delivery succeeds; the encrypted recovery copy is removed after successful delivery.

- Razorpay QR and Payment Link creation show a temporary processing message until checkout is ready.
