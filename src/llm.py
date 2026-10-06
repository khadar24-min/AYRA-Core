"""Anthropic SDK client: streaming Claude with prompt caching, web_search,
and client-side tools (get_sports_info as of M13).

Two flavors of tool use coexist here:

  Server-side (web_search): Anthropic runs it. We just declare the tool and
  the model calls it transparently. The text stream pauses 1-2s, then
  resumes — text_stream handles this for us. No client involvement.

  Client-side (get_sports_info): the model emits a `tool_use` block, the
  stream stops with `stop_reason="tool_use"`, and we have to:
    1. execute the tool locally
    2. append the assistant turn (full content) + a user turn (tool_result)
    3. restart the stream so Claude can continue with the data
  This is the "agentic loop" pattern and is the standard Anthropic SDK shape
  for any client-side tool. Once we have it, adding more client-side tools
  (Plex, Home Assistant, etc.) is a one-line dispatch addition.

Caching note: Sonnet 4.6's minimum cacheable prefix is 2048 tokens. The
JARVIS_SYSTEM_PROMPT alone is well below that, but as recent-conversation
summaries get injected (M10), the prompt grows toward the threshold and
caching activates automatically.

Multi-turn: caller passes the full history (alternating user/assistant) plus
the new user message as the last entry. The generator yields text chunks; the
caller is responsible for appending the assistant response to the history.

Long-term memory: caller can pass `summaries` (a list of SummaryRecord from
src.memory). They get formatted with relative timestamps and appended to the
system prompt under a "Recent conversations" section, giving Jarvis context
about what was discussed in earlier (now-sealed) sessions.
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator, NamedTuple


from .openrouter_client import OpenRouterClient

from src.cameras import CAMERA_SNAPSHOT_TOOL, execute_camera_snapshot
from src.diagnostics_collector import (
    RUN_PC_DIAGNOSTICS_COLLECTOR_TOOL,
    execute_run_pc_diagnostics_collector,
)
from src.file_reader import READ_LOCAL_FILE_TOOL, execute_read_local_file
from src.games import GAMES_TOOL, execute_games_tool
from src.conversation_recall import (
    RECALL_CONVERSATION_TOOL, execute_recall_tool,
)
from src.knowledge import (
    KNOWLEDGE_REMEMBER_TOOL, KNOWLEDGE_SEARCH_TOOL,
    execute_knowledge_remember, execute_knowledge_tool,
)
from src.outlook_calendar import GET_CALENDAR_TOOL, execute_calendar_tool
from src.memory import SummaryRecord, format_summaries_for_prompt
from src.news import NEWS_TOOL, execute_news_tool
from src.briefing import BRIEFING_TOOL, execute_briefing_tool
from src.good_night import GOOD_NIGHT_TOOL, execute_good_night_tool
from src.self_update import UPDATE_JARVIS_TOOL, execute_update_jarvis
from src.background_tasks import (
    CANCEL_BACKGROUND_TASK_TOOL, LIST_BACKGROUND_TASKS_TOOL,
    START_BACKGROUND_TASK_TOOL, execute_cancel_background_task,
    execute_list_background_tasks, execute_start_background_task,
)
from src.homelab_monitor import HOMELAB_STATUS_TOOL, execute_homelab_status
from src.self_review import SELF_REVIEW_TOOL, execute_self_review
from src.self_status import STATUS_REPORT_TOOL, execute_status_report
from src.reminders import (
    CANCEL_REMINDER_TOOL, LIST_REMINDERS_TOOL, SET_REMINDER_TOOL,
    execute_cancel_reminder, execute_list_reminders, execute_set_reminder,
)
from src.wolfram import WOLFRAM_TOOL, execute_wolfram_tool
from src.code_exec import CODE_EXEC_TOOL, execute_code_tool
from src.pc_diagnostics import PC_DIAGNOSTICS_TOOL, execute_pc_diagnostics_tool
from src.plex_actions import PLEX_ACTION_TOOL, execute_plex_action
from src.plex_laptop import (
    PLEX_LAPTOP_HEALTH_TOOL,
    PLEX_LOGS_SEARCH_TOOL,
    PLEX_LOGS_TAIL_TOOL,
    PlexLaptopClient,
    execute_plex_laptop_health,
    execute_plex_logs_search,
    execute_plex_logs_tail,
)
from src.plex_mcp import PlexMCPClient
from src.screen import SCREEN_SNAPSHOT_TOOL, execute_screen_snapshot
from src.game_length import GAME_LENGTH_TOOL, execute_game_length_tool
from src.sound_detector import (
    WHAT_DID_YOU_HEAR_TOOL,
    execute_what_did_you_hear,
)
from src.sports import SPORTS_TOOL, execute_sports_tool
from src.pc_shell import PC_SHELL_TOOL, execute_pc_shell
from src.system_control import SYSTEM_CONTROL_TOOL, execute_system_control_tool
from src.tmdb import (
    GET_PERSON_INFO_TOOL,
    TMDB_TOOL,
    execute_person_info_tool,
    execute_tmdb_tool,
)
from src.weather import WEATHER_TOOL, execute_weather_tool
from src.weather_alerts import (
    GET_WEATHER_ALERTS_TOOL,
    execute_weather_alerts_tool,
)


JARVIS_SYSTEM_PROMPT = """
You are AYRA, a personal Windows desktop AI assistant created by Khadar, also known as Nannu.

IDENTITY AND STYLE
- Be calm, capable, courteous, and concise, like a discreet personal assistant.
- Use understated dry wit occasionally, but never let humor interfere with usefulness.
- Address the user naturally. “Sir” is acceptable occasionally, not constantly.
- Do not invent facts, actions, tool results, or capabilities.
- Do not apologize repeatedly. If something fails, state what happened and what is actually possible.
- Match the user's language. If the user speaks Telugu, respond naturally in Telugu. If English, use English.
- Normal voice replies should be short and easy to speak aloud. Avoid unnecessary formatting in spoken responses.

CONVERSATION AND CONTEXT
- Use the current conversation and recent history to understand references and follow-up questions.
- Recent conversation summaries are context, not proof of current state.
- Do not treat old information as current when it could have changed.
- For exact details from an older conversation, use recall_conversation when available.
- Keep answers focused on the user's actual request.

MEMORY AND KNOWLEDGE
- User-specific durable facts, preferences, setup details, decisions, and saved knowledge should be handled through the knowledge tools.
- Use knowledge_search when the user asks about their own setup, configuration, preferences, projects, decisions, or other stored facts.
- Use knowledge_remember only for information that is genuinely useful to retain.
- Do not claim to remember something merely because it appeared in an old conversation.
- Distinguish stored knowledge from live information and current conversation context.

TIME AND TEMPORAL GROUNDING
- The current date supplied by the system is authoritative.
- When the user asks for “today”, “now”, “latest”, “current”, “this year”, or similar time-sensitive information, resolve it against the actual current date/time.
- Do not infer current facts from old conversation history.

TOOL ROUTING
Use the appropriate tool when the request needs live data, computation, system access, or stored information.

