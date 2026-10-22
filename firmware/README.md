# firmware

ESP-IDF firmware for [esp32-homeai](../README.md), targeting the [Waveshare ESP32-S3-AUDIO-Board](https://docs.waveshare.com/ESP32-S3-AUDIO-Board). Say the wake word **"Alexa"** and the board voice-activity-records your question, POSTs it to the [`api`](../api) service (or any server implementing the same contract), and plays the spoken reply back on the speaker. Volume is set with two physical buttons on the TCA9555 GPIO expander.

Board hardware in play: ES7210 4-mic ADC array, ES8311 DAC/speaker codec, TCA9555 GPIO expander (three buttons), 16MB flash, 8MB Octal PSRAM.

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
- A WiFi network and a server on that network to receive/answer voice requests (see "Voice assistant setup" below) — without this configured, the device still boots and the volume buttons still work, it just can't reach the assistant. The [`api`](../api) service in this repo implements the server side.

## Repo layout

`sdkconfig` is committed here, unusually for an ESP-IDF project. Normally it's regenerated from `sdkconfig.defaults` and left out of version control, but in this project most of what makes it actually build correctly for this board — 16MB flash size, Octal (not Quad) PSRAM mode, the WakeNet9 model selection — only ever ended up in the full `sdkconfig` via `idf.py menuconfig`, never distilled into `sdkconfig.defaults`. Committing just `sdkconfig.defaults` means a fresh clone doesn't build (missing the speech model, wrong flash size). The WiFi/URL fields in the committed `sdkconfig` are intentionally-invalid placeholders (`YourWiFiSSID`, etc.) — real values belong in the config portal (below) or your own local `sdkconfig`, never committed.

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

On boot the device initializes I2C, both audio codecs, the button expander, the LED ring, loads the wake-word model from the `model` partition, starts the debounced button scan, connects to WiFi (waits up to 15s, then keeps retrying in the background if it fails), and starts listening.

**The single-wake assistant flow.** Say **"Alexa"** — one utterance, that's the whole trigger. There is no second "assistant" command and no set of local voice commands; the wake word goes straight to the network round trip. On a verified wake, `main.c`'s `Speech_event_callback` (`ESP_SR_EVT_AWAKEN` case) calls `voice_assistant_ask()`, which:

1. **Records the question with VAD** (`audio_capture_vad()` in `audio_echo.c`). It captures from the mic until the user stops talking (a run of low-RMS trailing silence, `VAD_SILENCE_MS`) or a hard cap elapses (`VAD_MAX_SECONDS`), whichever comes first. The length is variable — a short "what time is it?" yields a short recording. It always returns some audio, even pure silence; deciding that a recording is "nothing was said" is deliberately left to the API's silence gate (see "Voice assistant setup"). The two VAD thresholds are room-specific and were dialed in on this board — see [VAD capture](#vad-capture-dialed-to-the-room-fixed) at the bottom.
2. Wraps the mono PCM in a 44-byte WAV header and POSTs it to the configured endpoint URL.
3. Reads the response and plays it back on the speaker.

If WiFi isn't connected, it flashes the ring red twice and does nothing else — there's nothing to send the recording to. The whole round trip runs synchronously on `detect_Task`'s thread, with the mic's `feed_Task` paused for the duration, so wake-word detection is suspended until the response has finished playing.

**Volume buttons.** The board's two working buttons sit on the TCA9555 expander (pins 10 and 11) and are scanned by the debounced `button_driver` (`main/button_driver/button_driver.c`, 100ms poll, 1000ms long-press threshold; only short presses act here). They're active-low inputs, one I2C read grabs all three at once:

| Button | Key | Action |
|---|---|---|
| K2 / SET | `KEY_ID_10` | volume up, step 10 (clamped 0–100); green ring flash |
| K3 / - | `KEY_ID_11` | volume down, step 10 (clamped 0–100); orange ring flash |

(Pin 9 / `KEY_ID_9` is read but not wired to a function; long presses are detected by the driver but ignored.) Volume is a codec percentage (0–100) via `esp_audio_set_play_vol()`; the board boots at `PLAYER_VOLUME` (60).

**Local record + playback (unwired).** `audio_echo.c` still contains `audio_echo_record_and_play(int seconds)`, a no-network sanity check that records N seconds and plays them straight back. It's no longer bound to any trigger (the old "record for five seconds" voice command is gone); it's there if you want to re-wire it to a button or call it directly.

**LED ring.** The 7-LED ring is the main feedback channel (the serial log is otherwise the only other sign of life):

| Ring state | Meaning |
|---|---|
| Off | Idle, waiting for "Alexa" |
| Solid magenta | VAD-recording the question |
| Solid yellow | Uploading / waiting for the assistant's response |
| Solid blue | Playing back the response |
| Red flash x2, then off | Assistant failed (no WiFi, or capture/upload error) |
| Quick green flash | Volume up (K2) |
| Quick orange flash | Volume down (K3) |

## WiFi & config portal

WiFi credentials and the assistant endpoint URL are stored in NVS at runtime and can be set two ways:

**A. Config portal (recommended — no reflash needed).** If the board can't connect to WiFi within 15s of boot, it opens an open access point named `ESP32-Setup-XXXX` (last 2 bytes of its MAC) alongside its normal background retry attempts, so the buttons keep working the whole time. Join that AP, browse to `http://192.168.4.1/`, and fill in the WiFi SSID/password and/or assistant server URL — saving writes both to NVS and reboots the board. Leaving the password field empty while keeping the same SSID keeps the already-saved password, so you can update just the server URL without re-entering the WiFi password.

You can force this open even if the board would connect fine — e.g. to move it to a new network — by holding the **BOOT** button through power-on/reset. Once connected, the same config page stays reachable at the board's normal WiFi IP (`nmap` your LAN or check the serial log), so you can update the assistant URL live without ever re-opening the setup AP — handy if your server's IP changes.

Anyone on your LAN who finds that IP can currently read/change these settings — there's no auth on the config page. Fine for a home network, worth knowing if you deploy this somewhere less trusted.

**B. Kconfig defaults (factory fallback).** `idf.py menuconfig` → `ESP SR Demo Configuration` sets the values baked in at build time (`CONFIG_ESP_WIFI_SSID`/`PASSWORD`/`CONFIG_VOICE_ASSISTANT_ENDPOINT_URL`) — used only until something's been saved via the portal, which then takes priority. These land in `sdkconfig`, which **is committed** (see "Repo layout" below for why) — don't leave real credentials in it before committing; the placeholder values checked in (`YourWiFiSSID` etc.) are intentionally invalid.

Plain HTTP is fine for a server on your LAN; HTTPS also works since `esp_http_client` supports it, but you'll additionally need to configure a CA certificate (`esp_http_client_config_t.cert_pem` or the ESP-IDF cert bundle component) if you point it at a public HTTPS endpoint — not set up here.

## Voice assistant setup

**The contract your server needs to implement** (already done by [`api`](../api) in this repo — point the URL above at `http://<host>:8000/talk` and skip the rest of this section):

| | Request (ESP → server) | Response (server → ESP) |
|---|---|---|
| Method | `POST` | HTTP 200 |
| Body | 44-byte-header WAV: PCM16, mono, 16000 Hz, **variable length** (VAD) | WAV (44-byte header), PCM16 mono 16000 Hz — or headerless raw PCM |
| `Content-Type` | `audio/wav` | (ignored by the firmware) |

The response is played fully into PSRAM before playback starts, capped at 2MB (~64s of mono 16-bit 16kHz audio). `voice_assistant.c`'s `find_pcm_payload()` walks the RIFF/WAVE chunk headers to find the `data` sub-chunk and plays back from there, rather than assuming a fixed 44-byte offset — so a server that returns a real WAV (what `api` does, via Python's `wave` module) plays correctly instead of ~22 header bytes being misread as noise up front. If the body doesn't start with a `RIFF`/`WAVE` container at all it falls straight through to treating the whole body as headerless raw PCM, so a server that returns raw PCM also works unmodified.

