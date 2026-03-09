import discord
import os
import io
import sys
import asyncio
import subprocess
import tempfile
import ollama
import struct
import time
import shutil
import whisper
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

_FFMPEG_CANDIDATES = [
    r"C:\Users\VICTUS\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe",
    "ffmpeg",   # fallback: system PATH
]
_FFMPEG_EXE: str = ""
for _ff_path in _FFMPEG_CANDIDATES:
    _resolved = _ff_path if os.path.isfile(_ff_path) else shutil.which(_ff_path)
    if _resolved:
        _FFMPEG_EXE = _resolved
        _ff_bin_dir = os.path.dirname(_resolved)
        # Add bin dir to process PATH so Whisper's subprocess can find ffmpeg too
        if _ff_bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _ff_bin_dir + os.pathsep + os.environ.get("PATH", "")
        print(f"[ffmpeg] Using: {_resolved}")
        break
else:
    print("[ffmpeg] WARNING: ffmpeg not found — MP3 export and Whisper will fail.")

if sys.platform == "win32":
    _selector_loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(_selector_loop)

if not discord.opus.is_loaded():
    try:
        discord.opus._load_default()
        print("[Opus] Loaded successfully.")
    except Exception as e:
        print(f"[Opus] Warning: Could not load opus: {e}")

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
WHISPER_MODEL   = os.getenv("WHISPER_MODEL", "base")
LANGUAGE        = os.getenv("LANGUAGE", "th")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")
_guild_id       = os.getenv("GUILD_ID")
GUILD_IDS       = [int(_guild_id)] if _guild_id and _guild_id != "your_guild_id_here" else None
# GUILD_IDS = None  → global commands (up to 1 hour to appear)
# GUILD_IDS = [123] → guild-only commands (appear instantly, good for development)

print(f"[Whisper] Loading model: {WHISPER_MODEL} ...")
_t0 = time.perf_counter()
whisper_model = whisper.load_model(WHISPER_MODEL)
print(f"[Whisper] Model ready. ({time.perf_counter()-_t0:.1f}s)")

intents = discord.Intents.default()

bot = discord.Bot(intents=intents)

import discord.voice_client as _vc_module
import discord.opus as _opus_module

def _patched_recv_decoded_audio(self, data):
    from discord.sinks.core import RawData 

    if data.ssrc not in self.user_timestamps:
        if not self.user_timestamps or not self.sync_start:
            self.first_packet_timestamp = data.receive_time
            silence = 0
        else:
            silence = ((data.receive_time - self.first_packet_timestamp) * 48000) - 960
    else:
        dRT = (data.receive_time - self.user_timestamps[data.ssrc][1]) * 48000
        dT = data.timestamp - self.user_timestamps[data.ssrc][0]
        diff = abs(100 - dT * 100 / dRT)
        silence = dRT - 960 if (diff > 60 and dT != 960) else dT - 960

    self.user_timestamps[data.ssrc] = (data.timestamp, data.receive_time)

    data.decoded_data = (
        struct.pack("<h", 0) * max(0, int(silence)) * _opus_module._OpusStruct.CHANNELS
        + data.decoded_data
    )

    deadline = time.perf_counter() + 2.0
    while data.ssrc not in self.ws.ssrc_map:
        if time.perf_counter() > deadline:
            print(f"[Patch] ssrc {data.ssrc} not in ssrc_map after 2 s — using ssrc as user key")
            print(f"[Patch] ssrc_map contents: {dict(self.ws.ssrc_map)}")
            self.sink.write(data.decoded_data, data.ssrc)
            return
        time.sleep(0.05)

    entry = self.ws.ssrc_map[data.ssrc]
    user_id = entry["user_id"] if isinstance(entry, dict) else entry
    self.sink.write(data.decoded_data, int(user_id))

_vc_module.VoiceClient.recv_decoded_audio = _patched_recv_decoded_audio


import select as _select

