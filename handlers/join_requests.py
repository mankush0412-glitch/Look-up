import logging

from telegram import Update
from telegram.ext import ChatJoinRequestHandler, ContextTypes

logger = logging.getLogger(__name__)


def register_join_request_handlers(application, config, db_module):

    async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
        req = update.chat_join_request
        if not req:
            return

        user = req.from_user
        chat = req.chat
        user_id = user.id
        chat_id = chat.id

        logger.info("Join request received: user=%s chat=%s", user_id, chat_id)

        # Do NOT auto-approve — only record for the 24h pass.
        try:
            db_module.record_join_request(user_id, chat_id)
        except Exception:
            logger.exception("Failed to record join request")

        # Revoke the one-time invite link (one request per link).
        try:
            link = db_module.pop_pending_join_link(user_id, chat_id)
            if link:
                await context.bot.revoke_chat_invite_link(
                    chat_id=chat_id, invite_link=link
                )
        except Exception as exc:
            logger.warning(
                "Revoke failed user=%s chat=%s: %s",
                user_id, chat_id, type(exc).__name__,
            )

    application.add_handler(ChatJoinRequestHandler(handle_join_request))