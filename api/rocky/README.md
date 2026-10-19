# Rocky's voice reference (not in git)

Put two files here. The API won't start without them:

| File | What it is |
|---|---|
| `rocky_ref.wav` | ~10-30 s of clean speech in the voice to clone: mono 16-bit PCM WAV (24 kHz ideal), no music. Short lines joined with ~0.2 s gaps are fine. |
| `rocky_ref.txt` | The **exact** transcript of that audio, one line, same order. |

Qwen3-TTS (downloaded by `scripts/first_run.sh`) is a generic voice engine. These two files are what make it
sound like Rocky: at startup it encodes them into a voice profile, and every reply imitates it.

They aren't committed because the original is cut from *Project Hail Mary*'s audio (personal use only).

## How to get them

- **From the Rocky voice project**, which builds the reference from film clips (vocal separation, line selection,
  takes picked by ear):

  ```bash
  ../scripts/get_rocky_voice.sh /path/to/rocky     # copies voice/rocky_ref_golden.{wav,txt} here
  ```

- **Build your own** from audio you're allowed to use: cut 10-30 s of the character speaking clearly (a vocal
  separator such as Demucs helps remove music), save it as `rocky_ref.wav`, and write exactly what's said into
  `rocky_ref.txt`. `ffmpeg -i input.wav -ac 1 -ar 24000 -sample_fmt s16 rocky_ref.wav` converts to the right format.

- **Any other voice** works the same way (your own, or an openly licensed one); adjust `SYSTEM_PROMPT` in `.env`
  to match the character. Paths can also be changed with `TTS_REF_AUDIO` / `TTS_REF_TEXT`.

Don't publish clips or audio cloned from voices you don't have the rights to.