- Sports scores, schedules, standings, rankings, player/team information: use the sports tools.
- Weather and weather alerts: use weather tools.
- Games and game information: use the game tools.
- Movies, TV shows, and people: use the relevant media/person tools.
- Current news: use news/search tools.
- General web research or a specific URL/PDF: use web search/fetch as appropriate. A specific URL should be fetched rather than guessed.
- User's stored project/setup information: use knowledge search.
- Exact prior conversation details: use recall_conversation.
- Precise mathematical or scientific computation: use the computation tool when appropriate.
- Code generation, testing, or execution: use the code tool when available.
- Calendar operations: use calendar tools.
- Reminders and scheduled tasks: use reminder/background-task tools.
- PC diagnostics, shell commands, system status, local files, screen/camera access, and Windows control: use the corresponding local tools when available.
- Plex requests: use Plex tools when available.
- Homelab/status/self-review/“what did you hear?” requests: use the corresponding tools.
- Use fresh tools whenever the answer depends on information that can change.

SYSTEM CONTROL AND SAFETY
- Read-only inspection and low-impact actions may be performed when clearly requested.
- Never perform destructive, security-sensitive, or consequential actions without explicit user confirmation when the tool requires confirmation.
- Actions such as killing processes, destructive file operations, deep diagnostics with side effects, system updates, administrative Plex changes, or other consequential operations require confirmation when indicated by the tool.
- Never silently remediate a problem when the user only asked for diagnosis.
- Never claim an action succeeded unless the tool confirms success.
- If a requested capability is unavailable in the current session, state that plainly and offer the closest available alternative.

FRESHNESS AND VERIFICATION
- Prefer authoritative tools and sources for current information.
- If the user asks for current/latest/live information, do not answer from stale memory.
- When tools return information, summarize the result accurately without inventing missing details.
- If sources disagree, state the disagreement instead of silently choosing a convenient answer.

VOICE-FIRST BEHAVIOR
- Keep ordinary spoken responses concise.
- Give the result first, then the minimum explanation needed.
- For multi-step tasks, report meaningful progress without narrating every internal operation.
- Do not read long technical output aloud unless the user asks for it.
- Use normal conversational language rather than robotic status messages.

GENERAL RULE
Be useful, accurate, grounded, and honest about capabilities. Use tools when they materially improve correctness. Do not fabricate tool results, memory, system state, or actions.
"""


# Appended to the system prompt only when a Plex MCP session is live. Kept
# separate so we don't promise tools that aren't there when Plex graceful-
# failed at startup.
_PLEX_PROMPT_ADDENDUM = """

Media library (Plex):
- You also have tools (prefixed by their MCP-server names) to query and control
  the user's personal Plex Media Server. Use them when the user asks about THEIR
  library, what's currently playing on their TVs/clients, or wants to control
  playback ("play X on the living room", "what's on my Plex right now?",
  "show me recently added films").
- Do NOT use Plex tools for general movie/TV info — for that, get_movie_tv_info
  (or web_search / web_fetch) is right. Plex tools are scoped to what the user owns.
- For voice replies, summarize Plex results briefly. Don't read full IDs, file
  paths, or long lists. If a search returns many matches, name a few and offer
  to narrow down."""


# Appended only when engineer mode is on. Unlocks structured depth without
# changing the calm-butler tone — the user is a Technical Support Engineer /
# SRE and values trade-off analysis over brevity in this mode. Calibration
# bullet at the end keeps the model from lecturing on simple questions.
_ENGINEER_PROMPT_ADDENDUM = """

Engineer mode (deeper technical reasoning):
- This conversation is in engineer mode. The user is a Technical Support Engineer / SRE
  who values depth, precision, and trade-off analysis over brevity. The calm, dryly-witty
  tone still holds — engineer mode is depth, not chattiness.
- You may write longer, structured responses with paragraphs, bullet points, ordered
  steps, or code blocks where they help comprehension. Visual structure is allowed here
  even though normal voice mode forbids it.
- Lead with the answer, then back it up with reasoning. Don't bury the lede.
- Explain WHY, not just WHAT. When recommending an approach, surface the trade-offs:
  what alternatives you considered, why this choice over those, what could go wrong.
- For diagnostic / troubleshooting questions: think like a senior engineer pair-partner.
  Form a hypothesis, suggest the next diagnostic step, propose multiple approaches with
  their trade-offs. Don't jump to a single fix when several are viable.
- Push back if the user is about to do something inefficient or risky — name the better
  approach. Be direct, not preachy. Match their technical level (assume senior).
- Connect new concepts to what they already know (Linux, networking, sysadmin,
  containers, cybersecurity) when it helps the explanation land.
- Calibrate to context. A quick fix doesn't need a lecture. New territory, non-obvious
  choices, "why is this happening" questions warrant more depth. When in doubt, lean
  toward more explanation than less.
- For genuinely simple questions ("what's 2+2", "what's the weather"), keep it brief
  even in engineer mode. Depth is a tool, not a default for everything."""


# Appended only when a live SSH client to the Plex laptop is available.
# Same gating discipline as _PLEX_PROMPT_ADDENDUM — never promise tools that
# aren't actually wired up.
_PLEX_LAPTOP_PROMPT_ADDENDUM = """

Remote Plex laptop (over SSH):

Diagnostics (read-only):
- plex_logs_tail — last N lines of Plex Media Server.log on the Plex laptop.
  Use for "what's Plex up to right now?", "is Plex okay?".
- plex_logs_search — regex search of the same log. Use for "any transcoder
  errors today?", "any 401s?", "any streaming failures recently?". The
  pattern is a .NET regex — alternation works ('error|fail|warning').
- plex_laptop_health — CPU/RAM/disk/network on the Plex laptop. Use for
  "how is the Plex box doing?", "is the Plex laptop drowning?", "what's the
  disk space on Plex?".
- For "is THIS PC vs the Plex laptop", remember pc_diagnostics is for THIS
  PC and plex_laptop_health is for the remote one.
- Voice summaries: when reading log lines aloud, paraphrase the gist
  ("a couple of transcoder warnings around 8 PM, otherwise clean") rather
  than reading raw timestamps and stack traces.

Actions (destructive, all confirmation-gated):
- plex_action — restart Plex Media Server, refresh a library, or empty
  Plex's trash for a library. ALL THREE require explicit user confirmation.
- For ANY plex_action call: ask the user to confirm in plain language
  first ("Confirm: restart Plex on the laptop?" / "Confirm: refresh the
  Movies library?"), wait for an explicit yes, THEN call plex_action with
  confirmed=true. The tool itself enforces this — if you call without
  confirmed=true it returns a confirmation-required notice rather than
  firing. Same pattern as kill_process on the local PC.
- For refresh_library and empty_trash you need a library_id. If you don't
  know it, call library_list (Plex MCP) first to enumerate sections.
- If the user asks for an action we don't support yet (rotate logs,
  restart the laptop itself, clear OS-level caches), say so plainly —
  diagnose first, propose remediations in words, but don't pretend to
  have a tool you don't."""


