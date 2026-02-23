import os
import atexit
import asyncio
import logging
from typing import Dict, Optional, Tuple
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

# Force .env values to override any pre-existing OS environment variables (e.g. stale tokens).
load_dotenv(override=True)
LOCK_FILE_PATH = os.path.join(os.path.dirname(__file__), ".bot.lock")
LOCK_FILE_FD: Optional[int] = None


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def release_single_instance_lock() -> None:
    global LOCK_FILE_FD
    if LOCK_FILE_FD is not None:
        try:
            os.close(LOCK_FILE_FD)
        except OSError:
            pass
        LOCK_FILE_FD = None

    try:
        os.remove(LOCK_FILE_PATH)
    except OSError:
        pass


def acquire_single_instance_lock() -> None:
    global LOCK_FILE_FD

    while True:
        try:
            LOCK_FILE_FD = os.open(LOCK_FILE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(LOCK_FILE_FD, str(os.getpid()).encode("ascii", errors="ignore"))
            atexit.register(release_single_instance_lock)
            return
        except FileExistsError:
            stale_lock = False
            try:
                with open(LOCK_FILE_PATH, "r", encoding="ascii", errors="ignore") as fh:
                    lock_pid = int((fh.read() or "0").strip())
                stale_lock = not process_exists(lock_pid)
            except (OSError, ValueError):
                stale_lock = True

            if stale_lock:
                try:
                    os.remove(LOCK_FILE_PATH)
                except OSError as exc:
                    raise RuntimeError(f"Could not remove stale lock file: {exc}") from exc
                continue

            raise RuntimeError(
                f"Another bot instance is already running (lock file: {LOCK_FILE_PATH}). "
                "Stop existing bot.py processes before starting a new one."
            )


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def parse_int_env(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"Ignoring invalid {name} value: {raw!r}")
        return None


ENABLE_PREFIX_COMMANDS = parse_bool_env("ENABLE_PREFIX_COMMANDS", default=True)
APP_COMMAND_GUILD_ID = parse_int_env("DISCORD_GUILD_ID")
AUTO_SYNC_COMMANDS = parse_bool_env("AUTO_SYNC_COMMANDS", default=False)
ALLOW_GLOBAL_SYNC = parse_bool_env("ALLOW_GLOBAL_SYNC", default=False)
INVITE_PERMISSIONS = parse_int_env("BOT_INVITE_PERMISSIONS") or 36817984
VOICE_CONNECT_TIMEOUT = parse_int_env("VOICE_CONNECT_TIMEOUT") or 20

intents = discord.Intents.default()
intents.message_content = ENABLE_PREFIX_COMMANDS
bot = commands.Bot(command_prefix="!", intents=intents)

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

YDL_OPTS = {
    "format": "18/bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "forceipv4": True,
    "extractor_args": {
        "youtube": {"player_client": ["android", "web"]},
    },
}


def headers_to_ffmpeg_args(headers: Dict[str, str]) -> str:
    if not headers:
        return ""
    header_blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    return f'-headers "{header_blob}"'


def resolve_stream(query: str) -> Tuple[str, str, Dict[str, str]]:
    lowered = query.lower().strip()
    if "discord.gg/" in lowered or "discord.com/invite/" in lowered:
        raise RuntimeError("Discord invite links are not playable audio sources.")

    fallback_opts = {key: value for key, value in YDL_OPTS.items() if key != "forceipv4"}
    attempts = (
        YDL_OPTS,
        {**YDL_OPTS, "format": "bestaudio/best"},
        fallback_opts,
        {**fallback_opts, "format": "bestaudio/best"},
    )

    info = None
    last_error: Optional[Exception] = None
    for options in attempts:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(query, download=False)
            break
        except Exception as exc:
            last_error = exc
            continue

    if not info:
        if last_error is not None:
            raise RuntimeError(str(last_error)) from last_error
        raise RuntimeError("No playable result found.")

    if "entries" in info and info["entries"]:
        info = next((entry for entry in info["entries"] if entry), None)

    if not info or "url" not in info:
        raise RuntimeError("No playable URL returned by yt-dlp.")

    return info["url"], info.get("title", "Unknown title"), info.get("http_headers", {})


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise RuntimeError("Join a voice channel first.")

    channel = ctx.author.voice.channel
    voice = ctx.voice_client

    try:
        if not voice or not voice.is_connected():
            voice = await channel.connect(timeout=VOICE_CONNECT_TIMEOUT, reconnect=True)
        elif voice.channel != channel:
            await voice.move_to(channel)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Timed out while connecting to voice. Check voice permissions and network/firewall/VPN."
        ) from exc

    await prepare_stage_voice(ctx, channel)

    return voice


async def prepare_stage_voice(ctx: commands.Context, channel: discord.abc.Connectable) -> None:
    if not isinstance(channel, discord.StageChannel) or not ctx.guild:
        return

    me = ctx.guild.me
    if not me:
        return

    voice_state = me.voice
    if voice_state and voice_state.suppress:
        try:
            await me.edit(suppress=False)
        except discord.Forbidden as exc:
            raise RuntimeError(
                "I need permission to speak in this Stage channel. Please unsuppress me manually."
            ) from exc
        except discord.HTTPException as exc:
            raise RuntimeError(f"Could not unsuppress in this Stage channel: {exc}") from exc

    request_to_speak = getattr(me, "request_to_speak", None)
    if callable(request_to_speak):
        try:
            await request_to_speak()
        except discord.HTTPException:
            pass


