import asyncio
import logging
import typing as t
from contextlib import suppress
from datetime import datetime, timedelta

import discord
from discord.ext import tasks
from discord.ext.commands import Cog, Context, command, group
from discord.utils import snowflake_time

from bot import constants
from bot.bot import Bot
from bot.cogs.moderation import ModLog
from bot.decorators import in_whitelist, with_role, without_role
from bot.utils.checks import InWhitelistCheckFailure, without_role_check
from bot.utils.redis_cache import RedisCache

log = logging.getLogger(__name__)

UNVERIFIED_AFTER = 3  # Amount of days after which non-Developers receive the @Unverified role
KICKED_AFTER = 30  # Amount of days after which non-Developers get kicked from the guild

# Number in range [0, 1] determining the percentage of unverified users that are safe
# to be kicked from the guild in one batch, any larger amount will require staff confirmation,
# set this to 0 to require explicit approval for batches of any size
KICK_CONFIRMATION_THRESHOLD = 0.01  # 1%

BOT_MESSAGE_DELETE_DELAY = 10

# Sent via DMs once user joins the guild
ON_JOIN_MESSAGE = f"""
Hello! Welcome to Python Discord!

As a new user, you have read-only access to a few select channels to give you a taste of what our server is like.

In order to see the rest of the channels and to send messages, you first have to accept our rules. To do so, \
please visit <#{constants.Channels.verification}>. Thank you!
"""

# Sent via DMs once user verifies
VERIFIED_MESSAGE = f"""
Thanks for verifying yourself!

For your records, these are the documents you accepted:

`1)` Our rules, here: <https://pythondiscord.com/pages/rules>
`2)` Our privacy policy, here: <https://pythondiscord.com/pages/privacy> - you can find information on how to have \
your information removed here as well.

Feel free to review them at any point!

Additionally, if you'd like to receive notifications for the announcements \
we post in <#{constants.Channels.announcements}>
from time to time, you can send `!subscribe` to <#{constants.Channels.bot_commands}> at any time \
to assign yourself the **Announcements** role. We'll mention this role every time we make an announcement.

If you'd like to unsubscribe from the announcement notifications, simply send `!unsubscribe` to \
<#{constants.Channels.bot_commands}>.
"""

# Sent via DMs to users kicked for failing to verify
KICKED_MESSAGE = f"""
Hi! You have been automatically kicked from Python Discord as you have failed to accept our rules \
within `{KICKED_AFTER}` days. If this was an accident, please feel free to join again.
"""

# Sent periodically in the verification channel
REMINDER_MESSAGE = f"""
<@&{constants.Roles.unverified}>

Welcome to Python Discord! Please read the documents mentioned above and type `!accept` to gain permissions \
to send messages in the community!

You will be kicked if you don't verify within `{KICKED_AFTER}` days.
""".strip()

REMINDER_FREQUENCY = 28  # Hours to wait between sending `REMINDER_MESSAGE`

MENTION_CORE_DEVS = discord.AllowedMentions(
    everyone=False, roles=[discord.Object(constants.Roles.core_developers)]
)
MENTION_UNVERIFIED = discord.AllowedMentions(
    everyone=False, roles=[discord.Object(constants.Roles.unverified)]
)


def is_verified(member: discord.Member) -> bool:
    """
    Check whether `member` is considered verified.

    Members are considered verified if they have at least 1 role other than
    the default role (@everyone) and the @Unverified role.
    """
    unverified_roles = {
        member.guild.get_role(constants.Roles.unverified),
        member.guild.default_role,
    }
    return len(set(member.roles) - unverified_roles) > 0


