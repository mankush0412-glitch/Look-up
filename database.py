import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from cryptography.fernet import Fernet, InvalidToken

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_lookup_bot").strip()

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI is missing. Add MONGO_URI to your .env before starting the bot."
    )

_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, tz_aware=True)
_db = _client[MONGO_DB_NAME]

authorized_users = _db["authorized_users"]
credits = _db["credits"]
referrals = _db["referrals"]
force_channels = _db["force_channels"]
join_requests = _db["join_requests"]
pending_join_links = _db["pending_join_links"]
reward_links = _db["reward_links"]
credit_orders = _db["credit_orders"]
app_settings = _db["app_settings"]
worker_leases = _db["worker_leases"]


def _fernet():
    key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise RuntimeError("APP_ENCRYPTION_KEY is invalid. Use a Fernet-compatible key.") from exc


def _encrypt_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    f = _fernet()
    if f is None:
        raise RuntimeError("APP_ENCRYPTION_KEY is missing; cannot store sensitive settings securely.")
    return "enc:v1:" + f.encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if not value.startswith("enc:v1:"):
        return value
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(value[len("enc:v1:"):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def _manual_txn_hash(value: str) -> str:
    import hashlib
    normalized = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    if len(normalized) < 4:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _backfill_manual_txn_hashes() -> None:
    rows = credit_orders.find(
        {
            "payment_method": {"$in": [None, "", "manual"]},
            "manual_txn_hash": {"$exists": False},
            "proof.type": "text",
        },
        {"_id": 1, "proof.value": 1},
    )
    for row in rows:
        proof = row.get("proof") or {}
        txn_hash = _manual_txn_hash(proof.get("value", ""))
        if not txn_hash:
            continue
        try:
            credit_orders.update_one(
                {"_id": row["_id"], "manual_txn_hash": {"$exists": False}},
                {"$set": {"manual_txn_hash": txn_hash}},
            )
        except DuplicateKeyError:
            # An older duplicate may already exist; the first indexed order owns the hash.
            continue


def _migrate_sensitive_settings() -> None:
    row = app_settings.find_one({"key": "razorpay_key_secret"}, {"_id": 0, "value": 1})
    if not row:
        return
    current = str(row.get("value") or "")
    if not current or current.startswith("enc:v1:"):
        return
    if _fernet() is None:
        return
    app_settings.update_one(
        {"key": "razorpay_key_secret", "value": current},
        {"$set": {"value": _encrypt_secret(current), "updated_at": datetime.now(timezone.utc)}},
    )


def _ensure_sparse_unique_index(collection, field: str, name: str):
    for idx in collection.list_indexes():
        if list(idx.get("key", {}).items()) == [(field, 1)]:
            if bool(idx.get("unique", False)) and bool(idx.get("sparse", False)):
                return idx.get("name")
            try:
                collection.drop_index(idx.get("name"))
            except Exception:
                pass
            break
    return collection.create_index(field, unique=True, sparse=True, name=name)


def _ensure_index(collection, field, *, unique=False, name=None):
    wanted_key = [(field, 1)] if isinstance(field, str) else list(field)
    for idx in collection.list_indexes():
        if list(idx.get("key", {}).items()) == wanted_key:
            if bool(idx.get("unique", False)) == bool(unique):
                return idx.get("name")
    return collection.create_index(field, unique=unique, name=name)


def init_db():
    _client.admin.command("ping")

    _ensure_index(authorized_users, "telegram_id", unique=True, name="telegram_id_unique")
    _ensure_index(credits, "user_id", unique=True, name="user_id_unique")
    _ensure_index(referrals, "referred_user_id", unique=True, name="referred_user_id_unique")
    _ensure_index(referrals, "referrer_id", name="referrer_id_idx")
    _ensure_index(force_channels, "chat_id", unique=True, name="chat_id_unique")
    _ensure_index(reward_links, "token", unique=True, name="reward_token_unique")
    _ensure_index(credit_orders, "order_id", unique=True, name="credit_order_id_unique")
    _ensure_index(credit_orders, "user_id", name="credit_order_user_idx")
    _ensure_index(credit_orders, "status", name="credit_order_status_idx")
    _ensure_index(credit_orders, [("payment_method", 1), ("status", 1)], name="credit_order_payment_status_idx")
    _ensure_sparse_unique_index(credit_orders, "manual_txn_hash", "manual_txn_hash_unique")
    _ensure_sparse_unique_index(credit_orders, "razorpay_payment_id", "razorpay_payment_id_unique")
    _ensure_sparse_unique_index(credit_orders, "razorpay_qr_id", "razorpay_qr_id_unique")
    _backfill_manual_txn_hashes()
    ensure_plan_indexes()
    _migrate_sensitive_settings()
    recover_stale_processing_orders()

    # Join-request records with 24h TTL
    try:
        join_requests.create_index(
            "requested_at", expireAfterSeconds=86400, name="jr_ttl"
        )
    except Exception:
        pass
    try:
        join_requests.create_index(
            [("user_id", 1), ("chat_id", 1)],
            unique=True,
            name="jr_user_chat_unique",
        )
    except Exception:
        pass

    # Pending one-time join links (2h safety TTL)
    try:
        pending_join_links.create_index(
            "created_at", expireAfterSeconds=7200, name="pjl_ttl"
        )
    except Exception:
        pass
    try:
        pending_join_links.create_index(
            [("user_id", 1), ("chat_id", 1)],
            unique=True,
            name="pjl_user_chat_unique",
        )
    except Exception:
        pass


# ----- Authorized users -----

def add_user(user_id: int, added_by: int = None) -> bool:
    user_id = int(user_id)
    try:
        authorized_users.insert_one({
            "telegram_id": user_id,
            "added_by": int(added_by) if added_by is not None else None,
            "added_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return False

    credits.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "balance": 5}},
        upsert=True,
    )
    return True


def remove_user(user_id: int) -> bool:
    result = authorized_users.delete_one({"telegram_id": int(user_id)})
    return result.deleted_count > 0


def is_dynamic_user(user_id: int) -> bool:
    return authorized_users.find_one(
        {"telegram_id": int(user_id)}, {"_id": 1}
    ) is not None


def get_user(user_id: int) -> bool:
    return is_dynamic_user(user_id)


def list_users() -> list[int]:
    rows = authorized_users.find(
        {}, {"_id": 0, "telegram_id": 1}
    ).sort("telegram_id", 1)
    return [int(row["telegram_id"]) for row in rows]


# ----- Credits -----

def init_credits(user_id: int) -> None:
    credits.update_one(
        {"user_id": int(user_id)},
        {"$setOnInsert": {"user_id": int(user_id), "balance": 5}},
        upsert=True,
    )


def get_credits(user_id: int) -> int:
    row = credits.find_one(
        {"user_id": int(user_id)}, {"_id": 0, "balance": 1}
    )
    return int(row.get("balance", 0)) if row else 0


def add_credits(user_id: int, amount: int) -> int:
    user_id = int(user_id)
    amount = int(amount)
    init_credits(user_id)
    row = credits.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"balance": amount}},
        return_document=ReturnDocument.AFTER,
    )
    return int(row.get("balance", 0)) if row else 0


