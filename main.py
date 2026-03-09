import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import base64
import asyncio
from datetime import datetime, timedelta

from myserver import server_on

SCRIPT_URL = "https://raw.githubusercontent.com/xphuphirayy-hue/Kyxhub/refs/heads/main/BFkaitunV1.lua"

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyDatabase.json")

# ================= ห้องที่จะส่ง listkeys อัตโนมัติ =================
AUTO_SEND_GUILD_ID   = 1445387021920112792
AUTO_SEND_CHANNEL_ID = 1480112270938865775

# ================= เวลาเปิด-ปิดบอท =================
BOT_OPEN_HOUR  = 12   # เปิด 12:00
BOT_CLOSE_HOUR = 20   # ปิด 19:30
BOT_CLOSE_MIN  = 00

def is_bot_open() -> bool:
    now = datetime.now()
    open_time  = now.replace(hour=BOT_OPEN_HOUR,  minute=0,             second=0, microsecond=0)
    close_time = now.replace(hour=BOT_CLOSE_HOUR, minute=BOT_CLOSE_MIN, second=0, microsecond=0)
    return open_time <= now < close_time

# ================= RATE LIMIT =================
RATE_LIMIT: dict = {}
RATE_LIMIT_MAX    = 3
RATE_LIMIT_WINDOW = 60

def check_rate_limit(user_id: str) -> bool:
    now = datetime.now().timestamp()
    history = RATE_LIMIT.get(user_id, [])
    history = [t for t in history if now - t < RATE_LIMIT_WINDOW]
    if len(history) >= RATE_LIMIT_MAX:
        RATE_LIMIT[user_id] = history
        return False
    history.append(now)
    RATE_LIMIT[user_id] = history
    return True

# ================= MAINTENANCE =================
MAINTENANCE_MODE = False

def is_maintenance(interaction: discord.Interaction) -> bool:
    return MAINTENANCE_MODE and not is_admin(interaction)

async def deny_maintenance(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🔧 **บอทปิดปรับปรุงอยู่**\nกรุณารอสักครู่ แล้วลองใหม่อีกครั้ง",
        ephemeral=True
    )

# ================= WARNINGS DB (Discord-backed) =================

def load_warnings() -> dict:
    """Sync wrapper — ใช้ใน context ที่ยังไม่ async (fallback ไฟล์)"""
    try:
        with open(WARNINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_warnings(data: dict):
    import tempfile, shutil
    dir_name = os.path.dirname(WARNINGS_FILE) or "."
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_name, delete=False, suffix=".tmp") as tmp:
        json.dump(data, tmp, indent=4, ensure_ascii=False)
        tmp_path = tmp.name
    shutil.move(tmp_path, WARNINGS_FILE)

# ================= IN-MEMORY STORE (sync กับ Discord) =================
# ข้อมูลทั้งหมดเก็บใน RAM และ sync กับ Discord channel เมื่อมีการเปลี่ยนแปลง

_MEM: dict = {
    "db":             {"used_keys": {}, "keys_7_days": [], "keys_30_days": [], "blacklist": {}},
    "warnings":       {},
    "notify_channel": None,      # int | None
    "ticket_config":  {"category_id": None, "log_channel_id": None},
    "appeals":        {},
    "appeal_channel": None,      # int | None
    "admin_roles":    {},
    "expiry_channel": None,      # int | None
}

# ── ฟังก์ชัน sync ไปยัง Discord (ไม่ block event loop) ──
def _schedule_sync(channel_key: str):
    """สั่งให้ sync ไปยัง Discord โดยไม่ await ตรงๆ"""
    async def _do_sync():
        key_to_mem = {
            "keyDatabase":    "db",
            "warnings":       "warnings",
            "ticket_config":  "ticket_config",
            "appeals":        "appeals",
            "admin_roles":    "admin_roles",
        }
        val_keys = {
            "notify_channel": "notify_channel",
            "appeal_channel": "appeal_channel",
            "expiry_channel": "expiry_channel",
        }
        if channel_key in key_to_mem:
            await _discord_write_json(channel_key, _MEM[key_to_mem[channel_key]])
        elif channel_key in val_keys:
            v = _MEM[val_keys[channel_key]]
            if v is not None:
                await _discord_write_value(channel_key, str(v))
    asyncio.create_task(_do_sync())

# ── Wrappers ที่โค้ดเดิมเรียก (sync signature เดิม, เขียน RAM ทันที, sync Discord bg) ──

def load_db() -> dict:
    data = _MEM["db"]
    if "blacklist" not in data:
        data["blacklist"] = {}
    if isinstance(data["blacklist"], list):
        data["blacklist"] = {uid: {"reason": "-", "by": "system", "at": "-"} for uid in data["blacklist"]}
    return data

def save_db(data: dict):
    _MEM["db"] = data
    _schedule_sync("keyDatabase")
    # ยังเขียนไฟล์ backup ด้วยเผื่อ bot restart
    try:
        import tempfile, shutil
        dir_name = os.path.dirname(DB_FILE) or "."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(data, tmp, indent=4, ensure_ascii=False)
            tmp_path = tmp.name
        shutil.move(tmp_path, DB_FILE)
    except Exception as e:
        print(f"[save_db FILE ERROR] {e}")

def load_warnings() -> dict:
    return _MEM["warnings"]

def save_warnings(data: dict):
    _MEM["warnings"] = data
    _schedule_sync("warnings")
    try:
        import tempfile, shutil
        dir_name = os.path.dirname(WARNINGS_FILE) or "."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(data, tmp, indent=4, ensure_ascii=False)
            tmp_path = tmp.name
        shutil.move(tmp_path, WARNINGS_FILE)
    except Exception as e:
        print(f"[save_warnings FILE ERROR] {e}")



# ================= TIME =================

def parse_time(t):
    t = t.replace("/24.00", "/23.59").replace("/24.59", "/23.59")
    parts = t.split("/")
    if len(parts) >= 3 and len(parts[2]) > 4:
        parts[2] = parts[2][:4]
        t = "/".join(parts)
    return datetime.strptime(t, "%d/%m/%Y/%H.%M")

def format_time(end):
    remain = end - datetime.now()
    if remain.total_seconds() <= 0:
        return "หมดเวลา"
    days = remain.days
    hours = remain.seconds // 3600
    minutes = (remain.seconds % 3600) // 60
    return f"{days} วัน {hours} ชั่วโมง {minutes} นาที"

# ================= OBFUSCATOR =================

def obfuscate_key(start_time_str: str, end_time_str: str, key_value: str) -> str:
    raw_value = f"{start_time_str}|{end_time_str}|{key_value[:8]}"
    xor_seed = sum(ord(c) for c in key_value) % 256
    xored = bytes([ord(c) ^ ((xor_seed + i) % 256) for i, c in enumerate(raw_value)])
    b64 = base64.b64encode(xored).decode()
    chunks = [b64[i:i+4] for i in range(0, len(b64), 4)]
    lua_concat = "..".join([f'"{c}"' for c in chunks])
    lua_code = f"""local _0x{xor_seed:02x}={xor_seed};local _0xb64={lua_concat};getgenv().Key=_0xb64;"""
    return lua_code

# ================= DISCORD CONFIG SYSTEM =================
# Guild ที่ใช้เก็บ config channels
CONFIG_GUILD_ID = 1480398927722447010
CONFIG_CATEGORY_NAME = "📁 KyxHub Configs"

# ชื่อ channel แต่ละ config
CONFIG_CHANNELS = {
    "keyDatabase":      "keydatabase",       # JSON ข้อมูล Key ทั้งหมด
    "warnings":         "warnings",          # JSON ประวัติ Warn
    "notify_channel":   "notify-channel",    # channel_id แจ้งเตือน Get Script
    "ticket_config":    "ticket-config",     # JSON category+log ของ Ticket
    "appeals":          "appeals",           # JSON คำร้องอุทธรณ์
    "appeal_channel":   "appeal-channel",    # channel_id รับคำร้อง
    "admin_roles":      "admin-roles",       # JSON role admin แต่ละ guild
    "expiry_channel":   "expiry-channel",    # channel_id แจ้งเตือน Key หมดอายุ
    "script_config":    "script-config",     # Lua config script
    "script_image":     "script-image",      # รูปใน /script embed (แนบไฟล์/URL)
}

# ─── In-memory cache (โหลดครั้งแรกตอน on_ready แล้วเก็บไว้) ───
_cache: dict = {k: None for k in CONFIG_CHANNELS}

def _get_cfg_guild() -> discord.Guild | None:
    return bot.get_guild(CONFIG_GUILD_ID)

async def _get_cfg_guild_async() -> discord.Guild:
    g = bot.get_guild(CONFIG_GUILD_ID)
    if not g:
        g = await bot.fetch_guild(CONFIG_GUILD_ID)
    return g

async def get_config_category(guild: discord.Guild) -> discord.CategoryChannel:
    for cat in guild.categories:
        if cat.name == CONFIG_CATEGORY_NAME:
            return cat
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    cat = await guild.create_category(CONFIG_CATEGORY_NAME, overwrites=overwrites)
    print(f"[Config] สร้าง category '{CONFIG_CATEGORY_NAME}' แล้ว")
    return cat

async def get_or_create_config_channel(guild: discord.Guild, channel_key: str) -> discord.TextChannel:
    cat = await get_config_category(guild)
    ch_name = CONFIG_CHANNELS[channel_key]
    for ch in cat.text_channels:
        if ch.name == ch_name:
            return ch
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    ch = await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
    print(f"[Config] สร้าง channel #{ch_name} แล้ว")
    return ch

# ─── อ่าน / เขียน JSON จาก Discord channel ───

# เก็บ message ID ของแต่ละ channel เพื่อ edit แทนส่งใหม่
_config_msg_ids: dict[str, int] = {}

async def _get_or_find_bot_msg(ch: discord.TextChannel, channel_key: str) -> discord.Message | None:
    """หาข้อความของบอทเท่านั้นใน channel (จาก cache หรือ history)"""
    msg_id = _config_msg_ids.get(channel_key)
    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            if msg.author == bot.user:
                return msg
        except Exception:
            pass
        _config_msg_ids.pop(channel_key, None)
    # ค้นจาก history — เอาเฉพาะข้อความของบอทเท่านั้น
    async for msg in ch.history(limit=50):
        if msg.author == bot.user and msg.content.strip():
            _config_msg_ids[channel_key] = msg.id
            return msg
    return None

async def _discord_read_json(channel_key: str, default):
    """อ่าน JSON จากทุกข้อความในห้อง (บอทหรือคนก็ได้) รวมถึง attachment"""
    try:
        guild = await _get_cfg_guild_async()
        ch = await get_or_create_config_channel(guild, channel_key)
        async for msg in ch.history(limit=30):
            # ลองอ่านจาก attachment ก่อน (message.txt / .json)
            if msg.attachments:
                for att in msg.attachments:
                    try:
                        import urllib.request
                        with urllib.request.urlopen(att.url) as resp:
                            raw = resp.read().decode("utf-8")
                        return json.loads(raw)
                    except Exception:
                        continue
            # ลองอ่านจาก content
            content = msg.content.strip()
            if content:
                try:
                    return json.loads(content)
                except Exception:
                    continue
        return default
    except Exception as e:
        print(f"[Config] _discord_read_json({channel_key}) ERROR: {e}")
        return default

async def _discord_read_value(channel_key: str) -> str | None:
    """อ่านค่า plain text จากทุกข้อความในห้อง เอาอันล่าสุดที่มีข้อความ"""
    try:
        guild = await _get_cfg_guild_async()
        ch = await get_or_create_config_channel(guild, channel_key)
        async for msg in ch.history(limit=30):
            content = msg.content.strip()
            if content:
                return content
        return None
    except Exception as e:
        print(f"[Config] _discord_read_value({channel_key}) ERROR: {e}")
        return None

async def _purge_channel(ch: discord.TextChannel):
    """ลบข้อความทั้งหมดในห้อง รวมถึงข้อความที่มี attachment"""
    try:
        # bulk delete (ได้แค่ข้อความไม่เกิน 14 วัน)
        deleted = await ch.purge(limit=100, bulk=True)
        # ถ้ายังมีข้อความเก่าค้างอยู่ ลบทีละอัน
        async for msg in ch.history(limit=50):
            try:
                await msg.delete()
            except Exception:
                pass
    except Exception as e:
        print(f"[Config] _purge_channel ERROR: {e}")

async def _discord_write_json(channel_key: str, data) -> bool:
    """เขียน JSON — edit ข้อความของบอทถ้ามี ถ้าไม่มีให้ purge ห้องแล้วส่งใหม่"""
    try:
        guild = await _get_cfg_guild_async()
        ch = await get_or_create_config_channel(guild, channel_key)
        content = json.dumps(data, ensure_ascii=False, indent=2)

        bot_msg = await _get_or_find_bot_msg(ch, channel_key)

        if len(content) > 1900:
            import io
            # ข้อมูลใหญ่เกิน — purge ห้องแล้วส่งเป็นไฟล์
            await _purge_channel(ch)
            _config_msg_ids.pop(channel_key, None)
            file = discord.File(io.BytesIO(content.encode("utf-8")), filename=f"{channel_key}.json")
            msg = await ch.send(file=file)
            _config_msg_ids[channel_key] = msg.id
            return True

        if bot_msg:
            # edit ข้อความของบอท
            await bot_msg.edit(content=content)
        else:
            # purge ทุกข้อความแล้วส่งใหม่
            await _purge_channel(ch)
            _config_msg_ids.pop(channel_key, None)
            msg = await ch.send(content)
            _config_msg_ids[channel_key] = msg.id
            try:
                await msg.pin()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[Config] _discord_write_json({channel_key}) ERROR: {e}")
        return False

async def _discord_write_value(channel_key: str, value: str) -> bool:
    """เขียนค่า plain text — edit ข้อความของบอทถ้ามี ถ้าไม่มีให้ purge แล้วส่งใหม่"""
    try:
        guild = await _get_cfg_guild_async()
        ch = await get_or_create_config_channel(guild, channel_key)
        bot_msg = await _get_or_find_bot_msg(ch, channel_key)
        if bot_msg:
            await bot_msg.edit(content=value)
        else:
            await _purge_channel(ch)
            _config_msg_ids.pop(channel_key, None)
            msg = await ch.send(value)
            _config_msg_ids[channel_key] = msg.id
            try:
                await msg.pin()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[Config] _discord_write_value({channel_key}) ERROR: {e}")
        return False

# ─── ฟังก์ชัน DB (keyDatabase) ───

async def load_db_async() -> dict:
    data = await _discord_read_json("keyDatabase", None)
    if data is None:
        data = {"used_keys": {}, "keys_7_days": [], "keys_30_days": [], "blacklist": {}}
    if "blacklist" not in data:
        data["blacklist"] = {}
    if isinstance(data["blacklist"], list):
        data["blacklist"] = {uid: {"reason": "-", "by": "system", "at": "-"} for uid in data["blacklist"]}
    return data

async def save_db_async(data: dict):
    await _discord_write_json("keyDatabase", data)

# ─── ฟังก์ชัน Warnings ───

