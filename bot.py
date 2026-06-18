import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


# Вставьте сюда токен вашего бота.
BOT_TOKEN = "PASTE_BOT_TOKEN_HERE"

# Укажите ID вашего сервера, чтобы команда синхронизировалась быстрее.
GUILD_ID = 1504503769164025889

# Каналы из этого списка бот полностью игнорирует.
# Пример: IGNORED_CHANNEL_IDS = [123456789012345678, 987654321098765432]
IGNORED_CHANNEL_IDS: list[int] = []

# При желании можно указать ID канала для логов, если у пользователя закрыты ЛС.
# Если логирование не нужно, оставьте 0.
STAFF_LOG_CHANNEL_ID = 0

# Если ID роли общей верификации неизвестен, оставьте 0 - бот создаст роль сам.
VERIFIED_ROLE_ID = 0

# Имя роли, которую бот создаст для доступа к обычным каналам.
VERIFIED_ROLE_NAME = "Верифицирован"

# Бот создаст канал с таким именем, если его нет.
WELCOME_CHANNEL_NAME = "добро-пожаловать"

# Каналы и категории из этих списков бот не будет менять при автоматической настройке прав.
# Сюда можно добавить админ-каналы и админ-категории.
PROTECTED_CHANNEL_IDS: list[int] = []
PROTECTED_CATEGORY_IDS: list[int] = []

# Если ID ранговых ролей неизвестны, оставьте 0 - бот создаст роли сам.
RANK_ROLE_IDS = {
    "Железо": 0,
    "Бронза": 0,
    "Серебро": 0,
    "Золото": 0,
    "Платина": 0,
    "Алмаз": 0,
    "Восхождение": 0,
    "Бессмертный": 0,
    "Радиант": 0,
}

RANK_OPTIONS = list(RANK_ROLE_IDS.keys())
DATABASE_FILE = "players.db"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("valorant_verification_bot")