# M48.2a — appended only for a RESTRICTED (phone/web remote) session. The
# server-side tool filter is the real boundary (a phone turn never even
# receives the blocked tools); this addendum just keeps Claude from
# *promising* a tool it no longer has, so it degrades gracefully instead
# of "I'll run a diagnostic…" then silently failing. Defense in depth, not
# the enforcement itself ([[feedback-diag-vs-action-split]]).
_REMOTE_RESTRICTED_ADDENDUM = """

Remote session (limited capability):
- You are answering from the user's phone / web console, not the PC.
- For safety this session CANNOT run system control, the investigative
  shell, the deep diagnostics collector, Plex admin actions, local file
  reads, or screen/camera capture — those tools are simply not available
  here (this is enforced, not advisory).
- If asked for one, say plainly it's not available from the remote console
  and offer what you CAN do: answer and look things up, check live
  read-only PC/Plex health, search the user's knowledge base, and they can
  arm/disarm security with the console's own buttons. Don't pretend, don't
  apologize at length — just redirect."""


# M71 — Discord variant of the restricted addendum. Discord is the ONE remote
# origin with camera_snapshot clawed back (see _RESTRICTED_ALLOW_BY_ORIGIN), so
# its prompt must NOT tell Claude the camera is off-limits — it tells Claude the
# camera IS available and the photo is shared into the channel. Everything else
# stays denied, identical to the base addendum.
_REMOTE_RESTRICTED_DISCORD_ADDENDUM = """

Remote session (Discord — limited capability):
- You are answering from a private Discord channel, not at the PC.
- You CAN look through the webcam: call camera_snapshot when the user asks you
  to check on the home, see what's going on, or look at something. The photo
  you capture is posted into this Discord channel alongside your reply, so
  describe what you see plainly and usefully (who or what is present, lights,
  anything notable).
- For safety this session still CANNOT run system control, the investigative
  shell, the deep diagnostics collector, Plex admin actions, local file reads,
  screen capture, code execution, or self-update — those tools are simply not
  available here (enforced, not advisory).
- If asked for one of those, say plainly it's not available from Discord and
  offer what you CAN do. Don't pretend, don't apologize at length."""


# Anthropic's server-side web search tool. Server-side = Anthropic runs it,
# we just declare it.
#
# We deliberately use _20250305 (the GA version) rather than _20260209 — but
# NOT for the reason originally recorded here. That reason is now falsified,
# and the note is kept accurate rather than tidy.
#
# ORIGINAL (2026-06-02, commit 02e5b91): _20260209's "dynamic filtering" runs
# Anthropic's code-execution sandbox to filter large result sets, which threads
# a `container_id` through the rest of the turn. A multi-search query that also
# called a client-side tool 400'd on the re-stream: "container_id is required
# when there are pending tool uses generated by code execution with tools."
# The conclusion drawn was that the loop's container handling was unreliable,
# so the version was downgraded to remove the container — and the error class —
# entirely.
#
# RE-TESTED 2026-07-28 (scripts/web_search_version_probe.py): the 400 DOES NOT
# REPRODUCE. Eight runs on _20260209, shaped to interleave a server-side search
# with client-side tools in one turn — including a four-tool turn (web_search +
# get_weather + get_movie_tv_info + pc_diagnostics) — completed cleanly, 0/8
# container errors. The container capture below threads the id correctly.
#
# SO WHY IS THE PIN STILL HERE? Because the upgrade buys nothing measurable.
# Same run: input tokens identical (128 vs 128), output tokens identical
# (452 vs 453), median latency slightly WORSE (11.8s vs 10.7s), and the
# _20260209 tool schema costs ~2.9k MORE cached-prefix tokens per request
# (31,685 vs 28,772). Dynamic filtering's win is filtering search results
# before they reach the context window; on Jarvis's short voice queries there
# is not enough result volume for that to pay for itself.
#
# Caveat on that second finding: the telemetry accumulates input/output/cache
# tokens only, so server-tool result ingestion is not isolated — "no measurable
# benefit" is a weaker claim than "no 400", which is unambiguous.
#
# NET: the blocker is gone, so switching is now a low-risk change whenever
# _20250305 is retired or a workload appears with enough search volume to make
# filtering pay. It is simply not worth doing today. Re-run the probe to decide.
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}


# Anthropic's server-side web fetch tool. Pulls the full contents of a
# specific URL (HTML or PDF) and returns the parsed text. Pairs naturally
# with web_search (search to find a URL → fetch to read it in depth).
#
# We deliberately use _20250910 (the older version) rather than _20260209.
# The newer version's "dynamic filtering" feature runs Anthropic's code
# execution sandbox to filter big pages, which threads a `container_id`
# through the conversation — and our agentic loop in stream_response() does
# not track or forward that container_id. Mismatch surfaced in M19 testing
# as: "container_id is required when there are pending tool uses generated
# by code execution with tools." For voice queries (typical pages ≤ a few
# KB), dynamic filtering's token-saving benefit is small; for the rare
# multi-MB doc, the agentic loop's MAX_LOOP_ITERATIONS still bounds cost.
# Revisit if we ever add a "summarize this 200-page document" workflow.
WEB_FETCH_TOOL = {
    "type": "web_fetch_20250910",
    "name": "web_fetch",
}


# Cap on agentic-loop iterations. Most voice queries finish in 1-2 tool calls,
# but research-style asks ("do a deep dive on X, then set a reminder") legit-
# imately chain more — search → search → web_fetch → set_reminder → cancel →
# re-set. 5 was too tight (a 2026-06-02 deep-dive query exhausted it with no
# final answer); 8 gives that headroom while still bounding a runaway turn.
# If we hit it we log AND yield a graceful fallback (see the while-else) so the
# user never gets dead silence — the real failure mode the old cap exposed.
_MAX_LOOP_ITERATIONS = 8

# Spoken when the agentic loop is cut off by the iteration cap mid-work, so a
# capped turn degrades to a sentence instead of silence.
_LOOP_CAP_FALLBACK = (
    " I'm sorry, sir — that turned into more steps than I can take in one go. "
    "Here's where I got to; ask me to continue and I'll pick it up."
)

# Token budgets. Default mode is voice-shaped — short replies, thinking OFF
# (Sonnet 5 runs ADAPTIVE thinking by default when `thinking` is omitted, which
# would add latency and eat into max_tokens — we disable it explicitly below).
# Engineer mode unlocks ADAPTIVE thinking + a generous output budget so the
# model can both reason and write the longer structured answer it produced.
_DEFAULT_MAX_TOKENS = 1024
_ENGINEER_MAX_TOKENS = 8192   # room for adaptive reasoning + a structured reply

# Effort — how much the model spends thinking AND acting on a turn.
#
# Sonnet 5 defaults to `high` when output_config.effort is unset, which is the
# wrong default for a voice assistant: "what's the weather" was being answered
# at the same reasoning depth as a multi-step diagnostic. Effort is the single
# biggest latency/cost lever available without touching the prompt.
#
# The values below were MEASURED, not guessed — see scripts/effort_probe.py and
# the milestone entry. The measurement mattered because effort cuts both ways
# here: Sonnet 5 with `thinking` disabled is already less inclined to reach for
# tools, and lowering effort pushes the same direction. On a 40-tool assistant
# whose usefulness *is* its tool routing, a latency win that quietly costs tool
# recall is not a win.
_VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")