**The silence gate.** The firmware uploads on every wake — it can't tell "nothing was said" locally (the VAD recording always contains some audio, even silence). That's the API's job: `api` measures the input's RMS; if it's below the threshold, or the STT transcript comes back empty, it skips STT/LLM/TTS and returns a short all-silence WAV (default 0.4s) with an `X-Reply: silence` header. The firmware just plays that (inaudible) back, and the API tallies answered-vs-silence requests for `/stats`. Tune it with `SILENCE_THRESHOLD_DBFS` / `SILENCE_REPLY_SECONDS` in `api/.env`.

**Building your own server instead of using `api`:** receive the WAV, run speech-to-text, send the transcript to an LLM, synthesize the reply with TTS, and return it as a WAV (or headerless raw PCM16 mono 16kHz) body. A minimal Python/Flask skeleton to validate the ESP round trip before wiring up real STT/LLM/TTS (it just echoes a 1-second beep back so you can confirm audio flows both ways):

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

Three sequential API calls like this can easily take several seconds to tens of seconds depending on the models and reply length, so `HTTP_TIMEOUT_MS` in `voice_assistant.c` is set to 180s rather than the more typical few-seconds default — a CPU-only server can spend a minute or more on the TTS step alone. The ring stays solid yellow (uploading/waiting) for that entire duration, so there's at least visible feedback that it hasn't hung.

