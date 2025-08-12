import re
import asyncio
import aiosqlite
from collections import deque, defaultdict
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
import aiohttp

WEBHOOK_URL = "https://discord.com/api/webhooks/1404204136488112218/KIBPV7UHU78G7Xp8bgaydK6g46Bd7C6KQXdZEta-q7YT3e8DwAY_rWNz8vc0eJN_eCuE"
ALLOWED_GUILDS = {1286045119715475527}  # Pon aquí los IDs de servidores donde puede entrar el bot

TOKEN = ""
if not TOKEN:
    raise SystemExit("Falta el token de Discord")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=commands.DefaultHelpCommand())

DB_PATH = "modbot.db"

SPAM_WINDOW = 7  # segundos
SPAM_THRESHOLD = 5
MUTE_DURATION = 60 * 5  # 5 min

URL_RE = re.compile(r"(?i)\b((?:https?://|www\d{0,3}[.]|discord[.])\S+)")

recent_messages = defaultdict(lambda: deque())  # guild_id -> deque of (user_id, timestamps)
temp_mutes = {}  # (guild_id, user_id) -> unmute_at (datetime)
spam_warnings = defaultdict(lambda: defaultdict(int))  # guild_id -> user_id -> warnings count


async def send_log_to_webhook(embed: discord.Embed):
    async with aiohttp.ClientSession() as session:
        webhook_data = {
            "username": "OxcyShop - Logs",
            "embeds": [embed.to_dict()]
        }
        async with session.post(WEBHOOK_URL, json=webhook_data) as resp:
            if resp.status not in (204, 200):
                print(f"Error enviando log al webhook: {resp.status}")