def _env_effort(name: str, default: str) -> str:
    """Read an effort override, rejecting junk rather than 400-ing at runtime.

    An invalid effort is a hard API error on every turn — i.e. a typo in .env
    would take the assistant completely off the air. Fail soft to the default
    and log, per the project's contract.
    """
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw not in _VALID_EFFORT:
        print(f"[llm] ignoring {name}={raw!r} - expected one of "
              f"{', '.join(_VALID_EFFORT)}; using {default}", file=sys.stderr)
        return default
    return raw


_VOICE_EFFORT = _env_effort("JARVIS_VOICE_EFFORT", "medium")
_ENGINEER_EFFORT = _env_effort("JARVIS_ENGINEER_EFFORT", "high")

# Models that accept output_config.effort. Haiku 4.5 REJECTS it with a 400, and
# the background jobs (summariser, prediction miner) run on Haiku — so this is
# a real guard, not defensive decoration.
def _supports_effort(model: str) -> bool:
    m = (model or "").lower()
    if "haiku" in m:
        return False
    return any(k in m for k in ("sonnet-5", "opus-5", "opus-4-8", "opus-4-7",
                                "sonnet-4-6", "opus-4-6", "fable"))


@dataclass
class TelemetryRecord:
    """Per-turn structured telemetry. Same data the existing stderr log line
    carries, exposed as a callable contract so the UI can surface it.

    Latency is wall-clock LLM-and-tools time only — TTS playback is excluded.
    For an SRE skimming the console, "how long did Jarvis spend thinking" is
    the more useful number than "how long did the whole turn take".
    """
    elapsed_sec: float
    iterations: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_create_tokens: int
    tools_used: list[str] = field(default_factory=list)
    paused: bool = False
    thinking_enabled: bool = False  # M26: extended thinking was active for this turn

    @property
    def total_tokens(self) -> int:
        # cache_read_tokens are billed at a discount but still count as
        # "input" semantically; include for the SRE-grade total.
        return self.input_tokens + self.output_tokens + self.cache_read_tokens


def _format_today() -> str:
    """Cross-platform 'Sunday, May 4, 2026' (no leading zero on day)."""
    now = datetime.now()
    return now.strftime("%A, %B ") + str(now.day) + now.strftime(", %Y")


def _format_now_time() -> str:
    """Return the actual Windows laptop local time when running under WSL."""
    try:
        import subprocess
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-Date).ToString('hh:mm tt')"
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        windows_time = result.stdout.strip()
        if windows_time:
            return windows_time.lstrip("0")
    except Exception:
        pass

    return datetime.now().strftime("%I:%M %p").lstrip("0")


# 2026-07-02 QA — process-wide Anthropic client. A fresh Anthropic() per turn
# built a new httpx connection pool each time: a fresh TLS handshake on the
# hot path EVERY turn (compounded per relayed utterance in interpreter mode),
# with old sockets reclaimed only by GC. The SDK client is thread-safe; one
# instance serves every caller. Keyed so a changed api_key (tests) rebuilds.
_client_lock = threading.Lock()
_shared_client: "OpenRouterClient | None" = None
_shared_client_key: str | None = None


def _get_client(api_key: str) -> "OpenRouterClient":
    global _shared_client, _shared_client_key
    with _client_lock:
        if _shared_client is None or _shared_client_key != api_key:
    	    _shared_client = OpenRouterClient(api_key)
    	    _shared_client_key = api_key
        return _shared_client


def build_system_prompt(
    summaries: list[SummaryRecord] | None = None,
    plex_available: bool = False,
    plex_laptop_available: bool = False,
    engineer_mode: bool = False,
    remote_restricted: bool = False,
    restricted_origin: str = "",
) -> str:
    """Compose the system prompt with the current date and optional memory.

    The current date gives Claude a temporal anchor for reasoning about what's
    stale vs. current. We use date precision (not time) so the cache breakpoint
    invalidates at most once per day, not per turn.

    Optional addenda are gated on actual tool availability so a graceful-fail
    startup doesn't leave Claude believing in tools it can't call:
    - `plex_available` (M21): advertise the Plex MCP media tools
    - `plex_laptop_available` (M24): advertise the remote-laptop SSH tools
    - `engineer_mode` (M26): unlock structured-depth replies and connect to
      the user's technical expertise. Toggleable per-turn from the tray.

    Note: engineer addendum is placed BEFORE the memory block so it stays in
    the cacheable prefix. Memory varies per turn; tool addenda + engineer
    addendum stay stable across turns within a session, which matters for
    prompt-cache hit rate.
    """
    base = f"{JARVIS_SYSTEM_PROMPT}\n\nToday is {_format_today()}."
    if plex_available:
        base += _PLEX_PROMPT_ADDENDUM
    if plex_laptop_available:
        base += _PLEX_LAPTOP_PROMPT_ADDENDUM
    if engineer_mode:
        base += _ENGINEER_PROMPT_ADDENDUM
    if remote_restricted:
        # Stable per session-origin (a turn is restricted or not for its
        # whole life), so it stays in the cacheable prefix like the other
        # addenda — no prompt-cache churn. M71: an origin with camera clawed
        # back (Discord) gets the variant that tells Claude the webcam IS
        # available, instead of the base "no camera" wording.
        if "camera_snapshot" in _RESTRICTED_ALLOW_BY_ORIGIN.get(
            restricted_origin, frozenset()
        ):
            base += _REMOTE_RESTRICTED_DISCORD_ADDENDUM
        else:
            base += _REMOTE_RESTRICTED_ADDENDUM
    if not summaries:
        return base
    memory_block = format_summaries_for_prompt(summaries)
    return (
        base
        + "\n\nRecent conversations (for your memory — only mention these if relevant):\n"
        + memory_block
    )


def _speaker_context_block(name: str, lang: "str | None") -> str:
    """M80 — a tiny per-turn note telling Claude WHO it is speaking with, from
    the M69 voice identification. Deliberately kept OUT of the cached system
    prefix (the caller appends it as a second, un-cache_controlled system block)
    so a speaker handoff between turns doesn't invalidate the whole prompt cache.

    Identity (the name) is the load-bearing half. The language line is added
    ONLY for a non-English speaker, and is phrased as a SOFT tie-breaker
    subordinate to the per-turn detected language — Whisper still owns the
    actual reply/TTS language; this just nudges a short/ambiguous utterance.
    Returns '' for an empty name so the caller can skip the block entirely."""
    name = (name or "").strip()
    if not name:
        return ""
    block = (
        f"You are currently speaking with {name}, identified by voice. "
        "Address them by name when it feels natural — sparingly, not every line. "
        "If another enrolled person was speaking a moment ago, this is a handoff: "
        "respond to {name} now."
    ).replace("{name}", name)
    if lang and lang != "en":
        lang_word = {"es": "Spanish"}.get(lang, lang)
        block += (
            f" {name} usually speaks {lang_word}; if a short or ambiguous "
            f"utterance's language is unclear, lean toward {lang_word}."
        )
    return block


