"""openWakeWord integration: blocks on an AudioSession until 'Hey AYRA' fires."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np
import openwakeword
from openwakeword.model import Model

if TYPE_CHECKING:
    from src.audio import AudioSession

WAKEWORD_NAME = "hey_ayra"
WAKEWORD_MODEL_PATH = "/mnt/c/Users/lokes/Downloads/AYRA-Core/models/hey_ayra.onnx"

# Cooldown / debounce window (seconds) to prevent a single "Hey AYRA" utterance
# or trailing acoustic resonance from triggering repeatedly.
WAKEWORD_COOLDOWN_SEC = 2.0

# Minimum audio RMS energy (int16 scale) required to accept a wake-word trigger.
# Guards against numerical drift on pure digital silence (RMS ~ 0).
MIN_SPEECH_RMS = 10.0

_last_trigger_ts: float = 0.0
_last_barge_ts: float = 0.0


def reset_cooldown() -> None:
    """Reset the wake-word debounce timers (used in tests or manual session resets)."""
    global _last_trigger_ts, _last_barge_ts
    _last_trigger_ts = 0.0
    _last_barge_ts = 0.0


def compute_rms(chunk: np.ndarray | None) -> float:
    """Compute Root-Mean-Square (RMS) amplitude of an int16/float32 audio chunk."""
    if chunk is None or chunk.size == 0:
        return 0.0
    x = chunk.astype(np.float32)
    rms = float(np.sqrt(np.mean(x * x)))
    return rms if math.isfinite(rms) else 0.0


def _resolve_model_path() -> str:
    """Resolve the custom hey_ayra.onnx path, preferring WAKEWORD_MODEL_PATH
    (/mnt/c/Users/lokes/Downloads/AYRA-Core/models/hey_ayra.onnx) and falling
    back to the repository's models/hey_ayra.onnx if relocated."""
    env_override = os.getenv("AYRA_WAKEWORD_MODEL", "").strip()
    if env_override and Path(env_override).is_file():
        return env_override
    primary = Path(WAKEWORD_MODEL_PATH)
    if primary.is_file():
        return str(primary)
    repo_fallback = Path(__file__).resolve().parent.parent / "models" / "hey_ayra.onnx"
    if repo_fallback.is_file():
        return str(repo_fallback)
    return WAKEWORD_MODEL_PATH


def _ensure_models_downloaded() -> None:
    """Ensure openWakeWord's shared feature extractors (melspectrogram.onnx and
    embedding_model.onnx) exist locally. Never downloads or activates hey_jarvis."""
    try:
        res_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        mel_ok = (res_dir / "melspectrogram.onnx").is_file()
        emb_ok = (res_dir / "embedding_model.onnx").is_file()
        if not (mel_ok and emb_ok):
            openwakeword.utils.download_models(["alexa"])
    except Exception as exc:  # noqa: BLE001
        print(f"[wake_word] feature model check warning: {exc}", file=sys.stderr)


def _instantiate_oww_model(model_path: str) -> Model:
    """Instantiate an openWakeWord Model loading ONLY the custom hey_ayra.onnx."""
    if not Path(model_path).is_file():
        raise FileNotFoundError(f"Wake-word ONNX model not found at: {model_path}")
    try:
        return Model(wakeword_models=[model_path], inference_framework="onnx")
    except TypeError:
        # Backwards-compatibility with older openWakeWord or test doubles
        return Model(wakeword_model_paths=[model_path])


# Process-wide singleton. Building a Model spins up ONNX Runtime inference
# sessions, and ORT is notorious for not fully releasing native memory on GC
# — so the original "new Model() per wake cycle" pattern leaked tens of MB
# every wake→process→respond round-trip. We build it once and reset() its
# streaming feature buffers between cycles instead.
_model: Model | None = None