async def create_embed(title, description, color=discord.Color.red()):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.utcnow())
    embed.set_footer(text="OxcyShop - Vanguard")
    return embed


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            anti_link INTEGER DEFAULT 1,
            anti_spam INTEGER DEFAULT 1,
            anti_image INTEGER DEFAULT 1,
            spam_threshold INTEGER DEFAULT {SPAM_THRESHOLD},
            spam_window INTEGER DEFAULT {SPAM_WINDOW}
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS infractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            action TEXT,
            reason TEXT,
            moderator_id INTEGER,
            timestamp TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS whitelists (
            guild_id INTEGER,
            type TEXT,
            ref_id INTEGER
        )""")
        await db.commit()

@bot.event
async def on_ready():
    await init_db()
    check_temp_mutes.start()
    await bot.change_presence(activity=discord.Game("Protegiendo servidores | Seguridad ON"))
    print(f"Bot listo como {bot.user} (id: {bot.user.id})")

@tasks.loop(seconds=10)
async def check_temp_mutes():
    now = datetime.utcnow()
    to_remove = []
    for (guild_id, user_id), unmute_time in list(temp_mutes.items()):
        if unmute_time and now >= unmute_time:
            guild = bot.get_guild(guild_id)
            if not guild:
                to_remove.append((guild_id, user_id))
                continue
            member = guild.get_member(user_id)
            if member:
                role = discord.utils.get(guild.roles, name="Muted")
                if role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Auto unmute")
                        await log_infraction(guild_id, user_id, "UNMUTE", "Auto unmute")
                    except Exception:
                        pass
            to_remove.append((guild_id, user_id))
    for key in to_remove:
        temp_mutes.pop(key, None)



        
async def get_guild_settings(guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT anti_link, anti_spam, anti_image, spam_threshold, spam_window FROM guild_settings WHERE guild_id = ?",
            (guild_id,))
        row = await cur.fetchone()
        if row:
            return {
                "anti_link": bool(row[0]),
                "anti_spam": bool(row[1]),
                "anti_image": bool(row[2]),
                "spam_threshold": int(row[3]),
                "spam_window": int(row[4])
            }
        await db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        return {"anti_link": True, "anti_spam": True, "anti_image": True, "spam_threshold": SPAM_THRESHOLD, "spam_window": SPAM_WINDOW}


async def is_whitelisted(guild_id, user, channel):
    if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM whitelists WHERE guild_id=? AND type='user' AND ref_id=?", (guild_id, user.id))
        if await cur.fetchone(): return True
        cur = await db.execute("SELECT 1 FROM whitelists WHERE guild_id=? AND type='channel' AND ref_id=?", (guild_id, channel.id))
        if await cur.fetchone(): return True
        for role in user.roles:
            cur = await db.execute("SELECT 1 FROM whitelists WHERE guild_id=? AND type='role' AND ref_id=?", (guild_id, role.id))
            if await cur.fetchone(): return True
    return False


async def log_infraction(guild_id, user_id, action, reason, moderator_id=None):
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO infractions (guild_id,user_id,action,reason,moderator_id,timestamp) VALUES (?,?,?,?,?,?)",
                         (guild_id, user_id, action, reason, moderator_id, ts))
        await db.commit()


async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="Muted")
    if not role:
        perms = discord.Permissions(send_messages=False, speak=False)
        role = await guild.create_role(name="Muted", reason="Role for muting via bot", permissions=perms)
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(role, send_messages=False, add_reactions=False)
            except Exception:
                pass
    return role


async def warn_member(guild, member: discord.Member, reason, moderator=None):
    await log_infraction(guild.id, member.id, "WARN", reason, moderator.id if moderator else None)
    try:
        await member.send(f"Has recibido una advertencia en **{guild.name}**: {reason}")
    except Exception:
        pass


async def temp_mute_member(guild, member: discord.Member, duration_seconds: int, reason, moderator=None, permanent=False):
    role = await ensure_muted_role(guild)
    await member.add_roles(role, reason=reason)
    if permanent:
        unmute_at = None
        action = "PERMANENT_MUTE"
    else:
        unmute_at = datetime.utcnow() + timedelta(seconds=duration_seconds)
        temp_mutes[(guild.id, member.id)] = unmute_at
        action = f"TEMP_MUTE_{duration_seconds}s"
    await log_infraction(guild.id, member.id, action, reason, moderator.id if moderator else None)

    # Crear hilo en canal de Mod-Log para informar al usuario
    channel = discord.utils.get(guild.text_channels, name="mod-log")
    if channel:
        try:
            thread = await channel.create_thread(name=f"Expulsión: {member.display_name}", type=discord.ChannelType.public_thread, auto_archive_duration=60)
            await thread.send(f"{member.mention}, has sido {'muteado permanentemente' if permanent else f'muteado por {duration_seconds//60} minutos'}.\nRazón: {reason}")
        except Exception:
            pass

    # Mensaje privado claro y profesional
    try:
        dm = await member.create_dm()
        if permanent:
            await dm.send(f"Has sido muteado **permanentemente** en el servidor **{guild.name}**.\nRazón: {reason}\nSi crees que fue un error, contacta a un moderador.")
        else:
            await dm.send(f"Has sido muteado por **{duration_seconds//60} minutos** en el servidor **{guild.name}**.\nRazón: {reason}\nPor favor respeta las reglas para evitar sanciones futuras.")
    except Exception:
        pass

async def temp_mute_member(guild, member: discord.Member, duration_seconds: int, reason, moderator=None, permanent=False):
    role = await ensure_muted_role(guild)
    if role not in member.roles:
        await member.add_roles(role, reason=reason)
    if permanent:
        action = "PERMANENT_MUTE"
    else:
        unmute_at = datetime.utcnow() + timedelta(seconds=duration_seconds)
        temp_mutes[(guild.id, member.id)] = unmute_at
        action = f"TEMP_MUTE_{duration_seconds}s"
    await log_infraction(guild.id, member.id, action, reason, moderator.id if moderator else None)

    # Mensaje DM
    try:
        dm = await member.create_dm()
        if permanent:
            await dm.send(f"Has sido muteado **permanentemente** en el servidor **{guild.name}**.\nRazón: {reason}")
        else:
            await dm.send(f"Has sido muteado por **{duration_seconds // 60} minutos** en el servidor **{guild.name}**.\nRazón: {reason}")
    except Exception:
        pass

    # Crear hilo en mod-log
    channel = discord.utils.get(guild.text_channels, name="mod-log")
    if channel:
        try:
            thread = await channel.create_thread(
                name=f"Sanción a {member.display_name}",
                type=discord.ChannelType.public_thread,
                auto_archive_duration=60
            )
            await thread.send(
                f"{member.mention} ha sido {'muteado permanentemente' if permanent else f'muteado por {duration_seconds//60} minutos'}.\nRazón: {reason}"
            )
        except Exception:
            pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    if guild_id not in ALLOWED_GUILDS:
        return

    settings = await get_guild_settings(guild_id)

    if await is_whitelisted(guild_id, message.author, message.channel):
        await bot.process_commands(message)
        return

    # --- Anti-link ---
    if settings["anti_link"]:
        if URL_RE.search(message.content):
            try:
                await message.delete()
            except Exception:
                pass

            reason = "Envío de links no permitido"
            await log_infraction(guild_id, message.author.id, "DELETE_LINK", reason)
            # Avisar por DM
            try:
                await message.author.send(f"Tu mensaje fue eliminado en **{message.guild.name}** porque no está permitido enviar links.")
            except Exception:
                pass

            # Log a webhook con embed profesional
            embed = discord.Embed(
                title="Mensaje eliminado - Enlace prohibido",
                description=f"**Usuario:** {message.author} (`{message.author.id}`)\n**Canal:** {message.channel.mention}\n**Mensaje:** {message.content}",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="ModBot Seguridad")
            embed.set_author(name=message.author, icon_url=message.author.display_avatar.url if message.author.display_avatar else None)
            await send_log_to_webhook(embed)
            return  # No procesar más

    # --- Anti-spam ---
    if settings["anti_spam"]:
        user_q = recent_messages[guild_id]
        now_ts = datetime.utcnow().timestamp()
        user_q.append((message.author.id, now_ts))
        window = settings["spam_window"]
        threshold = settings["spam_threshold"]

        # Limpiar mensajes fuera del rango temporal
        while user_q and now_ts - user_q[0][1] > window:
            user_q.popleft()

        cnt = sum(1 for (uid, _) in user_q if uid == message.author.id)

        if cnt >= threshold:
            # Eliminar el mensaje actual que causó el spam
            try:
                await message.delete()
            except Exception:
                pass

            spam_warnings[guild_id][message.author.id] += 1
            warns = spam_warnings[guild_id][message.author.id]

            if warns >= 3:
                # Mute permanente
                await temp_mute_member(
                    message.guild, message.author, 0,
                    "Mute permanente por spam tras 3 advertencias",
                    moderator=None, permanent=True
                )
                spam_warnings[guild_id][message.author.id] = 0
                channel = discord.utils.get(message.guild.text_channels, name="mod-log")
                if channel:
                    await channel.send(f"{message.author.mention} ha sido muteado **permanentemente** por spam tras 3 advertencias.")
            else:
                # Mute temporal 5 minutos
                await temp_mute_member(
                    message.guild, message.author, MUTE_DURATION,
                    f"Auto-mute por spam (advertencia {warns}/3)",
                    moderator=None, permanent=False
                )
                channel = discord.utils.get(message.guild.text_channels, name="mod-log")
                if channel:
                    await channel.send(f"{message.author.mention} ha sido muteado por spam. Advertencia {warns}/3.")

            # Log embed para spam también
            embed = discord.Embed(
                title="Acción anti-spam",
                description=(
                    f"**Usuario:** {message.author} (`{message.author.id}`)\n"
                    f"**Canal:** {message.channel.mention}\n"
                    f"**Mensaje eliminado:** {message.content}\n"
                    f"**Advertencias:** {warns}/3\n"
                    f"**Acción:** {'Mute permanente' if warns >= 3 else 'Mute temporal 5 min'}"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="ModBot Seguridad")
            embed.set_author(name=message.author, icon_url=message.author.display_avatar.url if message.author.display_avatar else None)
            await send_log_to_webhook(embed)

            return  # No procesar comandos luego

    # Procesar comandos normalmente
    await bot.process_commands(message)


# Resto de comandos (modcfg, mute, warn, cases, whitelist) puedes dejarlos igual


if __name__ == "__main__":
    bot.run(TOKEN)