async def load_warnings_async() -> dict:
    return await _discord_read_json("warnings", {})

async def save_warnings_async(data: dict):
    await _discord_write_json("warnings", data)

# ─── ฟังก์ชัน Notify Channel ───

async def load_notify_channel_async() -> int | None:
    val = await _discord_read_value("notify_channel")
    try:
        return int(val) if val else None
    except Exception:
        return None

async def save_notify_channel_async(channel_id: int):
    await _discord_write_value("notify_channel", str(channel_id))

# ─── ฟังก์ชัน Ticket Config ───

async def load_ticket_config_async() -> dict:
    return await _discord_read_json("ticket_config", {"category_id": None, "log_channel_id": None})

async def save_ticket_config_async(data: dict):
    await _discord_write_json("ticket_config", data)

# ─── ฟังก์ชัน Appeals ───

async def load_appeals_async() -> dict:
    return await _discord_read_json("appeals", {})

async def save_appeals_async(data: dict):
    await _discord_write_json("appeals", data)

# ─── ฟังก์ชัน Appeal Channel ───

async def load_appeal_channel_async() -> int | None:
    val = await _discord_read_value("appeal_channel")
    try:
        return int(val) if val else None
    except Exception:
        return None

async def save_appeal_channel_async(channel_id: int):
    await _discord_write_value("appeal_channel", str(channel_id))

# ─── ฟังก์ชัน Admin Roles ───

async def load_admin_roles_async() -> dict:
    return await _discord_read_json("admin_roles", {})

async def save_admin_roles_async(data: dict):
    await _discord_write_json("admin_roles", data)

# ─── ฟังก์ชัน Expiry Channel ───

async def load_expiry_channel_async() -> int | None:
    val = await _discord_read_value("expiry_channel")
    try:
        return int(val) if val else None
    except Exception:
        return None

async def save_expiry_channel_async(channel_id: int):
    await _discord_write_value("expiry_channel", str(channel_id))

# ─── ฟังก์ชัน Script Config (Lua) ───

_cached_script_config: str | None = None

async def get_script_config() -> str:
    global _cached_script_config
    try:
        val = await _discord_read_value("script_config")
        if val:
            _cached_script_config = val
            return val
    except Exception as e:
        print(f"[Config] get_script_config ERROR: {e}")
    if _cached_script_config:
        return _cached_script_config
    return "-- กรุณาส่ง script config ใน channel #script-config --"

# ─── ฟังก์ชัน Script Image ───

_cached_script_image_url: str | None = None

async def get_script_image_url() -> str | None:
    """ดึง URL รูปสำหรับ /script embed จาก #script-image"""
    global _cached_script_image_url
    try:
        guild = await _get_cfg_guild_async()
        ch = await get_or_create_config_channel(guild, "script_image")
        async for msg in ch.history(limit=20):
            # เช็กจาก attachment ที่แนบ
            if msg.attachments:
                _cached_script_image_url = msg.attachments[0].url
                return _cached_script_image_url
            # หรือจาก URL ที่พิมพ์ใน message
            if msg.content.strip().startswith("http"):
                _cached_script_image_url = msg.content.strip()
                return _cached_script_image_url
        return _cached_script_image_url  # fallback cache
    except Exception as e:
        print(f"[Config] get_script_image_url ERROR: {e}")
        return _cached_script_image_url

# REMOVE_ALL_SCRIPT ดึงจาก Discord แบบ async ผ่าน get_script_config()

def build_script(start_time_str: str, end_time_str: str, key_value: str) -> str:
    xor_seed = sum(ord(c) for c in key_value) % 256
    obf = obfuscate_key(start_time_str, end_time_str, key_value)
    script = f"""{obf}getgenv().KeySeed={xor_seed};
loadstring(game:HttpGet('{SCRIPT_URL}'))()"""
    return script

async def build_full_script_async(start_time_str: str, end_time_str: str, key_value: str) -> str:
    remove_all = await get_script_config()
    return f"""{remove_all}
{build_script(start_time_str, end_time_str, key_value)}"""


def load_notify_channel() -> int | None:
    return _MEM["notify_channel"]

def save_notify_channel(channel_id: int):
    _MEM["notify_channel"] = channel_id
    _schedule_sync("notify_channel")