def _patched_recv_audio(self, sink, callback, *args):
    import select as _sel
    self.user_timestamps = {}
    self.starting_time = time.perf_counter()
    packet_count = 0
    error_count = 0
    print("[RecvAudio] Thread started — waiting for UDP packets...")
    while self.recording:
        try:
            ready, _, err = _sel.select([self.socket], [], [self.socket], 0.01)
        except Exception as e:
            print(f"[RecvAudio] select() error: {e}")
            break
        if not ready:
            if err:
                print(f"[RecvAudio] Socket error from select: {err}")
            continue
        try:
            data = self.socket.recv(4096)
        except OSError as e:
            print(f"[RecvAudio] socket.recv OSError: {e}")
            self.stop_recording()
            continue
        packet_count += 1
        if packet_count <= 5 or packet_count % 100 == 0:
            print(f"[RecvAudio] Packet #{packet_count}  len={len(data)}  first_byte={data[0]:02x}  second_byte={data[1]:02x}")
        try:
            self.unpack_audio(data)
        except Exception as e:
            error_count += 1
            if error_count <= 5:
                import traceback
                print(f"[RecvAudio] unpack_audio error #{error_count}: {e}")
                traceback.print_exc()
    print(f"[RecvAudio] Loop ended — total packets={packet_count}  errors={error_count}")
    self.stopping_time = time.perf_counter()
    self.sink.cleanup()
    cb = asyncio.run_coroutine_threadsafe(callback(sink, *args), self.loop)
    result = cb.result()
    if result is not None:
        print(result)

_vc_module.VoiceClient.recv_audio = _patched_recv_audio

def _patched_empty_socket(self):
    try:
        if self.socket is None:
            return
        ready, _, _ = _select.select([self.socket], [], [], 0.0)
        while ready:
            self.socket.recv(4096)
            ready, _, _ = _select.select([self.socket], [], [], 0.0)
    except (TypeError, OSError):
        pass

_vc_module.VoiceClient.empty_socket = _patched_empty_socket

recording_sessions: dict[int, dict] = {}
voice_clients: dict[int, discord.VoiceClient] = {}

def _sync_mix_to_mp3(wav_bytes_list: list[bytes]) -> bytes:
    """
    Mix one or more WAV byte strings and return an MP3 byte string.
    Uses ffmpeg directly — bypasses pydub's export (which silently fails on some setups).
    """
    tmp_paths: list[str] = []
    try:
        for raw in wav_bytes_list:
            fd, path = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            tmp_paths.append(path)

        cmd = [_FFMPEG_EXE, "-y"]
        for p in tmp_paths:
            cmd += ["-i", p]
        if len(tmp_paths) > 1:
            cmd += ["-filter_complex", f"amix=inputs={len(tmp_paths)}:duration=longest:normalize=0"]
        cmd += ["-f", "mp3", "-ab", "128k", "pipe:1"]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[-800:]
            print(f"[MP3] ffmpeg exited {result.returncode}:\n{err}")
        print(f"[MP3] ffmpeg produced {len(result.stdout)} bytes")
        return result.stdout
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


async def transcribe_audio(audio_bytes: bytes, user_label: str = "") -> str:
    """
    Runs Whisper inference in a thread pool so the async event loop is not blocked.
    Whisper is CPU/GPU-bound and synchronous, so we offload it to a worker thread.
    """
    def _sync_transcribe() -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            print(f"[Whisper] Transcribing {user_label} ({len(audio_bytes)//1024} KB) ...")
            segments, _info = whisper_model.transcribe(tmp_path, language=LANGUAGE, beam_size=5)
            text = "".join(seg.text for seg in segments).strip()
            print(f"[Whisper] Done {user_label}: {text[:80]}{'...' if len(text)>80 else ''}")
            return text
        finally:
            os.unlink(tmp_path)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_transcribe)


