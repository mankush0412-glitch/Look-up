# Razorpay QR + Payment Link Setup

The bot supports two purchase modes:

- **Manual QR** — exact-amount UPI QR + screenshot/UTR + admin approval.
- **Razorpay** — at checkout choose either **UPI QR** (single-use fixed-amount) or **Razorpay Payment Link** (Razorpay-hosted HTTPS checkout); both verify automatically.

Only one mode is active for new purchases at a time.

## Configure from Telegram

As an admin open:

```text
/setting
```

Then select **💳 Payment Settings**.

Configure:

1. **Set Key ID** — Razorpay `rzp_live_...` or `rzp_test_...` Key ID.
2. **Set Key Secret** — matching secret. Set it only in the bot's private DM. It is encrypted before MongoDB storage and is not displayed back in the settings UI.
3. **Business Name** — used in QR and Payment Link descriptions.
4. **Payment Log Group** — send/forward the target group or enter its `-100...` chat ID. The bot must be able to send messages there.
5. **Test Razorpay** — checks QR Codes API and Payment Links API access separately. Both products may require enabling in the Razorpay dashboard.
6. Select **Razorpay** as the payment mode.

For both options to work, Razorpay QR Codes and Payment Links APIs must be enabled on the Razorpay account. **No webhook URL, callback URL, public domain, Cloudflare tunnel, or redirect handler is required to verify payments.**

Instead of Telegram settings, you may optionally provide the credentials as an environment pair (both are required):

```env
RAZORPAY_KEY_ID=rzp_live_YOUR_KEY_ID
RAZORPAY_KEY_SECRET=YOUR_KEY_SECRET
```

If both are set, they take precedence over the credentials stored in MongoDB. Do not commit `.env` or paste the secret into a public chat, logs or URLs. Keep an `APP_ENCRYPTION_KEY` in `.env` if you use Telegram settings for encrypted secret storage. In `/setting`, select **Razorpay** payment mode for new orders.

To run the Telegram bot **without any inbound URL or tunnel for payment checking**, use `AUTO_CLOUDFLARE_TUNNEL=false python3 bot.py` instead of the optional `start.sh` tunnel launcher. This bot uses Telegram polling plus outbound HTTPS to Razorpay. `ENABLE_PUBLIC_API=false` can also disable the separately hosted lookup API, but doing so disables public API customer access. Existing `API_URL`/`API_KEY` belong to the upstream *lookup service*, not to Razorpay.

## Razorpay Purchase Flow

1. Buyer selects a plan or credit amount, then chooses **📱 Pay with UPI QR** or **🔗 Pay with Payment Link**.
2. QR: bot generates a single-use exact-amount QR (`POST /v1/payments/qr_codes`) with order metadata and sends the image. Link: bot creates a unique Razorpay-hosted standard Payment Link (`POST /v1/payment_links`) for the exact amount, `accept_partial=false`, a matching `reference_id`, and buyer/order notes. It sends a **Pay Now** URL button. The payment link typically expires in 60 minutes; QR in 30 minutes.
3. Buyer pays using the QR or Razorpay-hosted checkout. **No merchant webhook URL or callback URL is configured or used.**
4. Bot's worker polls Razorpay API using Key ID + Key Secret or buyer taps **Check Payment**. QR orders verify QR metadata, QR payments list, and individual payment. Link orders verify payment link metadata/status/amount, the captured payment listed on that exact link, and the independent payment record (`GET /v1/payments/{id}`).
5. Activation requires an exact captured, INR, non-refunded payment. Payment Link **paid** status alone is not trusted. Stored order reference, Telegram user notes and amounts must match on new links. The payment ID has a MongoDB unique index; all activations are order-idempotent.
6. The purchased credits, 24-hour pass or API plan activate automatically after verification. API plan keys arrive in a separate Telegram message.

After QR/Link expiry, the bot keeps checking for delayed capture during a two-hour settlement grace. Gateway/API failures never approve orders. If settlement arrives unusually late (or your bot has been offline beyond the grace period), support may need to reconcile the order manually with Razorpay.

**Payment Link** is the Razorpay-hosted URL needed for the buyer to make a payment; it is not an incoming webhook or callback URL. Payment verification uses outbound HTTPS only.

## Payment Log Group

Configure through:

```text
/setting → Payment Settings → Payment Log Group
```

The log group receives important payment events including:

- Razorpay QR created
- Razorpay Payment Link created
- Razorpay payment verified / purchase activated
- Manual payment proof submitted
- Manual payment approved
- Manual payment rejected

Logs include order ID, Telegram user ID/name, plan, amount, payment method, timestamp in IST, and Razorpay payment/RRN identifiers when available. Full Razorpay credentials are never sent to the log group.

## Switching Back to Manual

Open **/setting → Payment Settings → Manual QR**.

New purchases immediately use the manual QR/proof/admin-approval flow. Existing pending Razorpay orders can still be checked while Razorpay credentials remain configured.

## Storage and Security

Razorpay configuration and payment-log group configuration persist in MongoDB `app_settings`. The Key Secret is encrypted with `APP_ENCRYPTION_KEY`, which `start.sh` stores in `.env`. On server migration, copy the same `.env` securely; otherwise re-enter the Razorpay Key Secret. Protect MongoDB access and do not expose database backups, `.env`, Cloudflare credentials, or Telegram bot tokens publicly.