async def send_notify(user: discord.User, key: str, end_str: str, is_new: bool):
    channel_id = load_notify_channel()
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    end = parse_time(end_str)
    action = "✅ ใช้ Key ใหม่" if is_new else "🔄 Get Script ซ้ำ"

    def make_embed():
        e = discord.Embed(
            title="🔔 มีคนกด Get Script",
            color=0x00ff99 if is_new else 0xffaa00
        )
        e.add_field(name="สถานะ", value=action, inline=False)
        e.add_field(name="ผู้ใช้", value=f"{user.mention} (`{user.id}`)", inline=True)
        e.add_field(name="Key", value=f"`{key}`", inline=True)
        e.add_field(name="หมดอายุ", value=end_str, inline=True)
        e.add_field(name="เวลาคงเหลือ", value=format_time(end), inline=True)
        e.set_footer(text=f"KyxHub • อัพเดทล่าสุด {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        return e

    msg = await channel.send(embed=make_embed())

    async def live_update():
        while True:
            await asyncio.sleep(60)
            if datetime.now() > end:
                break
            try:
                await msg.edit(embed=make_embed())
            except Exception:
                break

    asyncio.create_task(live_update())


GUILD_ID = 1445387021920112792
GUILD_ID_2 = 1416775454643191820

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= MODAL KEY =================

class KeyModal(discord.ui.Modal, title="ใส่ Key"):

    key = discord.ui.TextInput(label="กรอก Key")

    async def on_submit(self, interaction: discord.Interaction):

        key_input = self.key.value
        db = load_db()

        if not is_bot_open():
            await interaction.response.send_message(
                "🕐 บอทปิดให้บริการแล้ว\n⏰ เปิดให้บริการ **12:00 น.** — **19:30 น.**",
                ephemeral=True
            )
            return

        if is_maintenance(interaction):
            await deny_maintenance(interaction)
            return

        if not check_rate_limit(str(interaction.user.id)):
            await interaction.response.send_message(
                f"⏳ คุณกดเร็วเกินไป! รอ **{RATE_LIMIT_WINDOW} วินาที** แล้วลองใหม่",
                ephemeral=True
            )
            return

        if str(interaction.user.id) in db["blacklist"]:
            bl = db["blacklist"][str(interaction.user.id)]
            reason_text = bl.get("reason", "-")
            await interaction.response.send_message(
                f"🚫 คุณถูกแบนจากการใช้งาน Key\n📋 เหตุผล: **{reason_text}**",
                ephemeral=True
            )
            return

        if key_input in db["used_keys"]:

            data = db["used_keys"][key_input]

            if data["ผู้ใช้"] != str(interaction.user.id):
                await interaction.response.send_message("❌ คีย์นี้ถูกใช้แล้ว", ephemeral=True)
                return

            end = parse_time(data["หมดเวลา"])

            if datetime.now() > end:
                await interaction.response.send_message("❌ คีย์หมดอายุ", ephemeral=True)
                return

            script = build_script(data["เริ่มใช้"], data["หมดเวลา"], key_input)

            def make_msg_old():
                return f"⏳ เวลาคงเหลือ: {format_time(end)}\n\n```lua\n{script}\n```"

            await interaction.response.send_message(make_msg_old(), ephemeral=True)
            await send_notify(interaction.user, key_input, data["หมดเวลา"], is_new=False)

            async def live_old():
                while True:
                    await asyncio.sleep(60)
                    if datetime.now() > end:
                        break
                    try:
                        await interaction.edit_original_response(content=make_msg_old())
                    except Exception:
                        break
            asyncio.create_task(live_old())
            return

        if key_input in db["keys_7_days"]:
            start = datetime.now()
            end = start + timedelta(days=7)
            db["keys_7_days"].remove(key_input)

        elif key_input in db["keys_30_days"]:
            start = datetime.now()
            end = start + timedelta(days=30)
            db["keys_30_days"].remove(key_input)

        else:
            await interaction.response.send_message("❌ ไม่พบคีย์", ephemeral=True)
            return

        start_str = start.strftime("%d/%m/%Y/%H.%M")

        db["used_keys"][key_input] = {
            "ผู้ใช้": str(interaction.user.id),
            "เริ่มใช้": start_str,
            "หมดเวลา": end.strftime("%d/%m/%Y/%H.%M"),
            "คีย์ที่ใช้": key_input
        }

        save_db(db)

        script = build_script(start_str, end.strftime("%d/%m/%Y/%H.%M"), key_input)

        def make_msg_new():
            return f"✅ Key ถูกต้อง\n⏳ เวลาคงเหลือ: {format_time(end)}\n\n```lua\n{script}\n```"

        await interaction.response.send_message(make_msg_new(), ephemeral=True)
        await send_notify(interaction.user, key_input, end.strftime("%d/%m/%Y/%H.%M"), is_new=True)

        async def live_new():
            while True:
                await asyncio.sleep(60)
                if datetime.now() > end:
                    break
                try:
                    await interaction.edit_original_response(content=make_msg_new())
                except Exception:
                    break
        asyncio.create_task(live_new())

# ================= HELPER =================

def find_key_by_user(user_id: str, db: dict):
    for key, data in db["used_keys"].items():
        if data["ผู้ใช้"] == user_id:
            return key, data
    return None, None

# ================= CHECK TIME =================

async def handle_check_time(interaction: discord.Interaction):
    if not is_bot_open():
        await interaction.response.send_message(
            "🕐 บอทปิดให้บริการแล้ว\n⏰ เปิดให้บริการ **12:00 น.** — **19:30 น.**",
            ephemeral=True
        )
        return
    if is_maintenance(interaction):
        await deny_maintenance(interaction)
        return
    db = load_db()
    user_id = str(interaction.user.id)

    key_input, data = find_key_by_user(user_id, db)

    if not data:
        await interaction.response.send_message("❌ ยังไม่เคยใช้ Key หรือไม่พบข้อมูล", ephemeral=True)
        return

    end = parse_time(data["หมดเวลา"])

    if datetime.now() > end:
        await interaction.response.send_message("❌ คีย์หมดอายุ", ephemeral=True)
        return

    def make_msg():
        return (f"👤 ผู้ใช้: {data['ผู้ใช้']}\n"
                f"📅 เริ่มใช้: {data['เริ่มใช้']}\n"
                f"⏰ หมดเวลา: {data['หมดเวลา']}\n"
                f"🔑 คีย์: {data['คีย์ที่ใช้']}\n\n"
                f"⏳ เวลาคงเหลือ: {format_time(end)}")

    await interaction.response.send_message(make_msg(), ephemeral=True)

    async def live_update():
        while True:
            await asyncio.sleep(60)
            if datetime.now() > end:
                break
            try:
                await interaction.edit_original_response(content=make_msg())
            except Exception:
                break

    asyncio.create_task(live_update())

# ================= REMOVE ALL SCRIPTS =================

async def handle_remove_scripts(interaction: discord.Interaction):
    if not is_bot_open():
        await interaction.response.send_message(
            "🕐 บอทปิดให้บริการแล้ว\n⏰ เปิดให้บริการ **12:00 น.** — **19:30 น.**",
            ephemeral=True
        )
        return
    if is_maintenance(interaction):
        await deny_maintenance(interaction)
        return
    db = load_db()
    user_id = str(interaction.user.id)

    key_input, data = find_key_by_user(user_id, db)

    if not data:
        await interaction.response.send_message("❌ ยังไม่เคยใช้ Key หรือไม่พบข้อมูล", ephemeral=True)
        return

    end = parse_time(data["หมดเวลา"])

    if datetime.now() > end:
        await interaction.response.send_message("❌ คีย์หมดอายุ", ephemeral=True)
        return

    full_script = await build_full_script_async(data["เริ่มใช้"], data["หมดเวลา"], key_input)

    import io
    file = discord.File(
        io.BytesIO(full_script.encode("utf-8")),
        filename="KyxHub_Script.lua"
    )

    def make_msg():
        return f"📤 **Send Script**\n⏳ เวลาคงเหลือ: {format_time(end)}\n📄 กด Download แล้ว copy ไปวางใน executor ได้เลย"

    await interaction.response.send_message(make_msg(), file=file, ephemeral=True)

    async def live_update():
        while True:
            await asyncio.sleep(60)
            if datetime.now() > end:
                break
            try:
                await interaction.edit_original_response(content=make_msg())
            except Exception:
                break

    asyncio.create_task(live_update())


class ScriptView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

        buy = discord.ui.Button(
            label="Buy Key",
            style=discord.ButtonStyle.link,
            url="https://apexstore.4tunez.shop/categories/7eea7be2"
        )
        self.add_item(buy)

    @discord.ui.button(label="Get Script", style=discord.ButtonStyle.primary, custom_id="get_script_btn")
    async def get_script(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

    @discord.ui.button(label="Check Time", style=discord.ButtonStyle.secondary, custom_id="check_time_btn")
    async def check_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_check_time(interaction)

    @discord.ui.button(label="Send Script", style=discord.ButtonStyle.danger, custom_id="remove_scripts_btn")
    async def remove_scripts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_remove_scripts(interaction)

# ================= LISTKEYS EMBED =================

def make_listkeys_embed() -> discord.Embed:
    db = load_db()
    now = datetime.now()
    active = []
    expired = []

    for key, data in db["used_keys"].items():
        try:
            end = parse_time(data["หมดเวลา"])
        except Exception:
            continue
        if now > end:
            expired.append((key, data, end))
        else:
            active.append((key, data, end))

    active.sort(key=lambda x: x[2])

    e = discord.Embed(
        title="🗝️ KyxHub — Key List",
        color=0x2f3136,
        timestamp=datetime.now()
    )
    e.set_footer(text=f"KyxHub • อัพเดทล่าสุด")

    unused_7  = db["keys_7_days"]
    unused_30 = db["keys_30_days"]
    e.add_field(
        name="📦 Key ที่ยังไม่ได้ใช้",
        value=(
            f"7 วัน: **{len(unused_7)}** key\n"
            f"30 วัน: **{len(unused_30)}** key\n"
            f"*(กดปุ่มด้านล่างเพื่อดูรายละเอียดทาง DM)*"
        ),
        inline=False
    )

    if not active:
        e.description = "❌ ไม่มี Key ที่ active อยู่"
    else:
        e.description = f"✅ Active Keys: **{len(active)}** | ❌ หมดอายุ: **{len(expired)}**"
        lines = []
        for key, data, end in active:
            user_id = data["ผู้ใช้"]
            lines.append(
                f"👤 <@{user_id}>\n"
                f"🔑 `{key}`\n"
                f"⏳ {format_time(end)} (หมด {data['หมดเวลา']})\n"
            )
        chunk = ""
        field_num = 1
        for line in lines:
            if len(chunk) + len(line) > 1020:
                e.add_field(name=f"Active Keys ({field_num})", value=chunk, inline=False)
                chunk = ""
                field_num += 1
            chunk += line + "\n"
        if chunk:
            e.add_field(name=f"Active Keys ({field_num})", value=chunk, inline=False)

    return e


class ListKeysView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, custom_id="listkeys_refresh_btn")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await deny_not_owner(interaction)
            return
        await interaction.response.edit_message(embed=make_listkeys_embed(), view=self)

    @discord.ui.button(label="📋 Key 7 วัน", style=discord.ButtonStyle.primary, custom_id="listkeys_7day_btn")
    async def show_7day(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await deny_not_owner(interaction)
            return
        db = load_db()
        keys = db["keys_7_days"]
        await interaction.response.defer(ephemeral=True)
        if not keys:
            await interaction.followup.send("❌ ไม่มี Key 7 วันเหลืออยู่", ephemeral=True)
            return
        chunks = []
        chunk = ""
        for k in keys:
            line = f"`{k}`\n"
            if len(chunk) + len(line) > 1900:
                chunks.append(chunk)
                chunk = ""
            chunk += line
        if chunk:
            chunks.append(chunk)
        try:
            await interaction.user.send(f"📋 **Key 7 วัน ({len(keys)} key)**")
            for c in chunks:
                await interaction.user.send(c)
            await interaction.followup.send("✅ ส่ง Key 7 วัน ทาง DM แล้ว", ephemeral=True)
        except Exception:
            await interaction.followup.send("❌ ไม่สามารถส่ง DM ได้ กรุณาเปิด DM ก่อน", ephemeral=True)

    @discord.ui.button(label="📋 Key 30 วัน", style=discord.ButtonStyle.success, custom_id="listkeys_30day_btn")
    async def show_30day(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await deny_not_owner(interaction)
            return
        db = load_db()
        keys = db["keys_30_days"]
        await interaction.response.defer(ephemeral=True)
        if not keys:
            await interaction.followup.send("❌ ไม่มี Key 30 วันเหลืออยู่", ephemeral=True)
            return
        chunks = []
        chunk = ""
        for k in keys:
            line = f"`{k}`\n"
            if len(chunk) + len(line) > 1900:
                chunks.append(chunk)
                chunk = ""
            chunk += line
        if chunk:
            chunks.append(chunk)
        try:
            await interaction.user.send(f"📋 **Key 30 วัน ({len(keys)} key)**")
            for c in chunks:
                await interaction.user.send(c)
            await interaction.followup.send("✅ ส่ง Key 30 วัน ทาง DM แล้ว", ephemeral=True)
        except Exception:
            await interaction.followup.send("❌ ไม่สามารถส่ง DM ได้ กรุณาเปิด DM ก่อน", ephemeral=True)

# ================= READY =================

auto_listkeys_message: discord.Message | None = None

@bot.event
async def on_ready():
    global auto_listkeys_message

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)

    guild2 = discord.Object(id=GUILD_ID_2)
    bot.tree.copy_global_to(guild=guild2)
    await bot.tree.sync(guild=guild2)
    bot.add_view(ScriptView())
    bot.add_view(ListKeysView())
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())
    bot.add_view(AppealView(has_warn=True, has_blacklist=True))
    print(f"Bot Online | Synced {len(synced)} commands: {[c.name for c in synced]}")

    # ================= สร้าง Config Category + Channels และโหลดข้อมูลเข้า RAM =================
    try:
        cfg_guild = bot.get_guild(CONFIG_GUILD_ID)
        if cfg_guild:
            for key in CONFIG_CHANNELS:
                ch = await get_or_create_config_channel(cfg_guild, key)
                # cache message ID ของแต่ละห้องไว้เลยตอน startup
                msg = await _get_or_find_bot_msg(ch, key)
                if msg:
                    _config_msg_ids[key] = msg.id
            print(f"[Config] เตรียม category '{CONFIG_CATEGORY_NAME}' และ channels ทั้งหมดเรียบร้อย")

            # ── โหลดข้อมูลทั้งหมดจาก Discord เข้า _MEM ──
            print("[Config] กำลังโหลดข้อมูลจาก Discord...")

            db_data = await _discord_read_json("keyDatabase", None)
            if db_data:
                if "blacklist" not in db_data:
                    db_data["blacklist"] = {}
                _MEM["db"] = db_data
                print(f"[Config] โหลด keyDatabase ✅ ({len(db_data.get('used_keys', {}))} used keys)")
            else:
                # fallback จากไฟล์ถ้ามี
                try:
                    with open(DB_FILE, encoding="utf-8") as f:
                        _MEM["db"] = json.load(f)
                    print("[Config] โหลด keyDatabase จากไฟล์ backup ✅")
                except Exception:
                    print("[Config] keyDatabase: ใช้ค่าเริ่มต้น")

            warns_data = await _discord_read_json("warnings", None)
            if warns_data is not None:
                _MEM["warnings"] = warns_data
                print(f"[Config] โหลด warnings ✅ ({len(warns_data)} users)")
            else:
                try:
                    with open(WARNINGS_FILE, encoding="utf-8") as f:
                        _MEM["warnings"] = json.load(f)
                    print("[Config] โหลด warnings จากไฟล์ backup ✅")
                except Exception:
                    print("[Config] warnings: ใช้ค่าเริ่มต้น")

            for mem_key, ch_key, label in [
                ("ticket_config",  "ticket_config",  "ticket_config"),
                ("appeals",        "appeals",         "appeals"),
                ("admin_roles",    "admin_roles",     "admin_roles"),
            ]:
                val = await _discord_read_json(ch_key, None)
                if val is not None:
                    _MEM[mem_key] = val
                    print(f"[Config] โหลด {label} ✅")

            for mem_key, ch_key, label in [
                ("notify_channel", "notify_channel", "notify_channel"),
                ("appeal_channel", "appeal_channel", "appeal_channel"),
                ("expiry_channel", "expiry_channel", "expiry_channel"),
            ]:
                val = await _discord_read_value(ch_key)
                if val:
                    try:
                        _MEM[mem_key] = int(val)
                        print(f"[Config] โหลด {label} ✅ ({val})")
                    except Exception:
                        pass

            # โหลด script image cache
            await get_script_image_url()

            print("[Config] โหลดข้อมูลทั้งหมดเสร็จแล้ว ✅")
        else:
            print(f"[Config] ไม่พบ guild {CONFIG_GUILD_ID} — ตรวจสอบว่าบอทอยู่ใน guild นั้น")
    except Exception as e:
        print(f"[Config] on_ready ERROR: {e}")

    check_expiry_loop.start()
    check_key_expiry_announce.start()

    channel = bot.get_channel(AUTO_SEND_CHANNEL_ID)
    if channel:
        existing_msg = None
        try:
            async for msg in channel.history(limit=50):
                if msg.author == bot.user and msg.embeds:
                    existing_msg = msg
                    break
        except Exception:
            pass

        if existing_msg:
            auto_listkeys_message = existing_msg
            try:
                await auto_listkeys_message.edit(embed=make_listkeys_embed(), view=ListKeysView())
            except Exception:
                pass
            print(f"[AutoListKeys] Reused existing message in channel {AUTO_SEND_CHANNEL_ID}")
        else:
            auto_listkeys_message = await channel.send(embed=make_listkeys_embed(), view=ListKeysView())
            print(f"[AutoListKeys] Sent new message to channel {AUTO_SEND_CHANNEL_ID}")

        asyncio.create_task(auto_listkeys_live_update())
    else:
        print(f"[AutoListKeys] ไม่พบห้อง {AUTO_SEND_CHANNEL_ID} — ตรวจสอบว่าบอทอยู่ใน guild และมีสิทธิ์ส่งข้อความ")


async def auto_listkeys_live_update():
    global auto_listkeys_message
    _last_db_snapshot = None

    while True:
        await asyncio.sleep(5)
        try:
            current_snapshot = json.dumps(_MEM["db"], sort_keys=True)
            now = datetime.now()
            if current_snapshot != _last_db_snapshot or int(now.second) < 5:
                _last_db_snapshot = current_snapshot
                if auto_listkeys_message:
                    await auto_listkeys_message.edit(embed=make_listkeys_embed(), view=ListKeysView())
        except Exception as e:
            print(f"[AutoListKeys live_update ERROR] {e}")
            break

# ================= EXPIRY WARNING LOOP =================

WARNED_1DAY  = set()
WARNED_1HOUR = set()
SHOP_URL = "https://apexstore.4tunez.shop/categories/7eea7be2"

@tasks.loop(minutes=30)
async def check_expiry_loop():
    db = load_db()
    now = datetime.now()

    for key, data in db["used_keys"].items():
        user_id = data["ผู้ใช้"]
        try:
            end = parse_time(data["หมดเวลา"])
        except Exception:
            continue

        remain = end - now
        total_seconds = remain.total_seconds()

        if total_seconds <= 0:
            continue

        try:
            user = await bot.fetch_user(int(user_id))
        except Exception:
            continue

        if total_seconds <= 86400 and user_id not in WARNED_1DAY:
            WARNED_1DAY.add(user_id)
            embed = discord.Embed(
                title="⚠️ Key ของคุณใกล้หมดอายุแล้ว!",
                description=f"Key ของคุณจะหมดอายุใน **1 วัน**\nหมดเวลา: `{data['หมดเวลา']}`",
                color=0xffaa00
            )
            embed.add_field(
                name="🛒 ต่ออายุได้เลยที่",
                value=f"[คลิกซื้อ Key ใหม่]({SHOP_URL})",
                inline=False
            )
            embed.set_footer(text="KyxHub • แจ้งเตือนอัตโนมัติ")
            try:
                await user.send(embed=embed)
            except Exception:
                pass

        elif total_seconds <= 3600 and user_id not in WARNED_1HOUR:
            WARNED_1HOUR.add(user_id)
            embed = discord.Embed(
                title="🚨 Key ของคุณจะหมดใน 1 ชั่วโมง!",
                description=f"⏰ เหลือเวลาอีกไม่ถึง **1 ชั่วโมง**!\nหมดเวลา: `{data['หมดเวลา']}`",
                color=0xff3333
            )
            embed.add_field(
                name="🛒 ซื้อ Key ใหม่ได้เลยที่",
                value=f"[{SHOP_URL}]({SHOP_URL})",
                inline=False
            )
            embed.set_footer(text="KyxHub • แจ้งเตือนอัตโนมัติ")
            try:
                await user.send(embed=embed)
            except Exception:
                pass

@check_expiry_loop.before_loop
async def before_check():
    await bot.wait_until_ready()

# ================= SLASH COMMANDS =================

@bot.tree.command(name="script", description="รับ Script")
async def script(interaction: discord.Interaction):
    embed = discord.Embed(
        title="KyxHub",
        description=(
            "กดปุ่มด้านล่างเพื่อใช้งานรับสคริปต์\n"
            "ดู status ได้ที่ <#1445391749643112519>\n\n"
            "Kyx Hub Kaitun Configs\n"
            "ซื้อคีย์ได้ที่ https://apexstore.4tunez.shop/\n\n"
            "บอทปิดเวลา 19.30 น. เปิดเวลา 12.00 น."
        ),
        color=0x2f3136
    )

    # ── ดึงรูปจาก #script-image ──
    img_url = _cached_script_image_url or await get_script_image_url()
    if img_url:
        embed.set_image(url=img_url)
        await interaction.response.send_message(embed=embed, view=ScriptView())
    else:
        # fallback รูปจากไฟล์ local ถ้ามี
        try:
            file = discord.File(
                "zcdsvtgpt1rmy0cwn07b2xxkv8.png",
                filename="zcdsvtgpt1rmy0cwn07b2xxkv8.png"
            )
            embed.set_image(url="attachment://zcdsvtgpt1rmy0cwn07b2xxkv8.png")
            await interaction.response.send_message(embed=embed, view=ScriptView(), file=file)
        except Exception:
            await interaction.response.send_message(embed=embed, view=ScriptView())


@bot.tree.command(name="setch", description="ตั้งค่าห้องแจ้งเตือนเมื่อมีคนกด Get Script")
@app_commands.checks.has_permissions(administrator=True)
async def setch(interaction: discord.Interaction, channel: discord.TextChannel):
    save_notify_channel(channel.id)
    await interaction.response.send_message(
        f"✅ ตั้งค่าห้องแจ้งเตือนเป็น {channel.mention} แล้ว",
        ephemeral=True
    )

@setch.error
async def setch_error(interaction: discord.Interaction, error):
    await deny_not_admin(interaction)

# ================= HELP =================

class HelpCopyView(discord.ui.View):
    """View ที่มีปุ่ม Copy คำสั่งทั้งหมด"""

    def __init__(self, admin: bool, owner: bool):
        super().__init__(timeout=None)
        self.admin = admin
        self.owner = owner

    def build_copy_text(self) -> str:
        lines = [
            "📖 KyxHub — คำสั่งทั้งหมด",
            "",
            "🌐 คำสั่งทั่วไป",
            "/script — รับ Script พร้อมปุ่ม Get Script / Check Time / Send Script",
            "/profile — ดูข้อมูล Key ของตัวเอง พร้อม progress bar เวลาคงเหลือ",
            "/transferkey [@user] — โอน Key ของคุณให้ User อื่น",
            "/appeal — ยื่นอุทธรณ์คำร้อง Warn หรือ Blacklist",
            "/help — แสดงคำสั่งทั้งหมดและคำอธิบาย",
        ]
        if self.admin:
            lines += [
                "",
                "🔐 คำสั่ง Admin",
                "/announce [ข้อความ] — Broadcast DM ไปหาทุก User ที่มี Key Active",
                "/ticket [หัวข้อ] — ส่ง embed ให้ user กดเปิด Ticket",
                "/setticket [category] [#log] — ตั้งค่า Category และห้อง Log ของ Ticket",
                "/setch [#ห้อง] — ตั้งค่าห้องแจ้งเตือนเมื่อมีคนกด Get Script",
                "/setappealch [#ห้อง] — ตั้งค่าห้องรับคำร้องอุทธรณ์",
                "/setexpirychannel [#ห้อง] — ตั้งค่าห้องแจ้งเตือน Key หมดอายุ",
                "/setrole [@role] — ตั้งค่า Admin Role สำหรับเซิร์ฟเวอร์",
                "/blacklist [@user] — แบน User ไม่ให้ใช้ Key",
                "/unblacklist [@user] — ปลดแบน User",
                "/resetkey [@user] — Reset Key ของ User ให้ใช้ Key เดิมซ้ำได้อีกครั้ง",
                "/warn [@user] [เหตุผล] — ตักเตือน User (ครบ 3 ครั้ง = Auto-Blacklist)",
                "/warnings [@user] — ดูประวัติ Warn ของ User",
                "/clearwarnings [@user] — ล้างประวัติ Warn ของ User",
                "/pleasesendamessage [@user] — ส่งข้อความหา User ที่ระบุ",
                "/maintenance [on/off] — เปิด/ปิด Maintenance Mode",
                "/เช็กฟังชั่น — ตรวจสอบฟังก์ชันทั้งหมดว่ารันได้ไหม",
            ]
        if self.owner:
            lines += [
                "",
                "👑 คำสั่ง Owner เท่านั้น",
                "/listkeys — ดู Key ที่ Active ทั้งหมด พร้อมปุ่ม Refresh / ดู Key 7 วัน / 30 วัน",
                "/addkey [key] [7/30] — เพิ่ม Key เข้าระบบ",
                "/removekey [key] — ลบ Key ออกจากระบบ",
                "/keyinfo [key] — ดูข้อมูล Key ว่าใครใช้ / หมดเมื่อไหร่ / สถานะ",
                "/exportkeys — Export DB ทั้งหมดเป็นไฟล์ JSON ส่งทาง DM",
                "/extendkey [@user] [วัน] — ต่ออายุ Key ของ User",
                "/setscriptconfig — อัปโหลด Lua config script ใหม่",
                "/reloadconfig — โหลด Script Config ใหม่จาก Discord channel",
            ]
        return "\n".join(lines)

    @discord.ui.button(label="📋 Copy คำสั่งทั้งหมด", style=discord.ButtonStyle.secondary, custom_id="help_copy_btn")
    async def copy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = self.build_copy_text()
        await interaction.response.send_message(
            f"```\n{text}\n```",
            ephemeral=True
        )


@bot.tree.command(name="help", description="ดูคำสั่งทั้งหมดและคำอธิบาย")
async def help_cmd(interaction: discord.Interaction):
    admin = is_admin(interaction)
    owner = is_owner(interaction)

    e = discord.Embed(
        title="📖 KyxHub — คำสั่งทั้งหมด",
        color=0x5865f2
    )

    # ─── คำสั่งทั่วไป ───
    e.add_field(
        name="━━━━━━━━━━━━━━━━━━━━━━\n🌐 คำสั่งทั่วไป",
        value=(
            "`/script` — รับ Script พร้อมปุ่ม Get Script / Check Time / Send Script\n"
            "`/profile` — ดูข้อมูล Key ของตัวเอง พร้อม progress bar เวลาคงเหลือ\n"
            "`/transferkey [@user]` — โอน Key ของคุณให้ User อื่น\n"
            "`/appeal` — ยื่นอุทธรณ์คำร้อง Warn หรือ Blacklist\n"
            "`/help` — แสดงคำสั่งทั้งหมดและคำอธิบาย"
        ),
        inline=False
    )

    # ─── ปุ่มใน /script ───
    e.add_field(
        name="━━━━━━━━━━━━━━━━━━━━━━\n🔘 ปุ่มใน /script",
        value=(
            "**Get Script** — กรอก Key เพื่อรับ Script (ครั้งแรกจะเริ่มนับเวลา)\n"
            "**Check Time** — ตรวจสอบเวลาคงเหลือของ Key ที่ใช้อยู่\n"
            "**Send Script** — รับ Script ฉบับเต็ม พร้อม Config ทุกอย่าง\n"
            "**Buy Key** — ลิงก์ไปหน้าร้านซื้อ Key"
        ),
        inline=False
    )

    if admin:
        # ─── คำสั่ง Admin ───
        e.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━\n🔐 คำสั่ง Admin",
            value=(
                "`/announce [ข้อความ]` — Broadcast DM ไปหาทุก User ที่มี Key Active\n"
                "`/ticket [หัวข้อ]` — ส่ง embed ให้ user กดเปิด Ticket\n"
                "`/setticket [category] [#log]` — ตั้งค่า Category และห้อง Log ของ Ticket\n"
                "`/setch [#ห้อง]` — ตั้งค่าห้องแจ้งเตือนเมื่อมีคนกด Get Script\n"
                "`/setappealch [#ห้อง]` — ตั้งค่าห้องรับคำร้องอุทธรณ์\n"
                "`/setexpirychannel [#ห้อง]` — ตั้งค่าห้องแจ้งเตือน Key หมดอายุ\n"
                "`/setrole [@role]` — ตั้งค่า Admin Role สำหรับเซิร์ฟเวอร์นี้\n"
                "`/blacklist [@user]` — แบน User ไม่ให้ใช้ Key\n"
                "`/unblacklist [@user]` — ปลดแบน User\n"
                "`/resetkey [@user]` — Reset Key ของ User ให้ใช้ Key เดิมซ้ำได้อีกครั้ง\n"
                "`/warn [@user] [เหตุผล]` — ตักเตือน User (ครบ 3 ครั้ง = Auto-Blacklist)\n"
                "`/warnings [@user]` — ดูประวัติ Warn ของ User\n"
                "`/clearwarnings [@user]` — ล้างประวัติ Warn ของ User\n"
                "`/pleasesendamessage [@user]` — ส่งข้อความหา User ที่ระบุ\n"
                "`/maintenance [on/off]` — เปิด/ปิด Maintenance Mode\n"
                "`/เช็กฟังชั่น` — ตรวจสอบฟังก์ชันทั้งหมดว่ารันได้ไหม"
            ),
            inline=False
        )

    if owner:
        # ─── คำสั่ง Owner เท่านั้น ───
        e.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━\n👑 คำสั่ง Owner เท่านั้น",
            value=(
                "`/listkeys` — ดู Key ที่ Active ทั้งหมด พร้อมปุ่ม Refresh / ดู Key 7 วัน / 30 วัน\n"
                "`/addkey [key] [7/30]` — เพิ่ม Key เข้าระบบ\n"
                "`/removekey [key]` — ลบ Key ออกจากระบบ (ทั้ง unused และ used)\n"
                "`/keyinfo [key]` — ดูข้อมูล Key ว่าใครใช้ / หมดเมื่อไหร่ / สถานะ\n"
                "`/exportkeys` — Export DB ทั้งหมดเป็นไฟล์ JSON ส่งทาง DM\n"
                "`/extendkey [@user] [วัน]` — ต่ออายุ Key ของ User\n"
                "`/setscriptconfig` — อัปโหลด Lua config script ใหม่ไปเก็บใน Discord\n"
                "`/reloadconfig` — โหลด Script Config ใหม่จาก Discord channel\n"
                "`/deleteroomconfig` — ลบห้อง config ที่เลือกออก (บอทสร้างใหม่เมื่อ restart)"
            ),
            inline=False
        )

    if admin:
        e.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━\n🛡️ ความปลอดภัย",
            value=(
                f"• **Rate Limit** — กด Get Script ได้สูงสุด **{RATE_LIMIT_MAX} ครั้ง** ต่อ {RATE_LIMIT_WINDOW} วินาที\n"
                f"• **เวลาให้บริการ** — **12:00 น. — 19:30 น.** เท่านั้น\n"
                "• **Blacklist** — แบน User ที่ละเมิดได้ทันที"
            ),
            inline=False
        )
        e.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━\n🤖 ระบบอัตโนมัติ",
            value=(
                "• แจ้งเตือน DM เมื่อ Key เหลือ **1 วัน** และ **1 ชั่วโมง**\n"
                "• Live update embed ใน Key List channel ทุก 5 วินาที\n"
                "• แจ้งเตือน Log ในห้อง Notify ทุกครั้งที่มีคนกด Get Script\n"
                "• แจ้งเตือน Key หมดอายุอัตโนมัติในห้อง Expiry channel"
            ),
            inline=False
        )

    if owner:
        role_label = "👑 Owner"
    elif admin:
        role_label = "👑 Admin"
    else:
        role_label = "👤 แสดงเฉพาะคำสั่งทั่วไป"

    e.set_footer(text=f"KyxHub • {role_label}")
    await interaction.response.send_message(embed=e, view=HelpCopyView(admin=admin, owner=owner), ephemeral=True)

