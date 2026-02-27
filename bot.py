import os
import asyncio
import datetime as dt
from dataclasses import dataclass
from collections import deque

import discord
from discord.ext import commands
from discord import app_commands

import aiosqlite
from dotenv import load_dotenv
import yt_dlp

# =========================
# ENV
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
AUTO_VC_GUILD_ID = int(os.getenv("AUTO_VC_GUILD_ID", "0"))
AUTO_VC_CHANNEL_ID = int(os.getenv("AUTO_VC_CHANNEL_ID", "0"))  # ✅ NEW

DRIVE_FILE_ID = "1ZIsyyKutbOVSeeXSWbJgwVY1zOIRVjL2"
WELCOME_IMAGE_URL = f"https://drive.google.com/uc?export=view&id={DRIVE_FILE_ID}"

DB_PATH = "bot_data.db"

# =========================
# 24/7 Toggle
# =========================
always_on_guilds: set[int] = set()

# =========================
# DB init
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            date     TEXT    NOT NULL,
            month    TEXT    NOT NULL,
            PRIMARY KEY (guild_id, user_id, date)
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS radio (
            guild_id INTEGER NOT NULL,
            idx      INTEGER NOT NULL,
            query    TEXT    NOT NULL,
            PRIMARY KEY (guild_id, idx)
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS music_channels (
            guild_id   INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL
        );
        """)
        await db.commit()

def utc_today_str():
    return dt.datetime.utcnow().date().isoformat()

def utc_month_str():
    d = dt.datetime.utcnow().date()
    return f"{d.year:04d}-{d.month:02d}"

# =========================
# Music config
# =========================
YDL_OPTS = {
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["ios", "web"],
        }
    },
}
# ✅ only add cookiefile if exists (avoid cookiefile=None causing extract fail)
if os.path.exists("/app/cookies.txt"):
    YDL_OPTS["cookiefile"] = "/app/cookies.txt"

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ✅ fallback keyword (if radio empty)
DEFAULT_AUTOPLAY_QUERY = os.getenv("DEFAULT_AUTOPLAY_QUERY", "lofi hip hop")

@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str

class GuildMusicState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.text_channel_id: int | None = None
        self.radio_pos: int = 0
        self.is_playing_next: bool = False
        self.current_track: Track | None = None
        self.loop: bool = False
        self.autoplay: bool = True
        self.now_playing_msg: discord.Message | None = None

music_states: dict[int, GuildMusicState] = {}

def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]

# =========================
# yt-dlp helpers
# =========================
async def ytdlp_extract(query_or_url: str) -> Track:
    loop = asyncio.get_running_loop()

    # If URL contains playlist param, strip to single video id (v=)
    import re
    yt_match = re.match(r'https?://(?:www\.)?youtube\.com/watch\?.*?v=([\w-]+)', query_or_url)
    if yt_match:
        query_or_url = f"https://www.youtube.com/watch?v={yt_match.group(1)}"

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query_or_url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            return Track(
                title=info.get("title", "Unknown"),
                webpage_url=info.get("webpage_url", query_or_url),
                stream_url=info["url"],
            )

    return await loop.run_in_executor(None, _extract)

async def ytdlp_related(webpage_url: str) -> "Track | None":
    """Autoplay: find a related YouTube track"""
    loop = asyncio.get_running_loop()

    def _get_related():
        opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "noplaylist": True,
            "extract_flat": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            related = info.get("related_videos") or []
            if related:
                return related[0].get("id"), None
            return None, info.get("title", "")

    try:
        vid_id, fallback_title = await loop.run_in_executor(None, _get_related)
        if vid_id:
            return await ytdlp_extract(f"https://www.youtube.com/watch?v={vid_id}")
        elif fallback_title:
            return await ytdlp_extract(fallback_title)
    except Exception:
        pass
    return None

# =========================
# Voice helpers
# =========================
def pick_default_text_channel(guild: discord.Guild) -> int | None:
    me = guild.me
    if guild.system_channel and guild.system_channel.permissions_for(me).send_messages:
        return guild.system_channel.id
    for ch in guild.text_channels:
        if ch.permissions_for(me).send_messages:
            return ch.id
    return None

async def safe_connect(channel: discord.VoiceChannel, guild: discord.Guild):
    """Safe connect that clears stale sessions (fixes 4006 errors)"""
    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel and vc.channel.id == channel.id:
            return vc
        await vc.move_to(channel)
        return guild.voice_client

    if vc is not None:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        await asyncio.sleep(0.5)

    try:
        return await asyncio.wait_for(channel.connect(), timeout=15)
    except Exception as e:
        print(f"[safe_connect] failed: {e}")
        return None

async def ensure_voice(interaction: discord.Interaction):
    if not interaction.guild:
        raise RuntimeError("請在伺服器內使用。")
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise RuntimeError("無法取得使用者資訊。")
    if interaction.user.voice and interaction.user.voice.channel:
        vc = await safe_connect(interaction.user.voice.channel, interaction.guild)
        if vc is None:
            raise TimeoutError("連接語音頻道失敗，請確認 Bot 有加入語音頻道的權限。")
        return vc
    return None

# =========================
# Now Playing View (buttons)
# =========================
class NowPlayingView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild

    def _vc(self):
        return self.guild.voice_client

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="np_pause")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        vc = self._vc()
        if vc and vc.is_playing():
            vc.pause()
            button.emoji = "▶️"
            await interaction.message.edit(view=self)
        elif vc and vc.is_paused():
            vc.resume()
            button.emoji = "⏸️"
            await interaction.message.edit(view=self)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        vc = self._vc()
        state = get_state(self.guild.id)
        if vc and (vc.is_playing() or vc.is_paused()):
            state.is_playing_next = False
            vc.stop()
        await interaction.followup.send("⏭️ 已跳過。", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state = get_state(self.guild.id)
        state.loop = not state.loop
        button.style = discord.ButtonStyle.success if state.loop else discord.ButtonStyle.secondary
        await interaction.message.edit(view=self)
        status = "開啟 🔁" if state.loop else "關閉"
        await interaction.followup.send(f"單曲循環 {status}", ephemeral=True)

    @discord.ui.button(emoji="🎲", style=discord.ButtonStyle.secondary, custom_id="np_autoplay")
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state = get_state(self.guild.id)
        state.autoplay = not state.autoplay
        button.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        await interaction.message.edit(view=self)
        status = "開啟 ✅" if state.autoplay else "關閉"
        await interaction.followup.send(f"Autoplay {status}", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="np_stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state = get_state(self.guild.id)
        state.queue.clear()
        state.is_playing_next = False
        state.current_track = None
        state.loop = False
        vc = self._vc()
        if vc and vc.is_connected():
            vc.stop()
            await vc.disconnect()
        await interaction.message.edit(view=None)
        await interaction.followup.send("⏹️ 已停止並退出語音。", ephemeral=True)

# =========================
# DB helpers
# =========================
async def get_music_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT channel_id FROM music_channels WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
    return row[0] if row else None

async def set_music_channel(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO music_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id)
        )
        await db.commit()

async def load_radio_list(guild_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT query FROM radio WHERE guild_id = ? ORDER BY idx ASC",
            (guild_id,)
        )
        rows = await cur.fetchall()
    return [r[0] for r in rows]

async def radio_fill_queue(guild: discord.Guild, count: int = 3) -> bool:
    state = get_state(guild.id)
    radio = await load_radio_list(guild.id)
    if not radio:
        return False
    added = 0
    for _ in range(count):
        q = radio[state.radio_pos % len(radio)]
        state.radio_pos += 1
        try:
            track = await ytdlp_extract(q)
            state.queue.append(track)
            added += 1
        except Exception as e:
            print(f"[radio_fill_queue] extract fail: {e}")
            continue
    return added > 0

# =========================
# Core playback
# =========================
async def send_now_playing(guild: discord.Guild, track: Track):
    state = get_state(guild.id)
    if not state.text_channel_id:
        return
    ch = guild.get_channel(state.text_channel_id)
    if not isinstance(ch, discord.TextChannel):
        return

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None

    loop_status = "🔁 ON" if state.loop else "OFF"
    auto_status = "✅ ON" if state.autoplay else "OFF"

    embed = discord.Embed(
        title="▶️ Now Playing",
        description=f"**{track.title}**",
        color=0x1DB954,
    )
    embed.add_field(name="🔗 Link", value=f"<{track.webpage_url}>", inline=False)
    embed.set_footer(text=f"Loop: {loop_status}  |  Autoplay: {auto_status}  |  ⏸️ ⏭️ 🔁 🎲 ⏹️")

    view = NowPlayingView(guild)
    msg = await ch.send(embed=embed, view=view)
    state.now_playing_msg = msg

async def play_next(guild: discord.Guild):
    state = get_state(guild.id)
    if state.is_playing_next:
        return
    state.is_playing_next = True

    try:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            state.is_playing_next = False
            return
        if vc.is_playing() or vc.is_paused():
            state.is_playing_next = False
            return

        # loop current track
        if state.loop and state.current_track:
            state.queue.appendleft(state.current_track)

        if not state.queue:
            # 1) try radio list
            ok = await radio_fill_queue(guild, count=3)

            # 2) if radio empty, fallback query (guarantee start)
            if not ok:
                try:
                    track = await ytdlp_extract(DEFAULT_AUTOPLAY_QUERY)
                    state.queue.append(track)
                    ok = True
                except Exception as e:
                    print(f"[play_next] fallback extract fail: {e}")

            # 3) if still empty and autoplay enabled, try related from current track
            if (not state.queue) and state.autoplay and state.current_track:
                related = await ytdlp_related(state.current_track.webpage_url)
                if related:
                    state.queue.append(related)

        if not state.queue:
            state.is_playing_next = False
            print("[play_next] queue empty, nothing to play")
            return

        track = state.queue.popleft()
        state.current_track = track

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS)

        def _after(err):
            state.is_playing_next = False
            if err:
                print(f"[play_next] error: {err}")
            asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

        vc.play(source, after=_after)
        await send_now_playing(guild, track)

    except Exception as e:
        print(f"[play_next] exception: {e}")
        state.is_playing_next = False

async def start_autoplay_if_needed(guild: discord.Guild):
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
    if vc.is_playing() or vc.is_paused():
        return
    state = get_state(guild.id)
    if state.is_playing_next:
        return
    if state.text_channel_id is None:
        state.text_channel_id = pick_default_text_channel(guild)
    await play_next(guild)

# =========================
# Auto-join debounce
# =========================
_vc_join_tasks: dict[int, asyncio.Task] = {}

async def _delayed_join(guild: discord.Guild, channel: discord.VoiceChannel):
    await asyncio.sleep(0.5)
    vc = await safe_connect(channel, guild)
    if vc:
        await start_autoplay_if_needed(guild)

# =========================
# Bot setup
# =========================
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await init_db()

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print("Sync failed:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[env] AUTO_VC_GUILD_ID={AUTO_VC_GUILD_ID}, AUTO_VC_CHANNEL_ID={AUTO_VC_CHANNEL_ID}")

    # ✅ Auto join on startup (no need anyone to join VC)
    if AUTO_VC_GUILD_ID and AUTO_VC_CHANNEL_ID:
        guild = bot.get_guild(AUTO_VC_GUILD_ID)
        if not guild:
            print("[auto_vc] guild not found. Check AUTO_VC_GUILD_ID")
            return

        ch = guild.get_channel(AUTO_VC_CHANNEL_ID)
        if not isinstance(ch, discord.VoiceChannel):
            print("[auto_vc] AUTO_VC_CHANNEL_ID is not a voice channel. Check ID")
            return

        vc = await safe_connect(ch, guild)
        if vc:
            state = get_state(guild.id)
            if state.text_channel_id is None:
                state.text_channel_id = pick_default_text_channel(guild)
            print("[auto_vc] connected, start autoplay")
            await start_autoplay_if_needed(guild)
        else:
            print("[auto_vc] connect failed (permission / region / voice gateway?)")

# =========================
# Welcome
# =========================
@bot.event
async def on_member_join(member: discord.Member):
    if WELCOME_CHANNEL_ID == 0:
        return
    ch = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return
    embed = discord.Embed(
        title="歡迎加入！",
        description=f"{member.mention} 歡迎來到 **{member.guild.name}**\n請先閱讀規則並自我介紹～",
        color=0xE06C2F,
    )
    embed.set_image(url=WELCOME_IMAGE_URL)
    await ch.send(embed=embed)

# =========================
# Music channel: type song name directly (/setup)
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    music_ch_id = await get_music_channel(message.guild.id)
    if music_ch_id and message.channel.id == music_ch_id:
        query = message.content.strip()
        if not query or query.startswith("/"):
            return

        try:
            await message.delete()
        except Exception:
            pass

        member = message.author
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            try:
                await message.channel.send(f"{message.author.mention} 🎧 請先進入語音頻道再點歌！", delete_after=5)
            except Exception:
                pass
            return

        state = get_state(message.guild.id)
        state.text_channel_id = message.channel.id

        vc = await safe_connect(member.voice.channel, message.guild)
        if vc is None:
            return

        try:
            track = await ytdlp_extract(query)
        except Exception as e:
            print(f"[on_message] ytdlp_extract fail: {e}")
            try:
                await message.channel.send("❌ 找不到該歌曲，請換個關鍵字。", delete_after=5)
            except Exception:
                pass
            return

        state.queue.append(track)
        if not vc.is_playing() and not vc.is_paused():
            await play_next(message.guild)
        else:
            try:
                await message.channel.send(f"➕ 已加入播放清單：**{track.title}**", delete_after=8)
            except Exception:
                pass

    await bot.process_commands(message)

# =========================
# Voice state (optional auto follow humans)
# =========================
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if AUTO_VC_GUILD_ID and member.guild.id != AUTO_VC_GUILD_ID:
        return
    if member.bot:
        return

    guild = member.guild

    # follow user join/move
    if after.channel and (before.channel != after.channel):
        old_task = _vc_join_tasks.get(guild.id)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(_delayed_join(guild, after.channel))
        _vc_join_tasks[guild.id] = task
        return

    # leave when no humans (unless 24/7 enabled)
    if before.channel and (before.channel != after.channel):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        if vc.channel and vc.channel.id != before.channel.id:
            return

        humans = [m for m in vc.channel.members if not m.bot]
        if len(humans) == 0:
            if guild.id in always_on_guilds:
                await start_autoplay_if_needed(guild)
                return
            try:
                await vc.disconnect()
            except Exception:
                pass

# =========================
# Slash: Setup
# =========================
@bot.tree.command(name="setup", description="設定專屬音樂頻道（在該頻道輸入歌名直接播）")
@app_commands.describe(channel="指定為音樂請求頻道")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    await set_music_channel(interaction.guild.id, channel.id)
    embed = discord.Embed(
        title="🎵 音樂頻道設定完成",
        description=(
            f"已將 {channel.mention} 設為專屬音樂請求頻道。\n\n"
            "在該頻道直接輸入歌名或 YouTube URL 即可播放！"
        ),
        color=0x1DB954,
    )
    await interaction.response.send_message(embed=embed)

# =========================
# Slash: Check-in / Leaderboard
# =========================
@bot.tree.command(name="checkin", description="每日打卡（一天一次）")
async def checkin(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild or not interaction.user:
        return await interaction.followup.send("請在伺服器內使用。", ephemeral=True)

    guild_id = interaction.guild.id
    user_id = interaction.user.id
    today = utc_today_str()
    month = utc_month_str()

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO checkins (guild_id, user_id, date, month) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, today, month),
            )
            await db.commit()
        await interaction.followup.send(f"✅ 打卡成功：{today}", ephemeral=True)
    except aiosqlite.IntegrityError:
        await interaction.followup.send("你今天已經打過卡了。", ephemeral=True)

@bot.tree.command(name="leaderboard", description="本月打卡前三名")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    if not interaction.guild:
        return await interaction.followup.send("請在伺服器內使用。")

    guild_id = interaction.guild.id
    month = utc_month_str()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT user_id, COUNT(*) as cnt
            FROM checkins
            WHERE guild_id = ? AND month = ?
            GROUP BY user_id
            ORDER BY cnt DESC
            LIMIT 3
        """, (guild_id, month))
        rows = await cur.fetchall()

    if not rows:
        return await interaction.followup.send(f"本月（{month}）尚無打卡紀錄。")

    lines = []
    for i, (uid, cnt) in enumerate(rows, start=1):
        m = interaction.guild.get_member(uid)
        name = m.mention if m else f"<@{uid}>"
        lines.append(f"**#{i}** {name} — **{cnt}** 天")

    embed = discord.Embed(
        title=f"🏆 本月打卡排行榜（{month}）TOP 3",
        description="\n".join(lines),
        color=0x2ECC71,
    )
    await interaction.followup.send(embed=embed)