async def finished_callback(
    sink: discord.sinks.WaveSink,
    channel: discord.TextChannel,
    *args,
):
    """Triggered automatically when stop_recording() is called."""
    print(f"[Callback] sink type       : {type(sink)}")
    print(f"[Callback] audio_data keys : {list(sink.audio_data.keys())}")
    for uid, aud in sink.audio_data.items():
        aud.file.seek(0, 2)
        size = aud.file.tell()
        aud.file.seek(0)
        print(f"[Callback]   user {uid}: {size} bytes")

    mentioned_users = [f"<@{uid}>" for uid in sink.audio_data]

    await channel.send(
        f"🎙️ Recorded from: {', '.join(mentioned_users) if mentioned_users else 'no one'}\n"
        f"⏳ Converting audio & transcribing with Whisper ({WHISPER_MODEL})..."
    )

    user_audio_bytes: dict[int, bytes] = {}
    for user_id, audio in sink.audio_data.items():
        audio.file.seek(0)
        raw_bytes = audio.file.read()
        if len(raw_bytes) < 1_000:
            continue
        user_audio_bytes[user_id] = raw_bytes
        print(f"[Record] User {user_id}: captured {len(raw_bytes)} bytes")

    if user_audio_bytes:
        try:
            loop = asyncio.get_running_loop()
            wav_list = list(user_audio_bytes.values())
            mp3_bytes = await loop.run_in_executor(None, _sync_mix_to_mp3, wav_list)
            if mp3_bytes and len(mp3_bytes) > 1_000:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                await channel.send(
                    "🎵 **Meeting recording (MP3):**",
                    file=discord.File(io.BytesIO(mp3_bytes), filename=f"meeting_{ts}.mp3"),
                )
            else:
                await channel.send("⚠️ MP3 export produced an empty file — check ffmpeg PATH.")
        except Exception as exc:
            import traceback
            print(f"[MP3] Export failed: {exc}")
            traceback.print_exc()
            await channel.send(f"⚠️ MP3 export failed: {exc}")

    await channel.send(f"Transcribing with Whisper `{WHISPER_MODEL}` (this may take a minute on CPU)...")

    all_transcripts: list[str] = []
    for user_id, raw_bytes in user_audio_bytes.items():
        try:
            user_label = str(user_id)
            text = await transcribe_audio(raw_bytes, user_label)
            if not text:
                continue
            try:
                user = await bot.fetch_user(user_id)
                username = user.display_name
            except Exception:
                username = f"User-{user_id}"
            all_transcripts.append(f"**{username}**: {text}")
        except Exception as exc:
            print(f"[Whisper Error] user {user_id}: {exc}")

    if not all_transcripts:
        await channel.send("❌ No speech detected in this recording.")
        return

    full_transcript = "\n".join(all_transcripts)

    await channel.send(f"Summarizing with Ollama ({OLLAMA_MODEL})...")
    try:
        def _sync_summarize() -> str:
            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "คุณคือ AI ผู้ช่วยสรุปการประชุม "
                            "ให้เขียนสรุปเป็น Markdown โดยใช้หัวข้อดังนี้: "
                            "## ภาพรวม, ## ประเด็นสำคัญ, ## สิ่งที่ต้องดำเนินการ "
                            "เขียนให้กระชับ ชัดเจน และตอบเป็นภาษาไทยเสมอ"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"นี่คือบทสนทนาที่บันทึกไว้:\n\n{full_transcript}\n\nกรุณาสรุปการสนทนานี้เป็นภาษาไทย",
                    },
                ],
            )
            return response["message"]["content"]

        loop = asyncio.get_running_loop()
        summary_md = await loop.run_in_executor(None, _sync_summarize)
    except Exception as exc:
        await channel.send(
            f"❌ Summarization failed: {exc}\n"
            f"Make sure Ollama is running and `{OLLAMA_MODEL}` is pulled.\n"
            f"Run: `ollama pull {OLLAMA_MODEL}`"
        )
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"## 📋 สรุปการประชุม — {timestamp}\n\n"

    full_message = header + summary_md
    for chunk in [full_message[i : i + 1990] for i in range(0, len(full_message), 1990)]:
        await channel.send(chunk)

    transcript_msg = "### 📝 บทสนทนาทั้งหมด\n" + "\n".join(
        f"||{line}||" for line in all_transcripts
    )
    if len(transcript_msg) <= 1990:
        await channel.send(transcript_msg)