def _get_model() -> Model | None:
    global _model
    if _model is None:
        _ensure_models_downloaded()
        resolved = _resolve_model_path()
        try:
            _model = _instantiate_oww_model(resolved)
            keys = list(getattr(_model, "models", {}).keys())
            print(
                f"[wake_word] loaded custom ONNX model '{resolved}' (keys={keys})",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — avoid crashing if model unavailable
            print(
                f"[wake_word] ERROR: could not load wake-word model at '{resolved}': {exc}",
                file=sys.stderr,
            )
            return None
    else:
        try:
            _model.reset()
        except Exception:  # noqa: BLE001
            pass
    return _model


_HEARTBEAT_SEC = 30.0


def wait_for_wake_word(
    session: AudioSession,
    threshold: float = 0.5,
    shutdown_event: threading.Event | None = None,
    reset_event: threading.Event | None = None,
    armed_probe: Callable[[], bool] | None = None,
    challenge_event: threading.Event | None = None,
    cooldown_sec: float = WAKEWORD_COOLDOWN_SEC,
    min_rms: float = MIN_SPEECH_RMS,
) -> None:
    """Block reading from `session` until 'Hey AYRA' scores >= threshold,
    or until shutdown_event / reset_event / challenge_event is set.

    Includes:
      - Cooldown/debounce protection to avoid repeated triggers from a single utterance
      - Silence & noise floor gating (min_rms)
      - Graceful degradation if the ONNX model is unavailable
      - Diagnostic logging of score, audio RMS, and threshold
    """
    global _last_trigger_ts
    model = _get_model()

    if model is None:
        print(
            "[wake_word] WARNING: wake-word model unavailable; standing by without crashing.",
            file=sys.stderr,
        )
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                return
            if reset_event is not None and reset_event.is_set():
                return
            if challenge_event is not None and challenge_event.is_set():
                return
            time.sleep(0.1)

    print(
        f"[wake_word] listening for 'Hey AYRA' (model={WAKEWORD_NAME}, threshold={threshold:.2f})...",
        file=sys.stderr,
    )
    peak_score = 0.0
    peak_amp = 0
    last_beat = time.monotonic()

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return
        if reset_event is not None and reset_event.is_set():
            return
        if challenge_event is not None and challenge_event.is_set():
            return

        chunk = session.read()
        if chunk is None or getattr(chunk, "size", 0) == 0:
            continue

        rms = compute_rms(chunk)
        try:
            scores = model.predict(chunk)
            raw_score = scores.get(WAKEWORD_NAME, 0.0) if isinstance(scores, dict) else scores[WAKEWORD_NAME]
            score = float(raw_score)
            if not math.isfinite(score):
                score = 0.0
        except Exception as exc:  # noqa: BLE001
            print(f"[wake_word] prediction error on audio chunk: {exc}", file=sys.stderr)
            continue

        if score >= threshold:
            now = time.monotonic()
            # Guard 1: Reject phantom triggers on pure digital silence
            if rms < min_rms and getattr(chunk, "ndim", 1) > 0 and np.max(np.abs(chunk)) < min_rms:
                try:
                    model.reset()
                except Exception:  # noqa: BLE001
                    pass
                continue

            # Guard 2: Cooldown / debounce so one utterance never triggers twice
            if _last_trigger_ts > 0.0 and (now - _last_trigger_ts) < cooldown_sec:
                print(
                    f"[wake_word] debounced duplicate trigger "
                    f"(score={score:.3f}, rms={rms:.1f}, elapsed={now - _last_trigger_ts:.2f}s < {cooldown_sec:.1f}s)",
                    file=sys.stderr,
                )
                try:
                    model.reset()
                except Exception:  # noqa: BLE001
                    pass
                continue

            _last_trigger_ts = now
            try:
                model.reset()
            except Exception:  # noqa: BLE001
                pass
            print(
                f"[wake_word] detected '{WAKEWORD_NAME}' "
                f"(score={score:.3f}, rms={rms:.1f}, threshold={threshold:.2f})",
                file=sys.stderr,
            )
            return

        if armed_probe is not None:
            if score > peak_score:
                peak_score = score
            try:
                amp = int(np.abs(chunk).max()) if chunk.size else 0
            except Exception:  # noqa: BLE001 — diagnostics never break the loop
                amp = 0
            if amp > peak_amp:
                peak_amp = amp
            now = time.monotonic()
            if now - last_beat >= _HEARTBEAT_SEC:
                last_beat = now
                try:
                    armed = bool(armed_probe())
                except Exception:  # noqa: BLE001
                    armed = False
                if armed:
                    print(
                        f"[wake_word] armed heartbeat: peak_score="
                        f"{peak_score:.2f} (threshold {threshold:.2f}) "
                        f"peak_amp={peak_amp}",
                        file=sys.stderr,
                    )
                peak_score = 0.0
                peak_amp = 0


# M52 — barge-in. The monitor below runs openWakeWord *concurrently* with TTS
# playback, on its own thread, so the user can cut AYRA off mid-reply with
# "Hey AYRA". It uses its own singleton Model instance for thread safety.
_barge_model: Model | None = None


def _get_barge_model() -> Model | None:
    """Process-wide singleton for the barge-in monitor thread."""
    global _barge_model
    if _barge_model is None:
        _ensure_models_downloaded()
        resolved = _resolve_model_path()
        try:
            _barge_model = _instantiate_oww_model(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[barge-in] ERROR: could not load wake-word model at '{resolved}': {exc}",
                file=sys.stderr,
            )
            return None
    else:
        try:
            _barge_model.reset()
        except Exception:  # noqa: BLE001
            pass
    return _barge_model


def monitor_for_wake_word(
    session: AudioSession,
    interrupt_event: threading.Event,
    stop_event: threading.Event,
    threshold: float = 0.5,
    cooldown_sec: float = WAKEWORD_COOLDOWN_SEC,
    min_rms: float = MIN_SPEECH_RMS,
) -> None:
    """Barge-in monitor (M52). Runs on its own thread for the duration of a
    TTS reply: reads mic chunks, scores them with openWakeWord, and on a
    'Hey AYRA' detection sets `interrupt_event`."""
    global _last_barge_ts
    model = _get_barge_model()
    if model is None:
        print("[barge-in] wake-word model unavailable — monitor standing by", file=sys.stderr)
        stop_event.wait()
        return

    print("[barge-in] monitoring for 'Hey AYRA' during playback", file=sys.stderr)
    while not stop_event.is_set():
        try:
            chunk = session.read()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[barge-in] mic read failed ({exc}) — monitor exiting",
                file=sys.stderr,
            )
            return
        if stop_event.is_set():
            return
        if chunk is None or getattr(chunk, "size", 0) == 0:
            continue

        rms = compute_rms(chunk)
        try:
            scores = model.predict(chunk)
            raw_score = scores.get(WAKEWORD_NAME, 0.0) if isinstance(scores, dict) else scores[WAKEWORD_NAME]
            score = float(raw_score)
            if not math.isfinite(score):
                score = 0.0
        except Exception as exc:  # noqa: BLE001
            print(f"[barge-in] prediction error: {exc}", file=sys.stderr)
            continue

        if score >= threshold:
            if rms < min_rms and getattr(chunk, "ndim", 1) > 0 and np.max(np.abs(chunk)) < min_rms:
                try:
                    model.reset()
                except Exception:  # noqa: BLE001
                    pass
                continue
            now = time.monotonic()
            if _last_barge_ts > 0.0 and (now - _last_barge_ts) < cooldown_sec:
                try:
                    model.reset()
                except Exception:  # noqa: BLE001
                    pass
                continue
            _last_barge_ts = now
            try:
                model.reset()
            except Exception:  # noqa: BLE001
                pass
            print(
                f"[barge-in] interrupt detected '{WAKEWORD_NAME}' (score={score:.3f}, rms={rms:.1f})",
                file=sys.stderr,
            )
            interrupt_event.set()
            return
