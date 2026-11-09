"""Discovery cards use server-issued tokens; callbacks cannot choose arbitrary users."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

from i18n import tr


class BrowseFlow:
    def __init__(self, service):
        self.s = service
        self.db = service.db

    async def show(self, uid, mode="feed"):
        user = self.db.user(uid)
        lang = user["lang"]
        if not user["active"]:
            await self.s.send(uid, tr(lang, "paused"))
            await self.s.menu(uid)
            return
        result = self.db.prepare_view(uid, mode, self.s.settings.skip_days)
        if not result:
            await self.s.send(
                uid, tr(lang, "empty_feed" if mode == "feed" else "empty_likes", days=self.s.settings.skip_days)
            )
            await self.s.menu(uid)
            return
        profile, token = result

        def b(key, action):
            return InlineKeyboardButton(tr(lang, key), callback_data=f"r:{token}:{action}")

        keyboard = InlineKeyboardMarkup(
            [
                [b("like", "like"), b("pass", "skip")],
                [b("block", "block"), b("report", "report")],
                [InlineKeyboardButton(tr(lang, "home"), callback_data="nav:menu")],
            ]
        )
        try:
            await self.s.card(uid, profile, keyboard)
        except (BadRequest, Forbidden, RetryAfter):
            # Telegram explicitly rejected the request carrying the keyboard.
            self.db.abort_view(uid, token, may_have_arrived=False)
            raise
        except BaseException:
            # A timeout or cancellation can hide an accepted send. Keep the
            # token useful only for block/report and leave exposure unchanged.
            self.db.abort_view(uid, token, may_have_arrived=True)
            raise
        else:
            self.db.confirm_view(uid, token)

    async def react(self, uid, token, action, query):
        lang = self.db.user(uid)["lang"]
        if action == "report":
            if not self.db.view(uid, token):
                await self.s.send(uid, tr(lang, "stale"))
                return
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(tr(lang, key), callback_data=f"r:{token}:report_{key}")]
                    for key in ("spam", "underage", "abuse", "other")
                ]
            )
            await self.s.send(uid, tr(lang, "report_prompt"), reply_markup=keyboard)
            return
        reason = "other"
        if action.startswith("report_"):
            reason = action.removeprefix("report_")
            if reason not in {"spam", "underage", "abuse", "other"}:
                return
            action = "report"
        if action not in {"like", "skip", "block", "report"}:
            return
        result = self.db.react(uid, token, action, reason)
        if not result:
            await self.s.send(uid, tr(lang, "stale"))
            return
        try:
            await self.s.remove_buttons(query)
            if action in {"block", "report"}:
                await self.s.send(uid, tr(lang, "reported" if action == "report" else "blocked"))
            elif action == "like" and not result["matched"]:
                await self.s.send(uid, tr(lang, "liked"))
            await self.show(uid, result["mode"])
        finally:
            # The reaction and outbox are already committed. Let this member see
            # the next card before waking the sender; unrelated chats may be slow.
            # A failed UI request must still leave background delivery scheduled.
            self.s.schedule_notifications()