def consume_credit(user_id: int, amount: int = 1) -> bool:
    user_id = int(user_id)
    amount = int(amount)
    if amount <= 0:
        return True
    init_credits(user_id)
    row = credits.find_one_and_update(
        {"user_id": user_id, "balance": {"$gte": amount}},
        {"$inc": {"balance": -amount}},
        return_document=ReturnDocument.AFTER,
    )
    return row is not None


def set_credits(user_id: int, amount: int) -> int:
    user_id = int(user_id)
    amount = max(0, int(amount))
    credits.update_one(
        {"user_id": user_id},
        {"$set": {"balance": amount}, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )
    return amount


# ----- Persistent app settings -----

def set_app_setting(key: str, value) -> None:
    app_settings.update_one(
        {"key": str(key)},
        {
            "$set": {
                "value": value,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {"key": str(key)},
        },
        upsert=True,
    )


def get_app_setting(key: str, default=None):
    row = app_settings.find_one({"key": str(key)}, {"_id": 0, "value": 1})
    return row.get("value", default) if row else default


def set_contact_bot_username(username: str) -> str:
    username = str(username or "").strip().lstrip("@")
    set_app_setting("contact_bot_username", username)
    return username


def get_contact_bot_username() -> str:
    return str(get_app_setting("contact_bot_username", "") or "").strip().lstrip("@")


def set_welcome_image_file_id(file_id: str) -> str:
    file_id = str(file_id or "").strip()
    set_app_setting("welcome_image_file_id", file_id)
    return file_id


def get_welcome_image_file_id() -> str:
    return str(get_app_setting("welcome_image_file_id", "") or "").strip()


def clear_welcome_image_file_id() -> None:
    set_app_setting("welcome_image_file_id", "")


def set_runtime_public_api_url(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    set_app_setting("runtime_public_api_url", url)
    return url


def get_runtime_public_api_url() -> str:
    return str(get_app_setting("runtime_public_api_url", "") or "").strip().rstrip("/")


# ----- Payment settings -----

def get_payment_mode() -> str:
    mode = str(get_app_setting("payment_mode", "manual") or "manual").strip().lower()
    return mode if mode in {"manual", "razorpay"} else "manual"


def set_payment_mode(mode: str) -> str:
    mode = str(mode or "").strip().lower()
    if mode not in {"manual", "razorpay"}:
        raise ValueError("Invalid payment mode")
    set_app_setting("payment_mode", mode)
    return mode


def set_razorpay_key_id(value: str) -> None:
    set_app_setting("razorpay_key_id", str(value or "").strip())


def set_razorpay_key_secret(value: str) -> None:
    value = str(value or "").strip()
    set_app_setting("razorpay_key_secret", _encrypt_secret(value) if value else "")


def set_razorpay_business_name(value: str) -> None:
    set_app_setting("razorpay_business_name", str(value or "").strip()[:80])


def get_razorpay_config() -> dict:
    # An environment pair takes precedence only when BOTH credentials are supplied.
    # Never mix the Key ID of one Razorpay account with another account's secret.
    env_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
    env_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    saved_id = str(get_app_setting("razorpay_key_id", "") or "").strip()
    saved_secret = _decrypt_secret(str(get_app_setting("razorpay_key_secret", "") or "").strip())
    return {
        "key_id": env_id if env_id and env_secret else saved_id,
        "key_secret": env_secret if env_id and env_secret else saved_secret,
        "business_name": str(get_app_setting("razorpay_business_name", "") or "").strip(),
    }


def is_razorpay_configured() -> bool:
    cfg = get_razorpay_config()
    return bool(cfg["key_id"] and cfg["key_secret"])


def set_payment_log_chat(chat_id: int, title: str = "") -> None:
    set_app_setting("payment_log_chat_id", int(chat_id))
    set_app_setting("payment_log_chat_title", str(title or "").strip()[:150])


def clear_payment_log_chat() -> None:
    set_app_setting("payment_log_chat_id", "")
    set_app_setting("payment_log_chat_title", "")


def get_payment_log_chat_id() -> int | None:
    value = get_app_setting("payment_log_chat_id", "")
    try:
        return int(value) if str(value).strip() else None
    except (TypeError, ValueError):
        return None


def get_payment_log_chat_title() -> str:
    return str(get_app_setting("payment_log_chat_title", "") or "").strip()


# ----- Referrals -----

def create_referral(referrer_id: int, referred_user_id: int) -> bool:
    referrer_id = int(referrer_id)
    referred_user_id = int(referred_user_id)
    if referrer_id == referred_user_id:
        return False
    if not get_user(referrer_id) or not get_user(referred_user_id):
        return False
    try:
        referrals.insert_one({
            "referrer_id": referrer_id,
            "referred_user_id": referred_user_id,
            "created_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


def get_referrer(user_id: int) -> int | None:
    row = referrals.find_one(
        {"referred_user_id": int(user_id)},
        {"_id": 0, "referrer_id": 1},
    )
    return int(row["referrer_id"]) if row else None


def get_referred_users(referrer_id: int) -> list[int]:
    rows = referrals.find(
        {"referrer_id": int(referrer_id)},
        {"_id": 0, "referred_user_id": 1},
    ).sort("created_at", 1)
    return [int(row["referred_user_id"]) for row in rows]


def count_referred_users(referrer_id: int) -> int:
    return referrals.count_documents({"referrer_id": int(referrer_id)})


# ----- Force Join Channels -----

def add_force_channel(chat_id: int, title: str, invite_link: str, username: str | None = None) -> bool:
    chat_id = int(chat_id)
    result = force_channels.update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "title": str(title or chat_id),
                "invite_link": str(invite_link),
                "username": username,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "chat_id": chat_id,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )
    return bool(result.upserted_id is not None or result.modified_count > 0)


def remove_force_channel(chat_id: int) -> bool:
    result = force_channels.delete_one({"chat_id": int(chat_id)})
    return result.deleted_count > 0


def get_force_channel(chat_id: int) -> dict | None:
    return force_channels.find_one({"chat_id": int(chat_id)}, {"_id": 0})


def list_force_channels() -> list[dict]:
    return list(force_channels.find({}, {"_id": 0}).sort("created_at", 1))


# ----- Join Request Records (24h auto-expire) -----

def record_join_request(user_id: int, chat_id: int) -> None:
    join_requests.update_one(
        {"user_id": int(user_id), "chat_id": int(chat_id)},
        {
            "$set": {
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "requested_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def has_active_join_request(user_id: int, chat_id: int) -> bool:
    return join_requests.find_one(
        {"user_id": int(user_id), "chat_id": int(chat_id)},
        {"_id": 1},
    ) is not None


def clear_join_request(user_id: int, chat_id: int) -> None:
    join_requests.delete_one(
        {"user_id": int(user_id), "chat_id": int(chat_id)}
    )


# ----- Pending one-time join links -----

def store_pending_join_link(user_id: int, chat_id: int, invite_link: str) -> None:
    pending_join_links.update_one(
        {"user_id": int(user_id), "chat_id": int(chat_id)},
        {
            "$set": {
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "invite_link": invite_link,
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def get_pending_join_link(user_id: int, chat_id: int) -> str | None:
    row = pending_join_links.find_one(
        {"user_id": int(user_id), "chat_id": int(chat_id)},
        {"_id": 0, "invite_link": 1},
    )
    return row.get("invite_link") if row else None


def pop_pending_join_link(user_id: int, chat_id: int) -> str | None:
    row = pending_join_links.find_one_and_delete(
        {"user_id": int(user_id), "chat_id": int(chat_id)}
    )
    return row.get("invite_link") if row else None


# ----- Reward Links -----

def create_reward_link(token: str, credits: int, expires_at, created_by: int) -> None:
    reward_links.insert_one({
        "token": str(token),
        "credits": int(credits),
        "expires_at": expires_at,
        "created_by": int(created_by),
        "created_at": datetime.now(timezone.utc),
        "claimed_by": [],
        "uses": 0,
    })


def claim_reward(token: str, user_id: int) -> dict:
    """
    Returns one of:
        {"status": "ok", "credits": N}
        {"status": "already_claimed"}
        {"status": "expired"}
        {"status": "invalid"}
    """
    now = datetime.now(timezone.utc)
    user_id = int(user_id)

    doc = reward_links.find_one({"token": str(token)})
    if not doc:
        return {"status": "invalid"}

    exp = doc.get("expires_at")
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            return {"status": "expired"}

    if user_id in (doc.get("claimed_by") or []):
        return {"status": "already_claimed"}

    result = reward_links.update_one(
        {"token": str(token), "claimed_by": {"$ne": user_id}},
        {
            "$addToSet": {"claimed_by": user_id},
            "$inc": {"uses": 1},
        },
    )
    if result.modified_count == 0:
        return {"status": "already_claimed"}

    return {"status": "ok", "credits": int(doc.get("credits", 0))}


def get_reward_link(token: str) -> dict | None:
    return reward_links.find_one({"token": str(token)}, {"_id": 0})

# ----- Manual credit purchase orders -----

def create_credit_order(order_id: str, user_id: int, credits_amount: int, price_amount: int, proof: dict) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "order_id": str(order_id),
        "user_id": int(user_id),
        "credits_amount": int(credits_amount),
        "price_amount": int(price_amount),
        "proof": dict(proof or {}),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    credit_orders.insert_one(doc)
    doc.pop("_id", None)
    return doc


def get_credit_order(order_id: str) -> dict | None:
    return credit_orders.find_one({"order_id": str(order_id)}, {"_id": 0})


def get_user_orders(user_id: int, limit: int = 10) -> list[dict]:
    """Return a user's newest purchase orders first."""
    limit = max(1, min(int(limit), 50))
    return list(
        credit_orders.find(
            {"user_id": int(user_id)},
            {"_id": 0, "proof": 0},
        )
        .sort("created_at", -1)
        .limit(limit)
    )


def get_payment_stats() -> dict:
    """Aggregate payment/order statistics for the admin dashboard.

    Revenue only counts approved orders. Calendar-day/month figures use IST.
    """
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(timezone.utc).astimezone(ist)
    today_start_ist = datetime.combine(now_ist.date(), time.min, tzinfo=ist)
    month_start_ist = datetime(now_ist.year, now_ist.month, 1, tzinfo=ist)
    today_start_utc = today_start_ist.astimezone(timezone.utc)
    month_start_utc = month_start_ist.astimezone(timezone.utc)

    def _count(query):
        return int(credit_orders.count_documents(query))

    def _sum_revenue(query):
        rows = list(credit_orders.aggregate([
            {"$match": query},
            {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$price_amount", 0]}}}},
        ]))
        return int(rows[0].get("total", 0) or 0) if rows else 0

    approved_q = {"status": "approved"}
    return {
        "total_orders": _count({}),
        "approved": _count({"status": "approved"}),
        "pending": _count({"status": {"$in": ["pending", "processing"]}}),
        "rejected": _count({"status": "rejected"}),
        "failed": _count({"status": {"$in": ["payment_failed", "failed"]}}),
        "manual": _count({"payment_method": {"$in": [None, "", "manual"]}}),
        "razorpay": _count({"payment_method": "razorpay"}),
        "revenue_total": _sum_revenue(approved_q),
        "revenue_today": _sum_revenue({"status": "approved", "processed_at": {"$gte": today_start_utc}}),
        "revenue_month": _sum_revenue({"status": "approved", "processed_at": {"$gte": month_start_utc}}),
        "paid_today": _count({"status": "approved", "processed_at": {"$gte": today_start_utc}}),
        "paid_month": _count({"status": "approved", "processed_at": {"$gte": month_start_utc}}),
    }


def get_payment_history(page: int = 0, page_size: int = 6) -> dict:
    """Return paginated newest-first payment/order history for admins."""
    page = max(0, int(page))
    page_size = max(1, min(int(page_size), 20))
    total = int(credit_orders.count_documents({}))
    rows = list(
        credit_orders.find(
            {},
            {"_id": 0, "proof": 0, "razorpay_payload": 0},
        )
        .sort("created_at", -1)
        .skip(page * page_size)
        .limit(page_size)
    )
    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_prev": page > 0,
        "has_next": (page + 1) * page_size < total,
    }


def is_manual_transaction_used(value: str) -> bool:
    txn_hash = _manual_txn_hash(value)
    if not txn_hash:
        return False
    return credit_orders.find_one({"manual_txn_hash": txn_hash}, {"_id": 1}) is not None


def recover_stale_processing_orders(stale_minutes: int = 10) -> int:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(stale_minutes)))
    result = credit_orders.update_many(
        {"status": "processing", "updated_at": {"$lt": cutoff}},
        {
            "$set": {
                "status": "pending",
                "recovered_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"processed_by": ""},
        },
    )
    return int(result.modified_count or 0)


def acquire_worker_lease(name: str, owner: str, ttl_seconds: int = 60) -> bool:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(10, int(ttl_seconds)))
    query = {
        "_id": str(name),
        "$or": [
            {"expires_at": {"$lte": now}},
            {"owner": str(owner)},
            {"expires_at": {"$exists": False}},
        ],
    }
    result = worker_leases.update_one(
        query,
        {"$set": {"owner": str(owner), "expires_at": expires, "updated_at": now}},
        upsert=False,
    )
    if result.matched_count:
        return True
    try:
        worker_leases.insert_one({
            "_id": str(name),
            "owner": str(owner),
            "expires_at": expires,
            "updated_at": now,
        })
        return True
    except DuplicateKeyError:
        return False


def release_worker_lease(name: str, owner: str) -> None:
    worker_leases.delete_one({"_id": str(name), "owner": str(owner)})


def apply_credit_order(user_id: int, amount: int, order_id: str) -> tuple[int, bool]:
    user_id = int(user_id)
    amount = int(amount)
    order_id = str(order_id)
    init_credits(user_id)
    row = credits.find_one_and_update(
        {"user_id": user_id, "applied_payment_orders": {"$ne": order_id}},
        {
            "$inc": {"balance": amount},
            "$addToSet": {"applied_payment_orders": order_id},
        },
        return_document=ReturnDocument.AFTER,
    )
    if row is not None:
        return int(row.get("balance", 0)), True
    row = credits.find_one({"user_id": user_id}, {"_id": 0, "balance": 1}) or {}
    return int(row.get("balance", 0)), False


def activate_bot_pass_for_order(user_id: int, order_id: str, hours: int = 24):
    user_id = int(user_id)
    order_id = str(order_id)
    hours = max(1, int(hours))
    now = _utcnow()
    plans.update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id}}, upsert=True)
    row = plans.find_one_and_update(
        {"user_id": user_id, "bot_applied_orders": {"$ne": order_id}},
        [
            {
                "$set": {
                    "bot_unlimited_until": {
                        "$dateAdd": {
                            "startDate": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$bot_unlimited_until", now]}, now]},
                                    "$bot_unlimited_until",
                                    now,
                                ]
                            },
                            "unit": "hour",
                            "amount": hours,
                        }
                    },
                    "bot_applied_orders": {
                        "$concatArrays": [
                            {"$ifNull": ["$bot_applied_orders", []]},
                            [order_id],
                        ]
                    },
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "bot_unlimited_until": 1},
    )
    if row is None:
        row = plans.find_one({"user_id": user_id}, {"_id": 0, "bot_unlimited_until": 1}) or {}
        return _as_utc(row.get("bot_unlimited_until")), False
    return _as_utc(row.get("bot_unlimited_until")), True


def activate_api_plan_for_order(user_id: int, order_id: str, days: int = 30):
    """Idempotently extend an API plan once for a purchase order and return its key.

    The raw key is encrypted temporarily in the api_keys collection so a crash between
    plan activation and Telegram delivery can recover without extending the plan twice.
    """
    import hashlib
    import secrets

    user_id = int(user_id)
    order_id = str(order_id)
    days = max(1, int(days))
    now = _utcnow()
    plans.update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id}}, upsert=True)

    plan_row = plans.find_one_and_update(
        {"user_id": user_id, "api_applied_orders": {"$ne": order_id}},
        [
            {
                "$set": {
                    "api_unlimited_until": {
                        "$dateAdd": {
                            "startDate": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$api_unlimited_until", now]}, now]},
                                    "$api_unlimited_until",
                                    now,
                                ]
                            },
                            "unit": "day",
                            "amount": days,
                        }
                    },
                    "api_applied_orders": {
                        "$concatArrays": [
                            {"$ifNull": ["$api_applied_orders", []]},
                            [order_id],
                        ]
                    },
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "api_unlimited_until": 1},
    )
    newly_applied = plan_row is not None
    if plan_row is None:
        plan_row = plans.find_one({"user_id": user_id}, {"_id": 0, "api_unlimited_until": 1}) or {}
    expires = _as_utc(plan_row.get("api_unlimited_until"))
    if not expires:
        raise RuntimeError("Could not determine API plan expiry")

    existing = api_keys.find_one({"activation_order_id": order_id}, {"_id": 0})
    if existing:
        raw_enc = str(existing.get("raw_key_encrypted") or "")
        raw_key = _decrypt_secret(raw_enc) if raw_enc else ""
        if not raw_key:
            raise RuntimeError("API key recovery secret is unavailable; regenerate key from My API")
        # Ensure the key follows any recovered expiry value.
        api_keys.update_one({"activation_order_id": order_id}, {"$set": {"expires_at": expires}})
        api_keys.update_many(
            {"user_id": user_id, "activation_order_id": {"$ne": order_id}, "revoked": {"$ne": True}},
            {"$set": {"revoked": True, "revoked_at": now}},
        )
        plans.update_one({"user_id": user_id}, {"$set": {"api_key_prefix": existing.get("key_prefix", "")}})
        return raw_key, expires, newly_applied

    raw_key = "lk_" + secrets.token_urlsafe(28)
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    prefix = raw_key[:10]
    encrypted_raw = _encrypt_secret(raw_key)
    doc = {
        "user_id": user_id,
        "key_hash": key_hash,
        "key_prefix": prefix,
        "created_at": now,
        "expires_at": expires,
        "revoked": False,
        "total_requests": 0,
        "rate_limited_requests": 0,
        "activation_order_id": order_id,
        "raw_key_encrypted": encrypted_raw,
    }
    try:
        api_keys.insert_one(doc)
    except DuplicateKeyError:
        existing = api_keys.find_one({"activation_order_id": order_id}, {"_id": 0}) or {}
        raw_key = _decrypt_secret(str(existing.get("raw_key_encrypted") or ""))
        prefix = str(existing.get("key_prefix") or "")
        if not raw_key:
            raise

    api_keys.update_many(
        {"user_id": user_id, "activation_order_id": {"$ne": order_id}, "revoked": {"$ne": True}},
        {"$set": {"revoked": True, "revoked_at": now}},
    )
    plans.update_one({"user_id": user_id}, {"$set": {"api_key_prefix": prefix}})
    return raw_key, expires, newly_applied


def get_api_activation_for_order(order_id: str) -> dict | None:
    row = api_keys.find_one(
        {"activation_order_id": str(order_id)},
        {"_id": 0, "user_id": 1, "key_prefix": 1, "expires_at": 1, "raw_key_encrypted": 1},
    )
    if not row:
        return None
    encrypted = str(row.get("raw_key_encrypted") or "")
    raw_key = _decrypt_secret(encrypted) if encrypted else ""
    return {
        "user_id": int(row.get("user_id", 0) or 0),
        "key_prefix": str(row.get("key_prefix") or ""),
        "expires_at": _as_utc(row.get("expires_at")),
        "raw_key": raw_key,
    }


def mark_order_key_delivery(order_id: str, status: str, error: str = "") -> None:
    status = str(status or "pending")
    update = {
        "key_delivery_status": status,
        "key_delivery_updated_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    if error:
        update["key_delivery_error"] = str(error)[:500]
    else:
        update["key_delivery_error"] = ""
    credit_orders.update_one({"order_id": str(order_id)}, {"$set": update})


def get_pending_api_key_deliveries(limit: int = 20) -> list[dict]:
    from datetime import timedelta
    retry_before = _utcnow() - timedelta(minutes=2)
    return list(
        credit_orders.find(
            {
                "order_type": "api_month",
                "status": "approved",
                "key_delivery_status": {"$in": ["pending", "failed"]},
                "$or": [
                    {"key_delivery_updated_at": {"$lt": retry_before}},
                    {"key_delivery_updated_at": {"$exists": False}},
                ],
            },
            {"_id": 0},
        )
        .sort("key_delivery_updated_at", 1)
        .limit(max(1, min(int(limit), 100)))
    )


def mark_api_key_delivered_for_order(order_id: str) -> None:
    now = _utcnow()
    api_keys.update_one(
        {"activation_order_id": str(order_id)},
        {
            "$set": {"key_delivered_at": now},
            "$unset": {"raw_key_encrypted": ""},
        },
    )
    credit_orders.update_one(
        {"order_id": str(order_id)},
        {
            "$set": {
                "key_delivery_status": "delivered",
                "key_delivery_updated_at": now,
                "key_delivered_at": now,
                "updated_at": now,
            },
            "$unset": {"key_delivery_error": ""},
        },
    )


def claim_credit_order(order_id: str, admin_id: int, stale_after_seconds: int = 120) -> dict | None:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=max(30, int(stale_after_seconds)))
    return credit_orders.find_one_and_update(
        {
            "order_id": str(order_id),
            "$or": [
                {"status": "pending"},
                {"status": "processing", "updated_at": {"$lt": stale_cutoff}},
            ],
        },
        {
            "$set": {
                "status": "processing",
                "processed_by": int(admin_id),
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )


def finalize_credit_order(order_id: str, status: str, admin_id: int) -> dict | None:
    if status not in {"approved", "rejected"}:
        raise ValueError("Invalid credit order status")
    return credit_orders.find_one_and_update(
        {"order_id": str(order_id), "status": {"$in": ["pending", "processing"]}},
        {
            "$set": {
                "status": status,
                "processed_by": int(admin_id),
                "processed_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )


def reset_credit_order_pending(order_id: str, admin_id: int) -> None:
    credit_orders.update_one(
        {"order_id": str(order_id), "status": "processing", "processed_by": int(admin_id)},
        {
            "$set": {"status": "pending", "updated_at": datetime.now(timezone.utc)},
            "$unset": {"processed_by": ""},
        },
    )

# ----- Paid plans / API access -----

plans = _db["paid_plans"]
api_keys = _db["api_keys"]
api_usage_daily = _db["api_usage_daily"]


def _utcnow():
    return datetime.now(timezone.utc)


def _as_utc(value):
    """Normalize MongoDB/Python datetimes to timezone-aware UTC."""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_plan_indexes():
    _ensure_index(plans, "user_id", unique=True, name="paid_plan_user_unique")
    _ensure_index(api_keys, "key_hash", unique=True, name="api_key_hash_unique")
    _ensure_index(api_keys, "user_id", name="api_key_user_idx")
    _ensure_sparse_unique_index(api_keys, "activation_order_id", "api_activation_order_unique")
    _ensure_index(api_keys, "expires_at", name="api_key_expiry_idx")
    _ensure_index(api_usage_daily, [("key_hash", 1), ("date", 1)], unique=True, name="api_usage_key_date_unique")
    _ensure_index(api_usage_daily, "date", name="api_usage_date_idx")


def has_active_bot_pass(user_id: int) -> bool:
    row = plans.find_one(
        {"user_id": int(user_id), "bot_unlimited_until": {"$gt": _utcnow()}},
        {"_id": 1},
    )
    return row is not None


def get_bot_pass_until(user_id: int):
    row = plans.find_one({"user_id": int(user_id)}, {"_id": 0, "bot_unlimited_until": 1})
    return _as_utc(row.get("bot_unlimited_until")) if row else None


def activate_bot_pass(user_id: int, hours: int = 24):
    from datetime import timedelta
    user_id = int(user_id)
    now = _utcnow()
    row = plans.find_one({"user_id": user_id}, {"_id": 0, "bot_unlimited_until": 1}) or {}
    current = _as_utc(row.get("bot_unlimited_until"))
    base = current if current and current > now else now
    expires = base + timedelta(hours=max(1, int(hours)))
    plans.update_one(
        {"user_id": user_id},
        {"$set": {"bot_unlimited_until": expires}, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )
    return expires


def activate_api_plan(user_id: int, days: int = 30):
    """Activate/extend API plan and return (raw_key, expires_at).

    A fresh key is issued on each approval. Only its SHA-256 hash is stored.
    """
    import hashlib
    import secrets
    from datetime import timedelta

    user_id = int(user_id)
    now = _utcnow()
    row = plans.find_one({"user_id": user_id}, {"_id": 0, "api_unlimited_until": 1}) or {}
    current = _as_utc(row.get("api_unlimited_until"))
    base = current if current and current > now else now
    expires = base + timedelta(days=max(1, int(days)))

    raw_key = "lk_" + secrets.token_urlsafe(28)
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    prefix = raw_key[:10]

    # Revoke old active keys for this user so accidental sharing can be recovered by renewal.
    api_keys.update_many({"user_id": user_id, "revoked": {"$ne": True}}, {"$set": {"revoked": True}})
    api_keys.insert_one({
        "user_id": user_id,
        "key_hash": key_hash,
        "key_prefix": prefix,
        "created_at": now,
        "expires_at": expires,
        "revoked": False,
        "total_requests": 0,
        "rate_limited_requests": 0,
    })
    plans.update_one(
        {"user_id": user_id},
        {"$set": {"api_unlimited_until": expires, "api_key_prefix": prefix}, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )
    return raw_key, expires


def get_api_plan_until(user_id: int):
    row = plans.find_one({"user_id": int(user_id)}, {"_id": 0, "api_unlimited_until": 1})
    return _as_utc(row.get("api_unlimited_until")) if row else None


def get_api_plan_info(user_id: int) -> dict:
    row = plans.find_one(
        {"user_id": int(user_id)},
        {"_id": 0, "api_unlimited_until": 1, "api_key_prefix": 1},
    ) or {}
    return {
        "expires_at": _as_utc(row.get("api_unlimited_until")),
        "key_prefix": row.get("api_key_prefix", ""),
    }


def rotate_api_key(user_id: int):
    """Rotate an API key without extending the current plan expiry.

    Returns (raw_key, expires_at) when an active API plan exists, otherwise None.
    """
    import hashlib
    import secrets

    user_id = int(user_id)
    now = _utcnow()
    plan = plans.find_one(
        {"user_id": user_id, "api_unlimited_until": {"$gt": now}},
        {"_id": 0, "api_unlimited_until": 1},
    )
    if not plan:
        return None

    expires = _as_utc(plan["api_unlimited_until"])
    raw_key = "lk_" + secrets.token_urlsafe(28)
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    prefix = raw_key[:10]

    api_keys.update_many(
        {"user_id": user_id, "revoked": {"$ne": True}},
        {"$set": {"revoked": True, "revoked_at": now}},
    )
    api_keys.insert_one({
        "user_id": user_id,
        "key_hash": key_hash,
        "key_prefix": prefix,
        "created_at": now,
        "expires_at": expires,
        "revoked": False,
        "total_requests": 0,
        "rate_limited_requests": 0,
    })
    plans.update_one(
        {"user_id": user_id},
        {"$set": {"api_key_prefix": prefix}},
    )
    return raw_key, expires


def validate_api_key(raw_key: str) -> dict | None:
    import hashlib
    if not raw_key:
        return None
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    now = _utcnow()
    row = api_keys.find_one(
        {"key_hash": key_hash, "revoked": {"$ne": True}, "expires_at": {"$gt": now}},
        {"_id": 0},
    )
    return row



def record_api_request(key_row: dict, endpoint: str = "", *, rate_limited: bool = False) -> None:
    """Record API usage without storing the raw API key."""
    from zoneinfo import ZoneInfo

    key_hash = str((key_row or {}).get("key_hash") or "")
    if not key_hash:
        return

    now = _utcnow()
    ist_now = now.astimezone(ZoneInfo("Asia/Kolkata"))
    date_key = ist_now.strftime("%Y-%m-%d")
    inc = {"total_requests": 1}
    daily_inc = {"requests": 1}
    if rate_limited:
        inc["rate_limited_requests"] = 1
        daily_inc["rate_limited"] = 1

    api_keys.update_one(
        {"key_hash": key_hash},
        {
            "$inc": inc,
            "$set": {
                "last_used_at": now,
                "last_endpoint": str(endpoint or "")[:80],
            },
        },
    )
    api_usage_daily.update_one(
        {"key_hash": key_hash, "date": date_key},
        {
            "$inc": daily_inc,
            "$setOnInsert": {
                "key_hash": key_hash,
                "key_prefix": key_row.get("key_prefix", ""),
                "user_id": int(key_row.get("user_id", 0) or 0),
                "date": date_key,
                "created_at": now,
            },
        },
        upsert=True,
    )


def _api_usage_for_hashes(key_hashes: list[str]) -> dict[str, dict]:
    """Return today's and current month's usage using IST calendar boundaries."""
    from zoneinfo import ZoneInfo

    key_hashes = [str(x) for x in key_hashes if x]
    if not key_hashes:
        return {}

    now_ist = _utcnow().astimezone(ZoneInfo("Asia/Kolkata"))
    today = now_ist.strftime("%Y-%m-%d")
    month_prefix = now_ist.strftime("%Y-%m-")
    out = {h: {"today": 0, "month": 0, "today_rate_limited": 0, "month_rate_limited": 0} for h in key_hashes}

    rows = api_usage_daily.find(
        {"key_hash": {"$in": key_hashes}, "date": {"$regex": f"^{month_prefix}"}},
        {"_id": 0, "key_hash": 1, "date": 1, "requests": 1, "rate_limited": 1},
    )
    for row in rows:
        h = row.get("key_hash")
        if h not in out:
            continue
        req = int(row.get("requests", 0) or 0)
        rl = int(row.get("rate_limited", 0) or 0)
        out[h]["month"] += req
        out[h]["month_rate_limited"] += rl
        if row.get("date") == today:
            out[h]["today"] += req
            out[h]["today_rate_limited"] += rl
    return out


def get_api_admin_summary() -> dict:
    """High-level API key and usage statistics for the admin dashboard."""
    from zoneinfo import ZoneInfo

    now = _utcnow()
    active = api_keys.count_documents({"revoked": {"$ne": True}, "expires_at": {"$gt": now}})
    expired = api_keys.count_documents({"revoked": {"$ne": True}, "expires_at": {"$lte": now}})
    revoked = api_keys.count_documents({"revoked": True})
    total_keys = api_keys.count_documents({})
    active_users = len(api_keys.distinct("user_id", {"revoked": {"$ne": True}, "expires_at": {"$gt": now}}))

    now_ist = now.astimezone(ZoneInfo("Asia/Kolkata"))
    today = now_ist.strftime("%Y-%m-%d")
    month_prefix = now_ist.strftime("%Y-%m-")

    today_rows = list(api_usage_daily.aggregate([
        {"$match": {"date": today}},
        {"$group": {"_id": None, "requests": {"$sum": "$requests"}, "rate_limited": {"$sum": "$rate_limited"}}},
    ]))
    month_rows = list(api_usage_daily.aggregate([
        {"$match": {"date": {"$regex": f"^{month_prefix}"}}},
        {"$group": {"_id": None, "requests": {"$sum": "$requests"}, "rate_limited": {"$sum": "$rate_limited"}}},
    ]))
    total_rows = list(api_keys.aggregate([
        {"$group": {"_id": None, "requests": {"$sum": {"$ifNull": ["$total_requests", 0]}}, "rate_limited": {"$sum": {"$ifNull": ["$rate_limited_requests", 0]}}}},
    ]))

    return {
        "total_keys": total_keys,
        "active_keys": active,
        "expired_keys": expired,
        "revoked_keys": revoked,
        "active_users": active_users,
        "requests_today": int(today_rows[0].get("requests", 0)) if today_rows else 0,
        "requests_month": int(month_rows[0].get("requests", 0)) if month_rows else 0,
        "requests_total": int(total_rows[0].get("requests", 0)) if total_rows else 0,
        "rate_limited_today": int(today_rows[0].get("rate_limited", 0)) if today_rows else 0,
    }


def get_api_keys_admin_page(page: int = 0, page_size: int = 6) -> dict:
    page = max(0, int(page))
    page_size = max(1, min(int(page_size), 20))
    total = api_keys.count_documents({})
    max_page = max(0, (total - 1) // page_size) if total else 0
    page = min(page, max_page)
    rows = list(
        api_keys.find({}, {"_id": 0})
        .sort("created_at", -1)
        .skip(page * page_size)
        .limit(page_size)
    )
    usage = _api_usage_for_hashes([r.get("key_hash") for r in rows])
    now = _utcnow()
    for row in rows:
        row["created_at"] = _as_utc(row.get("created_at"))
        row["expires_at"] = _as_utc(row.get("expires_at"))
        row["last_used_at"] = _as_utc(row.get("last_used_at"))
        if row.get("revoked"):
            row["status"] = "revoked"
        elif row.get("expires_at") and row["expires_at"] <= now:
            row["status"] = "expired"
        else:
            row["status"] = "active"
        row["usage"] = usage.get(row.get("key_hash"), {"today": 0, "month": 0, "today_rate_limited": 0, "month_rate_limited": 0})
        # Never return the stored key hash to Telegram handlers/UI.
        row.pop("key_hash", None)
    return {"rows": rows, "page": page, "page_size": page_size, "total": total}


def get_due_plan_notifications(now=None) -> list[dict]:
    """Return due plan notifications. Callers mark each event after successful delivery."""
    from datetime import timedelta

    now = _as_utc(now) or _utcnow()
    rows = plans.find(
        {"$or": [
            {"api_unlimited_until": {"$exists": True}},
            {"bot_unlimited_until": {"$exists": True}},
        ]},
        {"_id": 0},
    )
    events = []
    for row in rows:
        user_id = int(row.get("user_id", 0) or 0)
        if not user_id:
            continue

        api_exp = _as_utc(row.get("api_unlimited_until"))
        if api_exp:
            remaining = api_exp - now
            if remaining.total_seconds() <= 0:
                # Do not send stale expiry messages for plans that ended long before this feature existed.
                if now - api_exp <= timedelta(hours=24) and _as_utc(row.get("api_notice_expired_for")) != api_exp:
                    events.append({"user_id": user_id, "kind": "api_expired", "expires_at": api_exp})
            elif remaining <= timedelta(days=1):
                if _as_utc(row.get("api_notice_1d_for")) != api_exp:
                    events.append({"user_id": user_id, "kind": "api_1d", "expires_at": api_exp})
            elif remaining <= timedelta(days=3):
                if _as_utc(row.get("api_notice_3d_for")) != api_exp:
                    events.append({"user_id": user_id, "kind": "api_3d", "expires_at": api_exp})

        bot_exp = _as_utc(row.get("bot_unlimited_until"))
        if bot_exp and bot_exp <= now:
            if now - bot_exp <= timedelta(hours=6) and _as_utc(row.get("bot_notice_expired_for")) != bot_exp:
                events.append({"user_id": user_id, "kind": "bot_expired", "expires_at": bot_exp})
    return events


def mark_plan_notification(user_id: int, kind: str, expires_at) -> None:
    field_map = {
        "api_3d": "api_notice_3d_for",
        "api_1d": "api_notice_1d_for",
        "api_expired": "api_notice_expired_for",
        "bot_expired": "bot_notice_expired_for",
    }
    field = field_map.get(str(kind))
    if not field:
        return
    plans.update_one(
        {"user_id": int(user_id)},
        {"$set": {field: _as_utc(expires_at), f"{field}_sent_at": _utcnow()}},
    )


def create_plan_order(
    order_id: str,
    user_id: int,
    order_type: str,
    price_amount: int,
    proof: dict | None = None,
    credits_amount: int = 0,
    *,
    payment_method: str = "manual",
    extra: dict | None = None,
) -> dict:
    now = _utcnow()
    proof = dict(proof or {})
    method = str(payment_method or "manual")
    doc = {
        "order_id": str(order_id),
        "user_id": int(user_id),
        "order_type": str(order_type),
        "credits_amount": int(credits_amount),
        "price_amount": int(price_amount),
        "proof": proof,
        "payment_method": method,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    if method == "manual" and proof.get("type") == "text":
        txn_hash = _manual_txn_hash(proof.get("value", ""))
        if txn_hash:
            doc["manual_txn_hash"] = txn_hash
    if extra:
        for key, value in dict(extra).items():
            if key not in {"_id", "order_id", "user_id", "status", "created_at"}:
                doc[key] = value
    credit_orders.insert_one(doc)
    return doc


def update_razorpay_order(order_id: str, *, payment_link_id: str = "", payment_url: str = "", payment_status: str = "created", expire_by=None, enforce_reference: bool = False) -> None:
    """Store payment link metadata before sending its URL to the buyer."""
    update = {
        "razorpay_payment_link_id": str(payment_link_id or ""),
        "payment_url": str(payment_url or ""),
        "payment_status": str(payment_status or "created"),
        "razorpay_link_reference_enforced": bool(enforce_reference),
        "updated_at": _utcnow(),
    }
    if expire_by is not None:
        update["razorpay_link_expire_by"] = int(expire_by)
    credit_orders.update_one(
        {"order_id": str(order_id), "status": "pending"},
        {"$set": update},
    )


def update_razorpay_qr_order(order_id: str, *, qr_id: str, image_url: str, payment_status: str = "active", close_by=None) -> None:
    update = {
        "razorpay_qr_id": str(qr_id or ""),
        "razorpay_qr_image_url": str(image_url or ""),
        "payment_status": str(payment_status or "active"),
        "updated_at": _utcnow(),
    }
    if close_by is not None:
        update["razorpay_qr_close_by"] = close_by
    credit_orders.update_one({"order_id": str(order_id)}, {"$set": update})


def update_razorpay_payment_status(order_id: str, payment_status: str, payload: dict | None = None, *, payment_id: str = "", rrn: str = "") -> bool:
    payment_status = str(payment_status or "")
    update = {
        "payment_status": payment_status,
        "payment_checked_at": _utcnow(),
    }
    # Never reset the processing timestamp on a status poll. Otherwise, a
    # crashed activation cannot become stale and be safely retried.
    if payment_status == "paid":
        update["updated_at"] = _utcnow()
    if payment_id:
        update["razorpay_payment_id"] = str(payment_id)
    if rrn:
        update["razorpay_rrn"] = str(rrn)
    query = {"order_id": str(order_id)}
    # A slow concurrent poll must not overwrite an already-approved payment.
    if payment_status != "paid":
        query["status"] = {"$in": ["pending", "processing"]}
    try:
        result = credit_orders.update_one(query, {"$set": update})
        return bool(result.matched_count)
    except DuplicateKeyError:
        return False


def record_verified_razorpay_payment(order_id: str, *, qr_id: str, payment_id: str, rrn: str = "") -> bool:
    """Atomically bind a verified payment to ONE matching Razorpay order.

    The unique razorpay_payment_id index blocks payment reuse across orders.
    Pending/processing status prevents reviving unrelated rejected/failed orders.
    """
    if not payment_id or not qr_id:
        return False
    now = _utcnow()
    try:
        result = credit_orders.update_one(
            {
                "order_id": str(order_id),
                "payment_method": "razorpay",
                "razorpay_qr_id": str(qr_id),
                "status": {"$in": ["pending", "processing"]},
                "$or": [
                    {"razorpay_payment_id": {"$exists": False}},
                    {"razorpay_payment_id": str(payment_id)},
                ],
            },
            {"$set": {
                "razorpay_payment_id": str(payment_id),
                "razorpay_rrn": str(rrn or ""),
                "payment_status": "paid",
                "payment_verified_at": now,
                "payment_checked_at": now,
            }},
        )
        return bool(result.matched_count)
    except DuplicateKeyError:
        return False


def record_verified_razorpay_link_payment(order_id: str, *, link_id: str, payment_id: str, rrn: str = "") -> bool:
    """Links require an authenticated, captured, uniquely reserved payment."""
    if not link_id or not payment_id:
        return False
    now = _utcnow()
    try:
        result = credit_orders.update_one(
            {
                "order_id": str(order_id),
                "payment_method": "razorpay",
                "razorpay_payment_link_id": str(link_id),
                "status": {"$in": ["pending", "processing"]},
                "$or": [
                    {"razorpay_payment_id": {"$exists": False}},
                    {"razorpay_payment_id": str(payment_id)},
                ],
            },
            {"$set": {
                "razorpay_payment_id": str(payment_id),
                "razorpay_rrn": str(rrn or ""),
                "payment_status": "paid",
                "payment_verified_at": now,
                "payment_checked_at": now,
            }},
        )
        return bool(result.matched_count)
    except DuplicateKeyError:
        return False


def get_pending_razorpay_orders(limit: int = 50) -> list[dict]:
    # Do not silently abandon a paid order after a 24-hour bot outage.
    # Re-check least-recently-checked orders first to prevent backlog starvation.
    limit = max(1, min(int(limit), 200))
    return list(
        credit_orders.find(
            {
                "payment_method": "razorpay",
                "status": {"$in": ["pending", "processing"]},
                "$or": [
                    {"razorpay_qr_id": {"$exists": True, "$ne": ""}},
                    {"razorpay_payment_link_id": {"$exists": True, "$ne": ""}},
                ],
            },
            {"_id": 0},
        )
        .sort([("payment_checked_at", 1), ("created_at", 1)])
        .limit(limit)
    )


def mark_order_payment_failed(order_id: str, reason: str = "") -> None:
    credit_orders.update_one(
        {"order_id": str(order_id), "status": "pending"},
        {"$set": {
            "status": "payment_failed",
            "payment_status": "failed",
            "payment_error": str(reason or "")[:500],
            "updated_at": _utcnow(),
        }},
    )