# =========================
# Slash: Music controls
# =========================
@bot.tree.command(name="play", description="播放音樂（YouTube 關鍵字或 URL）")
@app_commands.describe(query="YouTube 關鍵字或 URL（支援中文搜尋）")
async def play(interaction: discord.Interaction, query: str):
    try:
        await interaction.response.defer()
    except Exception:
        return

    if not interaction.guild:
        return await interaction.followup.send("請在伺服器內使用。")

    state = get_state(interaction.guild.id)
    state.text_channel_id = interaction.channel_id

    try:
        vc = await ensure_voice(interaction)
    except TimeoutError as e:
        return await interaction.followup.send(f"❌ {e}")
    except Exception as e:
        msg = str(e).strip() or "❌ 無法加入語音頻道，請確認 Bot 有足夠權限。"
        return await interaction.followup.send(msg)

    if vc is None:
        return await interaction.followup.send("🎧 請先進入語音頻道，再使用 `/play`。")

    try:
        track = await ytdlp_extract(query)
    except Exception as e:
        print(f"[slash /play] extract fail: {e}")
        return await interaction.followup.send("❌ 解析失敗：請換一個關鍵字或 URL。")

    state.queue.append(track)
    await interaction.followup.send(f"➕ 已加入播放清單：**{track.title}**")

    if not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild)

