"""Claude-powered chess coach via SAP AI Core (direct foundation-model invoke).

Reuses the SAP AI Core / XSUAA credentials from the user's
sap-architecture-validator project so no new secrets are introduced. Auth is
client-credentials → XSUAA bearer token (cached), then a direct Anthropic
Messages call to the deployed Claude model:

    POST {base}/v2/inference/deployments/{deployment_id}/invoke

Credentials are read from the process environment first; any missing values
fall back to a .env file (default: the validator project's .env, overridable
via CHESS_COACH_ENV_FILE). If nothing is configured or a call fails, callers
fall back to the built-in heuristic coach.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any

import chess
import httpx

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover
    dotenv_values = None  # type: ignore

# Where to borrow SAP AI Core creds from if not already in the environment.
_DEFAULT_ENV_FILE = os.environ.get(
    "CHESS_COACH_ENV_FILE",
    "/Users/jwan.sulyman/Documents/Repos/sap-architecture-validator/.env",
)

_KEYS = (
    "AICORE_ORCH_AUTH_URL",
    "AICORE_ORCH_CLIENT_ID",
    "AICORE_ORCH_CLIENT_SECRET",
    "AICORE_ORCH_BASE_URL",
    "AICORE_ORCH_RESOURCE_GROUP",
    "AICORE_DIRECT_DEPLOYMENT_ID",
    "AICORE_DIRECT_MODEL_NAME",
)

_cfg_cache: dict[str, str] | None = None
_token_lock = Lock()
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def _config() -> dict[str, str]:
    global _cfg_cache
    if _cfg_cache is not None:
        return _cfg_cache
    file_vals = {}
    if dotenv_values is not None and os.path.exists(_DEFAULT_ENV_FILE):
        try:
            file_vals = dotenv_values(_DEFAULT_ENV_FILE)
        except Exception:
            file_vals = {}
    cfg = {}
    for k in _KEYS:
        cfg[k] = os.environ.get(k) or (file_vals.get(k) or "")
    _cfg_cache = cfg
    return cfg


def is_available() -> bool:
    c = _config()
    return bool(
        c["AICORE_ORCH_AUTH_URL"] and c["AICORE_ORCH_CLIENT_ID"]
        and c["AICORE_ORCH_CLIENT_SECRET"] and c["AICORE_ORCH_BASE_URL"]
        and c["AICORE_ORCH_RESOURCE_GROUP"] and c["AICORE_DIRECT_DEPLOYMENT_ID"]
    )


def provider_label() -> str:
    c = _config()
    if not is_available():
        return "heuristic (SAP AI Core not configured)"
    model = c.get("AICORE_DIRECT_MODEL_NAME") or "anthropic"
    return f"SAP AI Core · {model} · rg={c['AICORE_ORCH_RESOURCE_GROUP']}"


def _get_token(c: dict[str, str]) -> str:
    now = time.time()
    with _token_lock:
        cached = _token_cache.get("access_token")
        if cached and _token_cache.get("expires_at", 0.0) - 30 > now:
            return cached
        r = httpx.post(
            c["AICORE_ORCH_AUTH_URL"],
            data={"grant_type": "client_credentials"},
            auth=(c["AICORE_ORCH_CLIENT_ID"], c["AICORE_ORCH_CLIENT_SECRET"]),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        r.raise_for_status()
        body = r.json()
        token = body["access_token"]
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + int(body.get("expires_in") or 3600)
        return token


def invoke(messages: list[dict], *, system: str | None = None,
           max_tokens: int = 400) -> str:
    """Call the deployed Claude model and return its text. Raises on failure.

    If AICORE_PROXY_URL + AICORE_PROXY_SECRET env vars are set, route through
    the Cloudflare Worker proxy instead of speaking to SAP AI Core directly.
    The proxy does the OAuth + Claude HTTPS work outside of this Python
    process, which is essential on memory-constrained free-tier hosts (e.g.
    Render 512 MB) where Stockfish + Python + a TLS client to SAP BTP all
    running together OOM-kills the container.

    The Worker accepts an Anthropic-Messages-compatible body (`messages`,
    `system`, `max_tokens`) and returns `{"content": "...", ...}` — same
    shape this function already produces. So the call site stays identical.
    """
    proxy_url = os.environ.get("AICORE_PROXY_URL", "").strip().rstrip("/")
    proxy_secret = os.environ.get("AICORE_PROXY_SECRET", "").strip()
    if proxy_url and proxy_secret:
        # ─── Route via Cloudflare Worker (free-tier-friendly) ───
        # ONE local-ish TLS connection to *.workers.dev, instead of TWO
        # heavy ones to SAP IDM + AI Core. Memory pressure problem solved.
        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
            # The Worker also accepts system in messages[0]; either works.
            # We pass it as a top-level field so Claude 4.7 Opus on Bedrock
            # doesn't have to special-case it.
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{proxy_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {proxy_secret}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        r.raise_for_status()
        return (r.json().get("content") or "").strip()

    # ─── Direct path (when no proxy configured) ───
    # This is what runs locally where there's plenty of RAM.
    c = _config()
    base = c["AICORE_ORCH_BASE_URL"].rstrip("/")
    dep = c["AICORE_DIRECT_DEPLOYMENT_ID"]
    url = f"{base}/v2/inference/deployments/{dep}/invoke"
    headers = {
        "Authorization": f"Bearer {_get_token(c)}",
        "AI-Resource-Group": c["AICORE_ORCH_RESOURCE_GROUP"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, headers=headers, json=body)
        if r.status_code == 401:  # token expired — refresh once
            _token_cache["access_token"] = None
            headers["Authorization"] = f"Bearer {_get_token(c)}"
            r = client.post(url, headers=headers, json=body)
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text").strip()


# ─── Coaching prompt ─────────────────────────────────────────────────────────

_SYSTEM = """You are Chess Rocket, an expert chess coach running a live, \
one-on-one coaching session. You blend three perspectives: a Grandmaster's \
insight, a learning scientist's sense of pacing, and an encouraging mentor's \
warmth.

