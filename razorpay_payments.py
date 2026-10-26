import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from payment_logs import send_payment_log

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
RAZORPAY_API = "https://api.razorpay.com/v1"
# An expired QR/link can have an in-flight payment that settles a little later.
PAYMENT_SETTLEMENT_GRACE_SECONDS = 2 * 60 * 60
QR_SETTLEMENT_GRACE_SECONDS = PAYMENT_SETTLEMENT_GRACE_SECONDS


def _as_utc(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_dt(value):
    if not value:
        return "-"
    try:
        return _as_utc(value).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(value)


def public_api_base(config, db_module) -> str:
    configured = (getattr(config, "public_api_url", "") or "").strip().rstrip("/")
    auto_tunnel = bool(getattr(config, "auto_cloudflare_tunnel", False))
    if configured and not auto_tunnel:
        return configured
    runtime = db_module.get_runtime_public_api_url()
    if runtime:
        return runtime.rstrip("/")
    return configured or "https://api.teamsecret.in"


def payment_qr_keyboard(order_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check Payment", callback_data=f"rzp_check:{order_id}")],
        [InlineKeyboardButton("📦 Order History", callback_data="order_history")],
    ])


def payment_link_keyboard(order_id: str, url: str):
    # URL is issued by authenticated Razorpay API, never by a Telegram user.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pay Now — Razorpay", url=url)],
        [InlineKeyboardButton("✅ Check Payment", callback_data=f"rzp_check:{order_id}")],
        [InlineKeyboardButton("📦 Order History", callback_data="order_history")],
    ])


def valid_payment_link_url(url: str) -> bool:
    """Prevent non-HTTPS or untrusted links from appearing in checkout."""
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and parts.username is None and parts.password is None
            and parts.port in (None, 443)
            and (host == "rzp.io" or host == "razorpay.me" or host.endswith(".razorpay.com"))
        )
    except ValueError:
        return False


async def create_payment_link(db_module, order_id: str, package: dict, user) -> dict:
    """Make a Razorpay-hosted checkout URL, verified later only by Basic Auth API."""
    cfg = db_module.get_razorpay_config()
    key_id = cfg.get("key_id", "")
    key_secret = cfg.get("key_secret", "")
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay credentials are not configured")
    amount_paise = int(package["price"]) * 100
    if amount_paise <= 0:
        raise ValueError("Invalid payment amount")
    business = (cfg.get("business_name") or "Bot Purchase").strip()
    expire_by = int(time.time()) + 60 * 60
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": str(order_id),
        "description": f"{business} - {package.get('label', 'Purchase')}"[:255],
        "expire_by": expire_by,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {
            "order_id": str(order_id),
            "telegram_user_id": str(getattr(user, "id", "")),
            "order_type": str(package.get("type", "")),
        },
        # Intentionally no callback_url or webhook. Telegram checks Razorpay API.
    }
    async with httpx.AsyncClient(timeout=20.0, auth=(key_id, key_secret)) as client:
        response = await client.post(f"{RAZORPAY_API}/payment_links", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"Razorpay create Payment Link failed (HTTP {response.status_code}): {response.text[:500]}")
        return response.json()


async def create_payment_qr(db_module, order_id: str, package: dict, user) -> dict:
    cfg = db_module.get_razorpay_config()
    key_id = cfg.get("key_id", "")
    key_secret = cfg.get("key_secret", "")
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay credentials are not configured")

    amount_paise = int(package["price"]) * 100
    business = (cfg.get("business_name") or "Bot Purchase").strip()
    # Keep QR usable long enough for normal checkout, but do not leave abandoned single-use QRs open forever.
    close_by = int(time.time()) + 30 * 60
    payload = {
        "type": "upi_qr",
        "name": business[:50],
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": amount_paise,
        "description": f"{business} - {package.get('label', 'Purchase')}"[:255],
        "close_by": close_by,
        "notes": {
            "order_id": str(order_id),
            "telegram_user_id": str(getattr(user, "id", "")),
            "order_type": str(package.get("type", "")),
        },
    }

    async with httpx.AsyncClient(timeout=20.0, auth=(key_id, key_secret)) as client:
        response = await client.post(f"{RAZORPAY_API}/payments/qr_codes", json=payload)
        if response.status_code >= 400:
            detail = response.text[:700]
            raise RuntimeError(f"Razorpay create QR failed ({response.status_code}): {detail}")
        return response.json()


