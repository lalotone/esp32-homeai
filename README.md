# esp32-homeai

A self-hosted voice assistant for the ESP32: speak to it, and it answers out loud.
Speech-to-text, the language model, and text-to-speech all run on your own machine.

```
ESP32 (mic) --WAV--> POST /talk --> STT --> LLM --> TTS
ESP32 (speaker) <--WAV-- response <-----------------'
```

## Components

| Folder | What it is |
| --- | --- |
| [`api/`](api/) | Python service that does the work: WAV in, STT → LLM → TTS, spoken WAV out. See [`api/README.md`](api/README.md). |
| `firmware/` | ESP32 code — records the clip, POSTs it, plays the reply. Not committed yet. |

## Hardware

An ESP32 with an I2S microphone (e.g. INMP441) and an I2S amplifier/speaker
(e.g. MAX98357A). The API returns 16 kHz / 16-bit / mono audio to suit them.
