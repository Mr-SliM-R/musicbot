import os
import asyncio
import discord
from discord.ext import commands
import yt_dlp

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")  # set to full path if needed

YDL_OPTS = {
    "format": "bestaudio[acodec~=/^(opus|mp4a)/]/bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "extract_flat": False,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

def headers_to_ffmpeg_args(headers: dict) -> str:
    # pass yt-dlp's HTTP headers to ffmpeg (critical for YouTube HLS)
    if not headers:
        return ""
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    # user-agent separately helps some environments
    ua = headers.get("User-Agent", "")
    ua_opt = f"-user_agent '{ua}'" if ua else ""
    return f'{ua_opt} -headers "{header_blob}"'

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        await ctx.author.voice.channel.connect()
    else:
        await ctx.send("Join a voice channel first.")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()

@bot.command()
async def play(ctx, *, query: str):
    # ensure we’re in voice
    if ctx.voice_client is None:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("Join a voice channel first.")

    vc = ctx.voice_client

    # stop current audio if any
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    # resolve query/url with yt-dlp
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:  # playlist/search results -> first entry
            info = next((e for e in info["entries"] if e), None)
            if info is None:
                return await ctx.send("No playable results.")
        stream_url = info.get("url")
        headers = info.get("http_headers", {})

    if not stream_url:
        return await ctx.send("Couldn’t get a streamable URL.")

    header_opts = headers_to_ffmpeg_args(headers)
    before = f"{FFMPEG_BEFORE} {header_opts}".strip()

    # Feed ffmpeg directly; avoids the .from_probe() failures
    audio_src = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_BIN,
        before_options=before,
        options=FFMPEG_OPTIONS,
    )

    # Optional: volume control wrapper (0.0–1.0)
    from discord import PCMVolumeTransformer
    audio_src = PCMVolumeTransformer(audio_src, volume=0.5)

    try:
        vc.play(audio_src)
        await ctx.send(f"▶️ Now playing: {info.get('title','(unknown)')}")
    except Exception as e:
        await ctx.send(f"Failed to start playback: {e}")

@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()

@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()

@bot.command()
async def stop(ctx):
    if ctx.voice_client:
        ctx.voice_client.stop()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}!")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set DISCORD_TOKEN in your environment before running the bot.")
    bot.run(token.strip())

