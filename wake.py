"""
Jarvis — Voice Pipeline (Keyboard Trigger)
==========================================
Press Cmd+Shift+J anywhere on your Mac to activate Jarvis.
Records your question, transcribes with Whisper, sends to Claude, speaks back.
The orb at localhost:5555/orb animates in real time.

INSTALL (one time):
  pip install pynput sounddevice numpy openai-whisper requests
  brew install ffmpeg portaudio

RUN:
  python wake.py

  Open localhost:5555/orb in a browser (full screen it).
  Press Cmd+Shift+J — orb shifts to listening.
  Speak your question. Jarvis thinks then speaks back.
"""

import time
import wave
import tempfile
import subprocess
import threading
import re
import numpy as np
import requests
from pathlib import Path

try:
    import sounddevice as sd
    AUDIO_OK = True
except ImportError:
    AUDIO_OK = False
    print("pip install sounddevice")

try:
    from pynput import keyboard
    KEYBOARD_OK = True
except ImportError:
    KEYBOARD_OK = False
    print("pip install pynput")

try:
    import whisper
    WHISPER_OK = True
except ImportError:
    WHISPER_OK = False
    print("pip install openai-whisper")

import config

# ── Config ────────────────────────────────────────────────────────────────────
DASHBOARD      = "http://localhost:5555"
SAMPLE_RATE    = 16000
RECORD_SECONDS = 8
SILENCE_RMS    = 400
SILENCE_SECS   = 1.8
MIC_DEVICE     = 4      # MacBook Pro Microphone
TTS_VOICE      = "Daniel"
TTS_RATE       = 185

# Hotkey: Cmd+Shift+J
HOTKEY = {keyboard.Key.cmd, keyboard.Key.shift, keyboard.KeyCode.from_char('j')}

# ── State ─────────────────────────────────────────────────────────────────────
_active    = False   # currently in a conversation
_pressed   = set()   # currently held keys
_whisper   = None    # loaded model
_recording = False   # currently recording (push-to-talk mode)
_stop_rec  = False   # signal to stop recording


def set_state(state, transcript="", response=""):
    try:
        requests.post(f"{DASHBOARD}/state", json={
            "state": state, "transcript": transcript, "response": response
        }, timeout=1)
    except Exception:
        pass


def speak(text):
    """Speak using edge-tts (Microsoft Edge neural voices, free, no API key)."""
    import asyncio
    import edge_tts

    text = re.sub(r'[*_`#]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return

    async def _speak():
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        communicate = edge_tts.Communicate(text, voice="en-GB-RyanNeural")
        await communicate.save(tmp.name)
        subprocess.run(["afplay", tmp.name], check=False)
        Path(tmp.name).unlink(missing_ok=True)

    try:
        asyncio.run(_speak())
    except Exception as e:
        print(f"⚠️  edge-tts failed: {e} — falling back to say")
        subprocess.run(["say", "-v", TTS_VOICE, "-r", str(TTS_RATE), text], check=False)


def ask_jarvis(question):
    import datetime
    import pytz
    tz  = pytz.timezone("Australia/Sydney")
    now = datetime.datetime.now(tz)
    # Prepend date context so Claude always knows the current time
    dated_question = f"[Today is {now.strftime('%A, %d %B %Y, %-I:%M %p AEST')}] {question}"
    try:
        r = requests.get(
            f"{DASHBOARD}/ask",
            params={"q": dated_question, "voice": "true"},
            timeout=30
        )
        return r.json().get("answer", "")
    except Exception as e:
        return f"Could not reach Jarvis: {e}"


def record_until_silence():
    """
    Records until silence OR the hotkey is released (push-to-talk).
    Silence detection is a backup in case you forget to release.
    """
    global _stop_rec
    _stop_rec = False
    chunks      = []
    silent      = 0
    max_chunks  = int(SAMPLE_RATE * RECORD_SECONDS / 512)
    need_silent = int(SAMPLE_RATE * SILENCE_SECS / 512)

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1,
        dtype='int16', blocksize=512, device=MIC_DEVICE
    ) as s:
        for _ in range(max_chunks):
            if _stop_rec:
                break
            data, _ = s.read(512)
            chunks.append(data.copy())
            rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
            if rms < SILENCE_RMS:
                silent += 1
                if silent >= need_silent and len(chunks) > 8:
                    break
            else:
                silent = 0

    return np.concatenate(chunks, axis=0)