@bot.event
async def on_ready():
    print(f"Bot ready as {bot.user} (id: {bot.user.id})")
    print(f"Connected guilds: {len(bot.guilds)}")
    if not bot.guilds:
        print("Bot is not in any server. Invite it with bot + applications.commands scopes.")
        scopes = quote("bot applications.commands")
        invite_url = (
            f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
            f"&scope={scopes}&permissions={INVITE_PERMISSIONS}"
        )
        print(f"Invite URL: {invite_url}")

    if ENABLE_PREFIX_COMMANDS:
        try:
            app_info = await bot.application_info()
            flags = app_info.flags
            has_message_content = bool(
                getattr(flags, "gateway_message_content", False)
                or getattr(flags, "gateway_message_content_limited", False)
            )
            if not has_message_content:
                print(
                    "Prefix commands are enabled in code, but Message Content intent is not enabled for this app."
                )
                print(
                    "Enable it in Discord Developer Portal: Bot > Privileged Gateway Intents > Message Content Intent."
                )
        except Exception as exc:
            print(f"Could not verify Message Content intent status: {exc}")
    else:
        print("Prefix commands are disabled. Use slash commands, or set ENABLE_PREFIX_COMMANDS=true.")
    if not AUTO_SYNC_COMMANDS:
        print("Auto sync is disabled. Set AUTO_SYNC_COMMANDS=true to push slash command updates.")

    if getattr(bot, "_command_sync_started", False):
        return

    if not AUTO_SYNC_COMMANDS:
        return

    bot._command_sync_started = True
    bot.loop.create_task(sync_app_commands_once())


async def sync_app_commands_once() -> None:
    if getattr(bot, "_commands_synced_once", False):
        return

    try:
        if APP_COMMAND_GUILD_ID:
            guild = discord.Object(id=APP_COMMAND_GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {APP_COMMAND_GUILD_ID}.")
        elif ALLOW_GLOBAL_SYNC:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global slash command(s).")
        else:
            print("Skipping global command sync (set ALLOW_GLOBAL_SYNC=true to enable).")
            return
        bot._commands_synced_once = True
    except Exception as exc:
        print(f"Could not sync slash commands: {exc}")


@bot.hybrid_command(name="play", description="Play audio from a YouTube URL or search query.")
@app_commands.describe(query="YouTube URL or search terms")
async def play(ctx: commands.Context, *, query: str):
    try:
        voice = await ensure_voice(ctx)
    except RuntimeError as err:
        return await ctx.send(str(err))
    except Exception as exc:
        return await ctx.send(f"Could not join the voice channel: {exc}")

    if voice.is_playing() or voice.is_paused():
        voice.stop()

    try:
        stream_url, title, headers = resolve_stream(query)
    except Exception as exc:
        print(f"resolve_stream failed for query {query!r}: {type(exc).__name__}: {exc}")
        message = "Could not retrieve playable audio for that query."
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 22:
            message = "Could not retrieve playable audio (temporary source/network issue). Try again."
        elif "Discord invite links are not playable" in str(exc):
            message = "Discord invite links are not playable. Use a YouTube URL or search terms."
        return await ctx.send(message)

    header_opts = headers_to_ffmpeg_args(headers)
    before_opts = f"{FFMPEG_BEFORE} {header_opts}".strip()

    source = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_BIN,
        before_options=before_opts,
        options=FFMPEG_OPTIONS,
    )

    def on_playback_done(error: Optional[Exception]) -> None:
        if not error:
            return
        bot.loop.call_soon_threadsafe(
            lambda: bot.loop.create_task(ctx.send(f"Playback error: {error}"))
        )

    try:
        voice.play(source, after=on_playback_done)
    except Exception as exc:
        return await ctx.send(f"Could not start playback: {exc}")

    await ctx.send(f"Now playing: {title}")


@bot.hybrid_command(name="stop", description="Stop playback and disconnect from voice.")
async def stop(ctx: commands.Context):
    voice = ctx.voice_client
    if not voice or not voice.is_connected():
        return await ctx.send("Nothing is currently playing.")

    if voice.is_playing() or voice.is_paused():
        voice.stop()

    await voice.disconnect()
    await ctx.send("Playback stopped.")


def clean_token(raw: str | None) -> str:
    """Normalise the Discord token read from the environment."""
    if not raw:
        return ""
    token = raw.strip().replace("\u200b", "")
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1].strip()
    return token


if __name__ == "__main__":
    if not ENABLE_PREFIX_COMMANDS:
        class IgnoreMessageContentWarning(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return "Privileged message content intent is missing" not in record.getMessage()

        logging.getLogger("discord.ext.commands.bot").addFilter(IgnoreMessageContentWarning())

    token = clean_token(os.getenv("DISCORD_TOKEN"))
    if not token:
        raise RuntimeError("Set DISCORD_TOKEN in your environment before running the bot.")
    acquire_single_instance_lock()
    bot.run(token)