@bot.tree.command(name="queue", description="查看播放清單")
async def queue_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    state = get_state(interaction.guild.id)
    items = list(state.queue)
    if not items:
        return await interaction.response.send_message("播放清單是空的。", ephemeral=True)

    lines = [f"{i+1}. {t.title}" for i, t in enumerate(items[:10])]
    more = f"\n... 還有 {len(items)-10} 首" if len(items) > 10 else ""
    await interaction.response.send_message("🎶 播放清單：\n" + "\n".join(lines) + more)

@bot.tree.command(name="pause", description="暫停播放")
async def pause(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
    vc = interaction.guild.voice_client
    if vc and vc.is_connected() and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message("⏸️ 已暫停。")
    await interaction.response.send_message("目前沒有在播放。", ephemeral=True)

@bot.tree.command(name="resume", description="繼續播放")
async def resume(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
    vc = interaction.guild.voice_client
    if vc and vc.is_connected() and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message("▶️ 已繼續。")
    await interaction.response.send_message("目前沒有暫停中的播放。", ephemeral=True)

@bot.tree.command(name="skip", description="跳過目前歌曲")
async def skip(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected() or (not vc.is_playing() and not vc.is_paused()):
        return await interaction.response.send_message("目前沒有在播放。", ephemeral=True)
    state = get_state(interaction.guild.id)
    state.is_playing_next = False
    vc.stop()
    await interaction.response.send_message("⏭️ 已跳過。")

@bot.tree.command(name="loop", description="單曲循環開關")
async def loop_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
    state = get_state(interaction.guild.id)
    state.loop = not state.loop
    status = "🔁 已開啟單曲循環" if state.loop else "🔁 已關閉單曲循環"
    await interaction.response.send_message(status)

@bot.tree.command(name="autoplay", description="自動選歌開關（播完自動找相關）")
async def autoplay_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
    state = get_state(interaction.guild.id)
    state.autoplay = not state.autoplay
    status = "✅ 已開啟 Autoplay" if state.autoplay else "❌ 已關閉 Autoplay"
    await interaction.response.send_message(status)

@bot.tree.command(name="clear", description="清空播放清單")
async def clear_queue(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
    state = get_state(interaction.guild.id)
    state.queue.clear()
    await interaction.response.send_message("🧹 播放清單已清空。")

@bot.tree.command(name="stop", description="停止播放並退出語音")
async def stop(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    state = get_state(interaction.guild.id)
    state.queue.clear()
    state.is_playing_next = False
    state.current_track = None
    state.loop = False

    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        try:
            vc.stop()
            await vc.disconnect()
        except Exception:
            pass

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(view=None)
        except Exception:
            pass
        state.now_playing_msg = None

    await interaction.response.send_message("⏹️ 已停止並退出語音。")

# =========================
# Slash: 24/7
# =========================
@bot.tree.command(name="24_7", description="24/7 背景播放（語音沒人也不退出）")
@app_commands.describe(mode="on/開啟 或 off/關閉")
async def always_on(interaction: discord.Interaction, mode: str):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    mode = mode.lower().strip()
    if mode in ("on", "開", "開啟", "true", "1"):
        always_on_guilds.add(interaction.guild.id)
        await interaction.response.send_message("✅ 已開啟 24/7：語音沒人也會持續播放、不自動退出。")
        state = get_state(interaction.guild.id)
        state.text_channel_id = interaction.channel_id
        try:
            vc = await ensure_voice(interaction)
            if vc:
                await start_autoplay_if_needed(interaction.guild)
        except Exception:
            await start_autoplay_if_needed(interaction.guild)
    elif mode in ("off", "關", "關閉", "false", "0"):
        always_on_guilds.discard(interaction.guild.id)
        await interaction.response.send_message("✅ 已關閉 24/7：語音沒人會自動退出。")
    else:
        await interaction.response.send_message("請輸入 on（開啟）或 off（關閉）。", ephemeral=True)

# =========================
# Slash: Radio
# =========================
@bot.tree.command(name="radio_add", description="加入電台清單（YouTube URL 或關鍵字）")
@app_commands.describe(query="YouTube URL 或關鍵字（支援中文）")
async def radio_add(interaction: discord.Interaction, query: str):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    guild_id = interaction.guild.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(idx), -1) FROM radio WHERE guild_id = ?", (guild_id,))
        (mx,) = await cur.fetchone()
        idx = int(mx) + 1
        await db.execute("INSERT INTO radio (guild_id, idx, query) VALUES (?, ?, ?)", (guild_id, idx, query))
        await db.commit()

    await interaction.response.send_message(f"✅ 已加入電台清單：`{query}`")

    # if bot already in voice, start if idle
    state = get_state(guild_id)
    state.text_channel_id = interaction.channel_id
    try:
        await start_autoplay_if_needed(interaction.guild)
    except Exception:
        pass

@bot.tree.command(name="radio_list", description="查看電台清單")
async def radio_list(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    radio = await load_radio_list(interaction.guild.id)
    if not radio:
        return await interaction.response.send_message("電台清單目前是空的。先用 `/radio_add` 加幾首。", ephemeral=True)

    lines = [f"{i+1}. {q}" for i, q in enumerate(radio[:15])]
    more = f"\n... 還有 {len(radio)-15} 筆" if len(radio) > 15 else ""
    await interaction.response.send_message("📻 電台清單：\n" + "\n".join(lines) + more, ephemeral=True)

@bot.tree.command(name="radio_clear", description="清空電台清單")
async def radio_clear(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM radio WHERE guild_id = ?", (interaction.guild.id,))
        await db.commit()

    state = get_state(interaction.guild.id)
    state.radio_pos = 0
    await interaction.response.send_message("🧹 已清空電台清單。", ephemeral=True)

# =========================
# Slash: Help
# =========================
@bot.tree.command(name="help", description="查看使用說明")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 MusicBot 使用說明",
        description="音樂播放、電台自動播放、24/7 背景播放",
        color=0x5865F2
    )
    embed.add_field(
        name="🎵 專屬音樂頻道",
        value="`/setup <頻道>` 設定後，在該頻道直接輸入歌名或 URL 即可播放！",
        inline=False
    )
    embed.add_field(
        name="📻 電台",
        value="`/radio_add` 加入　`/radio_list` 查看　`/radio_clear` 清空",
        inline=False
    )
    embed.add_field(
        name="♾️ 24/7",
        value="`/24_7 on/off`",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================
# Keep-alive
# =========================
from aiohttp import web as aio_web

async def _keepalive_server():
    port = int(os.getenv("PORT", "10000"))
    app = aio_web.Application()
    app.router.add_get("/", lambda r: aio_web.Response(text="OK"))
    runner = aio_web.AppRunner(app)
    await runner.setup()
    site = aio_web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[keepalive] HTTP server running on port {port}")

async def main():
    await _keepalive_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    asyncio.run(main())