@bot.slash_command(name="help", description="Show all available commands", guild_ids=GUILD_IDS)
async def help_command(ctx: discord.ApplicationContext):
    embed = discord.Embed(
        title="📖 Bot Commands",
        description="Voice recording + AI summarization bot",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="</join:0> `/join`",
        value="Bot joins your current voice channel.",
        inline=False,
    )
    embed.add_field(
        name="🔴 `/record [output_channel]`",
        value=(
            "Start recording audio in the voice channel.\n"
            "`output_channel` *(optional)* — channel to post the summary in. "
            "Defaults to the channel where you typed the command."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏹️ `/stop`",
        value=(
            "Stop recording and process the audio.\n"
            "Bot will **transcribe** with Whisper then **summarize** with Ollama "
            "and post a Markdown summary."
        ),
        inline=False,
    )
    embed.add_field(
        name="👋 `/leave`",
        value="Bot leaves the voice channel. Stops recording first if active.",
        inline=False,
    )
    embed.add_field(
        name="❓ `/help`",
        value="Show this help message.",
        inline=False,
    )

    embed.set_footer(text=f"Whisper: {WHISPER_MODEL}  |  LLM: {OLLAMA_MODEL}  |  Lang: {LANGUAGE}")

    await ctx.respond(embed=embed, ephemeral=True)



@bot.slash_command(name="join", description="Bot joins your current voice channel", guild_ids=GUILD_IDS)
async def join(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        try:
            await ctx.respond("❌ You must be in a Voice Channel first.", ephemeral=True)
        except Exception:
            pass
        return

    channel = ctx.author.voice.channel

    deferred = False
    try:
        await ctx.defer()
        deferred = True
    except discord.errors.NotFound:
        print("[Join] Interaction expired before defer — joining anyway")
    except Exception as exc:
        print(f"[Join] Defer failed: {exc}")

    try:
        existing_vc = ctx.guild.voice_client
        if existing_vc:
            await existing_vc.move_to(channel)
            vc = existing_vc
        else:
            vc = None
            last_exc = None
            for attempt in range(3):
                try:
                    stale = ctx.guild.voice_client
                    if stale:
                        await stale.disconnect(force=True)
                        await asyncio.sleep(0.5)
                    vc = await channel.connect(timeout=60, reconnect=True)
                    break
                except Exception as e:
                    last_exc = e
                    print(f"[Join] Connect attempt {attempt+1} failed: {e}, retrying...")
                    await asyncio.sleep(1)
            if vc is None:
                raise last_exc

        await asyncio.sleep(1)

        if hasattr(vc, '_connected'):
            vc._connected.set()
        voice_clients[ctx.guild_id] = vc
        print(f"[Join] Successfully joined {channel.name}")
        if deferred:
            await ctx.followup.send(f"✅ Joined **{channel.name}**")
        else:
            await ctx.channel.send(f"✅ Joined **{channel.name}**")
    except Exception as exc:
        import traceback
        print(f"[Join] Failed: {exc}")
        traceback.print_exc()
        msg = f"❌ Failed to join: {exc or type(exc).__name__}"
        try:
            await (ctx.followup.send(msg) if deferred else ctx.channel.send(msg))
        except Exception:
            pass


@bot.slash_command(name="record", description="Start recording audio in the voice channel", guild_ids=GUILD_IDS)
@discord.option(
    "output_channel",
    discord.TextChannel,
    description="Channel to post the summary in (default: this channel)",
    required=False,
)
async def record(
    ctx: discord.ApplicationContext,
    output_channel: discord.TextChannel = None,
):
    vc = ctx.guild.voice_client or voice_clients.get(ctx.guild_id)
    if not vc or not vc.channel:
        try:
            await ctx.respond("❌ Bot is not in a Voice Channel. Use `/join` first.", ephemeral=True)
        except Exception:
            await ctx.channel.send("❌ Bot is not in a Voice Channel. Use `/join` first.")
        return

    if ctx.guild_id in recording_sessions:
        try:
            await ctx.respond("⚠️ Already recording. Use `/stop` to stop.", ephemeral=True)
        except Exception:
            await ctx.channel.send("⚠️ Already recording. Use `/stop` to stop.")
        return

    deferred = False
    try:
        await ctx.defer()
        deferred = True
    except Exception:
        pass

    async def reply(msg):
        if deferred:
            await ctx.followup.send(msg)
        else:
            await ctx.channel.send(msg)

    target_channel = output_channel or ctx.channel
    recording_sessions[ctx.guild_id] = {"channel": target_channel}

    sock  = getattr(vc, 'socket', None)
    ws_obj = getattr(vc, 'ws', None)
    print(f"[Record] PRE-start: socket={sock!r}")
    print(f"[Record] PRE-start: ws={ws_obj!r}")
    print(f"[Record] PRE-start: _connected.is_set={vc._connected.is_set()}")
    if sock is not None:
        vc._connected.set()
    print(f"[Record] PRE-start: is_connected={vc.is_connected()}  recording={getattr(vc,'recording',False)}")

    try:
        vc.start_recording(
            discord.sinks.WaveSink(),
            finished_callback,
            target_channel,
        )
        print("[Record] start_recording() succeeded")
    except Exception as _sr_err:
        import traceback
        print(f"[Record] start_recording() FAILED: {_sr_err}")
        traceback.print_exc()
        del recording_sessions[ctx.guild_id]
        await reply(f"❌ Could not start recording: {_sr_err}")
        return

    await reply(
        f"🔴 **Recording started** — summary will be posted in {target_channel.mention}\n"
        f"Use `/stop` when done."
    )


@bot.slash_command(name="stop", description="Stop recording and generate a Markdown summary", guild_ids=GUILD_IDS)
async def stop(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client or voice_clients.get(ctx.guild_id)
    if not vc:
        try:
            await ctx.respond("❌ Bot is not in a Voice Channel.", ephemeral=True)
        except Exception:
            await ctx.channel.send("❌ Bot is not in a Voice Channel.")
        return

    if ctx.guild_id not in recording_sessions:
        try:
            await ctx.respond("⚠️ Not currently recording. Use `/record` first.", ephemeral=True)
        except Exception:
            await ctx.channel.send("⚠️ Not currently recording. Use `/record` first.")
        return

    deferred = False
    try:
        await ctx.defer()
        deferred = True
    except Exception:
        pass

    del recording_sessions[ctx.guild_id]
    vc.stop_recording()
    msg = "⏹️ Recording stopped — processing, please wait..."
    if deferred:
        await ctx.followup.send(msg)
    else:
        await ctx.channel.send(msg)


@bot.slash_command(name="leave", description="Bot leaves the voice channel", guild_ids=GUILD_IDS)
async def leave(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client or voice_clients.get(ctx.guild_id)
    if not vc:
        try:
            await ctx.respond("❌ Bot is not in a Voice Channel.", ephemeral=True)
        except Exception:
            await ctx.channel.send("❌ Bot is not in a Voice Channel.")
        return

    deferred = False
    try:
        await ctx.defer()
        deferred = True
    except Exception:
        pass

    if ctx.guild_id in recording_sessions:
        del recording_sessions[ctx.guild_id]
        try:
            vc.stop_recording()
        except Exception:
            pass
    voice_clients.pop(ctx.guild_id, None)
    await vc.disconnect()
    msg = "👋 Left the voice channel."
    if deferred:
        await ctx.followup.send(msg)
    else:
        await ctx.channel.send(msg)

@bot.event
async def on_ready():
    print(f"✅ Bot is ready: {bot.user} (ID: {bot.user.id})")
    print(f"   Guilds: {[g.name for g in bot.guilds]}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Log voice state changes for the bot itself."""
    if member.id != bot.user.id:
        return

    guild_id = member.guild.id
    if before.channel is not None and after.channel is None:
        print(f"[Voice] ⚠️ Bot was disconnected from '{before.channel.name}' (guild {guild_id})", flush=True)
        if guild_id in recording_sessions:
            print(f"[Voice] Recording was active — pycord will attempt internal reconnect", flush=True)
    elif before.channel is None and after.channel is not None:
        print(f"[Voice] Bot joined '{after.channel.name}'", flush=True)
    elif before.channel != after.channel and after.channel is not None:
        print(f"[Voice] Bot moved: '{before.channel.name}' → '{after.channel.name}'", flush=True)


if __name__ == "__main__":
    import signal

    loop = asyncio.get_event_loop()

    async def _runner():
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            if not bot.is_closed():
                await bot.close()

    try:
        loop.run_until_complete(_runner())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
