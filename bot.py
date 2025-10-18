# bot.py
import os
import re
import asyncio
from typing import Optional, Tuple, List

import discord
from discord.ext import commands
import yt_dlp

# ---- Intents / Bot ----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---- Config ----
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

YDL_COMMON = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": False,                 # allow playlists / mixes
    "default_search": "ytsearch",       # (we still set it, but we will block non-URL in !play)
    "ignore_no_formats_error": True,
    "geo_bypass": True,
    "cookiefile": "cookies.txt" if os.path.exists("cookies.txt") else None,
    "extract_flat": "discard_in_playlist",  # faster playlist handling
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

# ---- Simple URL validator for YouTube ----
_YT_RE = re.compile(
    r"""^
    (https?://)?
    (www\.)?
    (
        (youtube\.com/(watch\?v=[\w\-]{11}([^ \t\n\r&]*)?.*|playlist\?list=[\w\-]+.*)) |
        (youtu\.be/[\w\-]{11}(\?.*)?)
    )
    $""",
    re.IGNORECASE | re.VERBOSE
)

def is_youtube_url(s: str) -> bool:
    return bool(_YT_RE.match(s.strip()))

# ---- Per-guild player state ----
class GuildPlayer:
    def __init__(self):
        # queue items: (stream_url, title, page_url)
        self.queue: asyncio.Queue[Tuple[str, str, str]] = asyncio.Queue()
        self.play_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.last_channel: Optional[discord.VoiceChannel] = None

players: dict[int, GuildPlayer] = {}

def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]

# ---- Helpers ----
def pick_first_entry(info):
    if not info:
        return None
    if "entries" in info and info["entries"]:
        return info["entries"][0]
    return info

def select_playable_format(info_dict):
    """Return (direct_stream_url, title) from a yt_dlp info dict."""
    if not info_dict:
        return None, None
    title = info_dict.get("title", "Unknown")
    # direct already?
    if info_dict.get("url"):
        return info_dict["url"], title

    formats = info_dict.get("formats") or []
    audio_only, audio_with_video = [], []
    for f in formats:
        u = f.get("url"); ac = f.get("acodec"); vcx = f.get("vcodec")
        if not u or not ac or ac == "none":
            continue
        if not vcx or vcx == "none":
            audio_only.append(f)
        else:
            audio_with_video.append(f)

    if audio_only:
        audio_only.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return audio_only[0]["url"], title

    if audio_with_video:
        audio_with_video.sort(key=lambda x: ((x.get("abr") or 0), (x.get("tbr") or 0)), reverse=True)
        return audio_with_video[0]["url"], title

    return None, None

def extract_page_url(entry, fallback_target: str | None = None) -> str:
    """Get the public YouTube page URL for messaging/queue display."""
    return (
        entry.get("webpage_url")
        or entry.get("original_url")
        or entry.get("url")  # sometimes present
        or (f"https://www.youtube.com/watch?v={entry.get('id')}" if entry.get("id") else None)
        or (fallback_target or "")
    )

async def extract_entries(url_or_query: str, max_items: int = 50) -> List[Tuple[str, str, str]]:
    """
    Run yt_dlp in a worker thread; retry across client configs to avoid SABR/PO-gates.
    Returns list of (stream_url, title, page_url) for singles/playlists/mixes.
    """
    import functools

    def _do_extract(target: str, ydl_common):
        results: List[Tuple[str, str, str]] = []
        last_err = None

        # Try a few safe configurations in order:
        configs = [
            {"extractor_args": {"youtube": {"player_client": ["tv"]}}},
            {"extractor_args": {"youtube": {"player_client": ["web"]}}},
            {},  # let yt-dlp auto-pick client as a final fallback
        ]

        for cfg in configs:
            try:
                ydl_opts = {
                    **{k: v for k, v in ydl_common.items() if v is not None},
                    "format": "bestaudio[ext=m4a]/bestaudio/best",
                    "socket_timeout": 15,
                    **cfg,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target, download=False)
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue

            # Playlist / Mix
            if info and "entries" in info:
                count = 0
                for entry in info.get("entries") or []:
                    if not entry:
                        continue
                    # Some playlist entries are "flat"; re-extract to get formats
                    if not entry.get("url") and not entry.get("formats"):
                        fallback_url = extract_page_url(entry, fallback_target=target)
                        if not fallback_url:
                            continue
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                                entry = ydl2.extract_info(fallback_url, download=False)
                        except Exception:
                            continue

                    stream_url, title = select_playable_format(entry)
                    if stream_url:
                        page_url = extract_page_url(entry, fallback_target=target)
                        results.append((stream_url, title, page_url))
                        count += 1
                        if count >= max_items:
                            break
                if results:
                    return results

            # Single video
            single = pick_first_entry(info)
            stream_url, title = select_playable_format(single)
            if stream_url:
                page_url = extract_page_url(single, fallback_target=target)
                return [(stream_url, title, page_url)]

        # If we got here, nothing playable was found
        msg = "No playable audio format was found."
        if last_err:
            msg += f" Last error: {last_err}"
        raise RuntimeError(msg)

    target = url_or_query.strip()
    return await asyncio.to_thread(functools.partial(_do_extract, target, YDL_COMMON))

