# ESP32 Voice Assistant API

A small Python service that turns a voice clip into a spoken answer **in Rocky's voice** (the Eridian engineer from
*Project Hail Mary*). Speech-to-text and text-to-speech run in this process, the LLM is whatever OpenAI-compatible
endpoint you point it at, and the optional tools fetch weather, news and the like (see [Privacy](#privacy)).

```
ESP32 (mic) --WAV--> POST /talk --> STT (faster-whisper)
                                      -> LLM (your OpenAI-compatible endpoint) <-> tools (weather, news, timers...)
                                      -> TTS (Rocky: Qwen3-TTS voice clone)
ESP32 (speaker) <--WAV-- response <----'
ESP32 (idle) --GET /pending every 3 s--> due alarm/timer/reminder? -> Rocky's announcement as a WAV (else 204)
```

## Pipeline

1. **STT**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes the uploaded WAV.
2. **LLM**: the transcript goes to any OpenAI-compatible `/chat/completions` endpoint (Ollama, llama.cpp, LM Studio,
   vLLM…). The default system prompt gives it Rocky's personality ("Rocky fix ship. Good, good, good!").
3. **TTS**: [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) 12Hz 0.6B Base clones Rocky's voice zero-shot from a
   reference clip plus its exact transcript, picked by ear in the Rocky voice project (`~/src/repos/rocky`). The reply is
   downsampled to a 16 kHz / 16-bit / mono WAV the ESP32 can play.

## What Rocky can do

Ask in plain words; the LLM picks the tool (`app/tools.py`). Short-term memory keeps the last 4 exchanges for
5 minutes, so follow-ups work ("and tomorrow?", "tell me more about the first one").

| Ask | Tool | Source / where it's kept |
|---|---|---|
| "What time is it?" | `get_time` | local clock, `HOME_TZ` |
| "Do I need an umbrella tomorrow?" | `get_weather` | [Open-Meteo](https://open-meteo.com) (forecast + geocoding), default place `HOME_*` |
| "Give me the news" (world / Spain / local) | `get_news` | RSS: BBC World; El País + 20minutos; `LOCAL_NEWS_FEEDS`. Told in plain English |
| "Seconds in a week?" | `calculate` | safe arithmetic evaluator (no `eval`) |
| "Who was Ramón y Cajal?" | `lookup_wikipedia` | Wikipedia (en/es) |
| "Set a timer for 10 minutes", "remind me to call mum at 19:30" | `set_timer`, `set_reminder`, `list_alerts`, `cancel_alert` | `STATE_DIR/alerts.json`; announced by the speaker via `/pending` |
| "Wake me up at 7:30", "every weekday at 7" | `set_alarm` | same; rings with the morning briefing, composed ~2 min ahead |
| "Add milk to the list", "clear the list", "undo" | shopping-list tools | `STATE_DIR/shopping.json` |
| "Good morning!" | `get_morning_briefing` | weather + today's reminders + 2 news + shopping list size |
| "What song is this?" | `identify_song` | Shazam (unofficial, see below); the speaker's follow-up window records the music |
| "Send me the list / that recipe", "remind me **on my phone**" | `send_shopping_list`, `send_to_phone` | Telegram bot, one-way to `TELEGRAM_CHAT_ID` only |
| "How's my homelab?" | `get_homelab_status` | Kubernetes API + DCGM exporter (only when deployed in a cluster) |

**Honesty check.** With ~20 tools the model sometimes says "alarm set" without calling anything. `app/llm.py`
catches an action request ("wake me", "add", "cancel", "send"...) that ended without a tool call, nudges the model
once, and if it still claims success replies "Rocky could not do that" instead. Recognized songs also go to Telegram
when it's set up.

## Privacy

What leaves this machine, and only when a question needs it:

| Service | What it gets | When |
|---|---|---|
| Your LLM endpoint | the transcript, recent conversation, tool results | every question |
| Open-Meteo | coordinates of home or the place asked about | weather questions |
| RSS feeds (BBC, El País, 20minutos, `LOCAL_NEWS_FEEDS`) | a plain feed request | news questions |
| Wikipedia | the topic | fact look-ups |
| Shazam (via [shazamio](https://github.com/shazamio/ShazamIO), unofficial) | a fingerprint of ~10 s of the music recording | "what song is this?" |
| Telegram | the message text, to your chat only | "send it to my phone", phone reminders, recognized songs |

Never sent anywhere: the voice reference, your recordings (except the music clip's fingerprint), the bot token (not
logged: errors are scrubbed). The Telegram bot is **one-way**: nothing reads messages sent to it. `TOOLS_ENABLED=false`
turns all outside calls off but the LLM's.

shazamio is a reverse-engineered client of Shazam's API (no key): it can break whenever Shazam changes something, and
using it is against Shazam's terms. Rocky then just says he can't recognize songs right now.

## Setup

Needs [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `dnf install uv` on
Fedora 41+/`pip install uv` anywhere else). You don't need Python 3.12 pre-installed; `uv` fetches it.

```bash
./scripts/first_run.sh                # CPU
TORCH=cu128 ./scripts/first_run.sh    # NVIDIA GPU (RTX 50-series needs cu128); then set TTS_DEVICE=cuda in .env
```

It's safe to re-run, and every step skips what's already done:

1. creates `.venv` (Python 3.12) and installs `requirements.txt`
2. creates `.env` from `.env.example` if there isn't one (set `LLM_BASE_URL` / `LLM_MODEL` there)
3. checks for Rocky's voice reference in `rocky/`, and copies it from the Rocky voice project if it finds one
   (`ROCKY_REPO=/path`, default `~/src/repos/rocky`)
4. downloads the models into the Hugging Face cache: the Whisper model (`WHISPER_MODEL`) and Qwen3-TTS (`TTS_MODEL`,
   ~2.4 GB). Then the first `/talk` doesn't stall on a download (`scripts/download_models.py`)
5. checks that the LLM endpoint answers

On a fresh machine that's ~1 GB of Python packages (several GB with CUDA torch) plus ~2.5 GB of models; mind
metered connections. Offline check on a machine that already has everything:
`UV_OFFLINE=1 HF_HUB_OFFLINE=1 ./scripts/first_run.sh`.

## Voice reference (private, not in git)

Rocky's voice is cloned from a reference clip **cut from the film's audio**, so it's personal-use material and never
committed (`rocky/` and `*.wav` are gitignored). The API needs two files:

| File | What it is |
|---|---|
| `rocky/rocky_ref.wav` | ~10-30 s of clean speech in the target voice: mono, 16-bit PCM WAV (24 kHz is ideal), no music. Several short lines joined with ~0.2 s gaps are fine. |
| `rocky/rocky_ref.txt` | The **exact** transcript of that audio on one line, in the same order. Qwen3-TTS aligns text to audio, so a wrong word here audibly hurts the clone. |

Ways to get them:

- **From the Rocky voice project** (how it was built: vocal separation, line picking, A/B tested by ear):
  `./scripts/get_rocky_voice.sh /path/to/rocky`. It copies `voice/rocky_ref_golden.{wav,txt}`.
- **Bring your own**: drop any clip + transcript pair in `rocky/` (or point `TTS_REF_AUDIO` / `TTS_REF_TEXT`
  elsewhere). A different voice works the same way; adjust `SYSTEM_PROMPT` to match the character.

Only use voices you have the right to use, and don't publish clips or outputs cloned from them.

## Private settings (not in git)

| Where | What | How to set it up |
|---|---|---|
| `api/.env` | LLM endpoint/model, Whisper size, device, home location, Telegram token + chat id, any `SYSTEM_PROMPT` override | `first_run.sh` copies `.env.example`; edit it |
| `api/state/` | timers, alarms, reminders, shopping list | created on first use (`STATE_DIR`) |
| `api/rocky/` | voice reference | see above |
| ESP32 NVS | WiFi SSID/password and this server's URL (`http://<this machine's LAN IP>:8000/talk`) | the board's config portal, see [`firmware/README.md`](../firmware/README.md). The committed `sdkconfig` only has placeholders |

### Telegram (optional)

1. In Telegram, talk to **@BotFather**: `/newbot`, pick a name; it gives you the bot token.
2. Open your new bot's chat and press **Start**.
3. Find your chat id (in your own terminal, so the token doesn't end up anywhere else):
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'`
4. Put both in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) and restart.

## Run

```bash
# .env in the project root is picked up automatically
MALLOC_ARENA_MAX=2 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Bind to `0.0.0.0` so the ESP32 can reach it from the LAN. Startup loads Rocky's voice (~30 s on a CPU) before
the first request, so `/talk` doesn't pay for it.

## Run as a service

```bash
./scripts/install_service.sh --enable     # systemd user unit: starts at login, restarts on failure
journalctl --user -u esp32-homeai-api -f  # logs
```

It writes `~/.config/systemd/user/esp32-homeai-api.service` for this checkout's path, with `MALLOC_ARENA_MAX=2`.
Without `--enable` it only writes the unit.

## Docker

One `Dockerfile` builds both flavours (`TORCH` build arg). `docker-compose.yml` has a profile for each:

```bash
docker compose --profile cpu up -d --build    # CPU
docker compose --profile gpu up -d --build    # NVIDIA GPU (cu128 torch; needs the NVIDIA Container Toolkit)
docker compose logs -f
```

or without compose:

```bash
docker build -t esp32-homeai-api:cuda --build-arg TORCH=cu128 .
docker run -d --gpus 1 -p 8000:8000 \
  -v "$PWD/rocky:/app/rocky:ro,z" -v hf-cache:/cache/hf \
  -e TTS_DEVICE=cuda -e WHISPER_DEVICE=cuda -e WHISPER_COMPUTE_TYPE=float16 \
  -e LLM_BASE_URL=http://<llm-host>:11434/v1 esp32-homeai-api:cuda
```

- **SELinux (Fedora/RHEL)**: bind mounts need `,z` (as in the examples), or the container sees "Permission denied" and
  stops with "voice reference is missing".
- **Nothing private is in the image.** The voice reference is a read-only bind mount (`./rocky` → `/app/rocky`), and
  `.dockerignore` only lets `requirements.txt`, `app/` and two scripts in. The container refuses to start
  without the reference and says how to provide it.
- **Models live in the `hf-cache` volume**, downloaded on the first start (~2.5 GB) by the entrypoint
  (`scripts/docker_entrypoint.sh` → `download_models.py`). Later starts only check the cache, or skip the network
  entirely with `HF_HUB_OFFLINE=1`.
- **LLM address**: inside a container `localhost` is the container itself. Compose defaults to Ollama on the host
  (`host.docker.internal:11434`); set `DOCKER_LLM_BASE_URL` for anything else (e.g. `http://heavy1:30800/v1`).
  `.env` is shared with the native run for every other setting.
- **LLM speed**: with a Qwen3 thinking model on vLLM, `LLM_ENABLE_THINKING=false` skips the reasoning step: ~1 s
  instead of 4-9 s per reply on qwen3.8-27b, with the same answers on short voice questions.
- **Image size**: CPU ~2 GB; CUDA several GB (the torch wheels bundle CUDA + cuDNN, which faster-whisper reuses via
  `LD_LIBRARY_PATH`).
- **Thin GPU image** (`Dockerfile.thin`): this repo's `app/` plus the newer deps (faster-qwen3-tts, transformers 5,
  shazamio) as one ~75 MB layer on top of a published full `:cuda` image, so a node that already has it doesn't pull
  another ~12 GB: `docker build -f Dockerfile.thin -t <you>/esp32-homeai-api:cuda-tools .`
- Runs as a non-root user, has a `/health` healthcheck, and sets `MALLOC_ARENA_MAX=2`.

## Speed and memory

- **CPU-only laptop** (tested on a Ryzen 7 4700U): Rocky takes ~60 s for one short sentence, so a full `/talk`
  round trip is ~1-2 minutes. The firmware's `HTTP_TIMEOUT_MS` is 180 s to allow for that.
- **NVIDIA GPU**: install the cu128 torch wheels (see `requirements.txt`) and set `TTS_DEVICE=cuda` (and
  `WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE_TYPE=float16`). With faster-qwen3-tts (`TTS_ENGINE=faster`, the default on a
  GPU) speech is ~2.8x realtime on an RTX 5060 Ti: ~4-6 s per answer end to end, ~0.1 s of it Whisper.
- **15 GB RAM laptop**: this process (~3 GB: Whisper small + Qwen3-TTS fp32) plus an Ollama model on the iGPU is tight.
  llama3.2 at its default 12k context took ~3.6 GB and caused OOM kills. A 2k-context variant is plenty for short replies:

  ```bash
  printf 'FROM llama3.2\nPARAMETER num_ctx 2048\n' > Modelfile && ollama create llama3.2-2k -f Modelfile
  # then LLM_MODEL=llama3.2-2k
  ```

  On a machine that small, also:
  - `LLM_KEEP_ALIVE=0` (Ollama): unload the LLM right after each reply so its RAM is free while Rocky speaks.
  - Start uvicorn as `MALLOC_ARENA_MAX=2 .venv/bin/uvicorn ...`: stops per-thread memory pools from growing.
  - One `/talk` at a time: a request that arrives while the previous answer is still being made gets HTTP 503
    right away (a stacked second Whisper + TTS run is what got the process OOM-killed).

Every reply ends with a short fade and 0.5 s of silence. The firmware's I2S DMA keeps looping its last buffer after
playback, so audio ending mid-sound would repeat as a "tatatata" until the board is reset.

Tested end to end on 2026-10-19 (laptop, CPU, llama3.2-2k): "How long have you been together?" →
"A long time. Many years." in Rocky's voice, HTTP 200 in 70 s.

## Test without an ESP32

```bash
# record 4s from your mic (needs ffmpeg; on Fedora, `dnf install ffmpeg` from
# the RPM Fusion repo, since the stock repos only ship the -free build)
ffmpeg -f alsa -i default -ac 1 -ar 16000 -sample_fmt s16 -t 4 input.wav

./scripts/test_talk.sh input.wav
# -> prints X-Transcript / X-Reply headers, saves reply.wav
```

If `-f alsa -i default` can't find a device, your system is likely on PipeWire
without its ALSA compatibility layer (`pipewire-alsa` on Fedora, usually
installed by default on Workstation but not Server/minimal installs) — or
just record with any other tool and pass the resulting WAV to
`test_talk.sh` instead.

## API

`POST /talk` — send the recorded WAV either way:
- **raw body** — `Content-Type: audio/wav`, the WAV bytes as-is (simplest for the ESP32)
- **multipart** — form field **`audio`** = WAV file (curl `-F`, browsers)

Response: `audio/wav` body (the spoken reply). Debug headers:
`X-Transcript` and `X-Reply` (URL-encoded). The server also logs the received
content-type, byte count, and transcript for each request.

`GET /health` — `{"status": "ok", "counters": {"answered": N, "silence": N}}`; `GET /stats` — the counters alone.

`GET /pending` — polled by the speaker while idle: `200` + WAV with Rocky's announcement when an alarm, timer or
reminder is due, `204` otherwise (also `204` while a `/talk` is in progress).

A `/talk` right after "what song is this?" is treated as a music sample: it goes to Shazam instead of Whisper
(`X-Transcript: (music)`).

## ESP32 side

The [`firmware/`](../firmware) folder in this repo implements the other end: on **"Alexa"** or **"Hey Rocky"** it records mono 16-bit 16 kHz PCM into a WAV, POSTs it as the raw request body (not multipart) to `http://<host>:8000/talk`, plays the response back, and keeps listening ~6 s for a follow-up; while idle it polls `/pending` for announcements. See [`firmware/README.md`](../firmware/README.md) for the board it targets and how to configure the URL.

This endpoint returns a real WAV file (header included, via `wave.open()` in `app/tts.py`), not headerless raw PCM. The firmware handles this on its end — `find_pcm_payload()` in `voice_assistant.c` locates the actual `data` sub-chunk before playback instead of assuming either a fixed offset or a headerless body — so nothing needs to change here.

## Configuration

See `.env.example` for every knob (LLM endpoint/model, whisper size/device/hint words, Rocky's model/device/engine/
reference/temperature/language, output sample rate, silence gate, memory, tools, home location, Telegram). A `.env` in the project root is loaded on startup; real environment
variables still win over it. Inline `# comments` after unquoted values are ignored.

### Notes

- **Whisper decoding**: `app/stt.py` decodes PCM WAV itself and hands Whisper the samples. faster-whisper 1.2.1's own
  PyAV path calls `av.open(..., metadata_errors=...)`, which PyAV 19 rejects, and av isn't pinned.
- **Another language**: `TTS_LANGUAGE=Spanish` (and `WHISPER_LANGUAGE=es` plus a Spanish `SYSTEM_PROMPT`) makes Rocky
  speak Spanish with the same voice. Tried: it works, but cloned from the English reference he has a strong English
  accent. A reference clip in the target language would fix that.
- **Changing the voice**: it's tuned and A/B-tested by ear in `~/src/repos/rocky` (see its CLAUDE.md). Re-run
  `scripts/get_rocky_voice.sh` after the reference changes there.