class Verification(Cog):
    """
    User verification and role management.

    There are two internal tasks in this cog:

        * `update_unverified_members`
            * Unverified members are given the @Unverified role after `UNVERIFIED_AFTER` days
            * Unverified members are kicked after `UNVERIFIED_AFTER` days

        * `ping_unverified`
            * Periodically ping the @Unverified role in the verification channel

    Statistics are collected in the 'verification.' namespace.

    Moderators+ can use the `verification` command group to start or stop both internal
    tasks, if necessary. Settings are persisted in Redis across sessions.

    Additionally, this cog offers the !accept, !subscribe and !unsubscribe commands,
    and keeps the verification channel clean by deleting messages.
    """

    # Persist task settings & last sent `REMINDER_MESSAGE` id
    # RedisCache[
    #   "tasks_running": int (0 or 1),
    #   "last_reminder": int (discord.Message.id),
    # ]
    task_cache = RedisCache()

    def __init__(self, bot: Bot) -> None:
        """Start internal tasks."""
        self.bot = bot
        self.bot.loop.create_task(self.maybe_start_tasks())

    def cog_unload(self) -> None:
        """
        Cancel internal tasks.

        This is necessary, as tasks are not automatically cancelled on cog unload.
        """
        self.update_unverified_members.cancel()
        self.ping_unverified.cancel()

    @property
    def mod_log(self) -> ModLog:
        """Get currently loaded ModLog cog instance."""
        return self.bot.get_cog("ModLog")

    async def maybe_start_tasks(self) -> None:
        """
        Poll Redis to check whether internal tasks should start.

        Redis must be interfaced with from an async function.
        """
        log.trace("Checking whether background tasks should begin")
        setting: t.Optional[int] = await self.task_cache.get("tasks_running")  # This can be None if never set

        if setting:
            log.trace("Background tasks will be started")
            self.update_unverified_members.start()
            self.ping_unverified.start()

    # region: automatically update unverified users

    async def _verify_kick(self, n_members: int) -> bool:
        """
        Determine whether `n_members` is a reasonable amount of members to kick.

        First, `n_members` is checked against the size of the PyDis guild. If `n_members` are
        more than `KICK_CONFIRMATION_THRESHOLD` of the guild, the operation must be confirmed
        by staff in #core-dev. Otherwise, the operation is seen as safe.
        """
        log.debug(f"Checking whether {n_members} members are safe to kick")

        await self.bot.wait_until_guild_available()  # Ensure cache is populated before we grab the guild
        pydis = self.bot.get_guild(constants.Guild.id)

        percentage = n_members / len(pydis.members)
        if percentage < KICK_CONFIRMATION_THRESHOLD:
            log.debug(f"Kicking {percentage:.2%} of the guild's population is seen as safe")
            return True

        # Since `n_members` is a suspiciously large number, we will ask for confirmation
        log.debug("Amount of users is too large, requesting staff confirmation")

        core_devs = pydis.get_channel(constants.Channels.dev_core)
        confirmation_msg = await core_devs.send(
            f"<@&{constants.Roles.core_developers}> Verification determined that `{n_members}` members should "
            f"be kicked as they haven't verified in `{KICKED_AFTER}` days. This is `{percentage:.2%}` of the "
            f"guild's population. Proceed?",
            allowed_mentions=MENTION_CORE_DEVS,
        )

        options = (constants.Emojis.incident_actioned, constants.Emojis.incident_unactioned)
        for option in options:
            await confirmation_msg.add_reaction(option)

        core_dev_ids = [member.id for member in pydis.get_role(constants.Roles.core_developers).members]

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            """Check whether `reaction` is a valid reaction to `confirmation_msg`."""
            return (
                reaction.message.id == confirmation_msg.id  # Reacted to `confirmation_msg`
                and str(reaction.emoji) in options  # With one of `options`
                and user.id in core_dev_ids  # By a core developer
            )

        timeout = 60 * 5  # Seconds, i.e. 5 minutes
        try:
            choice, _ = await self.bot.wait_for("reaction_add", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            log.debug("Staff prompt not answered, aborting operation")
            return False
        finally:
            with suppress(discord.HTTPException):
                await confirmation_msg.clear_reactions()

        result = str(choice) == constants.Emojis.incident_actioned
        log.debug(f"Received answer: {choice}, result: {result}")

        # Edit the prompt message to reflect the final choice
        if result is True:
            result_msg = f":ok_hand: Request to kick `{n_members}` members was authorized!"
        else:
            result_msg = f":warning: Request to kick `{n_members}` members was denied!"

        with suppress(discord.HTTPException):
            await confirmation_msg.edit(content=result_msg)

        return result

    async def _send_requests(self, coroutines: t.Collection[t.Coroutine]) -> int:
        """
        Execute `coroutines` and log bad statuses, if any.

        The amount of successful requests is returned. If no requests fail, this number will
        be equal to the length of `coroutines`.
        """
        log.info(f"Sending {len(coroutines)} requests")
        n_success, bad_statuses = 0, set()

        for coro in coroutines:
            try:
                await coro
            except discord.HTTPException as http_exc:
                bad_statuses.add(http_exc.status)
            else:
                n_success += 1

        if bad_statuses:
            log.info(f"{len(coroutines) - n_success} requests have failed due to following statuses: {bad_statuses}")

        return n_success

    async def _kick_members(self, members: t.Collection[discord.Member]) -> int:
        """
        Kick `members` from the PyDis guild.

        Note that this is a potentially destructive operation. Returns the amount of successful
        requests. Failed requests are logged at info level.
        """
        log.info(f"Kicking {len(members)} members from the guild (not verified after {KICKED_AFTER} days)")

        async def kick_request(member_: discord.Member) -> None:
            """If `member_` still hasn't verified, send them `KICKED_MESSAGE` and kick them."""
            if is_verified(member_):  # Member could have verified in the meantime
                return
            with suppress(discord.Forbidden):
                await member_.send(KICKED_MESSAGE)  # Send message while user is still in guild
            await member_.kick(reason=f"User has not verified in {KICKED_AFTER} days")

        requests = [kick_request(member) for member in members]
        n_kicked = await self._send_requests(requests)

        self.bot.stats.incr("verification.kicked", count=n_kicked)

        return n_kicked

    async def _give_role(self, members: t.Collection[discord.Member], role: discord.Role) -> int:
        """
        Give `role` to all `members`.

        Returns the amount of successful requests. Status codes of unsuccessful requests
        are logged at info level.
        """
        log.info(f"Assigning {role} role to {len(members)} members (not verified after {UNVERIFIED_AFTER} days)")

        async def role_request(member_: discord.Member, role_: discord.Role) -> None:
            """If `member_` still isn't verified, give them `role_`."""
            if is_verified(member_):
                return
            await member_.add_roles(role_, reason=f"User has not verified in {UNVERIFIED_AFTER} days")

        requests = [role_request(member, role) for member in members]
        n_roles_added = await self._send_requests(requests)

        return n_roles_added

    async def _check_members(self) -> t.Tuple[t.Set[discord.Member], t.Set[discord.Member]]:
        """
        Check in on the verification status of PyDis members.

        This coroutine finds two sets of users:
            * Not verified after `UNVERIFIED_AFTER` days, should be given the @Unverified role
            * Not verified after `KICKED_AFTER` days, should be kicked from the guild

        These sets are always disjoint, i.e. share no common members.
        """
        await self.bot.wait_until_guild_available()  # Ensure cache is ready
        pydis = self.bot.get_guild(constants.Guild.id)

        unverified = pydis.get_role(constants.Roles.unverified)
        current_dt = datetime.utcnow()  # Discord timestamps are UTC

        # Users to be given the @Unverified role, and those to be kicked, these should be entirely disjoint
        for_role, for_kick = set(), set()

        log.debug("Checking verification status of guild members")
        for member in pydis.members:

            # Skip verified members, bots, and members for which we do not know their join date,
            # this should be extremely rare but docs mention that it can happen
            if is_verified(member) or member.bot or member.joined_at is None:
                continue

            # At this point, we know that `member` is an unverified user, and we will decide what
            # to do with them based on time passed since their join date
            since_join = current_dt - member.joined_at

            if since_join > timedelta(days=KICKED_AFTER):
                for_kick.add(member)  # User should be removed from the guild

            elif since_join > timedelta(days=UNVERIFIED_AFTER) and unverified not in member.roles:
                for_role.add(member)  # User should be given the @Unverified role

        log.debug(f"Found {len(for_role)} users for {unverified} role, {len(for_kick)} users to be kicked")
        return for_role, for_kick

    @tasks.loop(minutes=30)
    async def update_unverified_members(self) -> None:
        """
        Periodically call `_check_members` and update unverified members accordingly.

        After each run, a summary will be sent to the modlog channel. If a suspiciously high
        amount of members to be kicked is found, the operation is guarded by `_verify_kick`.
        """
        log.info("Updating unverified guild members")

        await self.bot.wait_until_guild_available()
        unverified = self.bot.get_guild(constants.Guild.id).get_role(constants.Roles.unverified)

        for_role, for_kick = await self._check_members()

        if not for_role:
            role_report = f"Found no users to be assigned the {unverified.mention} role."
        else:
            n_roles = await self._give_role(for_role, unverified)
            role_report = f"Assigned {unverified.mention} role to `{n_roles}`/`{len(for_role)}` members."

        if not for_kick:
            kick_report = "Found no users to be kicked."
        elif not await self._verify_kick(len(for_kick)):
            kick_report = f"Not authorized to kick `{len(for_kick)}` members."
        else:
            n_kicks = await self._kick_members(for_kick)
            kick_report = f"Kicked `{n_kicks}`/`{len(for_kick)}` members from the guild."

        await self.mod_log.send_log_message(
            icon_url=self.bot.user.avatar_url,
            colour=discord.Colour.blurple(),
            title="Verification system",
            text=f"{kick_report}\n{role_report}",
        )

    # endregion
    # region: periodically ping @Unverified

    @tasks.loop(hours=REMINDER_FREQUENCY)
    async def ping_unverified(self) -> None:
        """
        Delete latest `REMINDER_MESSAGE` and send it again.

        This utilizes RedisCache to persist the latest reminder message id.
        """
        await self.bot.wait_until_guild_available()
        verification = self.bot.get_guild(constants.Guild.id).get_channel(constants.Channels.verification)

        last_reminder: t.Optional[int] = await self.task_cache.get("last_reminder")

        if last_reminder is not None:
            log.trace(f"Found verification reminder message in cache, deleting: {last_reminder}")

            with suppress(discord.HTTPException):  # If something goes wrong, just ignore it
                await self.bot.http.delete_message(verification.id, last_reminder)

        log.trace("Sending verification reminder")
        new_reminder = await verification.send(REMINDER_MESSAGE, allowed_mentions=MENTION_UNVERIFIED)

        await self.task_cache.set("last_reminder", new_reminder.id)

    @ping_unverified.before_loop
    async def _before_first_ping(self) -> None:
        """
        Sleep until `REMINDER_MESSAGE` should be sent again.

        If latest reminder is not cached, exit instantly. Otherwise, wait wait until the
        configured `REMINDER_FREQUENCY` has passed.
        """
        last_reminder: t.Optional[int] = await self.task_cache.get("last_reminder")

        if last_reminder is None:
            log.trace("Latest verification reminder message not cached, task will not wait")
            return

        # Convert cached message id into a timestamp
        time_since = datetime.utcnow() - snowflake_time(last_reminder)
        log.trace(f"Time since latest verification reminder: {time_since}")

        to_sleep = timedelta(hours=REMINDER_FREQUENCY) - time_since
        log.trace(f"Time to sleep until next ping: {to_sleep}")

        # Delta can be negative if `REMINDER_FREQUENCY` has already passed
        secs = max(to_sleep.total_seconds(), 0)
        await asyncio.sleep(secs)

    # endregion
    # region: listeners

    @Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Attempt to send initial direct message to each new member."""
        if member.guild.id != constants.Guild.id:
            return  # Only listen for PyDis events

        log.trace(f"Sending on join message to new member: {member.id}")
        with suppress(discord.Forbidden):
            await member.send(ON_JOIN_MESSAGE)

    @Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Check new message event for messages to the checkpoint channel & process."""
        if message.channel.id != constants.Channels.verification:
            return  # Only listen for #checkpoint messages

        if message.content == REMINDER_MESSAGE:
            return  # Ignore bots own verification reminder

        if message.author.bot:
            # They're a bot, delete their message after the delay.
            await message.delete(delay=BOT_MESSAGE_DELETE_DELAY)
            return

        # if a user mentions a role or guild member
        # alert the mods in mod-alerts channel
        if message.mentions or message.role_mentions:
            log.debug(
                f"{message.author} mentioned one or more users "
                f"and/or roles in {message.channel.name}"
            )

            embed_text = (
                f"{message.author.mention} sent a message in "
                f"{message.channel.mention} that contained user and/or role mentions."
                f"\n\n**Original message:**\n>>> {message.content}"
            )

            # Send pretty mod log embed to mod-alerts
            await self.mod_log.send_log_message(
                icon_url=constants.Icons.filtering,
                colour=discord.Colour(constants.Colours.soft_red),
                title=f"User/Role mentioned in {message.channel.name}",
                text=embed_text,
                thumbnail=message.author.avatar_url_as(static_format="png"),
                channel_id=constants.Channels.mod_alerts,
            )

        ctx: Context = await self.bot.get_context(message)
        if ctx.command is not None and ctx.command.name == "accept":
            return

        if any(r.id == constants.Roles.verified for r in ctx.author.roles):
            log.info(
                f"{ctx.author} posted '{ctx.message.content}' "
                "in the verification channel, but is already verified."
            )
            return

        log.debug(
            f"{ctx.author} posted '{ctx.message.content}' in the verification "
            "channel. We are providing instructions how to verify."
        )
        await ctx.send(
            f"{ctx.author.mention} Please type `!accept` to verify that you accept our rules, "
            f"and gain access to the rest of the server.",
            delete_after=20
        )

        log.trace(f"Deleting the message posted by {ctx.author}")
        with suppress(discord.NotFound):
            await ctx.message.delete()

    # endregion
    # region: task management commands

    @with_role(*constants.MODERATION_ROLES)
    @group(name="verification")
    async def verification_group(self, ctx: Context) -> None:
        """Manage internal verification tasks."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @verification_group.command(name="status")
    async def status_cmd(self, ctx: Context) -> None:
        """Check whether verification tasks are running."""
        log.trace("Checking status of verification tasks")

        if self.update_unverified_members.is_running():
            update_status = f"{constants.Emojis.incident_actioned} Member update task is running."
        else:
            update_status = f"{constants.Emojis.incident_unactioned} Member update task is **not** running."

        mention = f"<@&{constants.Roles.unverified}>"
        if self.ping_unverified.is_running():
            ping_status = f"{constants.Emojis.incident_actioned} Ping {mention} is running."
        else:
            ping_status = f"{constants.Emojis.incident_unactioned} Ping {mention} is **not** running."

        embed = discord.Embed(
            title="Verification system",
            description=f"{update_status}\n{ping_status}",
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    @verification_group.command(name="start")
    async def start_cmd(self, ctx: Context) -> None:
        """Start verification tasks if they are not already running."""
        log.info("Starting verification tasks")

        if not self.update_unverified_members.is_running():
            self.update_unverified_members.start()

        if not self.ping_unverified.is_running():
            self.ping_unverified.start()

        await self.task_cache.set("tasks_running", 1)

        colour = discord.Colour.blurple()
        await ctx.send(embed=discord.Embed(title="Verification system", description="Done. :ok_hand:", colour=colour))

    @verification_group.command(name="stop", aliases=["kill"])
    async def stop_cmd(self, ctx: Context) -> None:
        """Stop verification tasks."""
        log.info("Stopping verification tasks")

        self.update_unverified_members.cancel()
        self.ping_unverified.cancel()

        await self.task_cache.set("tasks_running", 0)

        colour = discord.Colour.blurple()
        await ctx.send(embed=discord.Embed(title="Verification system", description="Tasks canceled.", colour=colour))

    # endregion
    # region: accept and subscribe commands

    def _bump_verified_stats(self, verified_member: discord.Member) -> None:
        """
        Increment verification stats for `verified_member`.

        Each member falls into one of the three categories:
            * Verified within 24 hours after joining
            * Does not have @Unverified role yet
            * Does have @Unverified role

        Stats for member kicking are handled separately.
        """
        if verified_member.joined_at is None:  # Docs mention this can happen
            return

        if (datetime.utcnow() - verified_member.joined_at) < timedelta(hours=24):
            category = "accepted_on_day_one"
        elif constants.Roles.unverified not in [role.id for role in verified_member.roles]:
            category = "accepted_before_unverified"
        else:
            category = "accepted_after_unverified"

        log.trace(f"Bumping verification stats in category: {category}")
        self.bot.stats.incr(f"verification.{category}")

    @command(name='accept', aliases=('verify', 'verified', 'accepted'), hidden=True)
    @without_role(constants.Roles.verified)
    @in_whitelist(channels=(constants.Channels.verification,))
    async def accept_command(self, ctx: Context, *_) -> None:  # We don't actually care about the args
        """Accept our rules and gain access to the rest of the server."""
        log.debug(f"{ctx.author} called !accept. Assigning the 'Developer' role.")
        await ctx.author.add_roles(discord.Object(constants.Roles.verified), reason="Accepted the rules")

        self._bump_verified_stats(ctx.author)  # This checks for @Unverified so make sure it's not yet removed

        if constants.Roles.unverified in [role.id for role in ctx.author.roles]:
            log.debug(f"Removing Unverified role from: {ctx.author}")
            await ctx.author.remove_roles(discord.Object(constants.Roles.unverified))

        try:
            await ctx.author.send(VERIFIED_MESSAGE)
        except discord.Forbidden:
            log.info(f"Sending welcome message failed for {ctx.author}.")
        finally:
            log.trace(f"Deleting accept message by {ctx.author}.")
            with suppress(discord.NotFound):
                self.mod_log.ignore(constants.Event.message_delete, ctx.message.id)
                await ctx.message.delete()

    @command(name='subscribe')
    @in_whitelist(channels=(constants.Channels.bot_commands,))
    async def subscribe_command(self, ctx: Context, *_) -> None:  # We don't actually care about the args
        """Subscribe to announcement notifications by assigning yourself the role."""
        has_role = False

        for role in ctx.author.roles:
            if role.id == constants.Roles.announcements:
                has_role = True
                break

        if has_role:
            await ctx.send(f"{ctx.author.mention} You're already subscribed!")
            return

        log.debug(f"{ctx.author} called !subscribe. Assigning the 'Announcements' role.")
        await ctx.author.add_roles(discord.Object(constants.Roles.announcements), reason="Subscribed to announcements")

        log.trace(f"Deleting the message posted by {ctx.author}.")

        await ctx.send(
            f"{ctx.author.mention} Subscribed to <#{constants.Channels.announcements}> notifications.",
        )

    @command(name='unsubscribe')
    @in_whitelist(channels=(constants.Channels.bot_commands,))
    async def unsubscribe_command(self, ctx: Context, *_) -> None:  # We don't actually care about the args
        """Unsubscribe from announcement notifications by removing the role from yourself."""
        has_role = False

        for role in ctx.author.roles:
            if role.id == constants.Roles.announcements:
                has_role = True
                break

        if not has_role:
            await ctx.send(f"{ctx.author.mention} You're already unsubscribed!")
            return

        log.debug(f"{ctx.author} called !unsubscribe. Removing the 'Announcements' role.")
        await ctx.author.remove_roles(
            discord.Object(constants.Roles.announcements), reason="Unsubscribed from announcements"
        )

        log.trace(f"Deleting the message posted by {ctx.author}.")

        await ctx.send(
            f"{ctx.author.mention} Unsubscribed from <#{constants.Channels.announcements}> notifications."
        )

    # endregion
    # region: miscellaneous

    # This cannot be static (must have a __func__ attribute).
    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        """Check for & ignore any InWhitelistCheckFailure."""
        if isinstance(error, InWhitelistCheckFailure):
            error.handled = True

    @staticmethod
    def bot_check(ctx: Context) -> bool:
        """Block any command within the verification channel that is not !accept."""
        if ctx.channel.id == constants.Channels.verification and without_role_check(ctx, *constants.MODERATION_ROLES):
            return ctx.command.name == "accept"
        else:
            return True

    # endregion


def setup(bot: Bot) -> None:
    """Load the Verification cog."""
    bot.add_cog(Verification(bot))
