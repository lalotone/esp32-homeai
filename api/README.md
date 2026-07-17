# ESP32 Voice Assistant API

A small Python service that turns a voice clip into a spoken answer, all offline
except for whatever LLM endpoint you point it at.

```
ESP32 (mic) --WAV--> POST /talk --> STT (faster-whisper)
                                      -> LLM (your OpenAI-compatible endpoint)
                                      -> TTS (Piper)
ESP32 (speaker) <--WAV-- response <----'
```

## Pipeline

1. **STT** — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes the uploaded WAV.
2. **LLM** — the transcript is sent to any OpenAI-compatible `/chat/completions` endpoint (Ollama, llama.cpp, LM Studio, vLLM…).
3. **TTS** — the [Piper](https://github.com/rhasspy/piper) standalone binary synthesizes the reply and it's downsampled to a 16 kHz / 16-bit / mono WAV the ESP32 can play.

## Setup

Needs [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `dnf install uv` on Fedora 41+/`pip install uv` anywhere else) and `curl`/`tar` for the download scripts — no other system packages required. You don't need Python 3.12 pre-installed either; `uv` downloads a matching interpreter itself if your system one doesn't match.

```bash
# Use Python 3.12 — faster-whisper deps may lack wheels on 3.13+
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt

# TTS: the standalone Piper binary -> ./piper/  (avoids the piper-phonemize
# pip package, which has no Linux wheels; x86_64 only, see script for details)
./scripts/download_piper.sh

# Download the default Piper voice into ./voices/
./scripts/download_voice.sh

# Configure (all optional — sane defaults otherwise)
cp .env.example .env   # edit LLM_BASE_URL / LLM_MODEL to match your setup
```

faster-whisper downloads its model automatically on first run.

## Run

```bash
# .env in the project root is picked up automatically
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Bind to `0.0.0.0` so the ESP32 can reach it from the LAN.

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

`GET /health` — `{"status": "ok"}`.

## ESP32 side

The [`firmware/`](../firmware) folder in this repo implements the other end: records mono 16-bit 16 kHz PCM into a WAV, POSTs it as the raw request body (not multipart) to `http://<host>:8000/talk` on hearing the **"assistant"** voice command, and plays the response back. See [`firmware/README.md`](../firmware/README.md) for the board it targets and how to configure the URL.

This endpoint returns a real WAV file (header included, via `wave.open()` in `app/tts.py`), not headerless raw PCM. The firmware handles this on its end — `find_pcm_payload()` in `voice_assistant.c` locates the actual `data` sub-chunk before playback instead of assuming either a fixed offset or a headerless body — so nothing needs to change here.

## Configuration

See `.env.example` for every knob (LLM endpoint/model, whisper size/device,
Piper voice, output sample rate). A `.env` in the project root is loaded on
startup; real environment variables still win over it.

### Another language

Grab a matching Piper voice, then point `PIPER_MODEL` at it and pin
`WHISPER_LANGUAGE` — auto-detect misfires on short clips, especially with the
smaller whisper models. For Castilian Spanish:

```bash
NAME=es_ES-davefx-medium REL=es/es_ES/davefx/medium ./scripts/download_voice.sh
```

```ini
WHISPER_MODEL=small
WHISPER_LANGUAGE=es
PIPER_MODEL=voices/es_ES-davefx-medium.onnx
SYSTEM_PROMPT=Eres un asistente de voz. Responde siempre en español de España, en una o dos frases cortas y naturales.
```

Voice list: [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