# ================= ADMIN =================

ADMIN_USER_IDS = {
    1150792949899210772,
    522014879373197312,
    1298644219925106803,
    1104393055454375957,
    1445634397222076418,
    1441041854463348841,
    1230496994879868941,
    1416775454643191820
}

# ================= ADMIN ROLE DB =================
# { "guild_id": role_id, ... }

def load_admin_roles() -> dict:
    return _MEM["admin_roles"]

def save_admin_roles(data: dict):
    _MEM["admin_roles"] = data
    _schedule_sync("admin_roles")


def is_admin(interaction: discord.Interaction) -> bool:
    # เจ้าของบอท / hardcoded admin IDs
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    # เช็ค role ที่ตั้งค่าไว้ในเซิร์ฟเวอร์นั้น
    if interaction.guild:
        roles = load_admin_roles()
        role_id = roles.get(str(interaction.guild_id))
        if role_id:
            return any(r.id == role_id for r in interaction.user.roles)
    return False

# ================= OWNER ONLY =================
BOT_OWNER_ID = 1298644219925106803  # เจ้าของบอทคนเดียวที่ใช้คำสั่ง key ได้

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID

async def deny_not_owner(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🔒 คำสั่งนี้ใช้ได้เฉพาะเจ้าของบอทเท่านั้น",
        ephemeral=True
    )

async def deny_not_admin(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้\n🆔 ID ของคุณ: `{interaction.user.id}`",
        ephemeral=True
    )

# ================= SETROLE =================

@bot.tree.command(name="setrole", description="ตั้งค่าบทบาท Admin สำหรับเซิร์ฟเวอร์นี้")
@app_commands.describe(role="บทบาทที่จะให้สิทธิ์ Admin")
async def setrole_cmd(interaction: discord.Interaction, role: discord.Role):
    # ต้องเป็น hardcoded admin หรือเจ้าของเซิร์ฟเวอร์เท่านั้นถึงจะตั้งได้
    is_owner = interaction.guild and interaction.guild.owner_id == interaction.user.id
    if interaction.user.id not in ADMIN_USER_IDS and not is_owner:
        await interaction.response.send_message(
            "❌ ต้องเป็นเจ้าของเซิร์ฟเวอร์หรือ Admin ระดับสูงเท่านั้นถึงจะตั้ง Admin Role ได้",
            ephemeral=True
        )
        return

    roles = load_admin_roles()
    roles[str(interaction.guild_id)] = role.id
    save_admin_roles(roles)

    embed = discord.Embed(
        title="✅ ตั้งค่า Admin Role แล้ว",
        description=f"บทบาท {role.mention} จะได้สิทธิ์ใช้คำสั่ง Admin ในเซิร์ฟเวอร์นี้",
        color=0x2ecc71
    )
    embed.add_field(name="Role", value=f"{role.mention} (`{role.id}`)", inline=True)
    embed.add_field(name="เซิร์ฟเวอร์", value=interaction.guild.name, inline=True)
    embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= LISTKEYS COMMAND =================

listkeys_message: discord.Message | None = None

@bot.tree.command(name="listkeys", description="ดู key ทั้งหมดที่ active (Owner only)")
async def listkeys(interaction: discord.Interaction):
    global listkeys_message

    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    if listkeys_message:
        try:
            await listkeys_message.delete()
        except Exception:
            pass
        listkeys_message = None

    await interaction.response.send_message(embed=make_listkeys_embed(), view=ListKeysView())
    listkeys_message = await interaction.original_response()

    async def live_update():
        _last_snap = None
        while True:
            await asyncio.sleep(5)
            try:
                snap = json.dumps(_MEM["db"], sort_keys=True)
                now = datetime.now()
                if snap != _last_snap or int(now.second) < 5:
                    _last_snap = snap
                    await interaction.edit_original_response(embed=make_listkeys_embed(), view=ListKeysView())
            except Exception:
                break

    asyncio.create_task(live_update())

@listkeys.error
async def listkeys_error(interaction: discord.Interaction, error):
    await deny_not_admin(interaction)

# ================= BLACKLIST =================

@bot.tree.command(name="blacklist", description="แบน user ไม่ให้ใช้ Key (Admin only)")
@app_commands.describe(user="User ที่จะแบน", reason="เหตุผล (ไม่บังคับ)")
async def blacklist_cmd(interaction: discord.Interaction, user: discord.Member, reason: str = "-"):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return
    db = load_db()
    user_id = str(user.id)
    if user_id in db["blacklist"]:
        bl = db["blacklist"][user_id]
        await interaction.response.send_message(
            f"⚠️ {user.mention} ถูก Blacklist อยู่แล้ว\n📋 เหตุผล: **{bl.get('reason', '-')}**",
            ephemeral=True
        )
        return

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db["blacklist"][user_id] = {
        "reason": reason,
        "by": str(interaction.user.id),
        "at": now_str
    }
    save_db(db)

    try:
        embed_dm = discord.Embed(
            title="🚫 คุณถูกแบนจากการใช้งาน Key",
            color=0xe74c3c
        )
        embed_dm.add_field(name="เหตุผล", value=reason, inline=False)
        embed_dm.add_field(name="แบนโดย", value=interaction.user.display_name, inline=True)
        embed_dm.add_field(name="วันที่", value=now_str, inline=True)
        embed_dm.add_field(
            name="📋 ต้องการอุทธรณ์?",
            value="พิมพ์ `/appeal` ในเซิร์ฟเวอร์เพื่อยื่นคำร้อง",
            inline=False
        )
        embed_dm.set_footer(text="KyxHub • หากคิดว่าผิดพลาดกรุณาใช้ /appeal")
        await user.send(embed=embed_dm)
    except Exception:
        pass

    embed = discord.Embed(title="🚫 Blacklist แล้ว", color=0xe74c3c)
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
    embed.add_field(name="เหตุผล", value=reason, inline=True)
    embed.set_footer(text=f"KyxHub • {now_str}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unblacklist", description="ปลดแบน user (Admin only)")
@app_commands.describe(user="User ที่จะปลดแบน", reason="เหตุผลการปลดแบน (ไม่บังคับ)")
async def unblacklist_cmd(interaction: discord.Interaction, user: discord.Member, reason: str = "-"):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return
    db = load_db()
    user_id = str(user.id)
    if user_id not in db["blacklist"]:
        await interaction.response.send_message(f"⚠️ {user.mention} ไม่ได้ถูก Blacklist", ephemeral=True)
        return

    old_reason = db["blacklist"][user_id].get("reason", "-")
    del db["blacklist"][user_id]
    save_db(db)

    try:
        embed_dm = discord.Embed(
            title="✅ คุณถูกปลดแบนแล้ว",
            description="คุณสามารถใช้งาน Key ได้อีกครั้งแล้ว",
            color=0x2ecc71
        )
        if reason != "-":
            embed_dm.add_field(name="หมายเหตุ", value=reason, inline=False)
        embed_dm.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        await user.send(embed=embed_dm)
    except Exception:
        pass

    embed = discord.Embed(title="✅ ปลดแบนแล้ว", color=0x2ecc71)
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
    embed.add_field(name="เหตุผลที่แบนเดิม", value=old_reason, inline=True)
    if reason != "-":
        embed.add_field(name="หมายเหตุการปลดแบน", value=reason, inline=False)
    embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= RESETKEY =================

