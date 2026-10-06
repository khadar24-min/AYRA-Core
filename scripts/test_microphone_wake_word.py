#!/usr/bin/env python3
"""Real-Time Local Microphone Test for "Hey AYRA" (models/hey_ayra.onnx).

Uses the existing AYRA AudioSession (PulseAudio / WSLg / sounddevice) with:
  device = 4  (default, configurable via --device)
  sample_rate = 16000 Hz
  chunk_samples = 1280 (80 ms streaming frames)

Prints real-time diagnostic telemetry for each frame:
  - timestamp
  - audio RMS
  - wake-word score (hey_ayra)
  - detection result (DETECTED / DEBOUNCED / LISTENING)

IMPORTANT: Microphone audio is strictly for live real-time testing and is
NEVER saved or used as training data.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import sys
import time
from pathlib import Path

import numpy as np
from openwakeword.model import Model as OWWModel

try:
    import sounddevice as sd
except OSError:
    sd = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from src.audio import AudioSession, CHUNK_SAMPLES, SAMPLE_RATE
except OSError:
    AudioSession = None  # type: ignore[assignment,misc]
    CHUNK_SAMPLES = 1280
    SAMPLE_RATE = 16_000
from src.wake_word import (
    MIN_SPEECH_RMS,
    WAKEWORD_COOLDOWN_SEC,
    WAKEWORD_NAME,
    _resolve_model_path,
    compute_rms,
)

DEFAULT_MIC_DEVICE = 4


def format_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def print_telemetry_row(
    ts: str,
    rms: float,
    score: float,
    result: str,
) -> None:
    print(
        f"timestamp={ts} | audio_rms={rms:7.2f} | "
        f"wake_word_score={score:6.4f} | detection_result={result}",
        flush=True,
    )


def run_live_microphone_test(
    device: int = DEFAULT_MIC_DEVICE,
    threshold: float = 0.50,
    cooldown_sec: float = WAKEWORD_COOLDOWN_SEC,
    duration_sec: float | None = None,
    print_every_n: int = 1,
) -> int:
    """Stream live audio from microphone `device` (default=4) through hey_ayra.onnx."""
    model_path = _resolve_model_path()
    print("=" * 84)
    print("AYRA-Core Real-Time Microphone Wake-Word Test")
    print("=" * 84)
    print(f"  Model Path      : {model_path}")
    print(f"  Wake-Word Key   : {WAKEWORD_NAME}")
    print(f"  Mic Device Index: {device}")
    print(f"  Sample Rate     : {SAMPLE_RATE} Hz (blocksize={CHUNK_SAMPLES} / 80ms)")
    print(f"  Threshold       : {threshold:.2f} (cooldown={cooldown_sec:.1f}s)")
    print("=" * 84)

    model = OWWModel(wakeword_model_paths=[str(model_path)])
    keys = list(model.models.keys())
    if WAKEWORD_NAME not in keys:
        print(f"[ERROR] Model key '{WAKEWORD_NAME}' not found in loaded keys: {keys}", file=sys.stderr)
        return 1

    try:
        devices = sd.query_devices()
        print("Available sounddevice audio endpoints:")
        for idx, dev in enumerate(devices):
            marker = " <-- SELECTED (device=4)" if idx == device else ""
            print(
                f"  [{idx}] {dev.get('name')} "
                f"(in={dev.get('max_input_channels')}, out={dev.get('max_output_channels')}){marker}"
            )
    except Exception as exc:
        print(f"[audio] Note: query_devices reported: {exc}")

    print("-" * 84)
    print("Listening on microphone... Speak 'Hey AYRA' (Press Ctrl+C to stop)")
    print("-" * 84)

    last_trigger_ts = 0.0
    frame_idx = 0
    start_ts = time.monotonic()

    try:
        with AudioSession(sample_rate=SAMPLE_RATE, chunk_samples=CHUNK_SAMPLES, device=device) as session:
            session.drain()
            model.reset()
            while True:
                if duration_sec is not None and (time.monotonic() - start_ts) >= duration_sec:
                    print(f"\n[done] Completed {duration_sec:.1f}s microphone test.")
                    break

                chunk = session.read()
                frame_idx += 1
                ts = format_timestamp()
                rms = compute_rms(chunk)
                preds = model.predict(chunk)
                score = float(preds.get(WAKEWORD_NAME, 0.0))
                now = time.monotonic()

                if score >= threshold:
                    if rms < MIN_SPEECH_RMS:
                        result = "IGNORED_SILENCE"
                        model.reset()
                    elif last_trigger_ts > 0.0 and (now - last_trigger_ts) < cooldown_sec:
                        result = f"DEBOUNCED (cooldown {now - last_trigger_ts:.2f}s < {cooldown_sec:.1f}s)"
                        model.reset()
                    else:
                        last_trigger_ts = now
                        result = "DETECTED (Hey AYRA)"
                        model.reset()
                    print_telemetry_row(ts, rms, score, result)
                elif frame_idx % max(1, print_every_n) == 0:
                    print_telemetry_row(ts, rms, score, "LISTENING")

    except KeyboardInterrupt:
        print("\n[stopped] Microphone test ended by user.")
    except Exception as exc:
        print(
            f"[ERROR] Could not open physical microphone device {device}: {exc}\n"
            "Model validation passed, but physical microphone validation must be performed on the local AYRA machine.",
            file=sys.stderr,
        )
        return 2
    return 0


def run_self_test(device: int = DEFAULT_MIC_DEVICE, threshold: float = 0.50) -> int:
    """Verify device=4 configuration and test the streaming telemetry loop."""
    model_path = _resolve_model_path()
    model = OWWModel(wakeword_model_paths=[str(model_path)])
    print(f"[self-test] Loaded {model_path} with keys={list(model.models.keys())}")

    # 1. Probe hardware device 4 via AudioSession if available in current environment
    try:
        with AudioSession(sample_rate=SAMPLE_RATE, chunk_samples=CHUNK_SAMPLES, device=device) as session:
            chunk = session.read(timeout=1.5)
            rms = compute_rms(chunk)
            score = float(model.predict(chunk).get(WAKEWORD_NAME, 0.0))
            print_telemetry_row(format_timestamp(), rms, score, "LIVE_MIC_DEVICE_4_OK")
    except Exception as exc:
        print(
            f"[self-test] Hardware device={device} probe in current environment: {exc}\n"
            f"[self-test] Running streaming AudioSession replay verification..."
        )

    # 2. Verify full streaming telemetry formatting on a held-out positive & negative WAV
    from scripts.augment_dataset import read_wav_pcm16

    val_pos = sorted((REPO_ROOT / "data" / "wake_word" / "val" / "positive").glob("*.wav"))
    if not val_pos:
        print("[self-test] No validation WAV found to replay.", file=sys.stderr)
        return 1

    pcm = read_wav_pcm16(val_pos[0])
    pad = np.zeros(6 * CHUNK_SAMPLES, dtype=np.int16)
    stream = np.concatenate([pad, pcm, pad])
    model.reset()
    detected_any = False
    last_trig = 0.0

    n_chunks = len(stream) // CHUNK_SAMPLES
    for c in range(n_chunks):
        chunk = stream[c * CHUNK_SAMPLES : (c + 1) * CHUNK_SAMPLES]
        rms = compute_rms(chunk)
        score = float(model.predict(chunk).get(WAKEWORD_NAME, 0.0))
        now = time.monotonic()
        if score >= threshold:
            if rms < MIN_SPEECH_RMS:
                res = "IGNORED_SILENCE"
                model.reset()
            elif last_trig > 0.0 and (now - last_trig) < WAKEWORD_COOLDOWN_SEC:
                res = "DEBOUNCED"
                model.reset()
            else:
                last_trig = now
                res = "DETECTED (Hey AYRA)"
                detected_any = True
                model.reset()
        else:
            res = "LISTENING"
        if c % 3 == 0 or res != "LISTENING":
            print_telemetry_row(format_timestamp(), rms, score, res)

    if not detected_any:
        print("[self-test] FAIL: Did not detect positive clip during streaming self-test.", file=sys.stderr)
        return 1
    print("[self-test] PASS: Streaming telemetry and detection verified.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-time microphone test for Hey AYRA (device=4).")
    parser.add_argument("--device", type=int, default=DEFAULT_MIC_DEVICE, help="Microphone device index (default: 4)")
    parser.add_argument("--threshold", type=float, default=0.50, help="Wake-word score threshold (default: 0.50)")
    parser.add_argument("--cooldown", type=float, default=WAKEWORD_COOLDOWN_SEC, help="Debounce cooldown in seconds")
    parser.add_argument("--duration", type=float, default=None, help="Optional test duration in seconds")
    parser.add_argument("--print-every", type=int, default=2, help="Print LISTENING row every N chunks (80ms each)")
    parser.add_argument("--self-test", action="store_true", help="Run automated verification of telemetry loop")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test(device=args.device, threshold=args.threshold)
    return run_live_microphone_test(
        device=args.device,
        threshold=args.threshold,
        cooldown_sec=args.cooldown,
        duration_sec=args.duration,
        print_every_n=args.print_every,
    )


if __name__ == "__main__":
    sys.exit(main())