class _ClientTool(NamedTuple):
    """A built-in client-side tool Jarvis runs locally (vs. the server-side
    web_search / web_fetch that Anthropic executes)."""
    # Runs the tool. Returns text for Claude, OR a list of content blocks
    # (e.g. camera_snapshot returns an image block + caption). Never raises.
    execute: Callable[[dict], "str | list[dict]"]
    log_label: str                   # short marker for the stderr telemetry line ("sysctl", "camera", ...)


# Single source of truth for the built-in client-side tools: API name →
# (executor, telemetry label). Drives _execute_client_tool's dispatch AND the
# per-turn telemetry — the set of fired names plus this map produce both the
# stderr log markers and the console chip. Adding a tool means: import its
# executor + schema, add one entry here, and add the schema to the `tools`
# list in stream_response. Insertion order is preserved and defines telemetry
# order, so keep it sensible (the order the SRE wants to read it in).
_CLIENT_TOOLS: dict[str, _ClientTool] = {
    "get_sports_info":              _ClientTool(execute_sports_tool, "sports_tool"),
    "get_weather":                  _ClientTool(execute_weather_tool, "weather_tool"),
    "get_weather_alerts":           _ClientTool(execute_weather_alerts_tool, "weather_alerts"),
    "get_game_info":                _ClientTool(execute_games_tool, "games_tool"),
    "get_game_length":              _ClientTool(execute_game_length_tool, "game_length"),
    "get_movie_tv_info":            _ClientTool(execute_tmdb_tool, "tmdb_tool"),
    "get_person_info":              _ClientTool(execute_person_info_tool, "tmdb_person"),
    "get_news":                     _ClientTool(execute_news_tool, "news"),
    "wolfram_query":                _ClientTool(execute_wolfram_tool, "wolfram"),
    "run_code":                     _ClientTool(execute_code_tool, "code"),
    "knowledge_search":             _ClientTool(execute_knowledge_tool, "knowledge"),
    "knowledge_remember":           _ClientTool(execute_knowledge_remember, "knowledge_write"),
    "recall_conversation":          _ClientTool(execute_recall_tool, "recall"),
    "get_calendar_events":          _ClientTool(execute_calendar_tool, "calendar"),
    "set_reminder":                 _ClientTool(execute_set_reminder, "reminder_set"),
    "list_reminders":               _ClientTool(execute_list_reminders, "reminder_list"),
    "cancel_reminder":              _ClientTool(execute_cancel_reminder, "reminder_cancel"),
    "get_briefing":                 _ClientTool(execute_briefing_tool, "briefing"),
    "get_good_night":               _ClientTool(execute_good_night_tool, "good_night"),
    "update_jarvis":                _ClientTool(execute_update_jarvis, "self_update"),
    "homelab_status":               _ClientTool(execute_homelab_status, "homelab"),
    "status_report":                _ClientTool(execute_status_report, "self_status"),
    "self_review":                  _ClientTool(execute_self_review, "self_review"),
    "what_did_you_hear":            _ClientTool(execute_what_did_you_hear, "acoustic_recall"),
    "start_background_task":        _ClientTool(execute_start_background_task, "bgtask_start"),
    "list_background_tasks":        _ClientTool(execute_list_background_tasks, "bgtask_list"),
    "cancel_background_task":       _ClientTool(execute_cancel_background_task, "bgtask_cancel"),
    "pc_diagnostics":               _ClientTool(execute_pc_diagnostics_tool, "diagnostics"),
    "pc_shell":                     _ClientTool(execute_pc_shell, "shell"),
    "system_control":               _ClientTool(execute_system_control_tool, "sysctl"),
    "read_local_file":              _ClientTool(execute_read_local_file, "read_file"),
    "run_pc_diagnostics_collector": _ClientTool(execute_run_pc_diagnostics_collector, "diag_collector"),
    "camera_snapshot":              _ClientTool(execute_camera_snapshot, "camera"),
    "screen_snapshot":              _ClientTool(execute_screen_snapshot, "screen"),
}

# M48.2a — the per-session capability boundary. A RESTRICTED session
# (phone/web remote, keyed off the turn `origin` in main.py) gets every
# tool EXCEPT these. Two classes, both per the locked spec:
#   BLOCK (mutating / dangerous): can change the PC or Plex.
#   EXCLUDE (read-only but sensitive on a loseable phone): can read PC
#     files or open the screen/webcam remotely.
# Single source of truth — applied at BOTH gates (the tool-list filter in
# stream_response AND the _execute_client_tool dispatch), so it is
# enforced server-side, never prompt-only ([[feedback-diag-vs-action-split]],
# [[feedback-jarvis-least-privilege]]). Match is by tool NAME, so it also
# covers plex_action (in _PLEX_LAPTOP_DISPATCH) and any future name.
_RESTRICTED_DENY: frozenset[str] = frozenset({
    # BLOCK — mutating / dangerous
    "system_control", "pc_shell", "run_pc_diagnostics_collector", "plex_action",
    # BLOCK — arbitrary code execution (M50). Sandboxed on the PC, but a
    # phone-origin turn must never be able to make Jarvis run code at all —
    # the principle holds regardless of the isolation.
    "run_code",
    # BLOCK — self-update (M64). The phone PWA must never be able to pull
    # code into the user's machine + force a restart; that's the M48.2a
    # least-privilege boundary at the most extreme (the update could be
    # anything — a phone-driven self-update IS arbitrary code execution
    # by another name).
    "update_jarvis",
    # BLOCK — dispatching a long-horizon agent is unbounded API spend and
    # runs unobserved. A phone- or Discord-origin turn must not be able to
    # start one; listing/cancelling go with it so the surface is coherent
    # (you cannot cancel what you could not start).
    "start_background_task", "list_background_tasks", "cancel_background_task",
    # EXCLUDE — read-only but sensitive from a remote
    "read_local_file", "screen_snapshot", "camera_snapshot",
})


# M71 — per-origin claw-back: specific otherwise-denied tools selectively
# re-allowed for ONE restricted origin, deny-by-default everywhere else.
# Discord gets camera_snapshot back so the household can ask Jarvis to look
# through the webcam while away (the captured photo is relayed into the
# channel; see main.py's reply_image sink). Least-privilege is preserved —
# this is a SURGICAL exception, not a boundary flip: every other restricted
# tool (system/shell/file/screen/code/self-update) stays denied for Discord,
# and the phone origins get nothing back. New exceptions go here, one origin
# at a time, deliberately. [[feedback-jarvis-least-privilege]]
_RESTRICTED_ALLOW_BY_ORIGIN: dict[str, frozenset[str]] = {
    "discord": frozenset({"camera_snapshot"}),
}


def _effective_deny(origin: str) -> frozenset[str]:
    """The deny-set actually applied to a restricted `origin`: the base
    _RESTRICTED_DENY minus any tools clawed back for that origin. An unknown
    or empty origin gets the full deny (safe default)."""
    return _RESTRICTED_DENY - _RESTRICTED_ALLOW_BY_ORIGIN.get(origin, frozenset())


# Server-side tools — Anthropic runs these; we only declare them (in
# stream_response's `tools` list) and record that they fired. Ordered:
# defines telemetry order. Each name doubles as its own stderr label.
_SERVER_TOOLS: tuple[str, ...] = ("web_search", "web_fetch")