@bot.tree.command(name="resetkey", description="Reset key ให้ user ใช้ใหม่ได้ (Admin only)")
async def resetkey_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    await interaction.response.defer(ephemeral=True)

    db = load_db()
    user_id = str(user.id)

    found_key = None
    for key, data in db["used_keys"].items():
        if data["ผู้ใช้"] == user_id:
            found_key = key
            break

    if not found_key:
        await interaction.followup.send(f"❌ ไม่พบ key ของ {user.mention}", ephemeral=True)
        return

    old_data = db["used_keys"].pop(found_key)
    try:
        end = parse_time(old_data["หมดเวลา"])
        diff = (end - parse_time(old_data["เริ่มใช้"])).days
        if diff >= 25:
            db["keys_30_days"].append(found_key)
        else:
            db["keys_7_days"].append(found_key)
    except Exception:
        db["keys_7_days"].append(found_key)

    save_db(db)

    try:
        embed = discord.Embed(
            title="🔄 Key ของคุณถูก Reset แล้ว",
            description=f"Key `{found_key}` ของคุณถูก Reset โดย Admin\nคุณสามารถกด **Get Script** เพื่อใช้ key เดิมซ้ำได้อีกครั้ง",
            color=0x00ccff
        )
        embed.add_field(name="🛒 หรือซื้อ Key ใหม่ได้ที่", value=f"[คลิกที่นี่]({SHOP_URL})", inline=False)
        embed.set_footer(text="KyxHub • แจ้งเตือนอัตโนมัติ")
        target = await bot.fetch_user(int(user_id))
        await target.send(embed=embed)
    except Exception:
        pass

    await interaction.followup.send(
        f"✅ Reset key `{found_key}` ของ {user.mention} แล้ว\nตอนนี้ user สามารถใช้ key เดิมซ้ำได้อีกครั้ง",
        ephemeral=True
    )

# ================= ADDKEY =================

@bot.tree.command(name="addkey", description="เพิ่ม Key เข้าระบบ (Owner only)")
@app_commands.describe(key="Key ที่จะเพิ่ม", duration="ระยะเวลา")
@app_commands.choices(duration=[
    app_commands.Choice(name="7 วัน", value="7"),
    app_commands.Choice(name="30 วัน", value="30"),
])
async def addkey_cmd(interaction: discord.Interaction, key: str, duration: str):
    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    db = load_db()
    key = key.strip()

    if key in db["used_keys"] or key in db["keys_7_days"] or key in db["keys_30_days"]:
        await interaction.response.send_message(f"⚠️ Key `{key}` มีในระบบอยู่แล้ว", ephemeral=True)
        return

    if duration == "7":
        db["keys_7_days"].append(key)
        label = "7 วัน"
    else:
        db["keys_30_days"].append(key)
        label = "30 วัน"

    save_db(db)
    await interaction.response.send_message(
        f"✅ เพิ่ม Key `{key}` ({label}) เข้าระบบแล้ว\n"
        f"📦 Key 7 วันทั้งหมด: **{len(db['keys_7_days'])}** | Key 30 วัน: **{len(db['keys_30_days'])}**",
        ephemeral=True
    )

# ================= REMOVEKEY =================

@bot.tree.command(name="removekey", description="ลบ Key ออกจากระบบ (Owner only)")
@app_commands.describe(key="Key ที่จะลบ")
async def removekey_cmd(interaction: discord.Interaction, key: str):
    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    db = load_db()
    key = key.strip()

    if key in db["keys_7_days"]:
        db["keys_7_days"].remove(key)
        save_db(db)
        await interaction.response.send_message(f"🗑️ ลบ Key `{key}` (7 วัน) ออกจากระบบแล้ว", ephemeral=True)

    elif key in db["keys_30_days"]:
        db["keys_30_days"].remove(key)
        save_db(db)
        await interaction.response.send_message(f"🗑️ ลบ Key `{key}` (30 วัน) ออกจากระบบแล้ว", ephemeral=True)

    elif key in db["used_keys"]:
        data = db["used_keys"].pop(key)
        save_db(db)
        await interaction.response.send_message(
            f"🗑️ ลบ Key `{key}` (ที่ <@{data['ผู้ใช้']}> ใช้อยู่) ออกจากระบบแล้ว",
            ephemeral=True
        )

    else:
        await interaction.response.send_message(f"❌ ไม่พบ Key `{key}` ในระบบ", ephemeral=True)

# ================= KEYINFO =================

@bot.tree.command(name="keyinfo", description="ดูข้อมูล Key (Owner only)")
@app_commands.describe(key="Key ที่ต้องการดู")
async def keyinfo_cmd(interaction: discord.Interaction, key: str):
    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    db = load_db()
    key = key.strip()

    if key in db["keys_7_days"]:
        e = discord.Embed(title="🔑 Key Info", color=0x3498db)
        e.add_field(name="Key", value=f"`{key}`", inline=False)
        e.add_field(name="สถานะ", value="📦 ยังไม่ได้ใช้", inline=True)
        e.add_field(name="ประเภท", value="7 วัน", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    elif key in db["keys_30_days"]:
        e = discord.Embed(title="🔑 Key Info", color=0x3498db)
        e.add_field(name="Key", value=f"`{key}`", inline=False)
        e.add_field(name="สถานะ", value="📦 ยังไม่ได้ใช้", inline=True)
        e.add_field(name="ประเภท", value="30 วัน", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    elif key in db["used_keys"]:
        data = db["used_keys"][key]
        try:
            end = parse_time(data["หมดเวลา"])
            expired = datetime.now() > end
            status = "❌ หมดอายุ" if expired else f"✅ Active — เหลือ {format_time(end)}"
            color  = 0xe74c3c if expired else 0x2ecc71
        except Exception:
            status = "⚠️ ไม่ทราบสถานะ"
            color  = 0x95a5a6

        e = discord.Embed(title="🔑 Key Info", color=color)
        e.add_field(name="Key", value=f"`{key}`", inline=False)
        e.add_field(name="สถานะ", value=status, inline=False)
        e.add_field(name="ผู้ใช้", value=f"<@{data['ผู้ใช้']}> (`{data['ผู้ใช้']}`)", inline=True)
        e.add_field(name="เริ่มใช้", value=data["เริ่มใช้"], inline=True)
        e.add_field(name="หมดเวลา", value=data["หมดเวลา"], inline=True)
        e.set_footer(text="KyxHub • Key Info")
        await interaction.response.send_message(embed=e, ephemeral=True)

    else:
        await interaction.response.send_message(f"❌ ไม่พบ Key `{key}` ในระบบ", ephemeral=True)

# ================= PROFILE =================

@bot.tree.command(name="profile", description="ดูข้อมูล Key ของตัวเอง")
async def profile_cmd(interaction: discord.Interaction):
    if not is_bot_open():
        await interaction.response.send_message(
            "🕐 บอทปิดให้บริการแล้ว\n⏰ เปิดให้บริการ **12:00 น.** — **19:30 น.**",
            ephemeral=True
        )
        return

    db = load_db()
    user_id = str(interaction.user.id)

    if user_id in db["blacklist"]:
        bl = db["blacklist"][user_id]
        reason_text = bl.get("reason", "-")
        await interaction.response.send_message(
            f"🚫 คุณถูกแบนจากการใช้งาน Key\n📋 เหตุผล: **{reason_text}**",
            ephemeral=True
        )
        return

    key_input, data = find_key_by_user(user_id, db)

    if not data:
        e = discord.Embed(
            title="👤 โปรไฟล์ของคุณ",
            description="❌ คุณยังไม่ได้ใช้ Key\nกด `/script` → **Get Script** เพื่อเริ่มใช้งาน",
            color=0x95a5a6
        )
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=e, ephemeral=True)
        return

    try:
        end = parse_time(data["หมดเวลา"])
        start = parse_time(data["เริ่มใช้"])
        expired = datetime.now() > end
        total_days = (end - start).days
    except Exception:
        expired = True
        total_days = 0

    if expired:
        color  = 0xe74c3c
        status = "❌ Key หมดอายุแล้ว"
        bar    = "░" * 10
        pct    = "0%"
    else:
        remain_sec = (end - datetime.now()).total_seconds()
        total_sec  = (end - start).total_seconds()
        ratio      = max(0.0, remain_sec / total_sec) if total_sec > 0 else 0
        filled     = round(ratio * 10)
        bar        = "█" * filled + "░" * (10 - filled)
        pct        = f"{round(ratio * 100)}%"
        color      = 0x2ecc71 if ratio > 0.3 else 0xe67e22
        status = f"✅ Active — เหลือ {format_time(end)}"

    e = discord.Embed(title="👤 โปรไฟล์ของคุณ", color=color)
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.add_field(name="ผู้ใช้", value=f"{interaction.user.mention}", inline=True)
    e.add_field(name="ประเภท Key", value=f"{total_days} วัน", inline=True)
    e.add_field(name="\u200b", value="\u200b", inline=True)
    e.add_field(name="🔑 Key", value=f"`{key_input}`", inline=False)
    e.add_field(name="📅 เริ่มใช้", value=data["เริ่มใช้"], inline=True)
    e.add_field(name="⏰ หมดเวลา", value=data["หมดเวลา"], inline=True)
    e.add_field(name="สถานะ", value=status, inline=False)
    e.add_field(name=f"เวลาคงเหลือ [{bar}] {pct}", value="\u200b", inline=False)

    if expired:
        e.add_field(
            name="🛒 ต่ออายุได้เลยที่",
            value=f"[คลิกซื้อ Key ใหม่]({SHOP_URL})",
            inline=False
        )

    e.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=e, ephemeral=True)

# ================= ANNOUNCE =================

@bot.tree.command(name="announce", description="ส่งข้อความหา User ทุกคนที่มี Key Active (Admin only)")
@app_commands.describe(message="ข้อความที่จะส่ง")
async def announce_cmd(interaction: discord.Interaction, message: str):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    await interaction.response.defer(ephemeral=True)

    db = load_db()
    now = datetime.now()
    success, failed = 0, 0
    sent_ids = set()

    for key, data in db["used_keys"].items():
        user_id = data["ผู้ใช้"]
        if user_id in sent_ids:
            continue
        try:
            end = parse_time(data["หมดเวลา"])
            if now > end:
                continue
        except Exception:
            continue

        try:
            user = await bot.fetch_user(int(user_id))
            embed = discord.Embed(
                title="📢 ประกาศจาก KyxHub",
                description=message,
                color=0x5865f2
            )
            embed.set_footer(text=f"KyxHub • {now.strftime('%d/%m/%Y %H:%M')} • ส่งโดย {interaction.user.display_name}")
            await user.send(embed=embed)
            sent_ids.add(user_id)
            success += 1
        except Exception:
            failed += 1

    await interaction.followup.send(
        f"📢 ส่ง Announce เสร็จแล้ว\n✅ สำเร็จ: **{success}** คน\n❌ ส่งไม่ได้ (DM ปิด): **{failed}** คน",
        ephemeral=True
    )

# ================= EXPORTKEYS =================

@bot.tree.command(name="exportkeys", description="Export ข้อมูล Key ทั้งหมดเป็นไฟล์ JSON (Owner only)")
async def exportkeys_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    await interaction.response.defer(ephemeral=True)

    db = load_db()
    now = datetime.now()

    export = {
        "exported_at": now.strftime("%d/%m/%Y %H:%M"),
        "summary": {
            "active_keys": 0,
            "expired_keys": 0,
            "unused_7_days": len(db["keys_7_days"]),
            "unused_30_days": len(db["keys_30_days"]),
            "blacklisted_users": len(db["blacklist"]),
        },
        "active": {},
        "expired": {},
        "unused_7_days": db["keys_7_days"],
        "unused_30_days": db["keys_30_days"],
        "blacklist": db["blacklist"],
    }

    for key, data in db["used_keys"].items():
        try:
            end = parse_time(data["หมดเวลา"])
            is_expired = now > end
        except Exception:
            is_expired = True

        entry = {**data, "เวลาคงเหลือ": "หมดอายุ" if is_expired else format_time(end)}
        if is_expired:
            export["expired"][key] = entry
            export["summary"]["expired_keys"] += 1
        else:
            export["active"][key] = entry
            export["summary"]["active_keys"] += 1

    import io
    file_bytes = json.dumps(export, indent=4, ensure_ascii=False).encode("utf-8")
    file = discord.File(
        io.BytesIO(file_bytes),
        filename=f"KyxHub_Export_{now.strftime('%d-%m-%Y_%H%M')}.json"
    )

    try:
        await interaction.user.send(
            content=(
                f"📦 **Export Keys — {now.strftime('%d/%m/%Y %H:%M')}**\n"
                f"✅ Active: **{export['summary']['active_keys']}** | "
                f"❌ หมดอายุ: **{export['summary']['expired_keys']}** | "
                f"📦 Unused: **{export['summary']['unused_7_days'] + export['summary']['unused_30_days']}**"
            ),
            file=file
        )
        await interaction.followup.send("✅ ส่งไฟล์ Export ทาง DM แล้ว", ephemeral=True)
    except Exception:
        await interaction.followup.send("❌ ไม่สามารถส่ง DM ได้ กรุณาเปิด DM ก่อน", ephemeral=True)

# ================= TICKET =================


def load_ticket_config() -> dict:
    return _MEM["ticket_config"]

def save_ticket_config(data: dict):
    _MEM["ticket_config"] = data
    _schedule_sync("ticket_config")


OPEN_TICKETS: dict = {}

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 เปิด Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)

        if user_id in OPEN_TICKETS:
            ch = interaction.guild.get_channel(OPEN_TICKETS[user_id])
            if ch:
                await interaction.response.send_message(
                    f"❌ คุณมี Ticket เปิดอยู่แล้วที่ {ch.mention}",
                    ephemeral=True
                )
                return
            else:
                del OPEN_TICKETS[user_id]

        cfg = load_ticket_config()
        category = interaction.guild.get_channel(cfg.get("category_id")) if cfg.get("category_id") else None

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        for admin_id in ADMIN_USER_IDS:
            member = interaction.guild.get_member(admin_id)
            if member:
                overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:32]

        try:
            ticket_ch = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket ของ {interaction.user} (ID: {interaction.user.id})"
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ ไม่สามารถสร้าง Ticket ได้: {e}", ephemeral=True)
            return

        OPEN_TICKETS[user_id] = ticket_ch.id

        embed = discord.Embed(
            title="🎫 Ticket เปิดแล้ว",
            description=(
                f"สวัสดี {interaction.user.mention}! 👋\n\n"
                "กรุณาอธิบายปัญหาหรือคำถามของคุณ แล้ว Admin จะเข้ามาช่วยเหลือโดยเร็ว\n\n"
                "กดปุ่ม **🔒 ปิด Ticket** เมื่อปัญหาได้รับการแก้ไขแล้ว"
            ),
            color=0x5865f2
        )
        embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")

        await ticket_ch.send(
            content=f"{interaction.user.mention}",
            embed=embed,
            view=TicketCloseView()
        )

        log_ch_id = cfg.get("log_channel_id")
        if log_ch_id:
            log_ch = interaction.guild.get_channel(log_ch_id)
            if log_ch:
                log_embed = discord.Embed(title="🎫 Ticket เปิดใหม่", color=0x2ecc71)
                log_embed.add_field(name="ผู้ใช้", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
                log_embed.add_field(name="ช่อง", value=ticket_ch.mention, inline=True)
                log_embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
                await log_ch.send(embed=log_embed)

        await interaction.response.send_message(
            f"✅ เปิด Ticket แล้วที่ {ticket_ch.mention}",
            ephemeral=True
        )


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 ปิด Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        user_id = str(interaction.user.id)
        is_owner = OPEN_TICKETS.get(user_id) == channel.id
        if not is_admin(interaction) and not is_owner:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ปิด Ticket นี้", ephemeral=True)
            return

        await interaction.response.send_message("🔒 กำลังปิด Ticket...")

        for uid, cid in list(OPEN_TICKETS.items()):
            if cid == channel.id:
                del OPEN_TICKETS[uid]
                break

        cfg = load_ticket_config()
        log_ch_id = cfg.get("log_channel_id")
        if log_ch_id:
            log_ch = interaction.guild.get_channel(log_ch_id)
            if log_ch:
                log_embed = discord.Embed(title="🔒 Ticket ปิดแล้ว", color=0xe74c3c)
                log_embed.add_field(name="ช่อง", value=f"`{channel.name}`", inline=True)
                log_embed.add_field(name="ปิดโดย", value=f"{interaction.user.mention}", inline=True)
                log_embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
                await log_ch.send(embed=log_embed)

        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"Ticket ปิดโดย {interaction.user}")
        except Exception:
            pass