---

## What was broken and what changed

The project as checked in did not compile or run against ESP-IDF v6.0.2, and the original design has since been reshaped into the single-wake architecture above. A condensed record of the fixes and changes that are still in the tree:

### Build — IDF v6 API drift (kept)

The code was written against an older esp-sr / esp_codec_dev / IDF API and no longer compiled:

- **`const`-qualifier errors** in `main/hardeware_driver/bsp_board.c` and `main/speech_det_driver/mic_speech.c`. The managed components now return `const` pointers from their factory functions (`audio_codec_new_i2s_data`, `audio_codec_new_i2c_ctrl`, `es7210_codec_new`, `es8311_codec_new`, `esp_afe_handle_from_config`, etc.), but the local static variables holding them were declared non-`const`. Fixed by adding `const` to those declarations.
- **`i2s_port_t` no longer exists.** The legacy I2S driver type was removed; `I2S_NUM_0`/`I2S_NUM_1` are now plain `#define` int constants. Changed `bsp_i2s_init()`/`bsp_i2s_deinit()` parameter types from `i2s_port_t` to `int`.

### Boot crash — no wake-word model configured (kept)

Once it compiled and flashed, the board panicked (`Guru Meditation Error: StoreProhibited`) a few hundred ms into boot with:

```
W (501) AFE_CONFIG: wakenet model not found. please load wakenet model...
E (511) SR_RINGBUF: Memory exhausted
```

`sdkconfig` had **no WakeNet model selected at all** (`CONFIG_SR_WN_*` all unset). `Speech_Init()` in `mic_speech.c` unconditionally calls into the speech pipeline, so with no model it crashed. Fixed via `idf.py menuconfig` → `ESP Speech Recognition` (or by editing `sdkconfig` directly + `idf.py reconfigure`) to enable a WakeNet9 wake-word model.

### Boot crash — PSRAM disabled (kept)

Same crash signature, still `Memory exhausted` from `sr_rb_create`, but now failing *after* successfully loading the wake model — the AFE pipeline simply doesn't fit in ~320KB of internal SRAM. `CONFIG_SPIRAM` was unset even though the board has 8MB of embedded PSRAM.

Fixed by enabling PSRAM (`CONFIG_SPIRAM=y`). First attempt used the Kconfig default of **Quad** SPI mode, which failed at boot (`PSRAM chip is not connected, or wrong PSRAM line mode`) — this board's PSRAM chip needs **Octal** mode instead:

```
# CONFIG_SPIRAM_MODE_QUAD is not set
CONFIG_SPIRAM_MODE_OCT=y
```

After this the boot log confirms a healthy Octal PSRAM chip (8MB, 40MHz) and the pipeline comes up, and `app_main()` returns normally with the device idling and listening.

### LED ring feedback (kept)

`main/hardeware_driver/bsp_board.h` already had `LED_STRIP_GPIO_PIN` (GPIO 38) and `LED_STRIP_LED_COUNT` (7) defined, matching Waveshare's spec of a 7-LED addressable ring on this board, but nothing drove them. Added `main/led_ring_driver/led_ring.c`/`.h` on top of the `espressif/led_strip` managed component (RMT-backed WS2812 driver), wired into the LED states in the "LED ring" table above and initialized/turned off at boot in `app_main()`.

### Mic exclusivity — `mic_speech_pause_feed()` (kept)

