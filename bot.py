import os
from typing import Dict, Tuple

import discord
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "nocheckcertificate": True,
}


def headers_to_ffmpeg_args(headers: Dict[str, str]) -> str:
    if not headers:
        return ""
    header_blob = "".join(f"{key}: {value}\\r\\n" for key, value in headers.items())
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

    return voice


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
    )

    try:
        voice.play(source)
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


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set DISCORD_TOKEN in your environment before running the bot.")
    bot.run(token.strip())