def save_wav(audio_np):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(tmp.name, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_np.tobytes())
    return Path(tmp.name)


def handle_activation():
    """Full pipeline: listen → transcribe → ask → speak."""
    global _active
    if _active:
        return
    _active = True

    try:
        print("\n🎯  Activated — listening...")
        set_state("listen", transcript="Listening...")

        audio = record_until_silence()
        path  = save_wav(audio)

        print("🧠  Transcribing...")
        set_state("think", transcript="Transcribing...")

        result   = _whisper.transcribe(str(path), language="en", fp16=False)
        question = result["text"].strip()
        path.unlink(missing_ok=True)

        if not question or len(question) < 2:
            print("⚠️   Nothing detected")
            set_state("idle")
            return

        print(f"❓  You: {question}")
        set_state("think", transcript=question)

        answer = ask_jarvis(question)
        print(f"💬  Jarvis: {answer[:100]}...")

        set_state("speak", response=answer)
        speak(answer)

        set_state("idle")
        print("💤  Ready. Press Cmd+Shift+J to activate again.\n")

    finally:
        _active = False


def on_press(key):
    _pressed.add(key)
    if all(k in _pressed for k in HOTKEY) and not _active:
        threading.Thread(target=handle_activation, daemon=True).start()


def on_release(key):
    global _stop_rec
    _pressed.discard(key)
    # If any hotkey key released while recording — stop recording
    if key in HOTKEY:
        _stop_rec = True


def handle_intent(question):
    """
    Detects and handles calendar/task intents directly.
    Returns answer string if handled, None if should fall through to /ask.
    """
    q = question.lower().strip()

    # Calendar creation intents
    calendar_triggers = ["block ", "schedule ", "add to calendar", "create event",
                         "set a reminder", "put on my calendar", "book "]
    if any(t in q for t in calendar_triggers):
        try:
            from jarvis_calendar import parse_and_create_event
            return parse_and_create_event(question)
        except Exception as e:
            return f"I couldn't create that event: {e}"

    # Task creation intents
    task_triggers = ["add a task", "add task", "remind me to", "don't let me forget",
                     "note to self", "add to my list", "make a note"]
    if any(t in q for t in task_triggers):
        try:
            from jarvis_calendar import add_task
            # Extract task title — remove the trigger phrase
            title = question
            for t in ["add a task to ", "add task to ", "add a task ", "remind me to ",
                      "don't let me forget to ", "note to self ", "add to my list "]:
                title = title.replace(t, "").replace(t.title(), "")
            title = title.strip().rstrip(".")
            add_task(title)
            return f"Got it. Added to your tasks: '{title}'"
        except Exception as e:
            return f"I couldn't add that task: {e}"

    # What's on my calendar
    if any(t in q for t in ["what's on my calendar", "what do i have today",
                              "what's on today", "my schedule today"]):
        try:
            from jarvis_calendar import get_today_events
            events = get_today_events()
            if not events:
                return "Your calendar is clear today."
            lines = ["Here's what you have today:"]
            for e in events:
                lines.append(f"{e['time']}: {e['title']}")
            return " ".join(lines)
        except Exception as e:
            return None

    # Plan my day
    if any(t in q for t in ["plan my day", "what should i do today",
                              "day plan", "plan today"]):
        try:
            from jarvis_calendar import generate_daily_plan
            return generate_daily_plan()
        except Exception as e:
            return None

    return None  # fall through to /ask


def run():
    if not all([AUDIO_OK, KEYBOARD_OK, WHISPER_OK]):
        print("\nMissing dependencies. Run:")
        print("  pip install pynput sounddevice numpy openai-whisper requests")
        print("  brew install ffmpeg portaudio\n")
        return

    global _whisper
    print("\n🤖  Jarvis voice pipeline")
    print("    ─────────────────────────")
    print("📦  Loading Whisper (downloads ~140MB on first run)...")
    _whisper = whisper.load_model("base")
    print("✅  Whisper ready")
    print("\n💤  Press Cmd+Shift+J anywhere to activate Jarvis")
    print("    Open localhost:5555/orb for the visual interface\n")

    set_state("idle")

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    run()
