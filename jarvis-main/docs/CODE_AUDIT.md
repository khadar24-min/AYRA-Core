# Jarvis — Code Audit (consolidation pass #6)

**Date:** 2026-08-22
**Scope:** Full end-to-end pass, user-requested ("go through this project from end
to end and make sure everything is clean, structured and organized... see where
improvements can be made in the python code. Make sure to double check your
work"). Four days after pass #5, and triggered by the same condition that
justified pass #5's own top finding: **the newest code is where the bugs are.**
Pass #5 found its #1 defect in code written two days earlier; this pass found its
#1 defect in code written *the previous evening* — the dedup fix from pass #5's
own follow-up commit (`fd36cc1`). No new product features.
**Method:** LOG REVIEW FIRST per the standing rule — the full 2026-06-28 →
2026-08-21 `jarvis.log` (3.1 MB, 131 restarts) before any code was read. Then
AST-based structural scans, then **empirical probes against the real corpus**
rather than reasoning about branches: the Tier-1 finding below was proven by
running the actual write path end-to-end on an isolated copy of production data
and watching the fact fail to reach disk.
**Baseline:** 58/58 gates green, 1,365 assertions. **After:** 58/58 green, 1,373
assertions (`knowledge_remember` 18 → 26).

Severity legend: 🔴 confirmed bug · 🟡 risk/latent · 🟢 polish.

---

## The log review (found nothing new — which is itself the result)

Two months of production logs, 131 restarts, **4 tracebacks total** — all four
already-diagnosed incidents (the M100 announcer death, and two M65 mic-stall
supervisor catches that *recovered on their own*, which is the supervisor doing
exactly its job). Everything else is transient network on a flaky uplink, and
every fail-soft path held.

Both recently-fixed classes were checked for recurrence **by date**, not by
presence: the empty-iCal-body misdiagnosis (pass #5, finding 2) has **zero**
occurrences after its 2026-08-18 fix, and the weather give-ups are all from
June/early July. The fixes are holding.

## Tier 1 — correctness

### 🔴 1. `knowledge_remember` silently discarded new facts and reported success

The dedup fix shipped the previous evening scored candidates with **containment
over the smaller token set**, `|A ∩ B| / min(|A|,|B|)`. That denominator
collapses to the *new fact* whenever the new fact is short, so the question
silently became "do all three of my words appear ANYWHERE in that 196-token
note?" — and for a large note the answer is nearly always yes. **Large notes
became attractors**: every short fact taught by voice scored 1.00 against the
biggest file in the corpus.

Proven end-to-end against an isolated copy of the real corpus:

```
FACT   'The Plex server is broken.'  (3 tokens)
match  homelab-and-runbooks.md @ 1.00  [judged fully covered]
reply  'I already had that one filed, sir.'
DISK   new=NONE  modified=NONE
==>    fact actually stored anywhere?  *** NO — LOST ***
```

`{plex, server, broken}` all occur in that note — in "Plex Media Server" and
"broken WMI provider", unrelated sentences. The corpus nowhere says the server
is broken. **A write path that drops the write and reports success is the worst
shape a bug can take**: the user has no symptom to report, and the loss is
silent and permanent.

Note the first fix's validation was **invalidated by its own data migration**.
It shipped the claim that *"Osiris is female"* scores 0.50 and stays separate —
true against the *pre*-consolidation corpus, where no note held both tokens. The
same commit consolidated ten cat notes into one that holds both, taking that
case to 1.00. The claim was true when written and false when committed.

**The replacement metric was chosen by measurement, not argument** — the project
rule is *measure before you tune*, and the previous threshold was tuned on
scores alone. 5 labelled restatements and 10 labelled distinct facts, scored
against the real corpus. A metric is usable only if `min(POS) > max(NEG)` — i.e.
some threshold separates them at all:

| metric | min(POS) | max(NEG) | verdict |
|---|---|---|---|
| containment / min *(shipped)* | 0.750 | **1.000** | **OVERLAPS — unusable at ANY threshold** |
| jaccard | 0.222 | 0.100 | separable, gap 0.12 |
| dice | 0.364 | 0.182 | separable, gap 0.18 |
| **ochiai** (set cosine) | **0.471** | **0.279** | **separable, gap 0.19 — best** |

So the old metric was not mis-tuned, it was **unusable**: no threshold existed.
(Jaccard, which the first fix explicitly rejected, was in fact separable — it
was rejected for scoring 0.32 against a 0.70 cut chosen for a *different*
metric.)

Fixed with **Ochiai** = `|A ∩ B| / sqrt(|A|·|B|)`, the geometric mean of the two
containments: still "is this largely a restatement?", but a fact can no longer
claim a note merely by being short. Cut at **0.40**, above the measured 0.375
midpoint, biased deliberately toward writing a new file. Routing on the real
corpus went from **5 of 10 cases wrong to 10 of 10 right**, with the flagship
accretion case still merging (0.51 / 0.47).

**Why the existing test did not catch it:** `knowledge_remember_test` already
had a negative case ("a distinct fact must not be swallowed") and it **passed —
for the wrong reason**. Its fixture corpus holds one *short* note, and this bug
only manifests against a *large* one. The new Test 8 builds a note far larger
than the fact under test, with every one of the fact's tokens present in
unrelated sentences. Reverting the metric fails 3 assertions; one of them
deliberately picks a fact whose tokens *are* in the fixture, because a fact
whose tokens are absent passes under either metric and asserts nothing.

## Tier 2 — latent

### 🟡 2. The corpus was the one durable store not written atomically

CLAUDE.md requires new durable state to go through `src/atomic_io.py`
(fsync-before-replace; this machine has no UPS). Seven modules do.
`src/knowledge.py` — which writes **the only user-*authored* data in the tree** —
used plain `path.write_text`. The merge path is worse than the new-file path: it
is a read-modify-write of the *whole* note, so a torn write there destroys every
fact already accreted into it, which is precisely the "silently destroying a
distinct fact" outcome the dedup design itself names as the dangerous one. Both
writes now go through `atomic_write_text`, pinned by Test 9.

### 🟡 3. Three of four direct-but-transitive imports were still undeclared

Pass #5 found `comtypes` imported directly while declared only transitively, and
fixed it — correctly reasoning that "a direct import deserves a direct
declaration". It did not check whether `comtypes` had peers. It had three:
`torch` (`security.py`, `sound_detector.py`), `icalendar`
(`outlook_calendar.py`), and `av` (`speech_to_text.py`, `remote_console.py`).
All are satisfied today by a transitive edge; if that edge moves, the feature
breaks at *runtime* on an already-rebuilt machine rather than at install time.
The `av` case is the sharpest — without it the phone PWA's voice path fails
while its text path keeps working, exactly the half-broken remote client nobody
reports. This is the project's own *fix the failure mode, not the instance* rule
catching the audit that articulated it, one pass later.

### 🟡 4. `plex_mcp._populate_tools` was append-only

`self.tools` / `self.tool_names` are appended to, never cleared. One caller, once
per session, so it cannot double up today — but a reconnect path is the obvious
next change here, and it would declare every tool to the API twice. Made
idempotent; cheaper than remembering why it wasn't.

## Tier 3 — polish

### 🟢 5. Dead code

13 unused imports across `src/` and `tests/` (AST-verified, then hand-checked —
the `Callable` imports that *look* unused are used in string annotations and
were kept). One genuinely orphaned function, `send_discord_alert_for_path`: a
convenience wrapper for "the caller has the path, not the bytes", whose only
plausible caller (`security.py`) always has the bytes. A helper extracted for a
caller that never came — what *rule of three before extracting* exists to
prevent. Removing it orphaned its `pathlib` import in turn, which the re-scan
caught.

### 🟢 6. Documentation drift — six stale counts

`README.md` claimed 54 gates / 51 suites / 107 milestone entries / "five gates
skipped in CI"; `CLAUDE.md` claimed 51 suites twice and described 58 *gates* as
58 *suites*; `.github/workflows/ci.yml` claimed "40 of 45 gates, 5 skipped". Real
figures: **58 gates = 55 suites + 3 structural**, 108 milestone entries.

The CI numbers were **measured, not guessed** — a `sitecustomize.py` injected on
`PYTHONPATH` (so it reaches every subprocess the gate spawns; an in-process
import hook does not, which is why the first attempt silently measured nothing)
blocked exactly the modules `requirements-ci.txt` omits, and the gate was re-run:
**49 of 58, 9 skipped**, not 5. The comment now also says the runtime skip list
is the authoritative one, so it degrades gracefully instead of going stale again.

## Verified non-issues (checked, deliberately not changed)

- **No bare `except:`, no mutable default arguments, no `subprocess` call
  without a timeout, no HTTP call without a timeout** (all AST-verified). The
  two `BaseException` handlers remain correct.
- **The 63 `except ...: pass` handlers are not silent swallows.** Sampled across
  the data paths: every one is a *narrow, typed* handler (`ValueError`,
  `(ValueError, TypeError)`) around a single `datetime` parse, most with an
  inline comment naming the fall-through. Idiomatic and deliberate.
- **The UI's `append`-in-a-loop sites are not leaks.** `console.py` and `hud.py`
  build their canvas-item lists **once** in the constructor and only *read* them
  in the render loop — they are not per-frame appends, which at 20-30 fps in an
  always-on process would have been one.
- **`plex_action` in `_RESTRICTED_DENY` is not a stale entry** despite not being
  a `_CLIENT_TOOLS` key — it lives in `_PLEX_LAPTOP_DISPATCH`, and line 1069
  documents exactly that.
- **"36 tools" is accurate**: 34 client + 2 server, 12 denied, 22 reachable from
  the phone and 23 from Discord.
- **`anticipation._is_duplicate` shares the *shape* of finding 1** (a short prior
  contained in a long new insight suppresses it) but not the severity: it matches
  whole normalized strings rather than token sets, which is far stricter; the
  buffer is a bounded `deque`; it is a backstop behind a prompt-level guard; and
  the failure mode is one skipped announcement, self-correcting next tick. Left
  alone deliberately — changing it would be speculative.
- **The three remaining unreferenced functions are deliberate and documented**:
  `face_auth.delete_encoding` is recorded in the M39 milestone as an unwired
  YAGNI hook, `face_auth.is_enrolled` is part of the documented defensive
  contract (every public function returns a sentinel if dlib is absent), and
  `autostart.desktop_shortcut_path` is one half of a symmetric accessor pair
  whose partner is used. Removing a documented decision is not cleanup.
- **Repo hygiene is clean**: nothing untracked-and-unignored, nothing
  tracked-but-ignored, no TODO/FIXME/HACK in `src/`, and both non-obvious
  top-level directories are legitimate (`sandbox/Containerfile` builds the
  `run_code` image; `stt_server/` is the GPU-offload box's deployable).

## Caught during close-out QA (worth its own heading)

The Test 8 fixture was originally **copied from the real corpus's largest note**
— hostname, username, and SSH key-auth configuration and all. `jarvis` is a
**PUBLIC** repo; `jarvis-knowledge` is private, and the 2026-07-14 scrub had
removed exactly that hostname (`git grep` at HEAD: **zero** occurrences). The
commit would have re-introduced it.

**The new vector is the point.** The scrub cleaned docs and code, and the
standing warning correctly says "PII was in the CODE not just docs" — but a
**test fixture** is a third surface, and it is the one you reach for precisely
when you want realistic data. Realism is exactly the wrong instinct there: a
fixture needs the right *shape* (a note far larger than the fact under test,
containing all its tokens in unrelated sentences), never the right *contents*.
Replaced with a generic runbook; the suite still fails 3 assertions when the
metric is reverted, so nothing was lost but the disclosure.

## Deferred (real, deliberately not done at the tail of an audit)

- **`listen_loop()` is 945 lines** — the sharpest structural finding in the tree,
  and a worse smell than any large *file*, because a large file of small
  functions is fine. It takes **15 parameters** and its body is largely one
  598-line closure (`_voice_session_loop`) that captures **11 of them** plus a
  `nonlocal`. That capture is *why* it is a closure and why extracting it is not
  mechanical: it needs a small state object (a `VoiceSession` holding the
  captured collaborators), not a longer parameter list. CLAUDE.md already names
  this ("extracting the text/voice intent dispatch... **wants a test at the right
  level first**"), and it is the hot path of an always-on voice assistant with no
  correctness payoff. It wants its own session, seam-tested first — the same
  judgement pass #5 made about `security.py`, for the same reasons.
- **`security.py` is still 1,714 lines**, unchanged from pass #5's deferral. Worth
  noting the shape though: it is **one class of 36 methods, largest 181 lines** —
  internally well-decomposed, a big class rather than a god function. That makes
  it materially less urgent than the 945-line function above.
- **`console.py::__init__` is 361 lines** of widget construction inside a
  1,189-line class. Benign, mechanical to split by widget group, and the
  lowest-risk of the three if a structural session ever happens.
- **Armed-mode capture starvation** remains open and unchanged (M99).

---

# Jarvis — Code Audit (consolidation pass #5)

**Date:** 2026-08-18
**Scope:** Full end-to-end pass, user-requested ("go end to end, doublechecking
everything... see if there could be any improvements"). Triggered on TWO of the
documented conditions at once: ~20 milestones since pass #4 (M101 vs pass #4 at
2026-07-03), and `src/security.py` crossing the 1,500-line god-file threshold
(now 1,700). No new product features.
**Method:** LOG REVIEW FIRST, per the standing rule — 14- and 60-day
`self_review` scans before any code was read. Then **AST-based structural scans
rather than grep**, targeting the classes of defect this project has actually
been bitten by: unguarded daemon-loop bodies (the M100 failure mode), blocking
subprocess calls without timeouts, resource acquisition without a release path,
bare/over-broad exception handlers, mutable default arguments, and
declared-vs-imported dependency drift. Every finding was re-verified against
source, and every fix was proven by reverting it and watching the new test fail.
**Baseline:** 58/58 gates green before any change. **After:** 58/58, with three
suites extended (`self_review` 23 to 28, `challenge_listen_trust` 19 to 25,
`outlook_calendar` 25 to 31).

Severity legend: 🔴 confirmed bug · 🟡 risk/latent · 🟢 polish.

---

## Tier 1 — correctness

### 🔴 1. `_auto_disarm` no longer mirrored `deactivate()` — could leave Jarvis transcribing the room forever

Found by auditing **code written two days earlier**, in M101. `_auto_disarm` is
the memory-watchdog / model-load-failure exit; its own docstring promises it
clears armed state "exactly like `deactivate()`", and the watcher thread exits
immediately after it, so anything left set is left set *permanently*.

M101 added two armed-scoped fields and cleared them only in `deactivate()`:

- **the challenge listening window.** Left raised, `listen_loop` skips the wake
  word and captures continuously — the exact failure the LOCKED path was
  explicitly guarded against ("LOCKED has no timer, so leaving it open would
  transcribe the room indefinitely"). The sibling path was missed.
- **the post-authentication trust window**, which must not outlive the armed
  session that granted it.

Reachable whenever the memory watchdog trips *during* an open challenge. Fixed,
and pinned by a **parity assertion** that compares every armed-scoped field
after `deactivate()` against the same field after `_auto_disarm()` — so the next
field added cannot break the promise silently. Reverting the fix fails 3
assertions.

This is the project's own *fix the failure mode, not the instance* rule catching
its own author, one milestone later.

### 🔴 2. An empty iCal body was reported as a bad URL

Outlook returned HTTP 200 with an empty body three times on 2026-08-12. The body
went straight to the parser, which raised, and the user-facing message was "I
couldn't parse the Outlook iCal feed, sir — the URL might be pointing at
something else." The URL was correct the whole time. A message that sends
someone chasing a configuration problem that does not exist is worse than no
message. Empty/whitespace bodies are now caught before the parser and reported
as the transient hiccup they are.

## Tier 2 — latent

### 🟡 3. The tray animation loop could die and freeze the status icon

The last unguarded long-lived loop body in the tree, found by an AST scan for
the M100 shape. It writes to a pystray icon (a Win32 resource) and builds PIL
images every tick — both can raise during teardown. An escape kills the thread,
and because the tray icon is the *primary* status indicator, the consequence is
not a missing animation: the icon **freezes on whatever state it last drew**, so
an idle assistant can sit showing a "speaking" dot indefinitely. Guarded, with
first-3-only error logging so a permanently dead icon cannot spin a log line
every two seconds.

### 🟡 4. `self_review` scored Jarvis's own speech as faults

Jarvis's spoken replies are written to the same log, and natural language is
full of the fault vocabulary. A real health report ranked "...Rhea Ripley was
**unable to** defend her..." alongside genuine faults. This is signal hygiene,
not cosmetics — `self_review` reports a ranked top-N, so chatty false positives
push real faults off the end of the list, and `self_review` is the instrument
that found M100.

The discriminator was **chosen from the log, not guessed**. Of the 13 untagged
"concerning" lines in the entire log:

| kind | length |
|---|---|
| real faults (`RuntimeError: microphone stream stalled...`) | 19-93 chars |
| model prose (`Yeah, it's not looking great. Thunder...`) | 476-567 chars |

A 5x gap, so the cut sits at 200. Tagged lines (`[outlook] ...`) stay faults at
any length. **A first attempt at this rule was wrong and was caught before
shipping:** requiring a `[tag]` dropped the genuine untagged
`Socket exception: An existing connection was forcibly closed` along with the
prose. Measuring the actual length distribution is what separated them.

### 🟡 5. Two dependency gaps that only bite on a rebuild

- **`face_recognition` was absent from `requirements.txt` entirely.** It is one
  of the **two** ways to clear a security challenge — and the one that cleared
  the user's 2026-08-18 live test in two seconds. It is lazily imported and
  every call site degrades to "face auth disabled", so a rebuilt machine loses
  an authentication factor **silently**. The omission was almost certainly
  deliberate (dlib compiles C++ and needs MSVC Build Tools, so an uncommented
  entry would break `pip install -r` for anyone without a toolchain) — so the
  fix is to make the intent explicit: a documented, commented-out optional
  entry, plus a `doctor.py` line reporting whether it is present.
- **`comtypes` is imported directly** by `system_control.py` but was present
  only transitively via pycaw. A direct import deserves a direct declaration,
  or a pycaw change breaks volume control at runtime instead of at install time.

### 🟡 6. `doctor.py` reported a working wake word as missing

It checked `~/.cache/openwakeword`, but openWakeWord ships `hey_jarvis` inside
the package and only uses that cache for models fetched later. So the health
check *meant to reassure you* reported "not yet downloaded" on a machine that
had been waking to "Hey Jarvis" for months. Now checks where the model lives.

## Verified non-issues (checked, deliberately not changed)

- **All blocking `subprocess` calls have timeouts** (AST-verified, 0 exceptions).
- **No bare `except:`, no `except BaseException` misuse, no mutable defaults.**
  The two `BaseException` handlers are correct: `atomic_io` unlinks its temp
  file and **re-raises**; the TTS producer records the error and still posts its
  sentinel so the consumer cannot hang.
- **Every camera capture releases on all paths** (`cap = None` + `try/finally`),
  the M44.3 contract.
- **The restricted-origin boundary is intact and enforced at both gates** — the
  tool-list filter in `stream_response` *and* the executor deny-check — plus a
  third touch so denied attempts are not misreported as "fired" in telemetry.
  34 client tools, 12 denied; every one of the 23 reachable from a phone or
  Discord is read-only or a deliberately-allowed write (reminders, knowledge).
- **The iCal "gave up after 3 attempts" lines are NOT a regression.** They are
  per-poll give-ups on a ~2-minute network blip, each recovered on the next
  60-second poll, with no user-visible effect. M94's "zero terminal failures"
  described a lucky 7-day window, not a permanent property — the memory has been
  corrected so a future session does not chase it.
- **No TODO/FIXME/HACK markers anywhere in `src/`.**
- **`CLAUDE.md`'s factual claims are accurate**: "36 tools" = 34 client + 2
  server; "gate is at 58" = 55 suites + 3 structural gates.

## Deferred (real, deliberately not done at the tail of an audit)

- **`src/security.py` is 1,700 lines** — one class, 36 methods, over the
  documented 1,500 threshold. The notification/evidence methods
  (`_send_discord_alert_async`, `_send_email_alert_async`,
  `_save_evidence_bytes`) are the clean extraction: they touch neither the
  challenge lock nor the armed state. But this is a working always-on security
  state machine, a split has **no correctness payoff**, and doing it at the tail
  of an audit is precisely the risky-change-at-session-end the close-out
  protocol says to surface rather than perform. It wants its own session, with
  the seam tested first.
- **The speaker threshold still cannot be fixed by moving it** (see M101 and
  `docs/ENV_VARS.md`): the score scales with clip length, so one global number
  cannot serve a 1.5 s command and a 5 s question. Wants a duration-aware
  threshold or a longer minimum capture.
- **Armed-mode capture starvation** remains open and unchanged (M99).

---

# Jarvis — Code Audit (consolidation pass)

**Date:** 2026-07-02
**Scope:** Full end-to-end QA pass, user-requested ("go through the code end to
end, review the logs, find any bugs… make sure the code is 100% correct").
Eleven days after pass #3 — triggered by the user's Fable-5 test drive, not the
usual ~20-milestone cadence. No new product features.
**Method:** LOG REVIEW FIRST this time — `jarvis.log` since 2026-06-28 was read
before any code, and it surfaced a live incident the tests never could (see
finding 1). Then **eight parallel read-only auditor agents**, partitioned by
subsystem cohesion, with the shared rubric tuned to this project's DELIBERATE
contracts (fail-soft; WASAPI thread-affinity; the cooperative speech gates; the
M44.3 persistent capture; fail-open speaker gate; no-gate `run_code`; the
restricted phone/Discord tool boundary; GET-only presence; Stop-Service without
`-Force`) so intentional design wasn't flagged. Full `src/` tree + `main.py` +
both launchers + the PWA JS. Every auditor finding was re-verified against the
source before a fix was written.
**Baseline:** `scripts/run_all_tests.py` = **44/44 gates green** before any
change. **After:** **45/45** (added `gates_test` [13]; `predictions` 42→47;
`dismissal` 27→28).

> Prior passes (2026-05-29 M1→M67; 2026-06-09 M68→M77; 2026-06-21 M78→M88) live
> in git history + the `[[project-qol-consolidation-pass]]` memory. This is
> pass #4 — ~37 fixes across 31 files.

Severity legend: 🔴 confirmed bug · 🟡 risk/latent · 🟢 polish.

---

## The live incident (found in the log, not by an auditor)

### 🔴 1. The listening loop was DEAD in production the morning of the audit

At 07:33:08 on 2026-07-02, `AudioSession.__enter__` raised
`PortAudioError: Device unavailable [-9985]` at startup. `listen_loop`'s `try`
had only a `finally` — the thread died unhandled while every other subsystem
(Discord, remote console, monitors, presence auto-arm) kept running. Jarvis was
deaf to voice ALL DAY with no outward sign; the M65 watchdog never fired because
it watches process exit, not thread death. The acoustic subsystem opened the
SAME device successfully 35 minutes later, so a retry would have recovered.
This violated the project's oldest engineering rule: *never crash the listening
loop*.

**Fix — a mic-session supervisor** (`main.py`): the session body is extracted
into `_voice_session_loop()` (byte-identical) and wrapped in a retry loop — any
escape (open failure, a mid-session device error out of the previously-unguarded
`wait_for_wake_word`, a stalled stream) logs the traceback once per outage,
surfaces it in the console AND aloud ("I seem to have lost the microphone,
sir"), then re-opens on a capped backoff (10 s → 60 s), announcing recovery.
Companions: `AudioSession.read()` gained a 10 s stall timeout (a silently-dead
stream — the documented KVM case — now raises into the supervisor instead of
wedging the loop AND blocking quit forever on the join), and the barge-in
monitor's `session.read()` is guarded so a mid-reply mic death ends the monitor
gracefully.

### 🔴 2. Chronic acoustic input overflow through every armed window

`[acoustic] stream status: input overflow` fired every ~3–4 s for the WHOLE of
every armed window — 1,000–2,000 log lines/day (the earlier "occasional
overflow" reading was an artifact of timestamped lines defeating `uniq -c`).
Two causes, both fixed: (a) the acoustic `InputStream` used the sounddevice
default LOW-latency host buffer (tens of ms) though nothing downstream needs
latency (2 s windows) — armed-mode PANNs+YOLO bursts overflowed it constantly,
and every overflow = dropped samples = a gap-corrupted detection window
(degrading exactly the armed rules: a knock transient can vanish;
`voice_while_armed` needs consecutive clean windows). Now `latency="high"`.
(b) The status print ran unthrottled INSIDE the 80 ms-budget PortAudio callback
— a timestamped file write lengthening the very callback whose lateness caused
the overflow. Now a counter + one deduped line per 60 s.

### 🟡 3. iCal fetch had zero retries on a flaky uplink

The log shows several transient TLS-handshake `ConnectTimeout`s per day; the
weather path retries once, the Outlook iCal path never did — one blip failed an
on-demand "what's on my calendar?" outright. Now one retry + 0.5 s backoff.
(The timeout values themselves — 10–15 s handshake budgets — were checked and
are fine.)

### 🟢 4. The clock bug ("what time is it" → "12:00 AM")

The documented follow-up from pass #3. The cached system prefix anchors the
DATE only (by design — cache invalidates once per day); the current TIME now
rides the per-turn UNcached system block (M80/M85's), so time answers come from
ground truth at a few fresh tokens per turn and zero cache-miss cost.

---

## Tier 1 — correctness / contract

### 🔴 5. Interpreter mode: STT-failure hot loop with NO exit

The exact bug pass #3 fixed for conversation mode was never applied to the M87
interpreter branch: `except: print; continue` with the mode still set re-enters
immediately — a tight wake-word-less spin, and the only exit ("stop
interpreting") requires a SUCCESSFUL transcription, so the mode was
unrecoverable short of an app restart. Now a failure counter exits the mode
after `_CONVERSATION_IDLE_EXITS` consecutive failures (reset on success).

### 🔴 6. Interpreter mode: tray "Reset conversation" silently inert

The interpreter branch never consulted `reset_event` (and bypasses
`wait_for_wake_word`, which does) — a reset clicked while interpreting did
nothing, then fired stale after a voice exit. Combined with #5, there was NO
recovery input at all when interpreter STT was failing. The branch now exits
the mode on a pending reset and falls through to the wake-word path, which
seals it normally.

### 🔴 7. `security._auto_disarm` left a permanent transcript-diversion stuck state

Unlike `deactivate()`, `_auto_disarm` (memory watchdog / model failure) never
cleared `_challenge_active`/`_locked`. Tripping mid-CHALLENGE meant the watcher
thread exited (so the timeout check never ran again) while `handle_transcript`
kept diverting EVERY subsequent utterance into `try_authenticate` forever —
disarmed but deaf to everything except the passphrase, with a stale 🔒 pinned.
Now clears challenge/locked exactly like `deactivate()` (incl.
`on_locked_changed(False)`).

### 🔴 8. The speech gates didn't nest — new `src/gates.py` `CountedEvent`

`pc_speaking`/`announce_speaking` are raised by MULTIPLE unserialized speakers
(turn replies, the Announcer, `speak_line`, interpret). With plain Events, a
reminder announce landing 2 s into a 20 s reply cleared both gates in its
`finally` while the reply was still playing — re-opening the 2026-05-29
omni-mic self-capture bug (the open follow-up window can transcribe Jarvis's
own reply tail) AND the M68 armed stutter (PANNs/YOLO resume mid-speech) for
the remainder of the reply. `CountedEvent` keeps the Event API (`set()`
increments, `clear()` decrements, flag drops on the LAST clear, stray clears
clamp at zero) so every call site works unchanged. New `scripts/gates_test.py`
(13). *The audible announce-over-reply overlap itself is a separate deferred
design item — the gates are now correct regardless.*

### 🔴 9. Long phone voice notes killed the WebSocket

The PWA sends a recording as ONE JSON frame (base64 ≈ 1.33× the blob) with a
60 s mic cap — a long Safari audio/mp4 clip tops the websockets default
`max_size` of 1 MiB, closing the connection (1009) and silently losing the
utterance. `serve(..., max_size=8 MiB)`.

### 🟡 10. Disarm race could leak an open camera for the whole disarmed period

`grab_frame_for_snapshot` checked `is_armed()` then called `_grab_frame`, which
OPENS a new persistent capture if `_cap is None` — a Discord snapshot passing
the armed check just as the M70 arrive-home disarm released the watcher's
capture would open one nobody ever releases (LED on, device blocked — the M44
leak class). It now serves ONLY from an already-open capture, under the lock.

### 🟡 11. Conversation-mode idle exits were defeatable

(a) With voice-lock ON, a gate-dropped media turn (TV/YouTube) RESET the idle
counter before the drop — background media kept hands-free mode alive (and STT
burning) forever. The reset moved below the gate; a dropped turn now COUNTS
toward the idle exit. (b) A proactive announce during a listening window aborts
the capture (suppress_event) → empty transcript → counted as user idleness; a
chatty reminder schedule could silently exit conversation mode with the user
present. A suppressed capture (pc_speaking still set) no longer counts.

---

## Tier 2 — latent

- **`knowledge.reindex` wasn't atomic** (🟡): Python's sqlite3 legacy mode runs
  DDL in AUTOCOMMIT, so the DROP/CREATE committed instantly and a crash
  mid-insert (power loss, no-UPS box) left a committed EMPTY index silently
  returning nothing — the exact state the docstring claimed impossible. Now an
  explicit `BEGIN IMMEDIATE` wraps the whole rebuild (SQLite DDL is
  transactional): a crash rolls back to the intact old index.
- **predictions: the mining watermark advanced on FAILURE** (🟡): a failed
  transcript scan or miner call (API down / unparseable output) was
  indistinguishable from "no predictions found" — the watermark advanced and
  any prediction in that window was permanently skipped (incremental scans
  never revisit). `_default_miner` now returns None on failure; the watermark
  only advances on success. +5 regression tests.
- **predictions: `surfaced` lost-update** (🟡): `take_unsurfaced` (briefing
  thread) races an in-flight background cycle whose `_save` of a stale copy
  reverted `surfaced=True` → repeated "you called it". New `_STORE_LOCK`; mine
  merges into a FRESH load; `resolve_due` collects mutations by id across its
  long resolver calls and applies them to a fresh load under the lock.
- **`DedupeStore.save` wasn't durable** (🟡): the one store missed by the
  atomic_io migration — and it backs the 168-hour WEATHER dedupe, so a torn/
  unsynced file after power loss re-announced still-active multi-day NWS
  warnings (the exact class the long retention prevents). → `atomic_write_text`.
- **quiet_hours record/take race** (🟡): both are load-mutate-save from
  different threads; one interleaving resurrects surfaced items, the other
  drops a fresh deferral. → module `_STORE_LOCK`.
- **sound_detector lifecycle** (🟡×4): `activate()`/`deactivate()` are now
  serialized by a lifecycle lock (two near-simultaneous activations — presence
  auto-arm + tray toggle — could BOTH open an InputStream and orphan one,
  capturing forever); a dying inference thread is joined before the spawn check
  (rapid toggle could leave active-with-no-loop); `_download` no longer stats
  the temp file after unlinking it (raised `FileNotFoundError` out of a
  returns-error-string contract); a stream that fails in `start()` is closed,
  not leaked.
- **Same stale-thread spawn race fixed uniformly** in calendar_monitor,
  weather_alerts, homelab_monitor, anticipation (join the mid-exit thread so
  `is_alive()` is truthful).
- **`vision_describe` had no timeout** (🟡): SDK default ≈ 10 min + retries per
  armed visual alert during an outage → thread pileup + stale photos. Now 30 s,
  1 retry (the pass-#3 anticipation fix applied to its sibling).
- **notifications: `httpx.InvalidURL` escaped the never-raises contract** (🟡):
  it's not an `HTTPError` subclass, and the security deterrent calls the
  Discord senders with no try-wrapper on exactly that documented premise. All
  three now catch `Exception`.
- **discord_bot: a non-Forbidden `create_thread` failure discarded the reply**
  (🟡): e.g. HTTP 400 "already has a thread" fell to the outer except and the
  composed answer (+ webcam images) vanished. Broadened to `HTTPException` →
  inline fallback.
- **Two lazy-load races** (🟡): `speech_to_text._get_model` (reachable from the
  listen-loop AND remote-origin threads; double ~250 MB Whisper load) and
  `embeddings.get_embedder` (tray reindex vs turn-thread recall; double ~80 MB
  model) both got load locks.
- **`TimestampStream` write race** (🟡): `print()` = two `write()` calls; an
  interleaving thread could emit its whole line UNstamped. Locked.
- **`_float_env` accepted `nan`/`inf`** (🟡): `JARVIS_SPEAKER_THRESHOLD=nan`
  makes every `score >= threshold` False — with voice-lock ON, a silent total
  lockout with no warning. Non-finite now rejected.
- **`resolve_input_device("--1")` crashed startup** (🟡): `lstrip("-")` strips
  all dashes; `int()` raised. Guarded → default mic.
- **One shared Anthropic client** (🟡/perf): `stream_response` and
  `stream_translation` built a NEW client (new httpx pool → fresh TLS
  handshake) every turn — and per relayed utterance in interpreter mode. Now a
  keyed lazy singleton (`_get_client`), thread-safe per the SDK.
- **remote_console token compare** (🟡): `hmac.compare_digest(str, str)` raises
  `TypeError` on non-ASCII — a pasted token with a Unicode dash killed the
  handler task with no `auth_fail`, so the client retried the bad token forever.
  Both auth paths now compare UTF-8 bytes inside a guard.
- **remote_console fire-and-forget task refs** (🟡): the loop holds only weak
  refs; an untracked send task could be GC'd mid-flight (dropped frame / reply
  audio). `_pending_tasks` set + done-callback discard.
- **pc_shell argv option-injection** (🟡): `_HOSTNAME_RE` allowed a leading
  `-`, which ping/tracert/nslookup would parse as an OPTION, not a host.
  Leading-hyphen targets rejected.
- **tmdb last-resort wrapper** (🟢): `execute_tmdb_tool`'s never-raises
  contract rested entirely on per-helper guards (unguarded `int(id)`); it now
  has the same defensive ceiling as system_control/pc_shell.
- **aec_barge None guard** (🟢): `self._interrupt.set()` without a None check
  inside the PortAudio callback (the event is Optional at every call site).
- **security deterrent LOCKED re-check** (🟡): a successful auth landing during
  the slow evidence/push window used to be followed by an unconditional
  `on_locked_changed(True)` + "authorities have been notified" right after
  "Welcome back, sir", pinning a stale 🔒. Re-checked under the lock; evidence/
  pushes still fire (correct — someone WAS unauthenticated the whole window).

---

## Tier 3 — polish

- **"good night" is no longer a dismissal** — as one it short-circuited before
  the LLM on every follow-up/conversation-mode turn, shadowing the M63
  `get_good_night` wrap. It now reaches Claude and routes to the wrap.
  (`dismissal_test` updated: 28.)
- **PWA transcript DOM capped** at 500 nodes (an installed PWA left connected
  for days grew without bound).
- **Watchdog no longer defeats log rotation** — its lifetime-open handle on
  `jarvis.log` made every child respawn's `rotate_if_needed` rename fail
  silently (unbounded growth past 5 MB). The watchdog now writes its own
  `jarvis_watchdog.log`. Also: spawn falls back to the running interpreter when
  the venv is absent (was a guaranteed FileNotFoundError → "giving up"), and
  the dead `_VENV_PYTHON` variable is gone.
- **weather_alerts** dead `_geocode_failed` field removed (documented a
  permanent-failure latch that never existed).
- **autostart** PowerShell single-quote escaping (`'` → `''`) on every
  interpolated path/description — an apostrophe profile path broke the shortcut
  script (and was technically an injection surface).
- **ui tray guard** — a pystray/Win32 construction failure used to kill the
  tray thread silently, leaving NO quit path (the console X only hides). Now
  logged + the console window is shown as the fallback surface.

## Deferred (real items, not rushed)

- **OneDrive-redirected Desktop**: `create_desktop_shortcut` writes to
  `%USERPROFILE%\Desktop` ignoring known-folder redirection — on an
  OneDrive-backup profile the icon lands in an invisible folder. Proper fix is
  `SHGetKnownFolderPath`; works on this box, so deferred.
- **Audible announce-over-reply overlap**: nothing serializes Announcer
  playback against a playing turn reply (both audible at once). The gate
  correctness is fixed (CountedEvent); making announces DEFER while a turn is
  speaking is a small design change for its own session.
- **Text/voice intent-dispatch rule-of-three extraction** — carried from pass
  #3; still wants its own test-harness-first pass.

---

## Verified non-issues (reassurance)

Everything pass #3 verified was re-verified clean by fresh eyes: no command
injection / allowlist bypass beyond the leading-hyphen nit (system_control +
pc_shell quote-out is sound; mutating verbs confirm + elevate server-side;
`plex_actions` mutations `retry=False`); the two-gate restricted-origin
boundary agrees at both gates incl. the Discord camera claw-back; the M44.3
persistent capture is intact (and now closed against the last escape path,
finding 10); WASAPI thread-affinity observed throughout; the async↔thread
bridges marshal correctly; per-turn reply sinks don't leak across origins; no
PWA XSS; `run_code` container isolation intact; prompt-cache discipline holds
(single cache_control breakpoint, byte-stable prefix; the new time line rides
the uncached per-turn block); the atomic_io implementation itself is correct;
`recall_conversation`'s day-window and speaker-filter fixes hold; the all-day
calendar TZ fix holds; presence's generation-token cancel race is closed.

Every fix is reversible and gated by `scripts/run_all_tests.py` (**45/45**).
Live-validation checklist for the next session: a normal voice turn (supervisor
didn't disturb the hot path), unplug/replug the mic mid-session (supervisor
recovers + speaks), an armed window (overflow lines rate-limited to ~1/min,
detection still fires), "good night" (routes to the M63 wrap), a long phone
voice note, and "what time is it" (real time).