async def fetch_qr_payments(db_module, qr_id: str) -> dict:
    cfg = db_module.get_razorpay_config()
    key_id = cfg.get("key_id", "")
    key_secret = cfg.get("key_secret", "")
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay credentials are not configured")
    async with httpx.AsyncClient(timeout=20.0, auth=(key_id, key_secret)) as client:
        response = await client.get(
            f"{RAZORPAY_API}/payments/qr_codes/{qr_id}/payments",
            params={"count": 20},
        )
        if response.status_code >= 400:
            detail = response.text[:700]
            raise RuntimeError(f"Razorpay QR payment check failed ({response.status_code}): {detail}")
        return response.json()


async def fetch_qr_details(db_module, qr_id: str) -> dict:
    """Read QR details directly from Razorpay (Basic Auth, no public callback)."""
    cfg = db_module.get_razorpay_config()
    if not cfg.get("key_id") or not cfg.get("key_secret"):
        raise RuntimeError("Razorpay credentials are not configured")
    async with httpx.AsyncClient(timeout=20.0, auth=(cfg["key_id"], cfg["key_secret"])) as client:
        response = await client.get(f"{RAZORPAY_API}/payments/qr_codes/{qr_id}")
        response.raise_for_status()
        return response.json()


async def fetch_payment_by_id(db_module, payment_id: str) -> dict:
    """Independently confirm a QR-listed payment on the Payment API."""
    cfg = db_module.get_razorpay_config()
    if not cfg.get("key_id") or not cfg.get("key_secret"):
        raise RuntimeError("Razorpay credentials are not configured")
    async with httpx.AsyncClient(timeout=20.0, auth=(cfg["key_id"], cfg["key_secret"])) as client:
        response = await client.get(f"{RAZORPAY_API}/payments/{payment_id}")
        response.raise_for_status()
        return response.json()


def _qr_matches_order(qr: dict, order: dict) -> bool:
    """Prevent another QR/plan from being substituted for this purchase."""
    if str(qr.get("id") or "") != str(order.get("razorpay_qr_id") or ""):
        return False
    if qr.get("type") != "upi_qr" or qr.get("usage") != "single_use":
        return False
    if qr.get("fixed_amount") is not True:
        return False
    try:
        if int(order.get("price_amount") or 0) <= 0:
            return False
        if int(qr.get("payment_amount") or 0) != int(order.get("price_amount") or 0) * 100:
            return False
    except (TypeError, ValueError):
        return False
    notes = qr.get("notes") or {}
    if isinstance(notes, dict) and notes:
        if notes.get("order_id") and str(notes["order_id"]) != str(order.get("order_id")):
            return False
        if notes.get("telegram_user_id") and str(notes["telegram_user_id"]) != str(order.get("user_id")):
            return False
    return True


def _captured_payment_matches_order(payment: dict, order: dict, payment_id: str, *, expected_method: str | None = "upi") -> bool:
    """Check trusted gateway fields; never approve from a callback or UTR alone."""
    try:
        return (
            payment.get("id") == payment_id
            and payment.get("status") == "captured"
            and payment.get("currency") == "INR"
            and (expected_method is None or payment.get("method") == expected_method)
            and payment.get("captured") is not False
            and int(payment.get("amount") or 0) == int(order.get("price_amount") or 0) * 100
            and int(payment.get("amount_refunded") or 0) == 0
        )
    except (TypeError, ValueError):
        return False


# Payment Link verification works for both newly created and older orders.
async def fetch_payment_link(db_module, payment_link_id: str) -> dict:
    cfg = db_module.get_razorpay_config()
    key_id = cfg.get("key_id", "")
    key_secret = cfg.get("key_secret", "")
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay credentials are not configured")
    async with httpx.AsyncClient(timeout=20.0, auth=(key_id, key_secret)) as client:
        response = await client.get(f"{RAZORPAY_API}/payment_links/{payment_link_id}")
        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(f"Razorpay status check failed ({response.status_code}): {detail}")
        return response.json()