@bot.tree.command(name="ticket", description="ส่ง embed ให้ user กดเปิด Ticket")
@app_commands.describe(title="หัวข้อของ embed (ไม่บังคับ)")
async def ticket_cmd(interaction: discord.Interaction, title: str = "📬 ติดต่อ Support"):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    embed = discord.Embed(
        title=title,
        description=(
            "มีปัญหาหรือต้องการความช่วยเหลือ?\n"
            "กดปุ่มด้านล่างเพื่อเปิด **Ticket** แล้ว Admin จะเข้ามาช่วยเหลือ\n\n"
            "⚠️ กรุณาเปิด Ticket ทีละ 1 อันเท่านั้น"
        ),
        color=0x5865f2
    )
    embed.set_footer(text="KyxHub • Ticket System")
    await interaction.response.send_message(embed=embed, view=TicketView())
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())


@bot.tree.command(name="setticket", description="ตั้งค่า Category และห้อง Log สำหรับ Ticket (Admin only)")
@app_commands.describe(category="Category ที่จะสร้าง Ticket ใน", log_channel="ห้อง Log สำหรับบันทึก Ticket")
async def setticket_cmd(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    log_channel: discord.TextChannel
):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    save_ticket_config({"category_id": category.id, "log_channel_id": log_channel.id})
    await interaction.response.send_message(
        f"✅ ตั้งค่า Ticket แล้ว\n📁 Category: **{category.name}**\n📋 Log: {log_channel.mention}",
        ephemeral=True
    )

# ================= WARN =================

MAX_WARNS = 3

@bot.tree.command(name="warn", description="ตักเตือน User (Admin only)")
@app_commands.describe(user="User ที่จะ warn", reason="เหตุผล")
async def warn_cmd(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    warns = load_warnings()
    user_id = str(user.id)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    if user_id not in warns:
        warns[user_id] = []

    warns[user_id].append({
        "reason": reason,
        "by": str(interaction.user.id),
        "at": now_str
    })
    save_warnings(warns)

    count = len(warns[user_id])

    try:
        embed_dm = discord.Embed(title="⚠️ คุณได้รับการตักเตือน", color=0xff9900)
        embed_dm.add_field(name="เหตุผล", value=reason, inline=False)
        embed_dm.add_field(name="ตักเตือนโดย", value=f"{interaction.user.display_name}", inline=True)
        embed_dm.add_field(name="จำนวน Warn", value=f"**{count} / {MAX_WARNS}**", inline=True)
        if count >= MAX_WARNS:
            embed_dm.add_field(
                name="🚫 ถูกแบนอัตโนมัติ",
                value=f"คุณถูก Warn ครบ {MAX_WARNS} ครั้ง และถูกแบนจากการใช้งาน Key",
                inline=False
            )
        embed_dm.add_field(
            name="📋 ต้องการอุทธรณ์?",
            value="พิมพ์ `/appeal` ในเซิร์ฟเวอร์เพื่อยื่นคำร้อง",
            inline=False
        )
        embed_dm.set_footer(text=f"KyxHub • {now_str}")
        await user.send(embed=embed_dm)
    except Exception:
        pass

    auto_banned = False
    if count >= MAX_WARNS:
        db = load_db()
        if user_id not in db["blacklist"]:
            db["blacklist"][user_id] = {
                "reason": f"Auto-Blacklist: Warn ครบ {MAX_WARNS} ครั้ง (ล่าสุด: {reason})",
                "by": str(interaction.user.id),
                "at": now_str
            }
            save_db(db)
            auto_banned = True

    embed_admin = discord.Embed(
        title="⚠️ Warn บันทึกแล้ว",
        color=0xff9900 if not auto_banned else 0xe74c3c
    )
    embed_admin.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
    embed_admin.add_field(name="จำนวน Warn", value=f"**{count} / {MAX_WARNS}**", inline=True)
    embed_admin.add_field(name="เหตุผล", value=reason, inline=False)
    if auto_banned:
        embed_admin.add_field(
            name="🚫 Auto-Blacklist",
            value=f"{user.mention} ถูกแบนอัตโนมัติเนื่องจาก Warn ครบ {MAX_WARNS} ครั้ง",
            inline=False
        )
    embed_admin.set_footer(text=f"KyxHub • {now_str}")
    await interaction.response.send_message(embed=embed_admin, ephemeral=True)


@bot.tree.command(name="warnings", description="ดูประวัติ Warn ของ User (Admin only)")
@app_commands.describe(user="User ที่ต้องการดู")
async def warnings_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    warns = load_warnings()
    user_id = str(user.id)
    user_warns = warns.get(user_id, [])

    embed = discord.Embed(
        title=f"📋 Warn ของ {user.display_name}",
        color=0xff9900 if user_warns else 0x2ecc71
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    if not user_warns:
        embed.description = "✅ ไม่มีประวัติ Warn"
    else:
        embed.description = f"จำนวนทั้งหมด: **{len(user_warns)} / {MAX_WARNS}** warn"
        for i, w in enumerate(user_warns, 1):
            by_user = f"<@{w['by']}>" if w.get("by") else "Unknown"
            embed.add_field(
                name=f"Warn #{i} — {w.get('at', '?')}",
                value=f"**เหตุผล:** {w['reason']}\n**โดย:** {by_user}",
                inline=False
            )

    db = load_db()
    if user_id in db["blacklist"]:
        bl = db["blacklist"][user_id]
        embed.add_field(
            name="🚫 สถานะ",
            value=f"ถูก Blacklist อยู่\n📋 เหตุผล: **{bl.get('reason', '-')}**\n🕐 วันที่: {bl.get('at', '-')}",
            inline=False
        )

    embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarnings", description="ล้าง Warn ของ User (Admin only)")
@app_commands.describe(user="User ที่ต้องการล้าง Warn")
async def clearwarnings_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    warns = load_warnings()
    user_id = str(user.id)

    if user_id not in warns or not warns[user_id]:
        await interaction.response.send_message(f"⚠️ {user.mention} ไม่มีประวัติ Warn", ephemeral=True)
        return

    old_count = len(warns[user_id])
    warns[user_id] = []
    save_warnings(warns)

    try:
        embed_dm = discord.Embed(
            title="✅ Warn ของคุณถูกล้างแล้ว",
            description=f"Warn จำนวน **{old_count}** ครั้งถูกล้างโดย Admin",
            color=0x2ecc71
        )
        embed_dm.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        await user.send(embed=embed_dm)
    except Exception:
        pass

    await interaction.response.send_message(
        f"✅ ล้าง Warn ของ {user.mention} ({old_count} ครั้ง) เรียบร้อยแล้ว",
        ephemeral=True
    )

# ================= MAINTENANCE =================

@bot.tree.command(name="maintenance", description="เปิด/ปิด Maintenance Mode (Admin only)")
@app_commands.describe(mode="on = ปิดปรับปรุง, off = เปิดให้บริการ")
@app_commands.choices(mode=[
    app_commands.Choice(name="🔧 เปิด Maintenance", value="on"),
    app_commands.Choice(name="✅ ปิด Maintenance", value="off"),
])
async def maintenance_cmd(interaction: discord.Interaction, mode: str):
    global MAINTENANCE_MODE

    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    MAINTENANCE_MODE = (mode == "on")

    if MAINTENANCE_MODE:
        await bot.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="🔧 ปิดปรับปรุงอยู่..."
            )
        )
        embed = discord.Embed(
            title="🔧 Maintenance Mode เปิดแล้ว",
            description="บอทปิดให้บริการชั่วคราว\nUser ทุกคนจะเห็นข้อความ **ปิดปรับปรุง** เมื่อพยายามใช้งาน",
            color=0xe74c3c
        )
    else:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="KyxHub | /help"
            )
        )
        embed = discord.Embed(
            title="✅ Maintenance Mode ปิดแล้ว",
            description="บอทเปิดให้บริการตามปกติแล้ว",
            color=0x2ecc71
        )

    embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= APPEAL SYSTEM =================

def load_appeals() -> dict:
    return _MEM["appeals"]

def save_appeals(data: dict):
    _MEM["appeals"] = data
    _schedule_sync("appeals")

def load_appeal_channel() -> int | None:
    return _MEM["appeal_channel"]

def save_appeal_channel(channel_id: int):
    _MEM["appeal_channel"] = channel_id
    _schedule_sync("appeal_channel")



class AppealModal(discord.ui.Modal, title="📝 ยื่นอุทธรณ์"):
    appeal_reason = discord.ui.TextInput(
        label="เหตุผลในการอุทธรณ์",
        style=discord.TextStyle.long,
        placeholder="อธิบายว่าทำไมคุณถึงคิดว่าไม่ได้รับความเป็นธรรม...",
        min_length=10,
        max_length=500
    )

    def __init__(self, appeal_type: str):
        super().__init__()
        self.appeal_type = appeal_type

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        db = load_db()
        warns = load_warnings()
        appeals = load_appeals()
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

        is_blacklisted = user_id in db["blacklist"]
        warn_count = len(warns.get(user_id, []))

        if self.appeal_type == "blacklist" and not is_blacklisted:
            await interaction.response.send_message("⚠️ คุณไม่ได้ถูก Blacklist อยู่", ephemeral=True)
            return
        if self.appeal_type == "warn" and warn_count == 0:
            await interaction.response.send_message("⚠️ คุณไม่มีประวัติ Warn", ephemeral=True)
            return

        appeal_key = f"{user_id}_{self.appeal_type}"
        if appeal_key in appeals:
            last = appeals[appeal_key].get("submitted_at", "")
            try:
                last_dt = datetime.strptime(last, "%d/%m/%Y %H:%M")
                if (datetime.now() - last_dt).total_seconds() < 300:
                    remaining = 300 - (datetime.now() - last_dt).total_seconds()
                    m = int(remaining // 60)
                    s = int(remaining % 60)
                    await interaction.response.send_message(
                        f"⏳ คุณยื่นอุทธรณ์ไปแล้ว รอ **{m} นาที {s} วินาที** ก่อนยื่นใหม่",
                        ephemeral=True
                    )
                    return
            except Exception:
                pass

        appeal_data = {
            "user_id": user_id,
            "type": self.appeal_type,
            "reason": self.appeal_reason.value,
            "submitted_at": now_str,
            "status": "pending",
            "warn_count": warn_count,
            "blacklist_reason": db["blacklist"].get(user_id, {}).get("reason", "-") if is_blacklisted else "-"
        }
        appeals[appeal_key] = appeal_data
        save_appeals(appeals)

        appeal_ch_id = load_appeal_channel()

        type_label = "🚫 Blacklist" if self.appeal_type == "blacklist" else "⚠️ Warn"
        color = 0xe74c3c if self.appeal_type == "blacklist" else 0xff9900

        embed = discord.Embed(
            title=f"📋 คำร้องอุทธรณ์ — {type_label}",
            color=color,
            timestamp=datetime.now()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="ผู้ยื่น", value=f"{interaction.user.mention} (`{user_id}`)", inline=True)
        embed.add_field(name="ประเภท", value=type_label, inline=True)
        embed.add_field(name="วันที่ยื่น", value=now_str, inline=True)

        if self.appeal_type == "warn":
            embed.add_field(name="จำนวน Warn", value=f"{warn_count} / {MAX_WARNS}", inline=True)
        if self.appeal_type == "blacklist" and is_blacklisted:
            embed.add_field(name="เหตุผลที่ถูกแบน", value=db["blacklist"][user_id].get("reason", "-"), inline=False)

        embed.add_field(name="📝 เหตุผลอุทธรณ์", value=self.appeal_reason.value, inline=False)
        embed.set_footer(text=f"KyxHub Appeal • ID: {appeal_key}")

        view = AppealAdminView(appeal_key=appeal_key, target_user_id=user_id, appeal_type=self.appeal_type)

        sent = False
        if appeal_ch_id:
            ch = bot.get_channel(appeal_ch_id)
            if ch:
                await ch.send(embed=embed, view=view)
                sent = True

        if not sent:
            for admin_id in ADMIN_USER_IDS:
                try:
                    admin_user = await bot.fetch_user(admin_id)
                    await admin_user.send(embed=embed, view=view)
                except Exception:
                    pass

        confirm_embed = discord.Embed(
            title="✅ ส่งคำร้องอุทธรณ์แล้ว",
            description=(
                f"คำร้องของคุณถูกส่งไปยัง Admin แล้ว\n"
                f"กรุณารอการพิจารณา Admin จะแจ้งผลทาง DM\n\n"
                f"📋 **ประเภท:** {type_label}\n"
                f"📝 **เหตุผล:** {self.appeal_reason.value}"
            ),
            color=0x5865f2
        )
        confirm_embed.set_footer(text=f"KyxHub • {now_str}")
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)


