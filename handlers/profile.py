"""A durable profile/settings wizard. Draft edits never overwrite a live profile."""

import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TelegramError

from db.models import (
    GENDERS,
    INTERESTS,
    MAX_INTERESTS,
    PREFERENCES,
    PURPOSES,
    age_range,
    clean_text,
    read_interests,
    read_purpose,
)
from i18n import tr

STEPS = ["consent", "name", "age", "gender", "seeking", "city", "purpose", "interests", "bio", "photo", "preview"]
SETTINGS_STEPS = {"range", "location", "delete_confirm"}


class ProfileFlow:
    def __init__(self, service):
        self.s = service
        self.db = service.db

    async def begin(self, uid, editing=False):
        session = self.db.session(uid)
        if session:
            await self.prompt(uid, *session)
            return
        user = self.db.user(uid)
        data = (
            {
                key: user[key]
                for key in (
                    "name",
                    "age",
                    "gender",
                    "seeking",
                    "city",
                    "purpose",
                    "interests",
                    "bio",
                    "photo",
                    "consent",
                )
            }
            if editing and user["photo"]
            else {}
        )
        data["editing"] = bool(data)
        await self.prompt(uid, "name" if data["editing"] else "consent", data)

    @staticmethod
    def _profile_draft(session):
        if session is None:
            return None
        step, data = session
        draft = session if step in STEPS else data.get("return_to") if step in SETTINGS_STEPS else None
        if (
            not isinstance(draft, (list, tuple))
            or len(draft) != 2
            or draft[0] not in STEPS
            or not isinstance(draft[1], dict)
        ):
            return None
        # A settings flow can suspend only the profile draft, never another
        # settings flow or an old confirmation to delete the account.
        return draft[0], {key: value for key, value in draft[1].items() if key != "return_to"}

    async def open_settings(self, uid, step):
        if step not in {"range", "delete_confirm"}:
            raise ValueError("Unknown settings flow")
        session = self.db.session(uid)
        draft = None if self.db.user(uid)["banned"] else self._profile_draft(session)
        if session and (session[0] == step or (step == "range" and session[0] == "location")):
            step, data = session
            data.pop("return_to", None)
        else:
            data = {}
        if draft is not None:
            data["return_to"] = draft
        await self.prompt(uid, step, data)

    def leave_settings(self, uid):
        """Revoke settings buttons on navigation while keeping a profile draft."""
        session = self.db.session(uid)
        if not session or session[0] not in SETTINGS_STEPS:
            return
        draft = None if self.db.user(uid)["banned"] else self._profile_draft(session)
        if draft is None:
            self.db.clear_session(uid)
            return
        step, data = draft
        # Navigation does not render a wizard prompt, so old draft buttons must
        # stay stale until the user explicitly resumes with /start or /edit.
        data["nonce"] = secrets.token_hex(5)
        self.db.save_session(uid, step, data)

    async def _restore_profile(self, uid, session, notice):
        if self.db.user(uid)["banned"]:
            return False
        draft = self._profile_draft(session)
        if draft is None:
            return False
        await self.prompt(uid, *draft, notice=notice)
        return True

    async def cancel(self, uid):
        session = self.db.session(uid)
        if session and session[0] in SETTINGS_STEPS and await self._restore_profile(uid, session, "draft_resumed"):
            return
        user = self.db.user(uid)
        self.db.clear_session(uid)
        await self.s.send(uid, tr(user["lang"], "cancelled"))
        if not user["banned"]:
            await self.s.menu(uid)

    async def prompt(self, uid, step, data, *, edit_query=None, notice=None):
        lang = self.db.user(uid)["lang"]
        # Defaults let existing registration/edit drafts resume across the upgrade.
        data["purpose"] = read_purpose(data.get("purpose"))
        data["interests"] = read_interests(data.get("interests"))
        data["nonce"] = secrets.token_hex(5)
        self.db.save_session(uid, step, data)

        def button(key, value):
            return InlineKeyboardButton(tr(lang, key), callback_data=f"w:{data['nonce']}:{value}")

        rows = []
        if step == "consent":
            rows = [[button("agree", "agree")]]
        elif step in {"gender", "seeking"}:
            rows = [[button(key, key)] for key in ("male", "female", "other")]
            if step == "seeking":
                rows.append([button("any", "any")])
        elif step == "purpose":
            rows = [[button("purpose_" + key, key)] for key in ("relationship", "friendship", "chat", "any")]
        elif step == "interests":
            buttons = [
                InlineKeyboardButton(
                    ("✅ " if key in data["interests"] else "") + tr(lang, "interest_" + key),
                    callback_data=f"w:{data['nonce']}:interest_{key}",
                )
                for key in INTERESTS
            ]
            rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
            rows.append([button("done", "done"), button("skip", "skip")])
        elif step == "bio":
            rows = [[button("skip", "skip")]]
        elif step == "preview":
            await self.s.card(uid, data)
            rows = [[button("publish", "publish")]]
        elif step == "location":
            rows = [[button("local", "local"), button("everywhere", "everywhere")]]
        elif step == "delete_confirm":
            rows = [[button("delete_yes", "delete")]]
        if data.get("editing") and step in STEPS[1:-1]:
            rows.append([button("keep", "keep")])
        rows.append([button("cancel", "cancel")])
        text = tr(lang, step)
        if step == "interests":
            selected = ", ".join(tr(lang, "interest_" + key) for key in data["interests"]) or tr(lang, "interests_none")
            text += "\n\n" + tr(lang, "interests_selected", interests=selected)
        if notice is not None:
            text = tr(lang, notice) + "\n\n" + text
        markup = InlineKeyboardMarkup(rows)
        if edit_query is not None:
            try:
                await edit_query.edit_message_text(text=text, reply_markup=markup)
            except (Forbidden, RetryAfter):
                raise  # Sending another message won't bypass a block or Telegram's retry delay.
            except (TelegramError, TypeError):
                pass  # The old prompt may no longer exist or be editable.
            else:
                return
        await self.s.send(uid, text, reply_markup=markup)

    async def accept(self, uid, message=None, nonce=None, choice=None, *, query=None):
        session = self.db.session(uid)
        lang = self.db.user(uid)["lang"]
        is_button = choice is not None or nonce is not None
        if not session or (is_button and (not nonce or nonce != session[1].get("nonce"))):
            await self.s.send(uid, tr(lang, "draft_stale"))
            return
        step, data = session
        if choice == "cancel":
            await self.cancel(uid)
            return
        # A callback can arrive for a draft saved by an earlier bot version,
        # without first passing through prompt() after the deployment.
        data["purpose"] = read_purpose(data.get("purpose"))
        data["interests"] = read_interests(data.get("interests"))
        text = message.text.strip() if message and message.text else ""
        try:
            if step == "delete_confirm":
                if choice != "delete":
                    raise ValueError()
                self.db.delete_user(uid)
                await self.s.send(uid, tr(lang, "deleted"))
                return
            if step == "range":
                data["low"], data["high"] = age_range(text)
                await self.prompt(uid, "location", data)
                return
            if step == "location":
                if choice not in {"local", "everywhere"}:
                    raise ValueError()
                self.db.set_filters(uid, data["low"], data["high"], choice == "local")
                if await self._restore_profile(uid, session, "filters_saved"):
                    return
                self.db.clear_session(uid)
                await self.s.send(uid, tr(lang, "filters_saved"))
                await self.s.menu(uid)
                return
            if choice == "keep" and data.get("editing") and step in STEPS[1:-1]:
                if step in {"purpose", "interests"}:
                    # In multi-select, Keep means the saved value, even after toggles.
                    data[step] = self.db.user(uid)[step]
            elif step == "consent" and choice == "agree":
                data["consent"] = 1
            elif step == "name":
                data["name"] = clean_text(text, 1, 40)
            elif step == "age":
                data["age"] = int(text)
                if not 18 <= data["age"] <= 100:
                    raise ValueError()
            elif step == "gender" and choice in GENDERS:
                data["gender"] = choice
            elif step == "seeking" and choice in PREFERENCES:
                data["seeking"] = choice
            elif step == "city":
                data["city"] = clean_text(text, 2, 60)
            elif step == "purpose" and choice in PURPOSES:
                data["purpose"] = choice
            elif step == "interests":
                selected = read_interests(data.get("interests"))
                if choice in {"done", "skip"}:
                    data["interests"] = [] if choice == "skip" else selected
                elif choice and choice.startswith("interest_"):
                    key = choice.removeprefix("interest_")
                    if key not in INTERESTS:
                        raise ValueError()
                    notice = None
                    if key in selected:
                        selected.remove(key)
                    elif len(selected) < MAX_INTERESTS:
                        selected.append(key)
                    else:
                        if query is None:
                            await self.s.send(uid, tr(lang, "interests_limit"))
                            return
                        notice = "interests_limit"
                    data["interests"] = selected
                    await self.prompt(uid, "interests", data, edit_query=query, notice=notice)
                    return
                else:
                    raise ValueError()
            elif step == "bio":
                if not text and choice != "skip":
                    raise ValueError()
                data["bio"] = "" if choice == "skip" else clean_text(text, 1, 500)
            elif step == "photo" and message and message.photo:
                data["photo"] = message.photo[-1].file_id
            elif step == "preview" and choice == "publish":
                self.db.save_profile(uid, data)
                await self.s.send(uid, tr(lang, "saved"))
                await self.s.menu(uid)
                return
            else:
                raise ValueError()
        except (ValueError, TypeError):
            await self.s.send(uid, tr(lang, "age_invalid" if step == "age" else "invalid"))
            return
        await self.prompt(uid, STEPS[STEPS.index(step) + 1], data)
