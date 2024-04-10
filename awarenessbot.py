from typing import Type, Tuple

from maubot import Plugin, MessageEvent
from maubot.handlers import command, event
from mautrix.client import MembershipEventDispatcher
from mautrix.errors import MForbidden, MatrixRequestError, IntentError
from mautrix.types import (EventType,
                           TextMessageEventContent, MessageType,
                           Format, RelatesTo, RelationType)
from mautrix.util.async_db import UpgradeTable, Connection
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

upgrade_table = UpgradeTable()


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("keywords")
        helper.copy("notification_room")
        helper.copy("message_warning")
        helper.copy("message_mute")
        helper.copy("message_report")
        helper.copy("message_notify")


@upgrade_table.register(description="Initial revision")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE data (
            user   TEXT PRIMARY KEY,
            warnings INT NOT NULL
        )"""
    )


class Awareness(Plugin):
    # react to regular messages and to reactions
    allowed_msgtypes: Tuple[MessageType, ...] = (MessageType.TEXT, MessageType.EMOTE)

    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()
        self.client.add_dispatcher(MembershipEventDispatcher)

    async def get_warning_count(self, sender: str) -> int:
        q = "SELECT warnings FROM data WHERE LOWER(user)=LOWER($1)"
        warnings = await self.database.fetchval(q, sender)
        self.log.log(10, "Warning count for user: " + sender + " is: " + str(warnings))
        if warnings:
            return int(warnings)
        else:
            return 0

    async def prettify_usernames(self, content: str, user: str, event_id, reporter=None) -> TextMessageEventContent:
        if reporter is not None:
            content = content.replace("[reporter]", f"<a href='https://matrix.to/#/{reporter}'>{reporter}</a>")
        return TextMessageEventContent(
            msgtype=MessageType.TEXT, format=Format.HTML,
            body=content,
            formatted_body=content.replace("[user]", f"<a href='https://matrix.to/#/{user}'>{user}</a>"),
            #relates_to=RelatesTo(
            #    rel_type=RelationType("org.besendorf.awarenessbot"),
            #    event_id=event_id,
            )

    async def set_warning_count(self, sender: str, warnings: str) -> None:
        q = """
            INSERT INTO data (user, warnings) VALUES ($1, $2)
            ON CONFLICT (user) DO UPDATE SET warnings=excluded.warnings
        """
        self.log.log(10, "set warning counter for " + sender + " to " + str(warnings))
        await self.database.execute(q, sender, warnings)

    async def mute(self, evt: MessageEvent) -> None:
        user_id = evt.sender
        level = -1
        try:
            levels = await self.client.get_state_event(evt.room_id, EventType.ROOM_POWER_LEVELS)
            levels.users[user_id] = level
            await self.client.send_state_event(evt.room_id, EventType.ROOM_POWER_LEVELS, levels)
        except MForbidden as e:
            await self.log.exception(f"I don't seem to have permission to update power levels: {e.message}")
        except (MatrixRequestError, IntentError):
            self.log.exception("Failed to update power levels")
            return await evt.reply("Failed to update power levels (see logs for more details)")

    @event.on(EventType.ROOM_MESSAGE)
    async def event_handler(self, evt: MessageEvent) -> None:
        if evt.sender == self.client.mxid or evt.content.msgtype not in self.allowed_msgtypes:
            self.log.log(10, "message ignored", )
            return
        # report via emoji reaction
        if evt.type == EventType.REACTION:
            if evt.content.relates_to.key == '🚨':
                self.report(evt)
        self.log.log(10, self.config["keywords"])
        for keyword in self.config["keywords"]:
            if keyword.lower() in evt.content.body.lower():
                self.log.log(10, "keyword found")
                warnings = await self.get_warning_count(evt.sender)
                warnings = warnings % 3
                if warnings > 1:
                    content = self.config["message_mute"]
                    await self.mute(evt)
                else:
                    content = self.config["message_warning"]
                warnings += 1
                content = await self.prettify_usernames(content.replace("[keyword]", keyword).replace("[count]", str(warnings)), evt.sender, evt.event_id)
                await self.set_warning_count(evt.sender, warnings)
                self.log.log(10, "send reply")
                await evt.reply(content)
            else:
                self.log.log(10, "no " + keyword + " in message " + evt.content.body)

    @command.new(name="report")
    async def report(self, evt: MessageEvent) -> None:
        reply_to = evt.content.get_reply_to()
        if reply_to is not None:
            self.log.log(10, "reply_to: " + reply_to)
            reported_msg = await self.client.get_event(evt.room_id, reply_to)

            # mute reported user
            await self.mute(reported_msg)

            # notify room about report
            reporter = evt.sender
            user = reported_msg.sender
            content = await self.prettify_usernames(self.config["message_report"], user, reply_to, reporter)
            await evt.reply(content)

            # notify moderators
            if self.config["notification_room"]:
                content = self.config["message_notify"].replace("[room]", evt.room_id)
                content = await self.prettify_usernames(content, user, reply_to, reporter)
                await self.client.send_message(self.config["notification_room"], content)
                # send original message
                quote_body = reported_msg.content.body
                quote_content = TextMessageEventContent(
                    msgtype=MessageType.TEXT, format=Format.HTML,
                    body="> " + quote_body,
                    formatted_body="<blockquote>\n<p>" + quote_body + "</p>\n</blockquote>\n",
                )
                await self.client.send_message(self.config["notification_room"], quote_content)

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table
