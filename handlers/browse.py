"""Discovery cards use server-issued tokens; callbacks cannot choose arbitrary users."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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
        result = self.db.present(uid, mode, self.s.settings.skip_days)
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
        await self.s.card(uid, profile, keyboard)

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
        await self.s.remove_buttons(query)
        if action in {"block", "report"}:
            await self.s.send(uid, tr(lang, "reported" if action == "report" else "blocked"))
        elif action == "like" and not result["matched"]:
            await self.s.send(uid, tr(lang, "liked"))
        await self.s.notifications()
        await self.show(uid, result["mode"])
