r"""Regression tests for the 2026-08-16 pair: post-auth trust + challenge listening.

WHY THIS EXISTS (jarvis.log 2026-08-16 14:38-14:39):

  14:38:42  motion — entering CHALLENGE state
  14:38:42  [announce] Identify yourself, sir.
  14:38:46  challenge prompt finished — 15s timer armed
  14:38:53  [wake_word] detected            <- 7s of a 15s budget spent WAKING him
  14:39:00  CHALLENGE cleared by voice auth <- passphrase accepted
  14:39:13  voice-lock active → ignoring unrecognized turn: 'Stand down.'   (0.72)
  14:39:21  voice-lock active → ignoring unrecognized turn: 'stand down'    (0.73)
  14:39:32  [ui] restart requested          <- he had to RESTART Jarvis to disarm

Two defects, one per group below:

  1. Authentication granted nothing. The voice lock went straight back to
     judging a voiceprint that armed-mode capture degradation had pushed under
     the threshold — so the user proved his identity and was then refused. A
     successful challenge now opens a short trusted session.

  2. The challenge prompt is a QUESTION that dropped back to IDLE, so answering
     it required a wake word first. Security now raises a listening-window event
     once the prompt finishes and lowers it when the challenge resolves.

Both edges matter: trust must not leak past arm/disarm, and the listening window
must not stay open (LOCKED has no timer — an open window would transcribe the
room indefinitely).

    python tests/challenge_listen_trust_test.py     # exit 0 = pass
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import security as sec  # noqa: E402
from src.listen_loop import _recently_authenticated  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok: {label}")
    else:
        FAILED += 1
        print(f"  FAIL: {label}  {detail}")


def make_watcher():
    w = sec.SecurityWatcher(
        camera_index=0,
        passphrase="strongest avenger",
        announce=lambda *a, **k: None,
        evidence_dir=Path("."),
    )
    w._announce = lambda *a, **k: None
    w._safe_call = lambda fn, *a, **k: None
    w._watch_loop = lambda: None
    return w


# --- 1. Post-authentication trust ------------------------------------------
print("\n[group] a successful passphrase opens a trusted session")

w = make_watcher()
w._armed.set()
w._challenge_active = True

check("not trusted before authenticating", w.recently_authenticated() is False)

w.try_authenticate("strongest avenger")
check("passphrase cleared the challenge", w._challenge_active is False)
check("...and opened the trust window", w.recently_authenticated() is True)
check("the listen_loop predicate agrees", _recently_authenticated(w) is True)

# THE BUG: "stand down" must now reach the deactivate intent.
w._armed.set()
consumed = w.handle_transcript("stand down")
check("'stand down' is consumed by security after auth", consumed is True)
check("...and actually DISARMED", w.is_armed() is False)

print("\n[group] trust does not outlive the situation")

w = make_watcher()
w._armed.set()
w._challenge_active = True
w.try_authenticate("strongest avenger")
check("trusted right after auth", w.recently_authenticated() is True)
w._armed.set()          # (auth disarmed nothing here; simulate still-armed)
w.deactivate()
check("disarming ends the trusted session", w.recently_authenticated() is False)

w = make_watcher()
w._authenticated_until = time.monotonic() + 999
w.activate()
check("a fresh arm does NOT inherit trust", w.recently_authenticated() is False)
w._armed.clear()

check("a broken watcher fails CLOSED (no trust)",
      _recently_authenticated(object()) is False)
check("no watcher fails CLOSED", _recently_authenticated(None) is False)


# --- 2. The challenge listening window -------------------------------------
print("\n[group] challenge opens a no-wake-word listening window")

w = make_watcher()
ev = threading.Event()
w.set_challenge_listen_event(ev)
w._armed.set()

check("window is closed before any challenge", ev.is_set() is False)

# _enter_challenge defers the timer to the announce's on_done; capture it.
captured = {}
w._announce = lambda text, on_done=None, **k: captured.update(on_done=on_done)
w._enter_challenge(b"", source="local", now=time.monotonic())
check("challenge opened", w._challenge_active is True)
check("window still CLOSED while the prompt is playing", ev.is_set() is False,
      "capturing during our own prompt would fight self-capture suppression")

captured["on_done"]()          # prompt finished
check("window OPENS once the prompt finishes", ev.is_set() is True)

w.try_authenticate("strongest avenger")
check("window CLOSES when the challenge is cleared", ev.is_set() is False)

print("\n[group] the window closes on the LOCKED path too")

w = make_watcher()
ev = threading.Event()
w.set_challenge_listen_event(ev)
w._armed.set()
w._announce = lambda text, on_done=None, **k: captured.update(on_done=on_done)
w._enter_challenge(b"", source="local", now=time.monotonic())
captured["on_done"]()
check("window open during the challenge", ev.is_set() is True)
# Force the deterrent path.
w._challenge_started_at = time.monotonic() - 999
w._save_evidence_bytes = lambda b: None
w._check_challenge_timeout()
check("LOCKED state entered", w._locked is True)
check("window CLOSED on lockout — no endless transcription", ev.is_set() is False)

# --- _auto_disarm must mirror deactivate() (2026-08-18 audit) -------------
# _auto_disarm is the memory-watchdog / model-load-failure exit. Its docstring
# promises it clears armed state "exactly like deactivate()", and the watcher
# thread exits immediately after, so anything left set is left set FOREVER.
# M101 added two armed-scoped fields and cleared them only in deactivate():
# a challenge listening window left raised would make listen_loop skip the wake
# word and transcribe the room indefinitely. Pin the parity so the next field
# added cannot quietly break it again.
print("")
print("[group] _auto_disarm clears all armed-scoped state")

w = make_watcher()
ev = threading.Event()
w.set_challenge_listen_event(ev)
w._armed.set()
w._challenge_active = True
w._locked = True
w._authenticated_until = time.monotonic() + 999
ev.set()

w._auto_disarm()

check("_auto_disarm disarms", w.is_armed() is False)
check("_auto_disarm clears the challenge", w._challenge_active is False)
check("_auto_disarm clears LOCKED", w._locked is False)
check("_auto_disarm CLOSES the listening window (no endless transcription)",
      ev.is_set() is False)
check("_auto_disarm ends the trusted session",
      w.recently_authenticated() is False)

_a, _b = make_watcher(), make_watcher()
for _w in (_a, _b):
    _w.set_challenge_listen_event(threading.Event())
    _w._armed.set()
    _w._challenge_active = True
    _w._locked = True
    _w._authenticated_until = time.monotonic() + 999
_a.deactivate()
_b._auto_disarm()
_fields = ("_challenge_active", "_locked", "_authenticated_until")
_diff = [f for f in _fields if bool(getattr(_a, f)) != bool(getattr(_b, f))]
check("deactivate() and _auto_disarm() agree on every armed-scoped field",
      not _diff, f"diverged on {_diff}")


print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