def _payment_link_matches_order(link: dict, order: dict) -> bool:
    """Bind a link to the stored order, exact price and server-issued reference."""
    if str(link.get("id") or "") != str(order.get("razorpay_payment_link_id") or ""):
        return False
    try:
        expected = int(order.get("price_amount") or 0) * 100
        if expected <= 0 or int(link.get("amount") or 0) != expected or link.get("currency") != "INR":
            return False
    except (TypeError, ValueError):
        return False
    if link.get("accept_partial") is True:
        return False
    if order.get("razorpay_link_reference_enforced"):
        if str(link.get("reference_id") or "") != str(order.get("order_id")):
            return False
        notes = link.get("notes") or {}
        if not isinstance(notes, dict):
            return False
        if str(notes.get("order_id") or "") != str(order.get("order_id")):
            return False
        if str(notes.get("telegram_user_id") or "") != str(order.get("user_id")):
            return False
    return True


async def send_api_key_delivery(application, config, db_module, order_id: str) -> bool:
    order = db_module.get_credit_order(order_id) or {}
    activation = db_module.get_api_activation_for_order(order_id)
    if not activation or not activation.get("raw_key"):
        db_module.mark_order_key_delivery(order_id, "failed", "API key recovery secret unavailable")
        return False

    user_id = int(order.get("user_id") or activation.get("user_id") or 0)
    if not user_id:
        db_module.mark_order_key_delivery(order_id, "failed", "Missing user ID")
        return False

    raw_key = activation["raw_key"]
    expires = activation.get("expires_at")
    base = public_api_base(config, db_module)
    secret_text = (
        "✅ API PLAN ACTIVATED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Order: #{order_id}\n"
        f"Valid until: {_fmt_dt(expires)}\n\n"
        f"API Key:\n{raw_key}\n\n"
        f"Base URL:\n{base}\n\n"
        "Endpoints:\n"
        f"Mobile: {base}/api/lookup?number=9876543210\n"
        f"ID: {base}/api/id?id=123456789012\n\n"
        "Required Header:\nX-API-Key: YOUR_API_KEY\n\n"
        "⚠️ Save this key securely. The full key is shown only now."
    )
    try:
        await application.bot.send_message(chat_id=user_id, text=secret_text)
        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🔐 Save the API key message above securely.\n"
                "You can view plan status from My API."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 API Documentation", callback_data="api_docs")],
                [InlineKeyboardButton("🔑 My API", callback_data="my_api")],
                [InlineKeyboardButton("📦 Order History", callback_data="order_history")],
            ]),
        )
    except Exception as exc:
        db_module.mark_order_key_delivery(order_id, "failed", str(exc))
        logger.exception("Could not deliver API key for order %s", order_id)
        return False

    db_module.mark_api_key_delivered_for_order(order_id)
    return True


async def activate_paid_order(application, config, db_module, order_id: str) -> bool:
    """Activate a paid Razorpay order with per-order idempotency.

    A crash after applying credits/pass/API but before final order approval is safe:
    retrying the same order will not apply the purchase twice.
    """
    claimed = db_module.claim_credit_order(order_id, 0)
    if not claimed:
        existing = db_module.get_credit_order(order_id)
        return bool(existing and existing.get("status") == "approved")

    user_id = int(claimed["user_id"])
    order_type = claimed.get("order_type", "credits")
    key_delivery_needed = False
    try:
        if order_type == "credits":
            amount = int(claimed.get("credits_amount", 0) or 0)
            balance, _ = db_module.apply_credit_order(user_id, amount, order_id)
            user_text = (
                "✅ RAZORPAY PAYMENT VERIFIED\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Order: #{order_id}\n"
                f"Credits added: +{amount}\n"
                f"New balance: {balance}"
            )
        elif order_type == "bot_day":
            expires, _ = db_module.activate_bot_pass_for_order(user_id, order_id, 24)
            user_text = (
                "✅ 24-HOUR UNLIMITED ACTIVATED\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Order: #{order_id}\n"
                f"Valid until: {_fmt_dt(expires)}\n"
                "Credits will not be deducted while the pass is active."
            )
        elif order_type == "api_month":
            _, expires, _ = db_module.activate_api_plan_for_order(user_id, order_id, 30)
            db_module.mark_order_key_delivery(order_id, "pending")
            key_delivery_needed = True
            user_text = (
                "✅ Razorpay payment verified and API plan activated.\n"
                "Your API key will be sent in a separate message."
            )
        else:
            raise ValueError(f"Unknown order type: {order_type}")

        finalized = db_module.finalize_credit_order(order_id, "approved", 0)
        if not finalized:
            existing = db_module.get_credit_order(order_id)
            if not existing or existing.get("status") != "approved":
                raise RuntimeError("Could not finalize paid order")
        db_module.update_razorpay_payment_status(order_id, "paid")
    except Exception:
        db_module.reset_credit_order_pending(order_id, 0)
        logger.exception("Failed to activate Razorpay order %s", order_id)
        return False

    if key_delivery_needed:
        await send_api_key_delivery(application, config, db_module, order_id)
    else:
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=user_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Order History", callback_data="order_history")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                ]),
            )
        except Exception:
            logger.exception("Could not notify user %s for Razorpay order %s", user_id, order_id)

    try:
        final_order = db_module.get_credit_order(order_id) or claimed
        await send_payment_log(
            application.bot,
            db_module,
            "PAID / ACTIVATED",
            final_order,
            payment_id=final_order.get("razorpay_payment_id", ""),
            rrn=final_order.get("razorpay_rrn", ""),
            status="approved",
        )
    except Exception:
        logger.exception("Could not send payment success log for %s", order_id)

    return True