async def ensure_player_task(ctx: commands.Context):
    """Start per-guild background consumer if not running."""
    gp = get_player(ctx.guild.id)
    if gp.play_task and not gp.play_task.done():
        return

    async def runner(guild_id: int):
        while True:
            stream_url, title, page_url = await gp.queue.get()
            async with gp.lock:
                guild = bot.get_guild(guild_id)
                vc: Optional[discord.VoiceClient] = discord.utils.get(bot.voice_clients, guild=guild)

                if (not vc) or (not vc.is_connected()):
                    channel = gp.last_channel
                    if not channel:
                        gp.queue.task_done()
                        continue
                    try:
                        vc = await channel.connect()
                    except discord.ClientException:
                        vc = discord.utils.get(bot.voice_clients, guild=guild)
                        if not vc:
                            gp.queue.task_done()
                            continue

                try:
                    source = discord.FFmpegPCMAudio(
                        stream_url,
                        executable=FFMPEG_BIN,
                        before_options=FFMPEG_OPTS["before_options"],
                        options=FFMPEG_OPTS["options"]
                    )
                    vc.play(source)
                except Exception as e:
                    print("Play error:", e)
                    gp.queue.task_done()
                    continue

            while vc.is_playing() or vc.is_paused():
                await asyncio.sleep(0.5)

            gp.queue.task_done()

    gp.play_task = asyncio.create_task(runner(ctx.guild.id))

# ---- Events ----
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

# ---- Commands ----
@bot.command()
async def hello(ctx):
    await ctx.send("👋 Bot is alive.")

@bot.command()
async def play(ctx, *, url: str):
    """Play a YouTube video/playlist by URL only (no search terms)."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("⚠️ Rejoins d’abord un salon vocal.")

    # Enforce YouTube URL only
    if not is_youtube_url(url):
        return await ctx.send("❌ Fournis une **URL YouTube** valide (ex: https://youtu.be/VIDEO_ID ou https://www.youtube.com/watch?v=VIDEO_ID).")

    gp = get_player(ctx.guild.id)
    gp.last_channel = ctx.author.voice.channel

    if not ctx.voice_client or not ctx.voice_client.is_connected():
        try:
            await gp.last_channel.connect()
        except Exception as e:
            return await ctx.send(f"⚠️ Impossible de se connecter au salon : {e}")

    try:
        entries = await extract_entries(url, max_items=50)
    except Exception as e:
        return await ctx.send(f"⚠️ {e}")

    for stream_url, title, page_url in entries:
        await gp.queue.put((stream_url, title, page_url))

    await ensure_player_task(ctx)

    if len(entries) == 1:
        _, title, page_url = entries[0]
        await ctx.send(f"🎵 Ajouté : **[{title}]({page_url})**")
    else:
        await ctx.send(f"📻 Playlist/Mix détecté — **{len(entries)}** pistes ajoutées.")

@bot.command()
async def pause(ctx):
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("⚠️ Le bot n’est pas connecté.")
    if vc.is_playing():
        vc.pause()
        return await ctx.send("⏸️ Lecture en pause.")
    await ctx.send("ℹ️ Rien n’est en lecture.")

@bot.command()
async def resume(ctx):
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("⚠️ Le bot n’est pas connecté.")
    if vc.is_paused():
        vc.resume()
        return await ctx.send("▶️ Reprise de la lecture.")
    await ctx.send("ℹ️ Le flux n’est pas en pause.")

@bot.command()
async def skip(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()
        return await ctx.send("⏭️ Piste suivante…")
    await ctx.send("ℹ️ Rien à passer.")

@bot.command(name="queue")
async def _queue(ctx):
    gp = get_player(ctx.guild.id)
    if gp.queue.empty():
        return await ctx.send("📭 La file est vide.")
    items = list(gp.queue._queue)  # preview only
    # items are (stream_url, title, page_url)
    preview = "\n".join(f"- [{title}]({page_url})" for _, title, page_url in list(items)[:10])
    more = "" if len(items) <= 10 else f"\n…(+{len(items)-10} autres)"
    await ctx.send(f"🧾 File d’attente (top 10):\n{preview}{more}")

@bot.command()
async def stop(ctx):
    vc = ctx.voice_client
    gp = get_player(ctx.guild.id)
    cleared = 0
    while not gp.queue.empty():
        try:
            gp.queue.get_nowait()
            gp.queue.task_done()
            cleared += 1
        except Exception:
            break
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await ctx.send(f"⏹️ Arrêt. File vidée ({cleared} éléments).")

@bot.command()
async def leave(ctx):
    vc = ctx.voice_client
    if vc and vc.is_connected():
        await ctx.send("👋 Déconnexion du salon vocal.")
        await vc.disconnect()

# ---- Run ----
if __name__ == "__main__":
    # Paste your token locally (don’t commit this file with the real value)
    TOKEN = os.getenv("DISCORD_TOKEN")
    TOKEN = TOKEN.strip().replace("\u200b", "")
    if not TOKEN or "." not in TOKEN or len(TOKEN) < 50:
        raise RuntimeError("Token looks malformed. Copy it again from the Bot tab.")
    print(f"Using token len={len(TOKEN)}")
    bot.run(TOKEN)