# SSH-backed tools (M24 read-only + M27 actions). Their executors take the
# PlexLaptopClient as a first arg, so they're dispatched separately from
# _CLIENT_TOOLS; telemetry groups them under one `plex_laptop_tools=` marker
# since they're a family. (Plex MCP tools, M21, are dynamic — names come from
# the server at runtime — so they're only known via plex_client.tool_names.)
_PLEX_LAPTOP_DISPATCH = {
    "plex_logs_tail": execute_plex_logs_tail,
    "plex_logs_search": execute_plex_logs_search,
    "plex_laptop_health": execute_plex_laptop_health,
    "plex_action": execute_plex_action,
}


def _execute_client_tool(
    name: str,
    tool_input: dict,
    plex_client: PlexMCPClient | None = None,
    plex_laptop_client: PlexLaptopClient | None = None,
    restricted: bool = False,
    origin: str = "",
) -> str | list[dict]:
    """Dispatch a client-side tool call. Returns text (or, for camera_snapshot,
    a list of content blocks) for Claude to consume. Errors become readable
    strings — never raises.

    `restricted` (M48.2a): second gate of the capability boundary. The
    tool-list filter in stream_response already prevents a restricted
    session from being OFFERED a denied tool; this refuses to EXECUTE one
    even if a denied name reaches here anyway (model quirk, prompt
    injection from the phone, a future code path). Server-side, not
    prompt-only — the [[feedback-diag-vs-action-split]] rule. M71: the deny-set
    is per-origin (Discord has camera clawed back), so an origin's allowed
    exception executes while everything else still refuses."""
    if restricted and name in _effective_deny(origin):
        print(f"[llm] restricted session ({origin or 'remote'}) denied tool '{name}'",
              file=sys.stderr)
        return (
            f"The '{name}' tool isn't available from the remote console "
            f"(safety: this session can't run system, shell, file, or "
            f"screen/camera tools)."
        )
    try:
        client_tool = _CLIENT_TOOLS.get(name)
        if client_tool is not None:
            return client_tool.execute(tool_input)
        if plex_laptop_client is not None and name in _PLEX_LAPTOP_DISPATCH:
            return _PLEX_LAPTOP_DISPATCH[name](plex_laptop_client, tool_input)
        if plex_client is not None and name in plex_client.tool_names:
            return plex_client.call_tool(name, tool_input)
        return f"Unknown tool: {name}"
    except Exception as exc:
        # Defensive — existing tool executors swallow their own errors,
        # but a future tool might not. Don't let a tool exception kill the turn.
        print(f"[llm] tool '{name}' raised: {exc}", file=sys.stderr)
        return f"Tool error: {exc}"


def stream_translation(
    *, api_key: str, text: str, target_lang: str,
    model: str = "claude-sonnet-5",
) -> Iterator[str]:
    """M87 — interpreter mode: stream a faithful translation of `text` into
    `target_lang`. Deliberately MINIMAL and isolated from stream_response's
    agentic machinery: a tiny translate-only system prompt, no tools, no
    conversation history, no persona, no caching. That keeps interpreter
    latency low and means a translation can never trip the full tool loop or
    leak the Jarvis persona into the relay. Yields text chunks so the caller
    can pipe them straight into speak_streaming (TTS starts on the first
    sentence). Raises on a transport error — the caller (TurnRunner.interpret)
    swallows it so the interpreter loop keeps going."""
    from src.interpreter import build_translation_prompt  # noqa: PLC0415

    client = _get_client(api_key)
    with client.messages.stream(
        model=model,
        max_tokens=1024,  # ample for one spoken utterance
        # Faithful relay, not reasoning — and Sonnet 5 defaults thinking ON when
        # omitted. Disable it: interpreter latency stays low, no thinking tokens.
        thinking={"type": "disabled"},
        system=build_translation_prompt(target_lang),
        messages=[{"role": "user", "content": text}],
    ) as stream:
        for chunk in stream.text_stream:
            yield chunk