You are given FACTUAL analysis of the student's latest move from the Stockfish \
engine (evaluations in centipawns from the side-to-move's view, the engine's \
best move and main line, and tactical facts such as hanging pieces). TRUST \
these facts completely. Never invent moves, evaluations, or threats that \
contradict or go beyond them. Refer to moves by the exact names given.

Write concise spoken-style coaching that appears in a side panel:
- Match your vocabulary to the student's level (given below).
- 2-3 short sentences for good moves; up to 5 for mistakes or blunders.
- Lead with a one-line verdict on the move they just played, then the single \
most important idea (the WHY).
- Then ALWAYS pivot to their upcoming move: in one sentence, tell them what to \
pay attention to now (the opponent's threat if there is one, or the key plan), \
and nudge them toward a good idea WITHOUT just naming the single best move \
(that is what the Hint button is for) — frame it as a question or a plan.
- For a blunder, clearly say what went wrong and remind them they can press \
Undo to take it back and try again.
- Plain sentences (no headings, no bullet lists). Light **bold** on key moves \
is fine. Be warm but not gushy. No preamble like "As your coach". """


_MOTIF_LABELS = {
    "fork": "fork", "pin": "pin", "skewer": "skewer",
    "back_rank_mate": "back-rank mate", "checkmate": "checkmate",
    "double_check": "double check", "discovered_attack": "discovered attack",
    "promotion": "pawn promotion",
}


def _motif_label(motif) -> str | None:
    if not motif:
        return None
    return _MOTIF_LABELS.get(motif, str(motif).replace("_", " "))


def _situation_str(facts: dict) -> str:
    """Render the current-position facts shared by review / hint / ask."""
    lines = []
    if facts.get("threat"):
        t = facts["threat"]
        desc = t.get("desc") or t.get("san")
        lines.append(f"Opponent's threat if the student does nothing: {desc}.")
    if facts.get("your_hanging"):
        h = facts["your_hanging"]
        lines.append(f"The student's {h['piece']} on {h['square']} is currently "
                     f"attacked and under-defended.")
    cands = facts.get("candidates") or []
    if cands:
        parts = []
        for c in cands[:3]:
            tag = (f"mate in {c['mate']}" if c.get("mate")
                   else f"{c.get('score_cp', 0)}cp")
            parts.append(f"{c['san']} ({tag})")
        lines.append("Engine's best moves for the student now: " + ", ".join(parts) + ".")
    bm = _motif_label(facts.get("best_motif"))
    if bm:
        lines.append(f"The engine's best move is a {bm} (an engine-confirmed "
                     f"tactic the student could look for).")
    return "\n".join(lines)


def _facts(ctx: dict) -> str:
    lines = []
    color = (ctx.get("mover") or "white").lower()
    opp = "black" if color == "white" else "white"
    lines.append(f"Student level: {ctx.get('level')} (~{ctx.get('elo')} Elo). "
                 f"The student plays {color} (their pieces are {color}); the "
                 f"opponent is {opp}. The student's move below is a {color} move.")
    if ctx.get("opening"):
        lines.append(f"Opening in progress: {ctx['opening']}.")
    lines.append(f"Move {ctx.get('move_number')}, the student (as {color}) "
                 f"just played {ctx.get('move_san')}.")
    lines.append(f"Engine classification: {ctx.get('classification')} "
                 f"(centipawn loss vs best: {ctx.get('cp_loss')}).")
    if ctx.get("is_best"):
        lines.append("This WAS the engine's top choice.")
    else:
        bm = _motif_label(ctx.get("best_motif"))
        motif_note = f" — an engine-confirmed {bm}" if bm else ""
        lines.append(f"Engine's best move was {ctx.get('best_move_san')}{motif_note}; "
                     f"main line: {ctx.get('best_line')}.")
    pm = [_motif_label(m) for m in (ctx.get("played_motifs") or [])]
    pm = [m for m in pm if m]
    # Only credit a tactic the student played if the move was actually sound —
    # don't praise a "pin" on a move that was really a blunder.
    if pm and ctx.get("cp_loss", 999) <= 80:
        lines.append(f"Good — the student's move creates: {', '.join(pm)} "
                     f"(engine-confirmed tactical motifs).")
    lines.append(f"Evaluation before the move: {ctx.get('eval_before')} "
                 f"(student's view, pawns). After: {ctx.get('eval_after')}.")
    tac = ctx.get("tactical")
    if tac:
        lines.append("TACTICAL ALERT (engine-verified): " + tac)
    if ctx.get("engine_reply"):
        lines.append(f"The engine then replied {ctx['engine_reply']}.")
    if ctx.get("game_over"):
        lines.append(f"The game is OVER: {ctx.get('result_text')}.")
    if ctx.get("recent"):
        lines.append(f"Recent moves: {ctx['recent']}.")
    sit = _situation_str(ctx)
    if sit:
        lines.append("\nThe student is now on move. " + sit)
    lines.append(f"Current FEN: {ctx.get('fen')}.")
    return "\n".join(lines)


# ─── Hints (graduated) ───────────────────────────────────────────────────────

_HINT_SYSTEM = """You are Chess Rocket, a chess coach giving a student a HINT \
for the move they are about to make. You get factual Stockfish analysis of the \
current position — trust it, never contradict it. Match the student's level.

The hint level controls how much you reveal:
- Level 1 (gentle nudge): Describe the most important feature of the position \
or what they should be asking themselves. Do NOT name a specific move. 1-2 \
sentences ending in a guiding question.
- Level 2 (warmer): Narrow it down — point to the right piece, square, or type \
of idea (e.g. "look for a knight move that hits two things"), or mention 2 \
candidate moves to compare. Still make them choose. 1-2 sentences.
- Level 3 (the answer): State the best move plainly and explain in one sentence \
why it's strong.
Plain sentences, light **bold** on moves. No preamble."""


def coach_hint(facts: dict, level: int) -> tuple[str | None, str]:
    if not is_available():
        return None, "unavailable"
    msg = (f"Hint level requested: {level}.\n"
           f"Student level: {facts.get('level')} (~{facts.get('elo')} Elo), "
           f"playing {facts.get('your_color')}.\n"
           f"Move {facts.get('move_number')}, {facts.get('turn')} to play. "
           f"Opening: {facts.get('opening') or 'out of book'}. "
           f"Evaluation (White's view, pawns): {facts.get('eval_white')}.\n"
           f"{_situation_str(facts)}\n"
           f"Recent moves: {facts.get('recent')}.\nFEN: {facts.get('fen')}.")
    try:
        text = invoke([{"role": "user", "content": msg}],
                      system=_HINT_SYSTEM, max_tokens=300)
        return (text or None), "sap-ai-core"
    except Exception as exc:  # noqa: BLE001
        return None, f"error: {type(exc).__name__}: {str(exc)[:120]}"


# ─── Ask the coach (free-form Q&A about the position) ────────────────────────

_ASK_SYSTEM = """You are Chess Rocket, a friendly chess coach answering a \
student's question about the CURRENT position in their live game. You get \
factual Stockfish analysis — trust it and never invent moves or claims beyond \
it. Match the student's level. Answer directly and concisely (2-4 sentences), \
using the move names and facts given. If they ask "what should I play", you may \
suggest a move and why. Plain sentences, light **bold** on moves. No preamble."""


_REVIEW_SYSTEM = """You are Chess Rocket giving a warm, encouraging post-game \
debrief to a student. You get the game's result and accuracy stats. Speak \
directly to the student (2-4 sentences): acknowledge the result with a growth \
mindset, name ONE clear strength and ONE concrete thing to work on next time \
based on the stats. Match their level. Plain sentences, no lists, no preamble."""


def coach_review(summary: dict) -> tuple[str | None, str]:
    if not is_available():
        return None, "unavailable"
    counts = summary.get("counts", {})
    breakdown = ", ".join(f"{v} {k}" for k, v in counts.items()) or "no moves"
    msg = (f"Game over. Result: {summary.get('result_text') or summary.get('result')}. "
           f"The student played {summary.get('player_color')} at "
           f"~{summary.get('target_elo')} Elo opponent strength; their level is "
           f"{summary.get('level')}.\n"
           f"Their accuracy: {summary.get('accuracy')}%. "
           f"Move quality: {breakdown}. "
           f"Blunders: {summary.get('blunders')}, mistakes: {summary.get('mistakes')}, "
           f"inaccuracies: {summary.get('inaccuracies')} over "
           f"{summary.get('total_moves')} of their moves.")
    try:
        text = invoke([{"role": "user", "content": msg}],
                      system=_REVIEW_SYSTEM, max_tokens=300)
        return (text or None), "sap-ai-core"
    except Exception as exc:  # noqa: BLE001
        return None, f"error: {type(exc).__name__}: {str(exc)[:120]}"


def _loss_phrase(cp: int) -> str:
    """Human phrasing for centipawn loss (raw mate-score numbers are huge)."""
    if cp >= 1000:
        return "a game-losing blunder that threw away a decisive advantage"
    if cp <= 0:
        return "barely any loss"
    return f"about {cp} centipawns lost"


_MOMENT_SYSTEM = """You are Chess Rocket walking a student through one key \
moment from their finished game. You get the position (FEN), which colour the \
student is, the move THEY played, the engine's best move, and the best line. \
Trust these facts.

CRITICAL — be rigorous about colour, or the explanation is useless:
- The student's move AND the engine's best move are BOTH the student's-colour \
moves. NEVER describe the student's own move as the opponent's.
- The student's pieces are their colour; call them "your" pieces. The opponent \
is the other colour.
- In the best line the moves alternate, starting with the student's colour. \
When you mention an opponent reply, say it explicitly, e.g. "then Black plays …".
- If two moves land on the same square (e.g. both sides can play …xa5), make \
crystal clear which side does which.

In 2-3 sentences at the student's level: explain what their move missed or \
allowed, what the better move achieves, and the takeaway lesson (name the \
pattern if clear: hanging piece, fork, pin, development, king safety...). \
Plain sentences, light **bold** on moves. No preamble."""


def coach_moment(moment: dict, summary: dict) -> tuple[str | None, str]:
    if not is_available():
        return None, "unavailable"
    color = (summary.get("player_color") or "white").lower()
    opp = "black" if color == "white" else "white"
    msg = (f"Student level: {summary.get('level')}. "
           f"The student is playing {color.upper()}; the opponent is {opp}.\n"
           f"On move {moment.get('move_number')}, the student (as {color}) played "
           f"**{moment.get('played')}** — {moment.get('classification')}, "
           f"{_loss_phrase(moment.get('cp_loss', 0))}. "
           f"The engine's best {color} move was **{moment.get('best')}**, "
           f"with the line: {moment.get('best_line')} "
           f"(moves alternate {color}, then {opp}, ...).\n"
           f"Position before the student's move (FEN, {color} to move): "
           f"{moment.get('fen_before')}.")
    try:
        text = invoke([{"role": "user", "content": msg}],
                      system=_MOMENT_SYSTEM, max_tokens=300)
        if not _audit_claims(text, moment.get("fen_before"), "moment"):
            return None, "validation_failed"
        return (text or None), "sap-ai-core"
    except Exception as exc:  # noqa: BLE001
        return None, f"error: {type(exc).__name__}: {str(exc)[:120]}"


def answer_question(facts: dict, question: str) -> tuple[str | None, str]:
    if not is_available():
        return None, "unavailable"
    msg = (f"Student question: \"{question}\"\n\n"
           f"Context — level: {facts.get('level')} (~{facts.get('elo')} Elo), "
           f"playing {facts.get('your_color')}. "
           f"Move {facts.get('move_number')}, {facts.get('turn')} to play. "
           f"Opening: {facts.get('opening') or 'out of book'}. "
           f"Evaluation (White's view, pawns): {facts.get('eval_white')}.\n"
           f"{_situation_str(facts)}\n"
           f"Recent moves: {facts.get('recent')}.\nFEN: {facts.get('fen')}.")
    try:
        text = invoke([{"role": "user", "content": msg}],
                      system=_ASK_SYSTEM, max_tokens=400)
        return (text or None), "sap-ai-core"
    except Exception as exc:  # noqa: BLE001
        return None, f"error: {type(exc).__name__}: {str(exc)[:120]}"


_SIM_SYSTEM = """You are Chess Rocket helping a student think ahead in an \
ANALYSIS SANDBOX. The student is trying out a hypothetical move to see whether \
it makes sense — this is NOT their real game and nothing is committed. You are \
given FACTUAL Stockfish analysis: the move being tested, its centipawn loss vs \
the engine's best move, the evaluation before and after (the mover's view, in \
pawns), any piece the move leaves hanging, the engine's best move, and the \
engine's predicted continuation. TRUST these facts completely; never invent \
moves, evaluations, or threats. Refer to moves by the exact names given. Match \
the student's level.

In 2-4 short sentences, deliver a clear verdict on whether this move makes \
sense and WHY — grounded in the eval swing and what the opponent gets to do \
next. If it loses material or drops the evaluation, say so plainly and point to \
the better idea (without a lecture). If it holds up, reassure them and name the \
plan it leads to. End by orienting them to the predicted continuation if there \
is one. Plain sentences, light **bold** on moves, no headings or preamble."""


def coach_simulation(ctx: dict) -> tuple[str | None, str]:
    """Verdict on a hypothetical sandbox move. (text, source) or (None, reason)."""
    if not is_available():
        return None, "unavailable"
    lines = [
        f"Student level: {ctx.get('level')} (~{ctx.get('elo')} Elo).",
        f"They are testing the move {ctx.get('move_san')} for {ctx.get('mover')}.",
        f"Engine classification: {ctx.get('classification')} "
        f"(centipawn loss vs best: {ctx.get('cp_loss')}).",
    ]
    if ctx.get("is_best"):
        lines.append("This IS the engine's top choice in this position.")
    else:
        lines.append(f"Engine's best move was {ctx.get('best_move_san')}; "
                     f"main line: {ctx.get('best_line')}.")
    lines.append(f"Evaluation before the move: {ctx.get('eval_before')} "
                 f"(mover's view, pawns). After: {ctx.get('eval_after')}.")
    hang = ctx.get("hanging")
    if hang:
        lines.append(f"After this move, the mover's {hang['piece']} on "
                     f"{hang['square']} would be attacked and under-defended.")
    if ctx.get("predicted_line"):
        lines.append(f"Engine's predicted continuation after this move: "
                     f"{ctx['predicted_line']}.")
    if ctx.get("recent"):
        lines.append(f"Moves leading to this exploration from the real game: "
                     f"{ctx['recent']}.")
    lines.append(f"Resulting FEN: {ctx.get('fen')}.")
    try:
        text = invoke([{"role": "user", "content": "\n".join(lines)}],
                      system=_SIM_SYSTEM, max_tokens=320)
        return (text or None), "sap-ai-core"
    except Exception as exc:  # noqa: BLE001
        return None, f"error: {type(exc).__name__}: {str(exc)[:120]}"


# ─── Anti-credulity: verify the coach's move claims against the engine ───────
#
# The blog's sharpest failure mode is a confidently-wrong claim reaching the
# learner. We extract SAN-looking tokens from the coach's prose and check them
# for legality in the position. NOTE: moves deep in a cited line are legal in
# their continuation but NOT in the base FEN, so this over-reports by design —
# it runs LOG-ONLY first (see _ENFORCE_CLAIMS) so we can measure the real
# false-positive rate on live coaching before ever suppressing output.
_ENFORCE_CLAIMS = False
_CLAIM_LOG = Path(__file__).resolve().parent.parent / "data" / "coach_claim_log.jsonl"
_SAN_RE = re.compile(
    r'\b(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?|'
    r'[a-h]x[a-h][1-8](?:=[QRBN])?)\b'
)


def _illegal_claims(text: str, fen: str | None) -> list[str]:
    """SAN tokens in `text` that are not legal moves in `fen` (best-effort)."""
    if not text or not fen:
        return []
    try:
        board = chess.Board(fen)
    except ValueError:
        return []
    legal = {board.san(m).rstrip("+#") for m in board.legal_moves}
    legal |= {"O-O", "O-O-O"}
    bad, seen = [], set()
    for tok in _SAN_RE.findall(text):
        base = tok.rstrip("+#")
        if base in seen:
            continue
        seen.add(base)
        if base not in legal:
            bad.append(tok)
    return bad


def _audit_claims(text: str, fen: str | None, kind: str) -> bool:
    """Log possibly-hallucinated move claims. Returns False only if enforcing
    AND illegal claims were found (so the caller can fall back)."""
    bad = _illegal_claims(text, fen)
    if bad:
        try:
            _CLAIM_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _CLAIM_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "kind": kind, "fen": fen, "not_legal_in_base_fen": bad,
                    "excerpt": (text or "")[:300],
                }) + "\n")
        except OSError:
            pass
    return not (_ENFORCE_CLAIMS and bad)


def coach_move(ctx: dict, *, max_tokens: int = 400) -> tuple[str | None, str]:
    """Return (coaching_text, source). On any failure returns (None, reason)."""
    if not is_available():
        return None, "unavailable"
    try:
        text = invoke(
            [{"role": "user", "content": _facts(ctx)}],
            system=_SYSTEM, max_tokens=max_tokens,
        )
        if not _audit_claims(text, ctx.get("fen"), "move"):
            return None, "validation_failed"
        return (text or None), "sap-ai-core"
    except Exception as exc:  # noqa: BLE001
        return None, f"error: {type(exc).__name__}: {str(exc)[:120]}"