def _extract_rrn(payment: dict) -> str:
    acquirer = payment.get("acquirer_data") or {}
    if isinstance(acquirer, dict):
        return str(acquirer.get("rrn") or acquirer.get("upi_transaction_id") or "")
    return ""


async def check_and_process_order(application, config, db_module, order: dict) -> tuple[str, bool]:
    order_id = str(order.get("order_id", ""))
    if not order_id:
        return "invalid", False

    qr_id = str(order.get("razorpay_qr_id", "") or "")
    if qr_id:
        try:
            # All three reads are authenticated with Razorpay Key ID + Key Secret.
            # No webhook, redirect URL, or public HTTP route participates.
            qr = await fetch_qr_details(db_module, qr_id)
            if not _qr_matches_order(qr, order):
                logger.error("Razorpay QR metadata does not match order %s", order_id)
                return "mismatch", False
            collection = await fetch_qr_payments(db_module, qr_id)
            if not isinstance(collection, dict) or not isinstance(collection.get("items"), list):
                logger.error("Unexpected Razorpay payment collection for %s", order_id)
                return "error", False

            for listed_payment in collection["items"]:
                if not isinstance(listed_payment, dict):
                    continue
                payment_id = str(listed_payment.get("id") or "")
                if not payment_id or not _captured_payment_matches_order(listed_payment, order, payment_id):
                    continue
                # GET /payments/{id} protects against stale/inconsistent list data.
                confirmed = await fetch_payment_by_id(db_module, payment_id)
                if not _captured_payment_matches_order(confirmed, order, payment_id):
                    logger.warning("Payment status/amount changed during verification: %s", payment_id)
                    continue

                rrn = _extract_rrn(confirmed)
                recorded = db_module.record_verified_razorpay_payment(
                    order_id, qr_id=qr_id, payment_id=payment_id, rrn=rrn
                )
                if not recorded:
                    latest = db_module.get_credit_order(order_id) or {}
                    if latest.get("status") == "approved" and latest.get("razorpay_payment_id") == payment_id:
                        return "paid", True
                    logger.error("Payment %s could not be reserved for order %s", payment_id, order_id)
                    return "duplicate", False
                ok = await activate_paid_order(application, config, db_module, order_id)
                return "paid", ok
        except Exception:
            logger.exception("Direct Razorpay API verification failed for %s", order_id)
            return "error", False

        # A QR may close before an already-initiated bank payment is fully captured.
        # Continue checking for a grace period so real customers are not stranded.
        close_by = order.get("razorpay_qr_close_by") or qr.get("close_by")
        try:
            expired = bool(close_by and int(close_by) <= int(time.time()))
            grace_over = bool(close_by and int(close_by) + QR_SETTLEMENT_GRACE_SECONDS <= int(time.time()))
        except (TypeError, ValueError):
            expired = grace_over = False
        if expired:
            if grace_over:
                db_module.mark_order_payment_failed(order_id, "Razorpay QR expired; no captured payment after settlement grace")
                return "expired", False
            db_module.update_razorpay_payment_status(order_id, "settlement_pending")
            return "settlement_pending", False

        db_module.update_razorpay_payment_status(order_id, "pending")
        return "pending", False

    # Razorpay-hosted Payment Link: no callback, signature in query, or webhook trusted.
    link_id = str(order.get("razorpay_payment_link_id", "") or "")
    if not link_id:
        return "invalid", False
    try:
        payload = await fetch_payment_link(db_module, link_id)
        if not _payment_link_matches_order(payload, order):
            logger.error("Razorpay Payment Link metadata mismatch for order %s", order_id)
            return "mismatch", False

        status = str(payload.get("status") or "").lower()
        expected = int(order.get("price_amount") or 0) * 100
        if status == "paid":
            # Even 'paid' from Razorpay must not grant anything without a
            # separately verified captured payment for the exact amount.
            if (int(payload.get("amount_paid") or 0) != expected
                    or ("amount_due" in payload and int(payload["amount_due"] or 0) != 0)):
                logger.error("Razorpay Payment Link amount_paid mismatch for order %s", order_id)
                return "mismatch", False
            payments = payload.get("payments") or []
            if not isinstance(payments, list):
                return "error", False
            for listed in payments:
                if not isinstance(listed, dict) or listed.get("status") != "captured":
                    continue
                if listed.get("plink_id") and str(listed["plink_id"]) != link_id:
                    continue
                if "amount" in listed and int(listed["amount"]) != expected:
                    continue
                payment_id = str(listed.get("payment_id") or "")
                if not payment_id:
                    continue
                confirmed = await fetch_payment_by_id(db_module, payment_id)
                if not _captured_payment_matches_order(confirmed, order, payment_id, expected_method=None):
                    continue
                recorded = db_module.record_verified_razorpay_link_payment(
                    order_id, link_id=link_id, payment_id=payment_id, rrn=_extract_rrn(confirmed)
                )
                if not recorded:
                    latest = db_module.get_credit_order(order_id) or {}
                    if latest.get("status") == "approved" and latest.get("razorpay_payment_id") == payment_id:
                        return "paid", True
                    return "duplicate", False
                ok = await activate_paid_order(application, config, db_module, order_id)
                return "paid", ok
            pending_status = "settlement_pending" if order.get("razorpay_link_reference_enforced") else "pending"
            db_module.update_razorpay_payment_status(order_id, pending_status)
            return pending_status, False

        if status in {"expired", "cancelled"}:
            # Check delayed captured payments for a limited grace window. This
            # also prevents an expired link from being treated as immediate proof.
            expire_by = order.get("razorpay_link_expire_by") or payload.get("expire_by")
            try:
                grace_over = (not expire_by) or (int(expire_by) + PAYMENT_SETTLEMENT_GRACE_SECONDS <= int(time.time()))
            except (TypeError, ValueError):
                grace_over = True
            if not grace_over:
                db_module.update_razorpay_payment_status(order_id, "settlement_pending")
                return "settlement_pending", False
            db_module.mark_order_payment_failed(order_id, f"Razorpay Payment Link {status}; no captured payment")
            return status, False

        db_module.update_razorpay_payment_status(order_id, status or "pending")
        return status or "pending", False
    except Exception:
        logger.exception("Razorpay Payment Link API verification failed for %s", order_id)
        return "error", False


async def razorpay_payment_worker(application, config, db_module, owner_id: str = "bot"):
    """Poll Razorpay orders with a MongoDB lease to avoid duplicate workers."""
    await asyncio.sleep(5)
    while True:
        try:
            if db_module.acquire_worker_lease("razorpay-payment-worker", owner_id, ttl_seconds=120):
                # Recover claims abandoned by a crashed task without issuing
                # duplicate credits (apply_* functions are order-idempotent).
                db_module.recover_stale_processing_orders()
                if db_module.is_razorpay_configured():
                    for order in db_module.get_pending_razorpay_orders(limit=40):
                        await check_and_process_order(application, config, db_module, order)
                        await asyncio.sleep(0.15)

                # Retry API key delivery independently from payment activation.
                for order in db_module.get_pending_api_key_deliveries(limit=10):
                    await send_api_key_delivery(application, config, db_module, str(order.get("order_id", "")))
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Razorpay payment worker error")
        await asyncio.sleep(20)