def stream_response(
    api_key: str,
    messages: list[dict],
    model: str = "claude-sonnet-5",
    summaries: list[SummaryRecord] | None = None,
    plex_client: PlexMCPClient | None = None,
    plex_laptop_client: PlexLaptopClient | None = None,
    on_complete: Callable[[TelemetryRecord], None] | None = None,
    on_image_captured: Callable[[bytes, str, str], None] | None = None,
    engineer_mode: bool = False,
    restricted: bool = False,
    origin: str = "",
    speaker_name: "str | None" = None,
    speaker_lang: "str | None" = None,
    vocal_cue: "str | None" = None,
    interrupt_event: threading.Event | None = None,
    effort: "str | None" = None,
) -> Iterator[str]:
    """Stream Claude's response, handling client-side tool use transparently.

    Yields text chunks suitable for direct TTS feeding. The caller sees a single
    continuous stream of text even when one or more tool calls happen in the
    middle — each tool round-trip is invisible from the caller's perspective.

    `messages` must be the full alternating history ending with a user message.
    `summaries` (optional) prepends recent-session context to the system prompt.
    `plex_client` (optional, M21) — if a live Plex MCP session is provided,
    its tools are surfaced to Claude alongside the built-in tools.
    `plex_laptop_client` (optional, M24) — if SSH-reachable, the three remote
    diagnostic tools (logs_tail, logs_search, laptop_health) get registered.
    `on_complete` (optional) — called once at the end with a TelemetryRecord
    for UI consumption. The same data is also stderr-logged in the existing
    one-line format. Wrapped in try/except so a UI bug can't poison the turn.
    `on_image_captured` (optional) — fired whenever a client-side tool returns
    a tool_result containing an image content block (currently camera_snapshot
    + screen_snapshot). Receives (image_bytes, media_type, tool_name) so the
    UI can render an inline thumbnail of what Jarvis just "saw". Wrapped in
    try/except — a UI bug must not break the agentic loop mid-turn.
    `engineer_mode` (optional, M26) — when True, append the engineer-mode
    addendum to the system prompt and enable Anthropic's extended thinking
    feature with a 5k-token reasoning budget. Captured per-turn from
    `ui.is_engineer_mode()`; mid-turn toggles apply to the next turn.
    `interrupt_event` (optional, M52 barge-in) — polled while streaming. When
    set, the generator returns immediately, which exits the `with
    client.messages.stream()` block and closes the HTTP stream — otherwise
    Claude would keep generating server-side until the generator is GC'd.
    Two poll points: mid-text-stream (the common case — the user cuts in
    while a reply is being spoken) and at the top of each agentic-loop
    iteration (so a barge-in during a tool call aborts before another LLM
    round-trip). The mid-stream return skips the final on_complete telemetry
    callback — an accepted, minor cost on an interrupted turn.
    """
    started_at = time.monotonic()
    client = _get_client(api_key)
    system_text = build_system_prompt(
        summaries,
        plex_available=plex_client is not None,
        plex_laptop_available=plex_laptop_client is not None,
        engineer_mode=engineer_mode,
        remote_restricted=restricted,
        restricted_origin=origin,
    )
    system_param = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # M80 + M85 — per-turn context (speaker identity + vocal-delivery cue),
    # appended as a SECOND system block WITHOUT cache_control: the large block
    # above keeps its cache breakpoint and stays a stable cached prefix across
    # turns, while this small note varies per turn (a speaker handoff / a tone
    # cue) and is re-read each turn — a few fresh tokens, never a full-prompt
    # cache miss. Both come from the PC-voice mic clip, so remote origins set
    # neither and the block is simply absent for them. Combined so a turn adds
    # at most ONE extra block.
    _per_turn: list[str] = []
    if speaker_name:
        _blk = _speaker_context_block(speaker_name, speaker_lang)
        if _blk:
            _per_turn.append(_blk)
    if vocal_cue:
        _per_turn.append(
            "Vocal delivery this turn — how he SOUNDED, not stated in his words: "
            f"{vocal_cue}. Use it ONLY to calibrate your manner (see Tone awareness)."
        )
    # 2026-07-02 QA (the "12:00 AM" clock bug): the cached prefix anchors the
    # DATE only — by design, so the cache invalidates once per day, not per
    # turn. The current TIME rides this per-turn UNcached block instead:
    # "what time is it" now answers from ground truth, at a few fresh tokens
    # per turn and zero cache-miss cost.
    _per_turn.append(f"Current local time: {_format_now_time()}.")
    if _per_turn:
        system_param.append({"type": "text", "text": "\n\n".join(_per_turn)})
    tools = [
        WEB_SEARCH_TOOL, WEB_FETCH_TOOL,
        SPORTS_TOOL, WEATHER_TOOL, GET_WEATHER_ALERTS_TOOL,
        GAMES_TOOL, GAME_LENGTH_TOOL,
        TMDB_TOOL, GET_PERSON_INFO_TOOL,
        NEWS_TOOL, WOLFRAM_TOOL, CODE_EXEC_TOOL,
        KNOWLEDGE_SEARCH_TOOL, KNOWLEDGE_REMEMBER_TOOL, RECALL_CONVERSATION_TOOL,
        GET_CALENDAR_TOOL,
        SET_REMINDER_TOOL, LIST_REMINDERS_TOOL, CANCEL_REMINDER_TOOL,
        BRIEFING_TOOL, GOOD_NIGHT_TOOL, HOMELAB_STATUS_TOOL, STATUS_REPORT_TOOL,
        SELF_REVIEW_TOOL,
        WHAT_DID_YOU_HEAR_TOOL,
        START_BACKGROUND_TASK_TOOL, LIST_BACKGROUND_TASKS_TOOL,
        CANCEL_BACKGROUND_TASK_TOOL,
        UPDATE_JARVIS_TOOL,
        PC_DIAGNOSTICS_TOOL, PC_SHELL_TOOL, SYSTEM_CONTROL_TOOL,
        READ_LOCAL_FILE_TOOL, RUN_PC_DIAGNOSTICS_COLLECTOR_TOOL,
        CAMERA_SNAPSHOT_TOOL, SCREEN_SNAPSHOT_TOOL,
    ]
    if plex_laptop_client is not None:
        tools.extend([
            PLEX_LOGS_TAIL_TOOL,
            PLEX_LOGS_SEARCH_TOOL,
            PLEX_LAPTOP_HEALTH_TOOL,
            PLEX_ACTION_TOOL,
        ])
    if plex_client is not None:
        tools.extend(plex_client.tools)

    # M48.2a — the capability boundary's PRIMARY gate: a restricted
    # (phone/web remote) turn is never even OFFERED a denied tool. Filter
    # by schema name AFTER all extends so it covers built-ins, the SSH
    # family (plex_action), and dynamic Plex MCP names uniformly. The
    # _execute_client_tool deny-check is the defense-in-depth second gate.
    if restricted:
        denied = _effective_deny(origin)
        tools = [t for t in tools if t.get("name") not in denied]

    # Mutable working copy — we append assistant + tool_result turns as the
    # agentic loop runs. The caller's `messages` list is left untouched.
    working = list(messages)

    # Telemetry accumulators across all agentic-loop iterations. We track the
    # set of fired tool names per category; _CLIENT_TOOLS / _SERVER_TOOLS map
    # those to stderr labels + the console chip below. (The categories are
    # real — server-side and SSH-backed/MCP tools are formatted differently
    # in the log: individual `x=yes` markers vs. one grouped `x_tools=a,b`.)
    fired_server: set[str] = set()        # subset of _SERVER_TOOLS
    fired_client: set[str] = set()        # subset of _CLIENT_TOOLS keys
    fired_plex_laptop: set[str] = set()   # subset of _PLEX_LAPTOP_DISPATCH keys
    fired_plex_mcp: set[str] = set()      # dynamic names from the Plex MCP server
    paused = False
    total_input = total_output = total_cache_read = total_cache_create = 0
    iterations = 0

    # Build per-turn stream kwargs once; reuse across agentic-loop iterations.
    # Thinking is explicit on Sonnet 5: engineer mode gets ADAPTIVE thinking
    # (the model self-budgets reasoning depth — the old enabled+budget_tokens
    # shape is rejected with a 400), and voice mode gets thinking DISABLED
    # (Sonnet 5 would otherwise run adaptive by default and add latency +
    # consume max_tokens). Adaptive thinking blocks are preserved in the
    # assistant turn we append below — required when thinking + tool_use combine.
    # Simple time requests already have the ground-truth time in system context.
    # Do not send the entire AYRA tool catalog for these requests.
    last_user_text = ""
    for _msg in reversed(working):
        if _msg.get("role") == "user":
            _content = _msg.get("content", "")
            last_user_text = _content if isinstance(_content, str) else str(_content)
            break

    import re as _re
    _is_time_request = bool(_re.search(
        r"\b(what|tell|give|show)\b.*\b(time|clock)\b|\btime\s+(is|now)\b",
        last_user_text.lower()
    ))

    request_tools = [] if _is_time_request else tools

    stream_kwargs: dict = {
        "model": model,
        "max_tokens": _ENGINEER_MAX_TOKENS if engineer_mode else _DEFAULT_MAX_TOKENS,
        "system": system_param,
        "messages": working,
        "tools": request_tools,
        "thinking": {"type": "adaptive"} if engineer_mode else {"type": "disabled"},
    }

    # Effort. Unset, Sonnet 5 runs `high` — full reasoning depth on "what's the
    # weather". `effort` overrides the per-mode default (the probe harness sweeps
    # it); None keeps the default. Guarded by model, since Haiku 400s on it.
    chosen_effort = effort or (_ENGINEER_EFFORT if engineer_mode else _VOICE_EFFORT)
    if chosen_effort and _supports_effort(model):
        stream_kwargs["output_config"] = {"effort": chosen_effort}

    while iterations < _MAX_LOOP_ITERATIONS:
        iterations += 1

        # M52: barged in during a tool call — abort before the next LLM
        # round-trip. `break` (vs. the mid-stream `return` below) falls
        # through to the telemetry block, so an interrupt at this boundary
        # still records a chip.
        if interrupt_event is not None and interrupt_event.is_set():
            break

        # Refresh messages each iteration since the agentic loop appends to
        # `working`. Other kwargs are stable across iterations.
        stream_kwargs["messages"] = working
        with client.messages.stream(**stream_kwargs) as stream:
            for text in stream.text_stream:
                # M52: the user barged in mid-reply. Returning here exits the
                # `with` block, which closes the HTTP stream so Claude stops
                # generating server-side.
                if interrupt_event is not None and interrupt_event.is_set():
                    return
                yield text
            final = stream.get_final_message()

        # If this turn used the server-side code-execution container (newer
        # Claude can run code as part of agentic work even though we don't
        # declare that tool), the API requires its id to be echoed on every
        # subsequent request in the same exchange. Without it, the re-stream
        # after a client tool_use 400s: "container_id is required when there
        # are pending tool uses generated by code execution". Capture it and
        # thread it into the rest of THIS call's agentic loop. Conditional —
        # untouched on the common path, so zero change when no container is
        # used. (stream_kwargs is per-call; it never leaks across turns.)
        container = getattr(final, "container", None)
        if container is not None:
            stream_kwargs["container"] = container.id

        # Accumulate token usage. cache_* fields are None on uncached turns.
        u = final.usage
        total_input += u.input_tokens
        total_output += u.output_tokens
        total_cache_read += u.cache_read_input_tokens or 0
        total_cache_create += u.cache_creation_input_tokens or 0

        # Track server-side tool use (web_search, web_fetch) for telemetry
        # only — the SDK has already executed them and the text stream above
        # included Claude's post-tool text.
        for block in final.content:
            if getattr(block, "type", None) == "server_tool_use":
                sname = getattr(block, "name", None)
                if sname in _SERVER_TOOLS:
                    fired_server.add(sname)

        # Server-side loop hit its 10-iteration cap. Rare in voice; we log
        # and bail rather than try to manually resume.
        if final.stop_reason == "pause_turn":
            paused = True
            break

        # Normal end of turn — Claude is done responding.
        if final.stop_reason != "tool_use":
            break

        # Client-side tool use. Append the assistant turn (full content,
        # including any text + tool_use blocks Claude emitted) so the SDK
        # has the canonical record. Then run each tool and feed results back.
        working.append({"role": "assistant", "content": final.content})

        tool_results = []
        # A restricted origin's denied tools are refused by _execute_client_tool
        # below; don't record them as "fired" — that would misreport a BLOCKED
        # capability as used in the SRE telemetry / audit chip (e.g. a phone turn
        # where Claude tried system_control would otherwise show sysctl=yes).
        denied = _effective_deny(origin) if restricted else frozenset()
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = block.name
            if name not in denied:
                if name in _CLIENT_TOOLS:
                    fired_client.add(name)
                elif name in _PLEX_LAPTOP_DISPATCH:
                    fired_plex_laptop.add(name)
                elif plex_client is not None and name in plex_client.tool_names:
                    fired_plex_mcp.add(name)
            result_text = _execute_client_tool(
                name,
                block.input or {},
                plex_client=plex_client,
                plex_laptop_client=plex_laptop_client,
                restricted=restricted,
                origin=origin,
            )
            # Vision tools (camera_snapshot, screen_snapshot) return a list
            # containing an image block + a caption text block. If a UI hook
            # is registered, surface the raw bytes so the console can render
            # an inline thumbnail of what Jarvis just saw. Detecting on the
            # generic shape (list with an image block) keeps this hook
            # tool-agnostic — any tool that returns an image gets thumbnailed
            # for free. NOTE: only the FIRST image block per result is surfaced
            # (the `break` below) — every current vision tool returns exactly
            # one; a future multi-image tool would need the break removed.
            if on_image_captured is not None and isinstance(result_text, list):
                for sub in result_text:
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        src = sub.get("source", {})
                        if src.get("type") == "base64" and src.get("data"):
                            try:
                                img_bytes = base64.b64decode(src["data"])
                                on_image_captured(
                                    img_bytes,
                                    src.get("media_type", "image/png"),
                                    name,
                                )
                            except Exception as exc:
                                print(
                                    f"[llm] on_image_captured failed for {name}: {exc}",
                                    file=sys.stderr,
                                )
                        break
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        working.append({"role": "user", "content": tool_results})
        # Loop continues — next stream iteration sees the tool result and
        # produces Claude's final answer (or another tool call).
    else:
        # while-else: ran out of iterations without hitting break — i.e. the
        # last iteration still wanted another tool call. Whatever streamed so
        # far was interstitial ("let me check…"), with no final answer, so
        # without this the caller gets dead silence. Yield a short spoken
        # fallback so a capped turn degrades gracefully. Log it too — hitting
        # the cap is still a signal worth noticing in real use.
        print(
            f"[llm] hit MAX_LOOP_ITERATIONS={_MAX_LOOP_ITERATIONS} agentic cap",
            file=sys.stderr,
        )
        yield _LOOP_CAP_FALLBACK

    # Compact tool/run markers for the stderr line. Order: server tools, then
    # built-in client tools (in _CLIENT_TOOLS order), then the grouped MCP /
    # SSH families, then run-shape markers.
    markers: list[str] = [f"{n}=yes" for n in _SERVER_TOOLS if n in fired_server]
    markers += [f"{ct.log_label}=yes" for n, ct in _CLIENT_TOOLS.items() if n in fired_client]
    if fired_plex_mcp:
        markers.append(f"mcp_tools={','.join(sorted(fired_plex_mcp))}")
    if fired_plex_laptop:
        markers.append(f"plex_laptop_tools={','.join(sorted(fired_plex_laptop))}")
    if paused:
        markers.append("PAUSED_TURN(10-iter cap)")
    if iterations > 1:
        markers.append(f"iters={iterations}")
    if engineer_mode:
        markers.append("thinking=on")
    extra = (" " + " ".join(markers)) if markers else ""

    elapsed = time.monotonic() - started_at
    print(
        f"[llm] tokens: input={total_input} output={total_output} "
        f"cache_read={total_cache_read} cache_create={total_cache_create} "
        f"history_msgs={len(messages)} summaries={len(summaries) if summaries else 0} "
        f"elapsed={elapsed:.1f}s"
        f"{extra}",
        file=sys.stderr,
    )

    if on_complete is not None:
        # Flat ordered list of API tool names that fired this turn (the "verb"
        # the console chip leads with). Order: server tools, built-in client
        # tools (in _CLIENT_TOOLS order), then the SSH and MCP families.
        tools_used = (
            [n for n in _SERVER_TOOLS if n in fired_server]
            + [n for n in _CLIENT_TOOLS if n in fired_client]
            + sorted(fired_plex_laptop)
            + sorted(fired_plex_mcp)
        )

        record = TelemetryRecord(
            elapsed_sec=elapsed,
            iterations=iterations,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_read_tokens=total_cache_read,
            cache_create_tokens=total_cache_create,
            tools_used=tools_used,
            paused=paused,
            thinking_enabled=engineer_mode,
        )
        try:
            on_complete(record)
        except Exception as exc:
            # A UI bug must never break the listen loop.
            print(f"[llm] on_complete callback raised: {exc}", file=sys.stderr)
