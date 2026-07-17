# firmware

ESP-IDF firmware for [esp32-homeai](../README.md), targeting the [Waveshare ESP32-S3-AUDIO-Board](https://docs.waveshare.com/ESP32-S3-AUDIO-Board).
Wake word "Alexa" opens a local command window; the **"assistant"** command records 5s of audio, POSTs it to the [`api`](../api) service (or any server implementing the same contract), and plays the spoken response back on the speaker.

Board hardware in play: ES7210 4-mic ADC array, ES8311 DAC/speaker codec, TCA9555 GPIO expander, 16MB flash, 8MB Octal PSRAM.

## Prerequisites

- ESP-IDF v6.0.2 installed (this project was built/tested against that version specifically — the code needed patching to compile against it, see below). If you don't have it yet:

  ```bash
  # System packages ESP-IDF's installer itself needs (Fedora):
  sudo dnf install git wget flex bison gperf python3 cmake ninja-build ccache dfu-util libusbx

  # Debian/Ubuntu equivalent:
  # sudo apt install git wget flex bison gperf python3 python3-pip python3-venv cmake ninja-build ccache libffi-dev libssl-dev dfu-util libusb-1.0-0

  git clone -b v6.0.2 --recursive https://github.com/espressif/esp-idf.git ~/.espressif/v6.0.2/esp-idf
  ~/.espressif/v6.0.2/esp-idf/install.sh esp32s3
  ```

  See [Espressif's official get-started guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/get-started/linux-macos-setup.html) if anything above doesn't match your setup.

- Board connected via USB. It enumerates as a USB-Serial/JTAG port, e.g. `/dev/ttyACM0` on Linux (or `/dev/ttyUSB0` if using an external UART bridge instead). If flashing fails with a permissions error (not the "no serial data" kind below, an outright "Permission denied" on the port), add yourself to the group that owns it and log out/in — usually `dialout` (`sudo usermod -aG dialout $USER`), occasionally `uucp` on some distros; check with `ls -l /dev/ttyACM0`.
- A WiFi network and a server on that network to receive/answer voice requests (see "Voice assistant setup" below) — without this configured, the device still boots and runs the local voice commands, it just can't reach the assistant. The [`api`](../api) service in this repo implements the server side.

## Repo layout

`sdkconfig` is committed here, unusually for an ESP-IDF project. Normally it's regenerated from `sdkconfig.defaults` and left out of version control, but in this project most of what makes it actually build correctly for this board — 16MB flash size, Octal (not Quad) PSRAM mode, the WakeNet9/MultiNet5 model selection, all 32 command phrases — only ever ended up in the full `sdkconfig` via `idf.py menuconfig`, never distilled into `sdkconfig.defaults`. Committing just `sdkconfig.defaults` means a fresh clone doesn't build (missing speech models, wrong flash size). The WiFi/URL fields in the committed `sdkconfig` are intentionally-invalid placeholders (`YourWiFiSSID`, etc.) — real values belong in the config portal (below) or your own local `sdkconfig`, never committed.

`managed_components/` (external component sources) and `build/` aren't committed — `idf.py build` regenerates both, the former from `main/idf_component.yml` + `dependencies.lock`.

## Build

```bash
source $HOME/.espressif/v6.0.2/esp-idf/export.sh   # adjust path to your IDF install
idf.py build
```

## Flash

```bash
idf.py -p /dev/ttyACM0 flash
```

Or build + flash + open the serial monitor in one step:

```bash
idf.py -p /dev/ttyACM0 flash monitor
```

(Exit the monitor with `Ctrl+]`.)

**If flashing fails with `Failed to connect... No serial data received`:** first check `ls /dev/ttyUSB* /dev/ttyACM*` — some boards/cables expose more than one serial device, and the wrong one gives exactly this error rather than a clearer "not found." If you're on the right port and it still fails, put the board in bootloader mode manually: hold **BOOT**, tap **RESET** while still holding BOOT, then release — some USB-serial adapters (older PL2303/CH340-based ones especially) don't drive the auto-reset lines ESP-IDF expects.

## Run

On boot the device initializes I2C, both audio codecs, loads the speech models from the `model` partition, connects to WiFi (waits up to 15s, then keeps retrying in the background if it fails), and starts listening.

Say **"Alexa"** — it's a pure wake word now, it doesn't do anything by itself. It just opens a listening window for one spoken command:

- "turn on the backlight" / "turn off the backlight" / "backlight is brightest" / "backlight is darkest" — just log a message in `main.c`'s `Speech_event_callback` (case `ESP_SR_EVT_CMD`); wire in real backlight control there if you want them to do something.
- "record for five seconds" — records 5s from the mic array and plays it straight back on the speaker, a local (no-network) sanity check that the mic array and speaker both work end-to-end.
- "highest volume" / "lowest volume" / "increase the volume" / "decrease the volume" — adjusts playback volume (0-100%); the increase/decrease commands step by 10 and clamp at the ends.
- **"assistant"** — if WiFi is connected, records 5 seconds of audio, POSTs it to `CONFIG_VOICE_ASSISTANT_ENDPOINT_URL`, and plays back whatever comes back on the speaker (see "Voice assistant setup" below for the server contract). If WiFi isn't connected, it flashes red twice instead — there's nothing to send the recording to.

So the full pattern for talking to the assistant is **"Alexa" → "assistant"**, two commands, not one — this keeps the wake word itself lightweight and lets you use any of the local commands above without triggering a network round trip.

Every command re-opens the listening window, so you can chain several without repeating "Alexa" each time — e.g. "Alexa" → "increase the volume" → "increase the volume" → "assistant" all in one go, as long as each command lands before the window times out.

The board's 7-LED ring gives visual feedback of what's happening, since the serial log is otherwise the only sign of life:

| Ring state | Meaning |
|---|---|
| Off | Idle, waiting for "Alexa" |
| Solid cyan | Wake word detected, listening for a command |
| Quick green flash, then back to cyan | Command recognized (still listening for another) |
| Off | Command window timed out, back to idle |
| Solid magenta | Recording (local echo, or the "assistant" command) |
| Solid yellow | Uploading to the assistant endpoint / waiting for its response |
| Solid blue | Playing back audio (local echo, or the assistant's response) |
| Red flash x2, then back to cyan | "Assistant" command failed (no WiFi, or recording/upload/server error) |

## WiFi & config portal

WiFi credentials and the assistant endpoint URL are stored in NVS at runtime and can be set two ways:

**A. Config portal (recommended — no reflash needed).** If the board can't connect to WiFi within 15s of boot, it opens an open access point named `ESP32-Setup-XXXX` (last 2 bytes of its MAC) alongside its normal background retry attempts, so local commands keep working the whole time. Join that AP, browse to `http://192.168.4.1/`, and fill in the WiFi SSID/password and/or assistant server URL — saving writes both to NVS and reboots the board.

You can force this open even if the board would connect fine — e.g. to move it to a new network — by holding the **BOOT** button through power-on/reset. Once connected, the same config page stays reachable at the board's normal WiFi IP (`nmap` your LAN or check the serial log), so you can update the assistant URL live without ever re-opening the setup AP — handy if your server's IP changes.

Anyone on your LAN who finds that IP can currently read/change these settings — there's no auth on the config page. Fine for a home network, worth knowing if you deploy this somewhere less trusted.

**B. Kconfig defaults (factory fallback).** `idf.py menuconfig` → `ESP SR Demo Configuration` sets the values baked in at build time (`CONFIG_ESP_WIFI_SSID`/`PASSWORD`/`CONFIG_VOICE_ASSISTANT_ENDPOINT_URL`) — used only until something's been saved via the portal, which then takes priority. These land in `sdkconfig`, which **is committed** (see "Repo layout" below for why) — don't leave real credentials in it before committing; the placeholder values checked in (`YourWiFiSSID` etc.) are intentionally invalid.

Plain HTTP is fine for a server on your LAN; HTTPS also works since `esp_http_client` supports it, but you'll additionally need to configure a CA certificate (`esp_http_client_config_t.cert_pem` or the ESP-IDF cert bundle component) if you point it at a public HTTPS endpoint — not set up here.

## Voice assistant setup

**The contract your server needs to implement** (already done by [`api`](../api) in this repo — point the URL above at `http://<host>:8000/talk` and skip the rest of this section):

| | Request (ESP → server) | Response (server → ESP) |
|---|---|---|
| Method | `POST` | HTTP 200 |
| Body | Canonical 44-byte-header WAV: PCM16, mono, 16000 Hz, ~5s | Raw PCM16 little-endian, mono, 16000 Hz — **no WAV header** |
| `Content-Type` | `audio/wav` | (ignored by the firmware) |

The response is capped at 2MB (~64s of audio) and played back with no parsing beyond "treat every 2 bytes as one sample" — so if you're generating it with a TTS API that returns WAV or MP3, strip the container/decode to raw PCM at that exact rate before sending it back (e.g. with `pydub`/`ffmpeg`, or your TTS provider's raw-PCM output option if it has one).

`api`'s `/talk` actually returns a real WAV file (44-byte header included, via Python's `wave` module in `app/tts.py`), not headerless raw PCM as the table above specifies. `voice_assistant.c` handles this: `find_pcm_payload()` walks the RIFF/WAVE chunk headers to find where the `data` sub-chunk actually starts and only plays back from there, rather than assuming a fixed 44-byte offset — so it also still works unmodified against a server that follows the documented contract literally and returns headerless raw PCM (it just falls through to treating the whole body as PCM if it doesn't look like a WAV container at all).

**Building your own server instead of using `api`:** receive the WAV, run speech-to-text, send the transcript to an LLM, synthesize the reply with TTS, convert that to raw PCM16 mono 16kHz, and return it as the response body. A minimal Python/Flask skeleton to validate the ESP round trip before wiring up real STT/LLM/TTS (it just echoes a 1-second beep back so you can confirm audio flows both ways):

```python
from flask import Flask, request, Response
import struct, math

app = Flask(__name__)

@app.route("/voice", methods=["POST"])
def voice():
    wav_bytes = request.get_data()
    pcm = wav_bytes[44:]  # strip the 44-byte WAV header
    print(f"Received {len(pcm)} bytes of audio")

    # TODO: replace with real STT -> LLM -> TTS. For now, reply with a
    # 1-second 440Hz beep as raw PCM16 mono 16kHz, to confirm playback works.
    sample_rate = 16000
    samples = [int(3000 * math.sin(2 * math.pi * 440 * t / sample_rate)) for t in range(sample_rate)]
    pcm_response = struct.pack(f"<{len(samples)}h", *samples)
    return Response(pcm_response, mimetype="application/octet-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

Run it, point `VOICE_ASSISTANT_ENDPOINT_URL` at `http://<your-machine-ip>:8000/voice`, reflash, and say "Alexa" — you should hear a beep back a few seconds later. Once that works, swap the `TODO` for a real pipeline.

**How the server "waits" for STT -> LLM -> TTS:** there's no special mechanism needed — it's just three ordinary blocking calls, one after another, inside the same request handler. Flask (or any WSGI server) doesn't send anything back to the ESP until your handler function actually returns, so as long as each step's blocking call is chained before the `return`, the ESP's HTTP connection just stays open for the whole chain. Concretely, with OpenAI's APIs as an example (`pip install flask openai pydub`, plus `ffmpeg` installed for `pydub` to decode the TTS output):

```python
import io, os
from flask import Flask, request, Response
from openai import OpenAI
from pydub import AudioSegment

app = Flask(__name__)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

@app.route("/voice", methods=["POST"])
def voice():
    wav_bytes = request.get_data()  # whole WAV file, header included

    # 1. Speech-to-text -- blocks until the transcript comes back
    transcript = client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.wav", wav_bytes, "audio/wav"),
    ).text
    print(f"Heard: {transcript}")

    # 2. LLM -- blocks until the chat completion finishes
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": transcript}],
    ).choices[0].message.content
    print(f"Replying: {reply}")

    # 3. Text-to-speech -- blocks until the synthesized audio comes back (as MP3)
    speech = client.audio.speech.create(model="tts-1", voice="alloy", input=reply)

    # Firmware expects raw PCM16 mono 16kHz with no container/header, so
    # decode + resample the MP3 before responding.
    audio = AudioSegment.from_file(io.BytesIO(speech.content), format="mp3")
    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)

    return Response(audio.raw_data, mimetype="application/octet-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

Three sequential API calls like this can easily take several seconds to tens of seconds depending on the models and reply length, so `HTTP_TIMEOUT_MS` in `voice_assistant.c` is set to 60s rather than the more typical few-seconds default — bump it further if you're using slower models. The ring stays solid yellow (uploading/waiting) for that entire duration, so there's at least visible feedback that it hasn't hung.

## Adding more voice commands

This is the same recipe used to add "record for five seconds" (command ID4). Commands 0–4 are taken; IDs 5–199 are free (Kconfig defines up to `CONFIG_EN_SPEECH_COMMAND_ID199`; MultiNet itself supports up to 400 registered phrases total, `ESP_MN_MAX_PHRASE_NUM`, so there's plenty of headroom).

**1. Set up the phoneme generator (one-time).** MultiNet5 — the model this project uses — doesn't match plain English text. Commands have to be converted to Espressif's own phonetic spelling first, using their tool:

```bash
python3 -m venv /tmp/g2p_venv
source /tmp/g2p_venv/bin/activate
pip install g2p_en pandas
python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng')"
```

**2. Generate the phonetic string(s).** Run `multinet_g2p.py` (ships inside the esp-sr component) with your phrase(s):

```bash
source /tmp/g2p_venv/bin/activate
python managed_components/espressif__esp-sr/tool/multinet_g2p.py -t "turn on the fan;play a sound"
```

```
in: turn on the fan;play a sound
out: TkN nN jc FfN;PLd c SxND;
```

- `;` separates **different commands** — each segment becomes one `CONFIG_EN_SPEECH_COMMAND_IDx` entry, in order.
- `,` (within one segment) registers **multiple alternate phrasings for the same command ID** — e.g. `-t "turn on the fan,switch the fan on"` makes either phrase trigger the same command.

**3. Register the phrase(s) in `sdkconfig`.** Pick the next free ID and set it, e.g.:

```
CONFIG_EN_SPEECH_COMMAND_ID5="TkN nN jc FfN"
```

Either edit `sdkconfig` directly (then run `idf.py reconfigure` so CMake picks it up cleanly) or use `idf.py menuconfig` → `ESP Speech Recognition` → `Add English speech commands` → `ID5`, which is less error-prone since it validates the field for you.

**4. Handle the command ID in code.** Add a `case` for that ID in `main.c`'s `Speech_event_callback()`, inside the `ESP_SR_EVT_CMD` switch:

```c
case 5:
    ESP_LOGI(TAG, "Turn on the fan");
    // do the actual thing here
    break;
```

**5. Rebuild and reflash:**

```bash
idf.py build
idf.py -p /dev/ttyACM0 flash
```

The boot log's `MN_COMMAND` block (see [Run](#run)) always lists every currently-registered command and its phonetic phrase — check there first if a new command doesn't seem to be recognized, to confirm it actually got picked up from `sdkconfig`.

**Gotchas:**
- MultiNet5's phonetic spelling is approximate and occasionally recognizes commands with similar-sounding phonemes as false positives if phrases are too short or too similar to each other — longer, more distinct phrases recognize more reliably.
- If your handler needs to do something slow or blocking (like `audio_echo_record_and_play()` does), remember it runs synchronously on `detect_Task`'s thread, which pauses wake-word/command detection for as long as it runs. See "5. Added: record + playback" below for how that command deals with also needing exclusive access to the mic.

---

## What was broken and what changed

The project as checked in did not compile or run against ESP-IDF v6.0.2. Three separate problems had to be fixed, in order, each one revealed only after the previous was solved.

### 1. Build failures — IDF v6 API drift

The code was written against an older esp-sr / esp_codec_dev / IDF API and no longer compiled:

- **`const`-qualifier errors** in `main/hardeware_driver/bsp_board.c` and `main/speech_det_driver/mic_speech.c`. The managed components (`espressif__esp_codec_dev`, `espressif__esp-sr`) now return `const` pointers from their factory functions (`audio_codec_new_i2s_data`, `audio_codec_new_i2c_ctrl`, `es7210_codec_new`, `es8311_codec_new`, `esp_afe_handle_from_config`, etc.), but the local static variables holding them were declared non-`const`. Fixed by adding `const` to those declarations.
- **`i2s_port_t` no longer exists.** The legacy I2S driver type was removed; `I2S_NUM_0`/`I2S_NUM_1` are now plain `#define` int constants. Changed `bsp_i2s_init()`/`bsp_i2s_deinit()` parameter types from `i2s_port_t` to `int`.

### 2. Boot crash #1 — no speech model configured

Once it compiled and flashed, the board panicked (`Guru Meditation Error: StoreProhibited`) a few hundred ms into boot with:

```
W (501) AFE_CONFIG: wakenet model not found. please load wakenet model...
E (511) SR_RINGBUF: Memory exhausted
```

`sdkconfig` had **no WakeNet or MultiNet model selected at all** (`CONFIG_SR_MN_EN_NONE=y`, no `CONFIG_SR_WN_*` enabled) — this was true even in `sdkconfig.old`, so it wasn't a regression, just never configured. `Speech_Init()` in `mic_speech.c` unconditionally calls into the speech pipeline, so with no model it crashed.

Fixed via `idf.py menuconfig` → `ESP Speech Recognition` (or by editing `sdkconfig` directly + `idf.py reconfigure`):

- Enabled WakeNet9 **"Hi, ESP"** (`CONFIG_SR_WN_WN9_HIESP=y`).
- Enabled MultiNet5 English, quantized (`CONFIG_SR_MN_EN_MULTINET5_SINGLE_RECOGNITION_QUANT8=y`) — this is the only English MultiNet model that `mic_speech.c`'s call to `esp_mn_commands_update_from_sdkconfig()` actually wires up; MultiNet6/7 require commands to be set at runtime via a different API instead of through Kconfig.
- Set the 4 command phrases (`CONFIG_EN_SPEECH_COMMAND_ID0`–`ID3`) to match the log messages in `main.c`'s `Speech_event_callback`. MultiNet5 doesn't take plain English text — it needs Espressif's own phonetic encoding, generated with their `multinet_g2p.py` tool (`managed_components/espressif__esp-sr/tool/`, requires `pip install g2p_en pandas`):

  ```
  turn on the backlight   -> TkN nN jc BaKLiT
  turn off the backlight  -> TkN eF jc BaKLiT
  backlight is brightest  -> BaKLiT gZ BRiTcST
  backlight is darkest    -> BaKLiT gZ DnRKcST
  ```

### 3. Boot crash #2 — PSRAM disabled

Same crash signature, still `Memory exhausted` from `sr_rb_create`, but now failing *after* successfully loading the WakeNet model — the AFE pipeline simply doesn't fit in ~320KB of internal SRAM. `CONFIG_SPIRAM` was unset even though the board has 8MB of embedded PSRAM.

Fixed by enabling PSRAM (`CONFIG_SPIRAM=y`). First attempt used the Kconfig default of **Quad** SPI mode, which failed at boot (`PSRAM chip is not connected, or wrong PSRAM line mode`) — this board's PSRAM chip needs **Octal** mode instead:

```
# CONFIG_SPIRAM_MODE_QUAD is not set
CONFIG_SPIRAM_MODE_OCT=y
```

After this the boot log confirms a healthy Octal PSRAM chip (`vendor id: 0x0d (AP)`, 8MB, 40MHz) and the full pipeline comes up: `AEC -> SE(BSS) -> WakeNet(wn9_hiesp) -> MultiNet5`, with all 4 commands registered, and `app_main()` returns normally with the device idling and listening.

### Known limitation

`CONFIG_EN_SPEECH_COMMAND_ID9` through `ID31` still hold Espressif's original Kconfig demo defaults (things like "turn on my standby lights", timer/alarm/thermostat phrases) since only IDs 0–8 were overridden or wired up. They're harmless — `main.c`'s switch statement only handles command IDs 0–8 and silently ignores anything else — but if you want a fully clean command set, clear those extra entries via `idf.py menuconfig` → `ESP Speech Recognition` → `Add English speech commands`.

### 3b. Wired up: volume commands ("highest/lowest volume", "increase/decrease the volume")

IDs 5–8 were already present as leftover Kconfig demo defaults (`hicST VnLYoM` = "highest volume", `LbcST VnLYoM` = "lowest volume", `gNKRmS jc VnLYoM` = "increase the volume", `DgKRmS jc VnLYoM` = "decrease the volume") — MultiNet was recognizing them, but `main.c`'s switch statement had no case for them, so they were silently dropped.

Wired them into `main.c`'s `ESP_SR_EVT_CMD` handler using the codec volume functions that already existed in `bsp_board.c`/`.h`:
- "Highest volume" / "lowest volume" → `esp_audio_set_play_vol(100)` / `esp_audio_set_play_vol(0)`
- "Increase/decrease the volume" → read the current level with `esp_audio_get_play_vol()`, step by ±10, clamp to [0, 100], write it back with `esp_audio_set_play_vol()`

Volume is a percentage (0–100) as understood by `esp_codec_dev_set_out_vol()`; the board boots with `PLAYER_VOLUME` (60) as the default.

### 4. Added: LED ring feedback

Originally there was no way to tell whether "Hi, ESP" had actually been heard short of watching the serial log. `main/hardeware_driver/bsp_board.h` already had `LED_STRIP_GPIO_PIN` (GPIO 38) and `LED_STRIP_LED_COUNT` (7) defined, matching Waveshare's spec of a 7-LED addressable ring on this board, but nothing in the code ever drove them.

Added a small driver, `main/led_ring_driver/led_ring.c`/`.h`, on top of the `espressif/led_strip` managed component (RMT-backed WS2812 driver, added to `main/idf_component.yml`). It's wired into `main.c`'s `Speech_event_callback` per the state table above, and initialized/turned off at boot in `app_main()`.

`led_ring_flash()` blocks the calling task for `times * 240ms` (used for the one-shot green confirm flash). It's called from `detect_Task`'s thread — the same task that feeds audio into the AFE pipeline — so keep any future flash patterns short if you extend this, to avoid stalling command detection.

### 5. Added: record + playback ("record for five seconds")

Added `main/audio_echo_driver/audio_echo.c`/`.h`, wired in as voice command ID4 (`main.c` case `4`). `audio_echo_record_and_play(int seconds)`:

1. Allocates a stereo `int16_t` buffer in PSRAM sized for `seconds * 16000` frames (5s ≈ 312KB) via `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`.
2. Reads raw mic frames with `esp_get_feed_data(true, ...)` and pulls out one microphone channel per frame. The board's fixed 4-channel capture layout is `R M N M` (`R` = AEC reference, `M` = mic, `N` = unused) — see the reordering logic in `bsp_board.c`'s `esp_get_feed_data()` — so channel index 1 is used. Each sample is duplicated into both stereo slots since the DAC is opened as 2-channel (`esp_board_init(16000, 2, 16)` in `main.c`).
3. Plays the buffer back with `esp_audio_play()`.

**Why the pause flag was needed:** `feed_Task` in `mic_speech.c` already reads the mic continuously via `esp_get_feed_data()` to feed the AFE/WakeNet pipeline, on its own FreeRTOS task. `esp_codec_dev` isn't documented as safe for concurrent reads from two tasks, so calling `esp_get_feed_data()` from the record routine (which runs on `detect_Task`'s thread, via the event callback) while `feed_Task` is also reading would race on the same codec handle. Added `mic_speech_pause_feed(bool)` (`mic_speech.h`/`.c`) — a flag `feed_Task`'s loop checks each iteration and just idles on when set — so the record routine can pause it, become the sole reader, and resume it afterward.

Net effect: while recording+playing back (record_ID4), wake-word/command detection is fully paused for the ~5-6 second duration (`detect_Task` itself is blocked inside the callback that's doing the recording). This is intentional and simplest for a demo; if you want detection to keep running during recording, that needs a real ring buffer shared between `feed_Task` and the recorder instead of the pause flag, which is a bigger change.

The phrase "record for five seconds" was converted to Espressif's phonetic encoding the same way as the other commands: `RfKkD FeR FiV SfKcNDZ`.

### 6. Added: WiFi + voice assistant round trip

Added `main/wifi_driver/wifi_conn.c`/`.h` and `main/voice_assistant_driver/voice_assistant.c`/`.h`, plus a new `main/Kconfig.projbuild` (previously empty) for WiFi credentials and the assistant endpoint URL. See "Voice assistant setup" above for how to configure and use it.

**WiFi (`wifi_conn.c`):** standard ESP-IDF WiFi-station boilerplate (`nvs_flash_init` -> `esp_netif_init` -> `esp_wifi_init` -> connect), except reconnection on drops is unconditional/indefinite rather than giving up after N retries, since this is meant to run unattended. `wifi_conn_init()` blocks for up to 15s on the *first* connect attempt only (so boot doesn't hang forever if WiFi is misconfigured or the router's down) and returns `ESP_FAIL` if that times out; `app_main()` just logs a warning and carries on in that case rather than treating it as fatal, and the background retry loop keeps trying regardless of what that initial call returned.

**Voice assistant (`voice_assistant.c`):** `voice_assistant_ask()` reuses `audio_capture_mono()` from the record/playback feature (see "5." above) to capture 5s of mono PCM, wraps it in a 44-byte WAV header, and POSTs it with `esp_http_client` using the `open` -> `write` -> `fetch_headers` -> `esp_http_client_read_response` -> `close` sequence (rather than the simpler `esp_http_client_perform()`, which doesn't support streaming a request body from an in-memory buffer the way this needs). The response is read into a PSRAM buffer capped at 2MB, then played back the same way the local echo command does (duplicated into a stereo buffer for `esp_audio_play()`).

**Wiring into the wake event:** `main.c`'s `ESP_SR_EVT_AWAKEN` case now calls `voice_assistant_ask()` directly when `wifi_conn_is_connected()`, instead of just lighting the ring cyan and waiting for a local command. This was a deliberate choice (see the design discussion that led here) to make "Hi, ESP" behave like an actual voice assistant trigger rather than requiring a second spoken command every time. When WiFi isn't up, it falls back to the original cyan/listen-for-a-local-command behavior so the board still does something sensible.

**Known limitations, by design for now:**
- No TLS certificate handling is wired up, so HTTPS endpoints won't work without adding that yourself (see "Voice assistant setup").
- WiFi credentials sit in `sdkconfig` as plaintext Kconfig strings — fine for a personal LAN project, not meant for anything more sensitive.
- Same mic-exclusivity tradeoff as the record/playback feature: the whole round trip (recording + upload + response playback, easily 10+ seconds depending on your server's LLM/TTS latency) runs synchronously on `detect_Task`'s thread, so wake-word/command detection is paused for that whole window.

### 7. Added, then removed: wake-word beep

Added `esp_audio_beep()` in `bsp_board.c`/`.h` — a short "beep-beep" synthesized on the fly and played through `esp_audio_play()` right when the wake word fired, so there'd be audible confirmation beyond just the LED ring. This went through several rounds of debugging (see "8." and "9." below) that fixed two real, separate bugs along the way — but the beep itself still came out glitchy/buzzy afterward, and rather than keep sinking time into a "nice to have" audible cue, it was removed. `main.c`'s `ESP_SR_EVT_AWAKEN` case now goes straight to checking WiFi/starting the assistant, same as before the beep existed.

### 8. Fixed (kept): WakeNet re-triggering continuously

While debugging the beep, found a real bug unrelated to audio playback itself: after a wake word fired, WakeNet stayed fully armed and could immediately re-detect on the tail of the same utterance (WakeNet evaluates a ~1.5s sliding window, so one utterance can produce several consecutive "detected" results already queued up in the pipeline), re-firing `ESP_SR_EVT_AWAKEN` repeatedly. This was invisible before the beep existed (the only effect was resetting the LED to the same color repeatedly) but is a real correctness issue regardless of whether anything audible is attached to the event — it also means the assistant round trip or local commands could theoretically have been re-triggered mid-interaction by the wake word appearing in whatever audio followed.

`detect_Task` in `mic_speech.c` already had the other half of the fix in place — `afe_handle->enable_wakenet(afe_data)` is called when the command-listening window times out — but nothing ever called the matching `disable_wakenet()` when a wake event first fires, and there was no state guard either. Fixed both: added `disable_wakenet()` in both wakeup branches, and gated them on `wakeup_flag == 0` so a repeat detection is structurally impossible regardless of any pipeline buffering/timing, not just less likely. **This fix is kept** — confirmed via serial log that `ESP_SR_EVT_AWAKEN` now fires exactly once per utterance.

### 9. Found and worked around (kept): `esp_audio_play()` frees its buffer before the DMA is actually done with it

The re-triggering fix in "8." didn't resolve the beep's audio quality — it still came out as a continuous buzz instead of two clean beeps. Instrumented `esp_audio_play()` in `bsp_board.c` with timing around the `esp_codec_dev_write()` call: for a buffer that should take ~240ms to play at 16kHz, the write call was consistently returning in ~155-156ms — noticeably less than the audio it was supposedly transferring. `esp_codec_dev_write()`'s return value doesn't reliably mean the DMA transfer is complete (the underlying `i2s_channel_write()` call in `managed_components/espressif__esp_codec_dev/platform/audio_codec_data_i2s.c` doesn't check `bytes_written` against the requested size before returning success), and the code immediately frees the source buffer right after the call returns — so the DMA can end up reading from memory that's already been freed and potentially reused for something else, corrupting playback into exactly the kind of continuous garbage/buzz being chased.

**Fix kept in `esp_audio_play()`:** after the write call, compute how long that much audio is actually supposed to take to play (from `written_bytes` and `s_play_sample_rate`), and if the call returned faster than that, `vTaskDelay()` for the difference before freeing the buffer — so the buffer is never freed before playback has actually had time to finish, regardless of what the write call's own timing implies. This is a real fix worth keeping for every caller of `esp_audio_play()` (the local echo command, the assistant's response playback), even though it turned out not to be the deciding factor for the beep specifically — a related but separate contention issue (`feed_Task`'s continuous mic reads/AEC processing competing with playback on the same I2S peripheral, fixed by pausing it during playback too, not just recording) also didn't fully resolve it. The beep was still audibly buzzy after both fixes, hence its removal in "7."

### 10. Changed: wake word, several times over

Went "Hi, ESP" -> "Jarvis" -> "computer" -> "Alexa" over the course of the session above, each time the same one-line `sdkconfig` change: disable the old `CONFIG_SR_WN_WN9_*` flag, enable the new one, `idf.py reconfigure`. Currently on **`CONFIG_SR_WN_WN9_ALEXA=y`**, confirmed in the boot log: `Set WakeNet Model: wn9_alexa`.

Worth knowing if you want a different wake word later: the WakeNet9 catalog in `managed_components/espressif__esp-sr/Kconfig.projbuild` has ~40 pre-trained options (`Alexa`, `computer`, `Hey,Willow`, `Jarvis`, several Chinese phrases, etc.) — enabling any of them is exactly this same one-line `sdkconfig` change, no different in difficulty. The phrase has to match one of those exactly, though; "Jarvis" is in the catalog, "Hey Jarvis" is not, since each model is trained on real recordings of one specific phrase. Getting an arbitrary custom phrase requires Espressif's separate wake-word training pipeline, a real ML training project rather than a config change. The Kconfig entries aren't mutually exclusive either, so multiple wake words can be enabled at once if you want more than one phrase to work.

### 11. Also fixed: WakeNet missing "Alexa" most of the time

Once "Alexa" was live, it was only being caught roughly 1 in 5 tries. esp-sr exposes a runtime API for this exact problem — `afe_handle->set_wakenet_threshold(afe_data, index, threshold)` (range 0.4–0.9999, lower = more sensitive) — that isn't wired to any Kconfig option, so it has to be called from code. The model's filename (`wakenet9l_v4h8_Alexa_3_0.640_0.650`) hints at a default threshold around 0.64–0.65; `Speech_Init()` in `main/speech_det_driver/mic_speech.c` now calls `afe_handle->set_wakenet_threshold(afe_data, 1, 0.5)` right after creating the AFE instance (index `1` = wakenet1, per the esp-sr header — it's 1-based here, not 0-based). If it's still missing too often, drop it further toward 0.4; if it starts false-triggering on background noise, bring it back up.

### 12. Added: "assistant" command, decoupled from the wake word

Originally "Alexa" triggered the assistant round trip directly, which meant the assistant and the local commands (backlight, volume, "record for five seconds") were mutually exclusive depending on WiFi state — see the git history of this section for that older design. That's backwards for daily use: you want the local commands to always work, and the network round trip to be something you opt into per-utterance.

Changed the architecture: "Alexa" is now a pure wake word again (`ESP_SR_EVT_AWAKEN` just lights the ring solid cyan and opens the local-command listening window, nothing else). A new MultiNet command, **"assistant"**, was added as command ID9 and wired in `main.c`'s `ESP_SR_EVT_CMD` switch to call `voice_assistant_ask()` — exactly what used to happen directly on wake. So the pattern is now "Alexa" (wake) → "assistant" (one of several commands MultiNet is listening for), same two-stage flow as any other command, and volume/backlight/record all work regardless of WiFi state.

Command ID9 was a free slot — it held one of Espressif's unused leftover demo phrases ("turn on the TV", never wired to anything, see the "Known limitation" note above). Its phonetic string was regenerated for "assistant": looked up the CMUdict pronunciation (`AH0 S IH1 S T AH0 N T`, already cached locally at `~/nltk_data/corpora/cmudict/cmudict` from prior work, so no need to install esp-sr's own `multinet_g2p.py` tool or its `g2p_en`/`nltk` dependencies) and ran it through the exact same ARPAbet-to-phonetic-letter table `multinet_g2p.py` uses internally, giving `CONFIG_EN_SPEECH_COMMAND_ID9="cSgSTcNT"`. Same recipe as "Adding more voice commands" below, just with the phoneme lookup done by hand instead of via the script.

### 13. Added: WiFi config portal + BOOT-button force-open

The "Known limitations" note in section 6 above (WiFi credentials only settable via Kconfig, baked in at build time) is no longer accurate — superseded by this.

Added `main/app_config_driver/app_config.c`/`.h` and `main/config_portal_driver/config_portal.c`/`.h`:

- **`app_config`**: WiFi SSID/password and the assistant endpoint URL now live in NVS (`app_cfg` namespace), read once at boot into RAM. Falls back to the Kconfig defaults only if nothing's been saved yet, so existing boards/builds behave identically until the portal is actually used.
- **`config_portal`**: if `wifi_conn_init()`'s initial 15s connect attempt fails, switches WiFi to `WIFI_MODE_APSTA` and brings up an open access point (`ESP32-Setup-XXXX`) plus an `esp_http_server` instance serving a form at `http://192.168.4.1/` for WiFi SSID/password and the assistant URL. Submitting it writes to NVS via `app_config` and calls `esp_restart()`.
- Once STA connects (whether via the portal's saved creds after reboot, or directly on a normal boot), the fallback AP is torn down but the HTTP server is **not** — it stays reachable at the station's own IP, so the assistant URL (or WiFi creds) can be changed live without ever opening the setup AP again.
- Holding the **BOOT** button (GPIO0 — otherwise unused by app code once flashed) through power-on/reset skips the connect attempt entirely and opens the portal immediately, for reconfiguring a board that would otherwise connect fine.

**One real bug hit and fixed along the way:** while the portal AP was up, `wifi_conn.c`'s disconnect handler kept calling `esp_wifi_connect()` on every retry against the (by-definition, at that point) unreachable saved network. Each attempt does a full channel scan, which — on this single-radio chip in `APSTA` mode — briefly knocks the softAP off the air, making the portal page flaky/unreachable exactly when it's needed most. Fixed by skipping the STA reconnect loop entirely while the portal is active; not worth fighting for background reconnection during that window since saving new config through the portal reboots the board anyway.

### 14. Fixed: assistant responses playing a WAV header as audio

`api`'s `/talk` (see [`../api`](../api)) returns a proper WAV file, 44-byte header included, but `voice_assistant.c`'s `play_mono_pcm()` treated the entire HTTP response body as headerless raw PCM16 — so every reply started with ~22 samples (1.4ms) of header bytes (`RIFF`, format fields, etc.) misread as audio, an audible click before the actual speech.

Added `find_pcm_payload()` in `voice_assistant.c`: walks the RIFF/WAVE chunk headers (`pos = 12`, then `chunk_id` + `chunk_size` pairs, each padded to an even byte boundary) to find the real `data` sub-chunk and its length, rather than assuming a fixed 44-byte offset — robust to a server emitting extra chunks before `data`. If the response doesn't start with a `RIFF`/`WAVE` container at all, it falls straight through to the old behavior (whole body treated as raw PCM), so a server that already implements the "no header" contract as originally documented needs no changes either.