class AppealAdminView(discord.ui.View):
    def __init__(self, appeal_key: str, target_user_id: str, appeal_type: str):
        super().__init__(timeout=None)
        self.appeal_key = appeal_key
        self.target_user_id = target_user_id
        self.appeal_type = appeal_type
        self.approve_button.custom_id = f"appeal_approve_{appeal_key}"
        self.reject_button.custom_id = f"appeal_reject_{appeal_key}"

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admin เท่านั้น", ephemeral=True)
            return

        appeals = load_appeals()
        if self.appeal_key not in appeals:
            await interaction.response.send_message("⚠️ ไม่พบข้อมูลอุทธรณ์นี้แล้ว", ephemeral=True)
            return

        appeals[self.appeal_key]["status"] = "approved"
        appeals[self.appeal_key]["reviewed_by"] = str(interaction.user.id)
        appeals[self.appeal_key]["reviewed_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        save_appeals(appeals)

        db = load_db()
        warns = load_warnings()

        if self.appeal_type == "blacklist":
            if self.target_user_id in db["blacklist"]:
                del db["blacklist"][self.target_user_id]
                save_db(db)
        elif self.appeal_type == "warn":
            if self.target_user_id in warns:
                warns[self.target_user_id] = []
                save_warnings(warns)

        try:
            target = await bot.fetch_user(int(self.target_user_id))
            result_embed = discord.Embed(title="✅ อุทธรณ์ได้รับการอนุมัติ", color=0x2ecc71)
            if self.appeal_type == "blacklist":
                result_embed.description = "🎉 การ Blacklist ของคุณถูกยกเลิกแล้ว\nคุณสามารถใช้งาน Key ได้อีกครั้ง"
            else:
                result_embed.description = "🎉 ประวัติ Warn ของคุณถูกล้างแล้ว"
            result_embed.set_footer(text=f"KyxHub • พิจารณาโดย {interaction.user.display_name}")
            await target.send(embed=result_embed)
        except Exception:
            pass

        for item in self.children:
            item.disabled = True
        new_embed = interaction.message.embeds[0]
        new_embed.color = 0x2ecc71
        new_embed.add_field(
            name="✅ ผลการพิจารณา",
            value=f"**Approved** โดย {interaction.user.mention} เมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            inline=False
        )
        await interaction.response.edit_message(embed=new_embed, view=self)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admin เท่านั้น", ephemeral=True)
            return
        await interaction.response.send_modal(RejectReasonModal(
            appeal_key=self.appeal_key,
            target_user_id=self.target_user_id,
            original_message=interaction.message
        ))


class RejectReasonModal(discord.ui.Modal, title="❌ เหตุผลการปฏิเสธ"):
    reject_reason = discord.ui.TextInput(
        label="เหตุผลที่ปฏิเสธ",
        style=discord.TextStyle.long,
        placeholder="อธิบายเหตุผลที่ปฏิเสธคำร้อง...",
        min_length=5,
        max_length=300
    )

    def __init__(self, appeal_key: str, target_user_id: str, original_message: discord.Message):
        super().__init__()
        self.appeal_key = appeal_key
        self.target_user_id = target_user_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        appeals = load_appeals()
        if self.appeal_key in appeals:
            appeals[self.appeal_key]["status"] = "rejected"
            appeals[self.appeal_key]["reject_reason"] = self.reject_reason.value
            appeals[self.appeal_key]["reviewed_by"] = str(interaction.user.id)
            appeals[self.appeal_key]["reviewed_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            save_appeals(appeals)

        try:
            target = await bot.fetch_user(int(self.target_user_id))
            result_embed = discord.Embed(
                title="❌ อุทธรณ์ถูกปฏิเสธ",
                description=f"คำร้องอุทธรณ์ของคุณถูกปฏิเสธ\n\n📋 **เหตุผล:** {self.reject_reason.value}",
                color=0xe74c3c
            )
            result_embed.set_footer(text=f"KyxHub • พิจารณาโดย {interaction.user.display_name}")
            await target.send(embed=result_embed)
        except Exception:
            pass

        try:
            new_embed = self.original_message.embeds[0]
            new_embed.color = 0xe74c3c
            new_embed.add_field(
                name="❌ ผลการพิจารณา",
                value=(
                    f"**Rejected** โดย {interaction.user.mention} เมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
                    f"เหตุผล: {self.reject_reason.value}"
                ),
                inline=False
            )
            view = discord.ui.View()
            b1 = discord.ui.Button(label="✅ Approve", style=discord.ButtonStyle.success, disabled=True)
            b2 = discord.ui.Button(label="❌ Reject",  style=discord.ButtonStyle.danger,  disabled=True)
            view.add_item(b1)
            view.add_item(b2)
            await self.original_message.edit(embed=new_embed, view=view)
        except Exception:
            pass

        await interaction.response.send_message("✅ บันทึกผลการปฏิเสธแล้ว", ephemeral=True)


class AppealView(discord.ui.View):
    def __init__(self, has_warn: bool, has_blacklist: bool):
        super().__init__(timeout=None)
        if not has_warn:
            self.warn_btn.disabled = True
        if not has_blacklist:
            self.blacklist_btn.disabled = True

    @discord.ui.button(label="⚠️ อุทธรณ์ Warn", style=discord.ButtonStyle.primary, custom_id="appeal_warn_btn")
    async def warn_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppealModal(appeal_type="warn"))

    @discord.ui.button(label="🚫 อุทธรณ์ Blacklist", style=discord.ButtonStyle.danger, custom_id="appeal_blacklist_btn")
    async def blacklist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppealModal(appeal_type="blacklist"))