`feed_Task` in `mic_speech.c` reads the mic continuously to feed the AFE/WakeNet pipeline, and the assistant round trip (and the local record command) also need to read the mic directly. `esp_codec_dev` isn't documented as safe for concurrent reads from two tasks, so `mic_speech_pause_feed(bool)` (`mic_speech.h`/`.c`) is a flag `feed_Task`'s loop checks each iteration and idles on when set — the recorder becomes the sole reader, then resumes `feed_Task` once it (and any playback) is done. Net effect: while a round trip is in flight, wake-word detection is paused for the whole window (it's the simplest correct option for this demo; keeping detection live during recording would need a shared ring buffer instead).

### WiFi + voice-assistant round trip (kept)

`main/wifi_driver/wifi_conn.c`/`.h` and `main/voice_assistant_driver/voice_assistant.c`/`.h`, plus a `main/Kconfig.projbuild` for WiFi credentials and the assistant endpoint URL. See "Voice assistant setup" above.

- **WiFi (`wifi_conn.c`):** standard ESP-IDF WiFi-station boilerplate, except reconnection on drops is unconditional/indefinite (this runs unattended). `wifi_conn_init()` blocks for up to 15s on the *first* connect attempt only and returns `ESP_FAIL` if that times out; `app_main()` logs a warning and carries on, and the background retry loop keeps trying regardless.
- **Voice assistant (`voice_assistant.c`):** `voice_assistant_ask()` VAD-captures the question via `audio_capture_vad()`, wraps it in a 44-byte WAV header, and POSTs it with `esp_http_client` using the `open` -> `write` -> `fetch_headers` -> `read_response` -> `close` sequence (rather than `esp_http_client_perform()`, which doesn't stream a request body from an in-memory buffer the way this needs). The response is read into a PSRAM buffer capped at 2MB, then played back the same way the local record command does.

### Wake-word beep: added, then removed (history)

A short `esp_audio_beep()` was added as an audible confirmation on wake, went through rounds of debugging (which surfaced the two real bugs below, both kept), but still came out glitchy/buzzy and was removed as a "nice to have." `main.c`'s `ESP_SR_EVT_AWAKEN` case goes straight to the assistant instead.

### WakeNet re-triggering continuously (fixed, kept)

After a wake word fired, WakeNet stayed armed and could immediately re-detect on the tail of the same utterance (WakeNet evaluates a ~1.5s sliding window, so one utterance can produce several consecutive "detected" results already queued in the pipeline), re-firing `ESP_SR_EVT_AWAKEN` repeatedly. `disable_wakenet()` alone only stops *future* inference, not results already computed. Fixed in `detect_Task`: call `disable_wakenet()` when a wake fires **and** gate it on `wakeup_flag == 0`, so a repeat detection is structurally impossible regardless of pipeline buffering/timing. Confirmed via serial log that `ESP_SR_EVT_AWAKEN` now fires exactly once per utterance. (The `wakeup_flag` guard also means the synchronous ask round trip can't be re-triggered mid-interaction.)

### `esp_audio_play()` frees its buffer before the DMA is done (worked around, kept)

`esp_audio_play()` in `bsp_board.c` was freeing the source buffer right after `esp_codec_dev_write()` returned, but that call doesn't reliably wait for the DMA to finish — so the DMA could read already-freed/reused memory, corrupting playback into a continuous buzz. Fix kept in `esp_audio_play()`: after the write, compute how long that much audio is actually supposed to take (from `written_bytes` and the sample rate), and if the call returned faster than that, `vTaskDelay()` for the difference before freeing — so the buffer is never freed before playback has had time to finish, for every caller.

### Wake word: settled on "Alexa" (kept)

Went "Hi, ESP" -> "Jarvis" -> "computer" -> "Alexa"; each change is the same one-line `sdkconfig` edit (disable the old `CONFIG_SR_WN_WN9_*`, enable the new one, `idf.py reconfigure`). Currently on **`CONFIG_SR_WN_WN9_ALEXA=y`**. The WakeNet9 catalog in `managed_components/espressif/esp-sr/Kconfig.projbuild` has ~40 pre-trained options; any of them is the same one-line change, but the phrase has to match one exactly (each model is trained on recordings of one specific phrase). Custom phrases need Espressif's separate wake-word training pipeline.

WakeNet was missing "Alexa" roughly 1 in 5 tries at the model's default threshold. `esp-sr` exposes `afe_handle->set_wakenet_threshold(afe_data, index, threshold)` (0.4–0.9999, lower = more sensitive) with no Kconfig binding, so `Speech_Init()` calls it with `0.5` (index `1`, 1-based) right after creating the AFE instance. Drop it toward 0.4 if it still misses too often; bring it back up if it false-triggers on background noise.

### Two-stage "Alexa" -> "assistant" flow: introduced, then simplified away (history)

The flow went through an intermediate design where "Alexa" was a pure wake word opening a window for a *second* spoken command, with a MultiNet command named **"assistant"** (command ID9) triggering the network round trip, plus a set of local MultiNet voice commands (backlight, "record for five seconds", and volume phrases). That let local commands work offline and made the network trip opt-in per utterance.

It was then simplified back to the current design: **"Alexa" alone triggers the assistant directly** (`ESP_SR_EVT_AWAKEN` -> `voice_assistant_ask()`), the MultiNet command set was removed (MultiNet is no longer initialized in `mic_speech.c`, and the `ESP_SR_EVT_CMD`/`ESP_SR_EVT_CMD_TIMEOUT` events and the `sr_cmd` field were dropped from `mic_speech.h`), and volume moved off voice commands onto the **physical buttons** described in "Run." The result is one utterance instead of two, with no per-phrase recognition to tune.

### Config portal + BOOT-button force-open (kept, extended)

`main/app_config_driver/app_config.c`/`.h` and `main/config_portal_driver/config_portal.c`/`.h`:

- **`app_config`**: WiFi SSID/password and the assistant endpoint URL live in NVS (`app_cfg` namespace), read once at boot into RAM, falling back to the Kconfig defaults only if nothing's been saved.
- **`config_portal`**: if the initial 15s connect attempt fails, switches WiFi to `WIFI_MODE_APSTA` and brings up an open AP (`ESP32-Setup-XXXX`) plus an `esp_http_server` serving a form at `http://192.168.4.1/`. Submitting writes to NVS via `app_config` and reboots. An empty password with the same SSID keeps the saved password (so changing only the URL doesn't need the WiFi password again).
- Once STA connects, the fallback AP is torn down but the HTTP server stays reachable at the station's IP, so settings can be changed live without re-opening the setup AP.
- Holding **BOOT** (GPIO0) through power-on/reset skips the connect attempt and opens the portal immediately.

One real bug hit and fixed along the way: while the portal AP was up, `wifi_conn.c`'s disconnect handler kept calling `esp_wifi_connect()` on every retry against the unreachable saved network; each attempt does a full channel scan that briefly knocks the softAP off the air in `APSTA` mode, making the portal flaky exactly when it's needed most. Fixed by skipping the STA reconnect loop while the portal is active.

### Assistant responses playing a WAV header as audio (fixed, kept)

`api`'s `/talk` returns a real WAV file (44-byte header included), but the original `voice_assistant.c` treated the whole response body as headerless raw PCM — so every reply started with ~22 samples of header bytes misread as audio, an audible click before the speech. `find_pcm_payload()` now walks the RIFF/WAVE chunk headers to the real `data` sub-chunk (falling back to raw-PCM if there's no `RIFF`/`WAVE` container), so a WAV or a headerless body both play back correctly.

### VAD capture: dialed to the room (fixed)

Two thresholds in `audio_capture_vad()` (`audio_echo.c`) were never right for this room, and each failure mode looked different:

- **`VAD_SILENCE_RMS` was 800 — effectively every capture ran the full 10s.** The compare is done on mean-square (rms-squared) samples, so that threshold is 640,000. In this room the measured noise floor is rms_sq ~50–250 and normal speech peaks around rms_sq ~15,000–26,000 (RMS ~120–160), so `started` never latched and the loop always ran to `VAD_MAX_SECONDS` before uploading. That flat 10s capture (plus the upload and the API round trip) is what felt like "several seconds after I stop talking".
- **After the threshold fix, `VAD_SILENCE_MS` at 500ms was too tight.** Once VAD actually worked, the same 500ms trailing-silence window that ends the capture is also how long a natural thinking-pause is, so questions got cut off mid-thought and the partial recording was sent to the API.

Current values, both commented in `audio_echo.c`: `VAD_SILENCE_RMS = 60` (threshold 3,600 rms_sq — well above the noise floor, well below speech) and `VAD_SILENCE_MS = 1000` (long enough to swallow a ~0.5–1s pause; the tradeoff is that every capture waits up to 1s after the last word before uploading). If the mic or the room changes, re-tune from the serial log: the loop prints `VAD: <ms> rms_sq=<n> started=<0|1>` every 500ms while capturing, so `idf.py monitor` during one test wake shows the floor, the speech level, and where `started` fires.
