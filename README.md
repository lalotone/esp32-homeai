# esp32-homeai

A self-hosted voice assistant for the ESP32: speak to it, and it answers out loud, as **Rocky** from
*Project Hail Mary* (his voice and his "Good, good, good!" personality).
Speech-to-text, the language model, and text-to-speech all run on your own machine.

```
ESP32 (mic) --WAV--> POST /talk --> STT --> LLM --> TTS
ESP32 (speaker) <--WAV-- response <-----------------'
```

## Components

| Folder | What it is |
| --- | --- |
| [`api/`](api/) | Python service that does the work: WAV in, STT (Whisper) → LLM (any OpenAI-compatible endpoint) → TTS (Rocky's voice, Qwen3-TTS), spoken WAV out. See [`api/README.md`](api/README.md). |
| [`firmware/`](firmware/) | ESP-IDF firmware — wake word, local voice commands, records the clip, POSTs it, plays the reply. Includes a WiFi config portal for on-device setup. See [`firmware/README.md`](firmware/README.md). |

## Quick start

1. **Server**: `cd api && ./scripts/first_run.sh`. It installs everything and downloads the models. It also tells you
   about the two private pieces that aren't in git: Rocky's voice reference and your `.env`. See
   [`api/README.md`](api/README.md). Then `./scripts/install_service.sh --enable` keeps it running.
2. **LLM**: any OpenAI-compatible endpoint (Ollama, vLLM, ...) set in `api/.env`.
3. **Board**: build + flash [`firmware/`](firmware/), then set WiFi and `http://<server LAN IP>:8000/talk` in its
   config portal. Say **"Alexa"**, then **"assistant"**, then your question.

## Hardware

Built against the [Waveshare ESP32-S3-AUDIO-Board](https://docs.waveshare.com/ESP32-S3-AUDIO-Board): ES7210 4-mic ADC array, ES8311 DAC/speaker codec, TCA9555 GPIO expander, 16MB flash, 8MB Octal PSRAM. The API returns 16 kHz / 16-bit / mono audio to suit it; other ESP32 boards with an I2S mic (e.g. INMP441) and I2S amp/speaker (e.g. MAX98357A) would need different firmware but the same API contract.