@bot.tree.command(name="appeal", description="ยื่นอุทธรณ์ Warn หรือ Blacklist")
async def appeal_cmd(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    db = load_db()
    warns = load_warnings()

    has_blacklist = user_id in db["blacklist"]
    warn_count = len(warns.get(user_id, []))
    has_warn = warn_count > 0

    if not has_blacklist and not has_warn:
        await interaction.response.send_message(
            "✅ คุณไม่มีประวัติ Warn หรือ Blacklist ที่ต้องอุทธรณ์",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📋 ยื่นอุทธรณ์",
        description="เลือกประเภทที่ต้องการอุทธรณ์\nAdmin จะพิจารณาและแจ้งผลทาง DM",
        color=0x5865f2
    )

    if has_warn:
        embed.add_field(name="⚠️ Warn ของคุณ", value=f"{warn_count} / {MAX_WARNS} ครั้ง", inline=True)
    if has_blacklist:
        bl = db["blacklist"][user_id]
        embed.add_field(name="🚫 Blacklist", value=f"เหตุผล: {bl.get('reason', '-')}", inline=True)

    embed.add_field(
        name="📌 หมายเหตุ",
        value="• สามารถยื่นซ้ำได้ทุก **5 นาที**\n• กรอกเหตุผลให้ครบถ้วนเพื่อเพิ่มโอกาสได้รับการพิจารณา",
        inline=False
    )
    embed.set_footer(text="KyxHub • Appeal System")

    await interaction.response.send_message(
        embed=embed,
        view=AppealView(has_warn=has_warn, has_blacklist=has_blacklist),
        ephemeral=True
    )

@bot.tree.command(name="setappealch", description="ตั้งค่าห้องรับคำร้องอุทธรณ์ (Admin only)")
@app_commands.describe(channel="ห้องที่จะรับคำร้องอุทธรณ์")
async def setappealch_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return
    save_appeal_channel(channel.id)
    await interaction.response.send_message(
        f"✅ ตั้งค่าห้อง Appeal เป็น {channel.mention} แล้ว\n"
        f"คำร้องอุทธรณ์ทั้งหมดจะส่งมาที่ห้องนี้",
        ephemeral=True
    )

# ================= เช็กฟังชั่น =================

@bot.tree.command(name="เช็กฟังชั่น", description="ตรวจสอบฟังก์ชันทั้งหมดว่ารันได้ไหม (Admin only)")
async def check_functions(interaction: discord.Interaction):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    await interaction.response.defer(ephemeral=True)

    results = []

    def check(name: str, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            results.append(("✅", name, ""))
        except Exception as e:
            results.append(("❌", name, str(e)))

    async def acheck(name: str, coro):
        try:
            await coro
            results.append(("✅", name, ""))
        except Exception as e:
            results.append(("❌", name, str(e)))

    # ── ฟังก์ชันไฟล์ / DB ──
    check("load_db",              load_db)
    check("load_warnings",        load_warnings)
    check("load_notify_channel",  load_notify_channel)
    check("load_ticket_config",   load_ticket_config)
    check("load_appeals",         load_appeals)
    check("load_appeal_channel",  load_appeal_channel)
    check("load_admin_roles",     load_admin_roles)

    # save_db — ทดสอบด้วย data ปัจจุบัน (ไม่แก้ไขข้อมูล)
    try:
        db = load_db()
        save_db(db)
        results.append(("✅", "save_db", ""))
    except Exception as e:
        results.append(("❌", "save_db", str(e)))

    # save_warnings
    try:
        w = load_warnings()
        save_warnings(w)
        results.append(("✅", "save_warnings", ""))
    except Exception as e:
        results.append(("❌", "save_warnings", str(e)))

    # save_notify_channel (อ่านค่าปัจจุบันแล้วเขียนกลับ)
    try:
        ch = load_notify_channel()
        if ch:
            save_notify_channel(ch)
        results.append(("✅", "save_notify_channel", ""))
    except Exception as e:
        results.append(("❌", "save_notify_channel", str(e)))

    # save_ticket_config
    try:
        tc = load_ticket_config()
        save_ticket_config(tc)
        results.append(("✅", "save_ticket_config", ""))
    except Exception as e:
        results.append(("❌", "save_ticket_config", str(e)))

    # save_appeals
    try:
        ap = load_appeals()
        save_appeals(ap)
        results.append(("✅", "save_appeals", ""))
    except Exception as e:
        results.append(("❌", "save_appeals", str(e)))

    # save_admin_roles
    try:
        ar = load_admin_roles()
        save_admin_roles(ar)
        results.append(("✅", "save_admin_roles", ""))
    except Exception as e:
        results.append(("❌", "save_admin_roles", str(e)))

    # save_appeal_channel
    try:
        ac = load_appeal_channel()
        if ac:
            save_appeal_channel(ac)
        results.append(("✅", "save_appeal_channel", ""))
    except Exception as e:
        results.append(("❌", "save_appeal_channel", str(e)))

    # ── ฟังก์ชัน utility ──
    check("is_bot_open",      is_bot_open)
    check("parse_time",       parse_time, "01/01/2025/12.00")
    check("format_time",      format_time, datetime.now() + timedelta(days=3))
    check("obfuscate_key",    obfuscate_key, "01/01/2025/12.00", "08/01/2025/12.00", "test-key-1234")
    check("build_script",     build_script,  "01/01/2025/12.00", "08/01/2025/12.00", "test-key-1234")
    check("build_full_script",build_full_script, "01/01/2025/12.00", "08/01/2025/12.00", "test-key-1234")
    check("make_listkeys_embed", make_listkeys_embed)
    check("find_key_by_user", find_key_by_user, "000000000000000000", load_db())
    check("check_rate_limit", check_rate_limit, "test_user_check")
    check("is_maintenance",   is_maintenance, interaction)
    check("is_admin",         is_admin, interaction)

    # ── ตรวจสอบ Slash Commands ที่ลงทะเบียนแล้ว ──
    registered_cmds = [c.name for c in bot.tree.get_commands()]
    expected_cmds = [
        "script", "setch", "help", "listkeys", "blacklist", "unblacklist",
        "resetkey", "addkey", "removekey", "keyinfo", "profile",
        "announce", "exportkeys", "ticket", "setticket", "warn",
        "warnings", "clearwarnings", "maintenance", "appeal", "setappealch",
        "setrole", "เช็กฟังชั่น"
    ]
    for cmd in expected_cmds:
        if cmd in registered_cmds:
            results.append(("✅", f"slash /{cmd}", ""))
        else:
            results.append(("❌", f"slash /{cmd}", "ไม่พบใน bot.tree"))

    # ── ตรวจสอบ Discord Config Channels ──
    try:
        cfg_guild = bot.get_guild(CONFIG_GUILD_ID)
        if cfg_guild:
            cat = discord.utils.get(cfg_guild.categories, name=CONFIG_CATEGORY_NAME)
            if cat:
                results.append(("✅", f"Config Category '{CONFIG_CATEGORY_NAME}'", ""))
                for key, ch_name in CONFIG_CHANNELS.items():
                    ch = discord.utils.get(cat.text_channels, name=ch_name)
                    if ch:
                        results.append(("✅", f"#{ch_name}", ""))
                    else:
                        results.append(("⚠️", f"#{ch_name}", "ยังไม่มีห้อง (จะสร้างเมื่อ restart)"))
            else:
                results.append(("⚠️", f"Config Category", "ยังไม่ได้สร้าง"))
        else:
            results.append(("❌", "Config Guild", f"ไม่พบ guild {CONFIG_GUILD_ID}"))
    except Exception as e:
        results.append(("❌", "Discord Config", str(e)))

    # ── ตรวจ _MEM ว่าโหลดข้อมูลครบ ──
    mem_checks = {
        "db (keyDatabase)":    bool(_MEM["db"].get("used_keys") is not None),
        "warnings":            isinstance(_MEM["warnings"], dict),
        "ticket_config":       isinstance(_MEM["ticket_config"], dict),
        "appeals":             isinstance(_MEM["appeals"], dict),
        "admin_roles":         isinstance(_MEM["admin_roles"], dict),
    }
    for label, ok in mem_checks.items():
        results.append(("✅" if ok else "❌", f"RAM: {label}", "" if ok else "ไม่ได้โหลด"))

    # ── สรุปผล ──
    ok_count   = sum(1 for r in results if r[0] == "✅")
    warn_count = sum(1 for r in results if r[0] == "⚠️")
    fail_count = sum(1 for r in results if r[0] == "❌")
    total      = len(results)

    color = 0x2ecc71 if fail_count == 0 else (0xff9900 if fail_count <= 3 else 0xe74c3c)

    embed = discord.Embed(
        title="🔍 ผลการตรวจสอบฟังก์ชัน",
        description=(
            f"✅ ผ่าน: **{ok_count}** | "
            f"⚠️ เตือน: **{warn_count}** | "
            f"❌ ล้มเหลว: **{fail_count}** / {total} รายการ"
        ),
        color=color,
        timestamp=datetime.now()
    )

    # แยกกลุ่มตามผลลัพธ์
    fail_lines = [f"`{r[1]}`\n  └ {r[2]}" for r in results if r[0] == "❌"]
    warn_lines = [f"`{r[1]}`\n  └ {r[2]}" for r in results if r[0] == "⚠️"]
    ok_lines   = [f"`{r[1]}`"              for r in results if r[0] == "✅"]

    if fail_lines:
        chunk = ""
        field_n = 1
        for line in fail_lines:
            if len(chunk) + len(line) > 1000:
                embed.add_field(name=f"❌ ล้มเหลว ({field_n})", value=chunk, inline=False)
                chunk = ""
                field_n += 1
            chunk += line + "\n"
        if chunk:
            embed.add_field(name=f"❌ ล้มเหลว ({field_n})", value=chunk, inline=False)

    if warn_lines:
        embed.add_field(name="⚠️ คำเตือน", value="\n".join(warn_lines), inline=False)

    # แสดง OK แบบย่อ (เอาไว้ 1 field)
    ok_text = "  ".join(ok_lines)
    if len(ok_text) > 1020:
        ok_text = ok_text[:1017] + "..."
    if ok_lines:
        embed.add_field(name="✅ ผ่านทั้งหมด", value=ok_text, inline=False)

    embed.set_footer(text=f"KyxHub • ตรวจสอบโดย {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ================= PLEASESENDAMESSAGE =================

@bot.tree.command(name="pleasesendamessage", description="ส่งข้อความหา User ที่ระบุ")
@app_commands.describe(
    user="User ที่จะส่งข้อความถึง",
    ephemeral="ส่งเป็นความลับ (เฉพาะคุณเห็น) หรือไม่",
    message="ข้อความที่จะส่ง",
    image_url="ลิงค์รูปภาพ (ไม่บังคับ)"
)
@app_commands.choices(ephemeral=[
    app_commands.Choice(name="ใช่ (เป็นความลับ)", value="yes"),
    app_commands.Choice(name="ไม่ (ทุกคนเห็น)", value="no"),
])
async def pleasesendamessage_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    ephemeral: str,
    message: str,
    image_url: str = None
):
    is_secret = (ephemeral == "yes")
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    # ตรวจสอบว่าลิงค์รูปถูกต้องไหม
    valid_image = False
    if image_url:
        lower = image_url.lower().split("?")[0]
        if (image_url.startswith("http://") or image_url.startswith("https://")) and \
           (any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")) or \
            "cdn.discordapp.com" in image_url or "media.discordapp.net" in image_url):
            valid_image = True
        else:
            await interaction.response.send_message(
                "❌ ลิงค์รูปไม่ถูกต้อง\nรองรับเฉพาะ URL ที่ลงท้ายด้วย `.png` `.jpg` `.jpeg` `.gif` `.webp` เท่านั้น",
                ephemeral=True
            )
            return

    embed_to_user = discord.Embed(
        title="📩 คุณได้รับข้อความ",
        description=message,
        color=0x5865f2
    )
    embed_to_user.add_field(name="จาก", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
    if valid_image:
        embed_to_user.set_image(url=image_url)
    embed_to_user.set_footer(text=f"KyxHub • {now_str}")

    sent_dm = False
    try:
        await user.send(embed=embed_to_user)
        sent_dm = True
    except Exception:
        pass

    confirm_embed = discord.Embed(
        title="📬 ส่งข้อความแล้ว" if sent_dm else "⚠️ ส่ง DM ไม่ได้",
        color=0x2ecc71 if sent_dm else 0xe74c3c
    )
    confirm_embed.add_field(name="ผู้รับ", value=f"{user.mention} (`{user.id}`)", inline=True)
    confirm_embed.add_field(name="โหมด", value="🔒 ความลับ" if is_secret else "🌐 สาธารณะ", inline=True)
    confirm_embed.add_field(name="ข้อความ", value=message, inline=False)
    if valid_image:
        confirm_embed.add_field(name="🖼️ รูปภาพ", value=f"[ดูรูป]({image_url})", inline=False)
    if not sent_dm:
        confirm_embed.add_field(
            name="❌ สาเหตุ",
            value="User ปิด DM อยู่ ไม่สามารถส่งได้",
            inline=False
        )
    confirm_embed.set_footer(text=f"KyxHub • {now_str}")

    await interaction.response.send_message(embed=confirm_embed, ephemeral=is_secret)

# ================= TRANSFERKEY =================

@bot.tree.command(name="transferkey", description="โอน Key ของคุณให้ User อื่น")
@app_commands.describe(target="User ที่จะโอน Key ให้")
async def transferkey_cmd(interaction: discord.Interaction, target: discord.Member):
    if not is_bot_open():
        await interaction.response.send_message(
            "🕐 บอทปิดให้บริการแล้ว\n⏰ เปิดให้บริการ **12:00 น.** — **19:30 น.**",
            ephemeral=True
        )
        return

    db = load_db()
    sender_id = str(interaction.user.id)
    target_id = str(target.id)

    if sender_id == target_id:
        await interaction.response.send_message("❌ ไม่สามารถโอน Key ให้ตัวเองได้", ephemeral=True)
        return

    if target_id in db["blacklist"]:
        await interaction.response.send_message("❌ ไม่สามารถโอน Key ให้ User ที่ถูก Blacklist ได้", ephemeral=True)
        return

    # เช็คว่าผู้ส่งมี Key ไหม
    sender_key, sender_data = find_key_by_user(sender_id, db)
    if not sender_data:
        await interaction.response.send_message("❌ คุณไม่มี Key ที่จะโอน", ephemeral=True)
        return

    end = parse_time(sender_data["หมดเวลา"])
    if datetime.now() > end:
        await interaction.response.send_message("❌ Key ของคุณหมดอายุแล้ว ไม่สามารถโอนได้", ephemeral=True)
        return

    # เช็คว่าผู้รับมี Key อยู่แล้วไหม
    target_key, target_data = find_key_by_user(target_id, db)
    if target_data:
        target_end = parse_time(target_data["หมดเวลา"])
        if datetime.now() < target_end:
            await interaction.response.send_message(
                f"❌ {target.mention} มี Key Active อยู่แล้ว ไม่สามารถรับโอนได้",
                ephemeral=True
            )
            return

    # โอน Key
    db["used_keys"][sender_key]["ผู้ใช้"] = target_id
    db["used_keys"][sender_key]["โอนจาก"] = sender_id
    db["used_keys"][sender_key]["โอนเมื่อ"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_db(db)

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    # แจ้ง DM ผู้รับ
    try:
        embed_target = discord.Embed(
            title="🎁 คุณได้รับโอน Key แล้ว!",
            color=0x2ecc71
        )
        embed_target.add_field(name="🔑 Key", value=f"`{sender_key}`", inline=False)
        embed_target.add_field(name="⏰ หมดเวลา", value=sender_data["หมดเวลา"], inline=True)
        embed_target.add_field(name="⏳ เวลาคงเหลือ", value=format_time(end), inline=True)
        embed_target.add_field(name="โอนจาก", value=f"{interaction.user.mention}", inline=False)
        embed_target.set_footer(text=f"KyxHub • {now_str}")
        await target.send(embed=embed_target)
    except Exception:
        pass

    # แจ้ง DM ผู้ส่ง
    try:
        embed_sender = discord.Embed(
            title="✅ โอน Key สำเร็จ",
            description=f"Key ของคุณถูกโอนให้ {target.mention} แล้ว",
            color=0x5865f2
        )
        embed_sender.add_field(name="🔑 Key", value=f"`{sender_key}`", inline=False)
        embed_sender.add_field(name="⏰ หมดเวลา", value=sender_data["หมดเวลา"], inline=True)
        embed_sender.set_footer(text=f"KyxHub • {now_str}")
        await interaction.user.send(embed=embed_sender)
    except Exception:
        pass

    confirm = discord.Embed(
        title="✅ โอน Key สำเร็จ",
        color=0x2ecc71
    )
    confirm.add_field(name="🔑 Key", value=f"`{sender_key}`", inline=False)
    confirm.add_field(name="ผู้รับ", value=f"{target.mention}", inline=True)
    confirm.add_field(name="⏳ เวลาคงเหลือ", value=format_time(end), inline=True)
    confirm.set_footer(text=f"KyxHub • {now_str}")
    await interaction.response.send_message(embed=confirm, ephemeral=True)


# ================= SCRIPT CONFIG COMMANDS =================

@bot.tree.command(name="setscriptconfig", description="ส่ง Lua config script ไปเก็บไว้ใน Discord channel (Admin only)")
@app_commands.describe(config="Lua config script ที่จะเก็บ (REMOVE_ALL_SCRIPT)")
async def setscriptconfig_cmd(interaction: discord.Interaction, config: str):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    try:
        cfg_guild = bot.get_guild(CONFIG_GUILD_ID)
        if not cfg_guild:
            cfg_guild = await bot.fetch_guild(CONFIG_GUILD_ID)
        ch = await get_or_create_config_channel(cfg_guild, "script_config")
        await ch.send(f"```lua\n{config}\n```\n---\nอัปเดตโดย {interaction.user} เมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')}")

        # ส่งแบบ plain text ด้วยเพื่อให้อ่านค่าได้ง่าย
        await ch.send(config)

        global _cached_script_config
        _cached_script_config = config

        await interaction.response.send_message(
            f"✅ บันทึก Script Config ลง {ch.mention} แล้ว\nบอทจะใช้ config ใหม่ทันที",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


@bot.tree.command(name="reloadconfig", description="โหลด Script Config ใหม่จาก Discord channel (Admin only)")
async def reloadconfig_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        content = await get_script_config()
        preview = content[:300] + "..." if len(content) > 300 else content
        await interaction.followup.send(
            f"✅ โหลด Script Config ใหม่แล้ว\n```lua\n{preview}\n```",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


@bot.tree.command(name="deleteroomconfig", description="ลบห้อง config ที่เลือกออกจาก category (Owner only)")
@app_commands.describe(room="ชื่อห้องที่จะลบ")
@app_commands.choices(room=[
    app_commands.Choice(name="#keydatabase",    value="keyDatabase"),
    app_commands.Choice(name="#warnings",       value="warnings"),
    app_commands.Choice(name="#notify-channel", value="notify_channel"),
    app_commands.Choice(name="#ticket-config",  value="ticket_config"),
    app_commands.Choice(name="#appeals",        value="appeals"),
    app_commands.Choice(name="#appeal-channel", value="appeal_channel"),
    app_commands.Choice(name="#admin-roles",    value="admin_roles"),
    app_commands.Choice(name="#expiry-channel", value="expiry_channel"),
    app_commands.Choice(name="#script-config",  value="script_config"),
    app_commands.Choice(name="#script-image",   value="script_image"),
])
async def deleteroomconfig_cmd(interaction: discord.Interaction, room: str):
    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        cfg_guild = await _get_cfg_guild_async()
        ch = await get_or_create_config_channel(cfg_guild, room)
        ch_name = ch.name

        await ch.delete(reason=f"ลบโดย {interaction.user} ผ่าน /deleteroomconfig")

        # เคลียร์ cache
        _config_msg_ids.pop(room, None)

        embed = discord.Embed(
            title="🗑️ ลบห้อง Config สำเร็จ",
            description=f"ลบห้อง `#{ch_name}` ออกแล้ว\nบอทจะสร้างห้องใหม่อัตโนมัติเมื่อ restart",
            color=0xe74c3c
        )
        embed.set_footer(text=f"KyxHub • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


@bot.tree.command(name="extendkey", description="ต่ออายุ Key ของ User (Owner only)")
@app_commands.describe(
    user="User ที่จะต่ออายุ Key",
    days="จำนวนวันที่จะต่อเพิ่ม"
)
@app_commands.choices(days=[
    app_commands.Choice(name="1 วัน",  value=1),
    app_commands.Choice(name="3 วัน",  value=3),
    app_commands.Choice(name="7 วัน",  value=7),
    app_commands.Choice(name="14 วัน", value=14),
    app_commands.Choice(name="30 วัน", value=30),
])
async def extendkey_cmd(interaction: discord.Interaction, user: discord.Member, days: int):
    if not is_owner(interaction):
        await deny_not_owner(interaction)
        return

    db = load_db()
    user_id = str(user.id)

    key, data = find_key_by_user(user_id, db)
    if not data:
        await interaction.response.send_message(f"❌ ไม่พบ Key ของ {user.mention}", ephemeral=True)
        return

    old_end = parse_time(data["หมดเวลา"])
    # ถ้า Key หมดแล้วให้นับจากวันนี้ ถ้ายังไม่หมดให้ต่อจากวันหมด
    base = max(old_end, datetime.now())
    new_end = base + timedelta(days=days)
    new_end_str = new_end.strftime("%d/%m/%Y/%H.%M")

    db["used_keys"][key]["หมดเวลา"] = new_end_str
    save_db(db)

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    # แจ้ง DM User
    try:
        embed_dm = discord.Embed(
            title="🎉 Key ของคุณได้รับการต่ออายุแล้ว!",
            color=0x2ecc71
        )
        embed_dm.add_field(name="🔑 Key", value=f"`{key}`", inline=False)
        embed_dm.add_field(name="⏰ หมดเวลาเดิม", value=data["หมดเวลา"], inline=True)
        embed_dm.add_field(name="✅ หมดเวลาใหม่", value=new_end_str, inline=True)
        embed_dm.add_field(name="➕ ต่อเพิ่ม", value=f"**{days} วัน**", inline=False)
        embed_dm.add_field(name="⏳ เวลาคงเหลือ", value=format_time(new_end), inline=False)
        embed_dm.set_footer(text=f"KyxHub • {now_str}")
        await user.send(embed=embed_dm)
    except Exception:
        pass

    embed = discord.Embed(title="✅ ต่ออายุ Key สำเร็จ", color=0x2ecc71)
    embed.add_field(name="User", value=f"{user.mention}", inline=True)
    embed.add_field(name="🔑 Key", value=f"`{key}`", inline=True)
    embed.add_field(name="⏰ หมดเวลาเดิม", value=data["หมดเวลา"], inline=True)
    embed.add_field(name="✅ หมดเวลาใหม่", value=new_end_str, inline=True)
    embed.add_field(name="➕ ต่อเพิ่ม", value=f"**{days} วัน**", inline=True)
    embed.set_footer(text=f"KyxHub • {now_str}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ================= SETEXPIRYCHANNEL =================

def load_expiry_channel() -> int | None:
    return _MEM["expiry_channel"]

def save_expiry_channel(channel_id: int):
    _MEM["expiry_channel"] = channel_id
    _schedule_sync("expiry_channel")


@bot.tree.command(name="setexpirychannel", description="ตั้งค่าห้องแจ้งเตือน Key หมดอายุ (Admin only)")
@app_commands.describe(channel="ห้องที่จะส่งแจ้งเตือนเมื่อ Key หมดอายุ")
async def setexpirychannel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await deny_not_admin(interaction)
        return
    save_expiry_channel(channel.id)
    await interaction.response.send_message(
        f"✅ ตั้งค่าห้องแจ้งเตือน Key หมดอายุเป็น {channel.mention} แล้ว",
        ephemeral=True
    )


# ================= KEY EXPIRY AUTO ANNOUNCE LOOP =================

ANNOUNCED_EXPIRY = set()  # ป้องกันแจ้งซ้ำ

@tasks.loop(minutes=1)
async def check_key_expiry_announce():
    db = load_db()
    now = datetime.now()
    channel_id = load_expiry_channel()
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    for key, data in db["used_keys"].items():
        try:
            end = parse_time(data["หมดเวลา"])
        except Exception:
            continue

        user_id = data["ผู้ใช้"]
        announce_key = f"{key}_{user_id}"

        # Key เพิ่งหมดอายุ (ภายใน 2 นาทีที่ผ่านมา) และยังไม่เคยแจ้ง
        if 0 <= (now - end).total_seconds() <= 120 and announce_key not in ANNOUNCED_EXPIRY:
            ANNOUNCED_EXPIRY.add(announce_key)

            embed = discord.Embed(
                title="⏰ Key หมดอายุแล้ว",
                color=0xe74c3c,
                timestamp=now
            )
            embed.add_field(name="👤 ผู้ใช้", value=f"<@{user_id}> (`{user_id}`)", inline=True)
            embed.add_field(name="🔑 Key", value=f"`{key}`", inline=True)
            embed.add_field(name="📅 หมดเมื่อ", value=data["หมดเวลา"], inline=False)
            embed.add_field(
                name="🛒 ต่ออายุได้ที่",
                value=f"[คลิกซื้อ Key ใหม่]({SHOP_URL})",
                inline=False
            )
            embed.set_footer(text="KyxHub • แจ้งเตือนอัตโนมัติ")

            await channel.send(content=f"<@{user_id}>", embed=embed)

            # แจ้ง DM ด้วย
            try:
                user = await bot.fetch_user(int(user_id))
                await user.send(embed=embed)
            except Exception:
                pass

@check_key_expiry_announce.before_loop
async def before_expiry_announce():
    await bot.wait_until_ready()

server_on()

bot.run(os.getenv('TOKEN'))