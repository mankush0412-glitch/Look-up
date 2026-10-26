import asyncio
import logging
import os
import threading
import socket

import uvicorn

from telegram.ext import Application

import database
from api_client import APIClient
from config import load_config
from handlers.admin import register_admin_handlers
from handlers.settings import register_settings_handlers
from handlers.join_requests import register_join_request_handlers
from handlers.broadcast import register_broadcast_handlers
from handlers.reward import register_reward_handlers
from handlers.lookup import register_lookup_handlers
from handlers.start import register_start_handlers
from handlers.buy_credits import register_buy_credit_handlers
from tunnel_manager import start_auto_tunnel, start_named_tunnel
from notifications import plan_notification_worker
from razorpay_payments import razorpay_payment_worker


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


async def _primary_lease_heartbeat(application):
    owner_id = application.bot_data.get("instance_id", "bot")
    while True:
        try:
            ok = database.acquire_worker_lease(
                "telegram-polling-primary", owner_id, ttl_seconds=45
            )
            if not ok:
                logger.error("Lost primary bot lease; stopping this instance to avoid polling conflicts.")
                await application.stop()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Primary bot lease heartbeat failed")
        await asyncio.sleep(15)


async def post_init(application):
    await application.bot_data["api_client"].start()
    owner_id = application.bot_data.get("instance_id", "bot")
    application.bot_data["primary_lease_task"] = asyncio.create_task(
        _primary_lease_heartbeat(application),
        name="primary-bot-lease-heartbeat",
    )
    application.bot_data["plan_notification_task"] = asyncio.create_task(
        plan_notification_worker(application, database, owner_id=owner_id),
        name="plan-notification-worker",
    )
    application.bot_data["razorpay_payment_task"] = asyncio.create_task(
        razorpay_payment_worker(application, application.bot_data["config"], database, owner_id=owner_id),
        name="razorpay-payment-worker",
    )


async def post_shutdown(application):
    for task_name in ("plan_notification_task", "razorpay_payment_task", "primary_lease_task"):
        task = application.bot_data.get(task_name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    owner_id = application.bot_data.get("instance_id", "bot")
    try:
        database.release_worker_lease("telegram-polling-primary", owner_id)
    except Exception:
        logger.exception("Could not release primary bot lease")
    await application.bot_data["api_client"].close()


def _start_public_api(config):
    if not getattr(config, "public_api_enabled", True):
        return
    port = int(os.getenv("API_PORT", "8000"))
    thread = threading.Thread(
        target=lambda: uvicorn.run("public_api:app", host="0.0.0.0", port=port, log_level="info"),
        daemon=True,
        name="public-api",
    )
    thread.start()


def main():
    config = load_config()
    database.init_db()

    # Acquire the primary lease before opening the API/tunnel. This prevents a
    # second server from briefly exposing a healthy API while its Telegram
    # polling process is about to exit due to a duplicate instance.
    instance_id = f"{socket.gethostname()}:{os.getpid()}"
    if not database.acquire_worker_lease(
        "telegram-polling-primary", instance_id, ttl_seconds=45
    ):
        raise RuntimeError(
            "Another bot instance is already active. Stop the old server or wait up to 45 seconds for its lease to expire."
        )

    _start_public_api(config)

    # start.sh manages the production named tunnel by default.
    # Internal tunnel management only runs when explicitly enabled.
    if getattr(config, "auto_cloudflare_tunnel", False):
        tunnel_token = getattr(config, "cloudflare_tunnel_token", "").strip()
        if tunnel_token:
            start_named_tunnel(tunnel_token, config.public_api_url)
        else:
            port = int(os.getenv("API_PORT", "8000"))
            start_auto_tunnel(port)

    api_client = APIClient(config.api_url, config.api_key)

    app = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.bot_data["api_client"] = api_client
    app.bot_data["config"] = config
    app.bot_data["instance_id"] = instance_id

    register_start_handlers(app, config, database)
    register_buy_credit_handlers(app, config, database)
    register_lookup_handlers(app, config, database, api_client)
    register_admin_handlers(app, config, database)
    register_settings_handlers(app, config, database)
    register_join_request_handlers(app, config, database)
    register_broadcast_handlers(app, config, database)
    register_reward_handlers(app, config, database)

    async def _error_handler(update, context):
        logger.error("Unhandled Telegram update error", exc_info=(type(context.error), context.error, context.error.__traceback__))

    app.add_error_handler(_error_handler)

    logger.info("Bot starting...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[
            "message",
            "callback_query",
            "chat_join_request",
        ],
    )


if __name__ == "__main__":
    main()