class DatabaseManager:
    def __init__(self, path: str) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    discord_id INTEGER PRIMARY KEY,
                    valorant_nick TEXT NOT NULL,
                    valorant_rank TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def get_player(self, discord_id: int) -> Optional[dict]:
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT discord_id, valorant_nick, valorant_rank, updated_at FROM players WHERE discord_id = ?",
                (discord_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return {
            "discord_id": row[0],
            "valorant_nick": row[1],
            "valorant_rank": row[2],
            "updated_at": row[3],
        }

    def upsert_player(self, discord_id: int, valorant_nick: str, valorant_rank: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO players (discord_id, valorant_nick, valorant_rank, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    valorant_nick = excluded.valorant_nick,
                    valorant_rank = excluded.valorant_rank,
                    updated_at = excluded.updated_at
                """,
                (discord_id, valorant_nick, valorant_rank, timestamp),
            )
            connection.commit()


class ValorantVerificationBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)
        self.database = DatabaseManager(DATABASE_FILE)
        self.verified_role_id = VERIFIED_ROLE_ID
        self.rank_role_ids = dict(RANK_ROLE_IDS)

    async def setup_hook(self) -> None:
        self.add_view(WelcomeVerificationView(self))
        self.add_view(UpdatePromptView(self))

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        logger.info("Бот запущен как %s (%s)", self.user, self.user.id if self.user else "unknown")

        if GUILD_ID:
            guild = self.get_guild(GUILD_ID)
            if guild is not None:
                await self.ensure_guild_resources(guild)
            else:
                logger.warning("Не удалось найти сервер с GUILD_ID=%s", GUILD_ID)

    async def ensure_guild_resources(self, guild: discord.Guild) -> None:
        verified_role = await self.ensure_verified_role(guild)
        await self.ensure_rank_roles(guild)
        await self.ensure_welcome_channel(guild, verified_role)
        await self.configure_general_channel_access(guild, verified_role)

    async def ensure_rank_roles(self, guild: discord.Guild) -> None:
        for rank_name in RANK_OPTIONS:
            configured_role_id = self.rank_role_ids.get(rank_name, 0)
            role = guild.get_role(configured_role_id) if configured_role_id else None

            if role is None:
                role = discord.utils.get(guild.roles, name=rank_name)

            if role is None:
                role = await guild.create_role(
                    name=rank_name,
                    mentionable=False,
                    reason="Создание ранговой роли Valorant",
                )
                logger.info("Создана ранговая роль %s (%s)", role.name, role.id)

            self.rank_role_ids[rank_name] = role.id

    async def ensure_verified_role(self, guild: discord.Guild) -> discord.Role:
        if self.verified_role_id:
            existing_role = guild.get_role(self.verified_role_id)
            if existing_role is not None:
                return existing_role

        role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if role is None:
            role = await guild.create_role(
                name=VERIFIED_ROLE_NAME,
                mentionable=False,
                reason="Создание роли для верифицированных пользователей",
            )
            logger.info("Создана роль верификации %s (%s)", role.name, role.id)

        self.verified_role_id = role.id
        return role

    async def ensure_welcome_channel(self, guild: discord.Guild, verified_role: discord.Role) -> discord.TextChannel:
        channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)

        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                verified_role: discord.PermissionOverwrite(view_channel=False),
            }
            channel = await guild.create_text_channel(
                name=WELCOME_CHANNEL_NAME,
                overwrites=overwrites,
                reason="Создание канала для первичной верификации",
            )
            logger.info("Создан канал приветствия #%s (%s)", channel.name, channel.id)
        else:
            await self.apply_welcome_channel_permissions(channel, verified_role)

        await self.ensure_welcome_message(channel)
        return channel

    async def apply_welcome_channel_permissions(
        self,
        channel: discord.TextChannel,
        verified_role: discord.Role,
    ) -> None:
        everyone_overwrite = channel.overwrites_for(channel.guild.default_role)
        everyone_overwrite.view_channel = True
        everyone_overwrite.send_messages = False

        verified_overwrite = channel.overwrites_for(verified_role)
        verified_overwrite.view_channel = False

        try:
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=everyone_overwrite,
                reason="Настройка канала приветствия для новых участников",
            )
            await channel.set_permissions(
                verified_role,
                overwrite=verified_overwrite,
                reason="Скрытие канала приветствия от верифицированных участников",
            )
        except discord.Forbidden:
            logger.warning("Не удалось настроить права канала #%s", channel.name)

    async def ensure_welcome_message(self, channel: discord.TextChannel) -> None:
        instruction = "Проверьте личные сообщения и ответьте боту, чтобы получить доступ ко всем каналам сервера."

        try:
            async for message in channel.history(limit=10):
                if message.author == self.user and message.content == instruction:
                    return
        except discord.HTTPException:
            logger.warning("Не удалось проверить историю канала #%s", channel.name)

        try:
            sent_message = await channel.send(instruction)
            await sent_message.pin(reason="Сообщение с инструкцией по верификации")
        except discord.Forbidden:
            logger.warning("Не удалось отправить сообщение в канал #%s", channel.name)
        except discord.HTTPException:
            logger.warning("Не удалось закрепить сообщение в канале #%s", channel.name)

    async def configure_general_channel_access(self, guild: discord.Guild, verified_role: discord.Role) -> None:
        for channel in guild.channels:
            if channel.id in PROTECTED_CHANNEL_IDS:
                continue

            if channel.category_id in PROTECTED_CATEGORY_IDS:
                continue

            if isinstance(channel, discord.TextChannel) and channel.name == WELCOME_CHANNEL_NAME:
                continue

            if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel, discord.StageChannel)):
                continue

            if channel.permissions_for(guild.me).manage_channels is False:
                continue

            await self.ensure_channel_visibility(channel, verified_role)

    async def ensure_channel_visibility(
        self,
        channel: discord.abc.GuildChannel,
        verified_role: discord.Role,
    ) -> None:
        everyone_overwrite = channel.overwrites_for(channel.guild.default_role)
        verified_overwrite = channel.overwrites_for(verified_role)

        everyone_overwrite.view_channel = False
        verified_overwrite.view_channel = True

        try:
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=everyone_overwrite,
                reason="Закрытие обычных каналов для неверифицированных пользователей",
            )
            await channel.set_permissions(
                verified_role,
                overwrite=verified_overwrite,
                reason="Открытие обычных каналов для верифицированных пользователей",
            )
        except discord.Forbidden:
            logger.warning("Не удалось настроить права канала %s (%s)", channel.name, channel.id)
        except discord.HTTPException:
            logger.warning("Ошибка Discord API при настройке канала %s (%s)", channel.name, channel.id)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return

        await self.ensure_guild_resources(member.guild)

        try:
            await member.send(
                (
                    f"{member.mention}, добро пожаловать! Укажите ник Valorant и выберите свой ранг, "
                    "чтобы получить доступ ко всем каналам сервера."
                ),
                view=WelcomeVerificationView(self),
            )
        except discord.Forbidden:
            logger.warning("Не удалось отправить ЛС пользователю %s", member.id)
            await self.log_dm_blocked(member)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.guild and message.channel.id in IGNORED_CHANNEL_IDS:
            return

        await self.process_commands(message)

    async def is_ignored_channel_interaction(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.channel is None:
            return False

        return interaction.channel.id in IGNORED_CHANNEL_IDS

    async def log_dm_blocked(self, member: discord.Member) -> None:
        if not STAFF_LOG_CHANNEL_ID:
            return

        channel = member.guild.get_channel(STAFF_LOG_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f"Не удалось отправить ЛС пользователю {member.mention} ({member.id}). "
                    "Проверьте, открыты ли у него личные сообщения."
                )
            except discord.HTTPException:
                logger.exception("Не удалось отправить лог о закрытых ЛС пользователя %s", member.id)

    def get_rank_role(self, guild: discord.Guild, rank_name: str) -> Optional[discord.Role]:
        role_id = self.rank_role_ids.get(rank_name, 0)
        if not role_id:
            role = discord.utils.get(guild.roles, name=rank_name)
            if role is not None:
                self.rank_role_ids[rank_name] = role.id
            return role

        role = guild.get_role(role_id)
        if role is None:
            role = discord.utils.get(guild.roles, name=rank_name)
            if role is not None:
                self.rank_role_ids[rank_name] = role.id

        return role

    def get_verified_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        if self.verified_role_id:
            role = guild.get_role(self.verified_role_id)
            if role is not None:
                return role
        return discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)

    async def update_member_profile(
        self,
        member: discord.Member,
        valorant_nick: Optional[str],
        valorant_rank: Optional[str],
    ) -> str:
        current_data = self.database.get_player(member.id)

        final_nick = valorant_nick or (current_data["valorant_nick"] if current_data else None)
        final_rank = valorant_rank or (current_data["valorant_rank"] if current_data else None)

        if not final_nick or not final_rank:
            return (
                "Не удалось сохранить данные: для этого действия не хватает текущих данных игрока. "
                "Сначала используйте полное обновление ника и ранга."
            )

        self.database.upsert_player(member.id, final_nick, final_rank)
        result_lines = ["Данные Valorant успешно сохранены."]

        rank_role = self.get_rank_role(member.guild, final_rank)
        roles_to_remove = []
        target_role_id = rank_role.id if rank_role else None
        for rank_name in RANK_OPTIONS:
            role = self.get_rank_role(member.guild, rank_name)
            if role and role in member.roles and role.id != target_role_id:
                roles_to_remove.append(role)

        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason="Обновление ранга Valorant")
            except discord.Forbidden:
                result_lines.append("Не удалось снять старые ранговые роли: не хватает прав.")
            except discord.HTTPException:
                result_lines.append("Не удалось снять старые ранговые роли из-за ошибки Discord API.")

        if rank_role:
            if rank_role not in member.roles:
                try:
                    await member.add_roles(rank_role, reason="Выдача роли ранга Valorant")
                except discord.Forbidden:
                    result_lines.append("Не удалось выдать роль ранга: не хватает прав.")
                except discord.HTTPException:
                    result_lines.append("Не удалось выдать роль ранга из-за ошибки Discord API.")
        else:
            result_lines.append(
                "Не удалось найти или создать роль для выбранного ранга. Проверьте права бота на управление ролями."
            )

        verified_role = self.get_verified_role(member.guild)
        if verified_role and verified_role not in member.roles:
            try:
                await member.add_roles(verified_role, reason="Верификация игрока Valorant")
            except discord.Forbidden:
                result_lines.append("Не удалось выдать роль верификации: не хватает прав.")
            except discord.HTTPException:
                result_lines.append("Не удалось выдать роль верификации из-за ошибки Discord API.")

        if member.display_name != final_nick:
            try:
                await member.edit(nick=final_nick, reason="Синхронизация ника Valorant")
            except discord.Forbidden:
                result_lines.append(
                    "Не удалось изменить ваш ник на сервере. Обычно это происходит, если у бота "
                    "не хватает прав или ваша роль выше роли бота."
                )
            except discord.HTTPException:
                result_lines.append("Не удалось изменить ник из-за ошибки Discord API.")

        result_lines.append(f"Текущий ник: `{final_nick}`")
        result_lines.append(f"Текущий ранг: `{final_rank}`")
        return "\n".join(result_lines)

    async def send_update_dm(
        self,
        user: discord.abc.User,
        guild_id: int,
        mode: str,
    ) -> tuple[bool, str]:
        try:
            await user.send(
                "Если у вас изменился ник или ранг в Valorant, обновите данные ниже.",
                view=UpdatePromptView(self, guild_id=guild_id, forced_mode=mode),
            )
            return True, "Я отправил вам сообщение в ЛС для обновления данных."
        except discord.Forbidden:
            return False, "Не удалось отправить ЛС. Откройте личные сообщения от участников сервера и попробуйте снова."


class NicknameModal(discord.ui.Modal):
    def __init__(self, bot: ValorantVerificationBot, guild_id: int, mode: str) -> None:
        super().__init__(title="Ник Valorant")
        self.bot = bot
        self.guild_id = guild_id
        self.mode = mode

        self.valorant_nick = discord.ui.TextInput(
            label="Введите ваш ник в Valorant",
            placeholder="Например: Nick#EUW",
            required=True,
            max_length=32,
        )
        self.add_item(self.valorant_nick)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "Сервер не найден. Проверьте значение GUILD_ID в коде.",
                ephemeral=True,
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "Вы не найдены на сервере. Возможно, вы уже покинули его.",
                ephemeral=True,
            )
            return

        nickname = self.valorant_nick.value.strip()
        if self.mode == "nick_only":
            result = await self.bot.update_member_profile(member, nickname, None)
            await interaction.response.send_message(result, ephemeral=True)
            return

        await interaction.response.send_message(
            "Теперь выберите ваш ранг Valorant.",
            view=RankSelectView(self.bot, guild.id, nickname, self.mode),
            ephemeral=True,
        )


class RankSelect(discord.ui.Select):
    def __init__(self, bot: ValorantVerificationBot, guild_id: int, nickname: Optional[str], mode: str) -> None:
        options = [discord.SelectOption(label=rank_name, value=rank_name) for rank_name in RANK_OPTIONS]
        super().__init__(placeholder="Выберите ранг Valorant", min_values=1, max_values=1, options=options)
        self.bot = bot
        self.guild_id = guild_id
        self.nickname = nickname
        self.mode = mode

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "Сервер не найден. Проверьте значение GUILD_ID в коде.",
                ephemeral=True,
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "Вы не найдены на сервере. Возможно, вы уже покинули его.",
                ephemeral=True,
            )
            return

        selected_rank = self.values[0]
        nickname = None if self.mode == "rank_only" else self.nickname
        result = await self.bot.update_member_profile(member, nickname, selected_rank)
        await interaction.response.send_message(result, ephemeral=True)


class RankSelectView(discord.ui.View):
    def __init__(self, bot: ValorantVerificationBot, guild_id: int, nickname: Optional[str], mode: str) -> None:
        super().__init__(timeout=300)
        self.add_item(RankSelect(bot, guild_id, nickname, mode))


class WelcomeVerificationView(discord.ui.View):
    def __init__(self, bot: ValorantVerificationBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Указать ник и ранг",
        style=discord.ButtonStyle.primary,
        custom_id="welcome_verify_button",
    )
    async def start_verification(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button

        if interaction.guild is not None:
            await interaction.response.send_message(
                "Эта кнопка работает только в личных сообщениях бота.",
                ephemeral=True,
            )
            return

        if not GUILD_ID:
            await interaction.response.send_message(
                "Сначала укажите GUILD_ID в коде бота.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(NicknameModal(self.bot, GUILD_ID, "full_update"))


class UpdatePromptView(discord.ui.View):
    def __init__(self, bot: ValorantVerificationBot, guild_id: Optional[int] = None, forced_mode: Optional[str] = None) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.forced_mode = forced_mode

    async def _send_dm_flow(self, interaction: discord.Interaction, mode: str) -> None:
        if await self.bot.is_ignored_channel_interaction(interaction):
            await interaction.response.send_message(
                "В этом канале бот отключен.",
                ephemeral=True,
            )
            return

        target_guild_id = self.guild_id or (interaction.guild.id if interaction.guild else GUILD_ID)
        if not target_guild_id:
            await interaction.response.send_message(
                "Сначала укажите GUILD_ID в коде бота.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            if mode == "rank_only":
                await interaction.response.send_message(
                    "Выберите новый ранг Valorant.",
                    view=RankSelectView(self.bot, target_guild_id, None, "rank_only"),
                    ephemeral=True,
                )
                return

            await interaction.response.send_modal(NicknameModal(self.bot, target_guild_id, mode))
            return

        sent, message = await self.bot.send_update_dm(interaction.user, target_guild_id, mode)
        await interaction.response.send_message(message, ephemeral=True)

        if not sent:
            logger.warning("Не удалось отправить ЛС пользователю %s для обновления данных", interaction.user.id)

    @discord.ui.button(
        label="Обновить ник и ранг",
        style=discord.ButtonStyle.primary,
        custom_id="update_prompt_full",
    )
    async def update_nick_and_rank(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_dm_flow(interaction, self.forced_mode or "full_update")

    @discord.ui.button(
        label="Обновить только ранг",
        style=discord.ButtonStyle.secondary,
        custom_id="update_prompt_rank",
    )
    async def update_rank_only(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_dm_flow(interaction, self.forced_mode or "rank_only")

    @discord.ui.button(
        label="Обновить только ник",
        style=discord.ButtonStyle.secondary,
        custom_id="update_prompt_nick",
    )
    async def update_nick_only(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._send_dm_flow(interaction, self.forced_mode or "nick_only")

    @discord.ui.button(
        label="Пропустить",
        style=discord.ButtonStyle.danger,
        custom_id="update_prompt_skip",
    )
    async def skip_update(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button

        if await self.bot.is_ignored_channel_interaction(interaction):
            await interaction.response.send_message("В этом канале бот отключен.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Действие отменено, ваши данные остались прежними.",
            ephemeral=True,
        )


bot = ValorantVerificationBot()


@bot.tree.command(name="update_prompt", description="Отправить сообщение для обновления данных Valorant")
@app_commands.default_permissions(administrator=True)
async def update_prompt_slash(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Эту команду можно использовать только на сервере.",
            ephemeral=True,
        )
        return

    if interaction.channel_id in IGNORED_CHANNEL_IDS:
        await interaction.response.send_message(
            "В этом канале бот отключен.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Если у вас изменился ник или ранг в Valorant, обновите данные. Если ничего не изменилось, нажмите «Пропустить».",
        view=UpdatePromptView(bot, guild_id=interaction.guild.id),
    )


@bot.command(name="апдейт")
@commands.has_permissions(administrator=True)
async def update_prompt_text(ctx: commands.Context) -> None:
    if ctx.guild is None:
        await ctx.reply("Эту команду можно использовать только на сервере.")
        return

    if ctx.channel.id in IGNORED_CHANNEL_IDS:
        return

    await ctx.send(
        "Если у вас изменился ник или ранг в Valorant, обновите данные. Если ничего не изменилось, нажмите «Пропустить».",
        view=UpdatePromptView(bot, guild_id=ctx.guild.id),
    )


@update_prompt_text.error
async def update_prompt_text_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Эта команда доступна только администраторам.")
        return

    raise error


if __name__ == "__main__":
    if BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError("Укажите BOT_TOKEN в файле bot.py перед запуском бота.")

    bot.run(BOT_TOKEN)
