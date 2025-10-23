import os
import subprocess
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

# Force .env values to override any pre-existing OS environment variables (e.g. stale tokens).
load_dotenv(override=True)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

YDL_OPTS = {
    "format": "18/bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "nocheckcertificate": True,
    "source_address": "0.0.0.0",
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
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)

    if not info:
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

    if not voice or not voice.is_connected():
        voice = await channel.connect()
    elif voice.channel != channel:
        await voice.move_to(channel)

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


async def handle_playback_after(
    ctx: commands.Context,
    error: Optional[Exception],
    source: discord.AudioSource,
    title: str,
) -> None:
    if error:
        await ctx.send(f"Playback error: {error}")
        return

    process = getattr(source, "process", None) or getattr(source, "_process", None)
    if not process:
        return

    return_code = process.returncode
    if return_code in (0, None):
        return

    stderr_output = ""
    stderr_pipe = getattr(process, "stderr", None)
    if stderr_pipe:
        try:
            stderr_output = stderr_pipe.read().decode("utf-8", errors="ignore")
        except Exception:
            stderr_output = ""

    message = f"Playback for '{title}' stopped unexpectedly. Exit code: {return_code}."
    if stderr_output:
        message += f" Details: {stderr_output.strip()}"

    print(message)
    await ctx.send(message)


@bot.event
async def on_ready():
    print(f"Bot ready as {bot.user} (id: {bot.user.id})")


@bot.command()
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
        return await ctx.send(f"Could not retrieve audio: {exc}")

    header_opts = headers_to_ffmpeg_args(headers)
    before_opts = f"{FFMPEG_BEFORE} {header_opts}".strip()

    source = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_BIN,
        before_options=before_opts,
        options=FFMPEG_OPTIONS,
        stderr=subprocess.PIPE,
    )

    try:
        voice.play(
            source,
            after=lambda err: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(handle_playback_after(ctx, err, source, title))
            ),
        )
    except Exception as exc:
        return await ctx.send(f"Could not start playback: {exc}")

    await ctx.send(f"Now playing: {title}")


@bot.command()
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
    token = clean_token(os.getenv("DISCORD_TOKEN"))
    if not token:
        raise RuntimeError("Set DISCORD_TOKEN in your environment before running the bot.")
    bot.run(token)
