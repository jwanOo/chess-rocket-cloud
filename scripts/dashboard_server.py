"""HTTP server for the interactive Chess Rocket web dashboard.

Serves dashboard.html and JSON API endpoints. The dashboard is fully
playable in the browser: the player drags pieces (POST /api/move), the
engine replies, and coaching is generated server-side. Game state is also
mirrored to data/current_game.json.

Launch: uv run python scripts/dashboard_server.py [--port 8088]
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DATA_DIR = _PROJECT_ROOT / "data"
_SCRIPTS_DIR = Path(__file__).resolve().parent
_DASHBOARD_HTML = _SCRIPTS_DIR / "dashboard.html"
_TACTICS_HTML = _SCRIPTS_DIR / "tactics.html"
_SETUP_HTML = _SCRIPTS_DIR / "setup.html"
_VOICE_JS = _SCRIPTS_DIR / "voice_control.js"
_MANIFEST = _SCRIPTS_DIR / "manifest.webmanifest"
_SW_JS = _SCRIPTS_DIR / "sw.js"

from scripts.game_manager import GameManager  # noqa: E402
from scripts import sap_coach  # noqa: E402
from scripts import tactics_trainer  # noqa: E402


# ─────────────────────────── Per-session game state ──────────────────────
# Until now we shared one GameManager singleton across every request. Once
# this server is reachable from the public internet (Fly.io behind Vercel)
# that's untenable: two visitors would clobber each other's board. We now
# key the GameManager by a cookie-issued session id and keep a small lock
# per session so concurrent moves on different boards never serialise.

_SESSION_COOKIE = "cr_session"

# How long to keep an idle session in memory before garbage-collecting it.
# The trade-off: shorter = lower memory; longer = users can leave a tab open
# all day without losing their game. 4 hours feels right for a coaching app.
_SESSION_TTL_SECONDS = 4 * 60 * 60


class _Sessions:
    """Thread-safe registry of (GameManager, lock, last_seen) per session."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get_or_create(self, sid: str) -> tuple[GameManager, threading.Lock]:
        with self._lock:
            entry = self._sessions.get(sid)
            now = time.time()
            if entry is None:
                entry = {
                    "game": GameManager(),
                    "lock": threading.Lock(),
                    "last_seen": now,
                }
                self._sessions[sid] = entry
            entry["last_seen"] = now
        return entry["game"], entry["lock"]

    def gc(self) -> None:
        """Drop sessions idle longer than TTL. Cheap; called from each
        request, so we don't need a separate sweeper thread."""
        now = time.time()
        cutoff = now - _SESSION_TTL_SECONDS
        with self._lock:
            stale = [sid for sid, e in self._sessions.items()
                     if e["last_seen"] < cutoff]
            for sid in stale:
                self._sessions.pop(sid, None)


_SESSIONS = _Sessions()

# Tactics trainer state is independent of the live game (no Stockfish needed
# for puzzle validation), so it also gets its own per-session entry — one
# TacticsSession per cookie, indexed under the same session id.
_TACTICS_SESSIONS: dict[str, tuple[tactics_trainer.TacticsSession, threading.Lock]] = {}
_TACTICS_REGISTRY_LOCK = threading.Lock()


def _tactics_for(sid: str):
    with _TACTICS_REGISTRY_LOCK:
        entry = _TACTICS_SESSIONS.get(sid)
        if entry is None:
            entry = (tactics_trainer.TacticsSession(), threading.Lock())
            _TACTICS_SESSIONS[sid] = entry
    return entry


# ─────────────── CORS allow-list (for Vercel + Fly deployment) ────────────
# `CORS_ALLOWED_ORIGINS` is a comma-separated list set in Fly's env.
# When unset (local dev) we mirror whatever Origin the browser sent.
_CORS_ALLOWED = [
    o.strip().rstrip("/")
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Serves dashboard HTML, game-state JSON, and move/new/undo actions."""

    # ---- session cookie helpers -----------------------------------------
    def _read_or_create_session_id(self) -> str:
        """Return the session id for this request, creating one if missing.

        The id is stored in a `cr_session` cookie. We mark it for emission
        in `_session_set_cookie` so subsequent responses re-confirm it.
        """
        raw = self.headers.get("Cookie", "") or ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:  # noqa: BLE001
            cookie = SimpleCookie()
        if _SESSION_COOKIE in cookie:
            sid = cookie[_SESSION_COOKIE].value
            if sid and len(sid) >= 16:
                self._session_id = sid
                self._session_new = False
                return sid
        sid = secrets.token_urlsafe(24)
        self._session_id = sid
        self._session_new = True
        return sid

    def _game_for_request(self) -> tuple[GameManager, threading.Lock]:
        sid = getattr(self, "_session_id", None) or self._read_or_create_session_id()
        return _SESSIONS.get_or_create(sid)

    def _tactics_for_request(self):
        sid = getattr(self, "_session_id", None) or self._read_or_create_session_id()
        return _tactics_for(sid)

    # ---- GET -------------------------------------------------------------
    def do_GET(self) -> None:
        # Touch the session cookie on every request so the user keeps the
        # same game across page reloads.
        self._read_or_create_session_id()
        _SESSIONS.gc()
        # `self.path` includes the query string, so "/" with `?play=1` from
        # the Setup page comes through as "/?play=1" — strip the query so
        # all the page routes still match.
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_file(_DASHBOARD_HTML, "text/html; charset=utf-8")
        elif path in ("/tactics", "/tactics.html"):
            self._serve_file(_TACTICS_HTML, "text/html; charset=utf-8")
        elif path in ("/setup", "/setup.html"):
            self._serve_file(_SETUP_HTML, "text/html; charset=utf-8")
        elif path == "/voice_control.js":
            self._serve_file(_VOICE_JS, "application/javascript; charset=utf-8")
        elif path == "/manifest.webmanifest":
            self._serve_file(_MANIFEST, "application/manifest+json; charset=utf-8")
        elif path == "/sw.js":
            self._serve_file(_SW_JS, "application/javascript; charset=utf-8")
        elif path == "/healthz":
            # Fly.io and Vercel health probes — must be cheap and not
            # touch any session state.
            self._send_json(200, {"ok": True, "service": "chess_rocket"})
        elif path == "/api/game":
            game, lock = self._game_for_request()
            with lock:
                self._send_json(200, game.state_dict())
        elif path == "/api/progress":
            self._serve_json(_DATA_DIR / "progress.json")
        elif path == "/api/tactics/themes":
            # Read-only — safe outside the tactics lock; trainer module caches.
            self._send_json(200, {"themes": tactics_trainer.list_themes()})
        elif path == "/api/tactics/progress":
            self._send_json(200, tactics_trainer.progress_summary())
        elif path == "/api/tactics/state":
            tactics, lock = self._tactics_for_request()
            with lock:
                self._send_json(200, tactics.state())
        elif path == "/api/coach-status":
            # Diagnostic endpoint: shows whether SAP AI Core is reachable
            # and which env-var keys the container actually sees set.
            # Never returns the secret values — only booleans + provider label.
            keys = (
                "AICORE_ORCH_AUTH_URL",
                "AICORE_ORCH_CLIENT_ID",
                "AICORE_ORCH_CLIENT_SECRET",
                "AICORE_ORCH_BASE_URL",
                "AICORE_ORCH_RESOURCE_GROUP",
                "AICORE_DIRECT_DEPLOYMENT_ID",
                "AICORE_DIRECT_MODEL_NAME",
                "AICORE_PROXY_URL",
                "AICORE_PROXY_SECRET",
            )
            present = {k: bool(os.environ.get(k, "").strip()) for k in keys}
            # Surface the proxy URL value (non-secret) and lengths so we can
            # tell whether the env vars actually reached the container.
            proxy_url_val = (os.environ.get("AICORE_PROXY_URL", "") or "").strip()
            proxy_secret_len = len((os.environ.get("AICORE_PROXY_SECRET", "") or "").strip())
            self._send_json(200, {
                "available": sap_coach.is_available(),
                "provider": sap_coach.provider_label(),
                "env_keys_present": present,
                "missing_keys": [k for k, v in present.items() if not v],
                "proxy_url": proxy_url_val,
                "proxy_secret_len": proxy_secret_len,
                "proxy_active": bool(proxy_url_val and proxy_secret_len),
            })
        else:
            self.send_error(404)

    # ---- POST ------------------------------------------------------------
    def do_POST(self) -> None:
        # Same session-cookie touch + GC as do_GET — the cookie may be
        # set fresh on the very first POST when the user hits /api/new.
        self._read_or_create_session_id()
        _SESSIONS.gc()
        try:
            body = self._read_json()
        except ValueError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        path = urlparse(self.path).path

        if path == "/api/move":
            frm = body.get("from")
            to = body.get("to")
            promo = body.get("promotion")
            if not frm or not to:
                self._send_json(400, {"error": "from/to required"})
                return
            game, lock = self._game_for_request()
            with lock:
                result = game.player_move(frm, to, promo)
            self._send_json(200, result)
        elif path == "/api/new":
            raw = body.get("elo", 800)
            elo = raw if raw in (None, "auto") else int(raw)  # "auto" => adaptive
            color = body.get("color", "white")
            # Optional: start the game from a custom FEN (the /setup editor).
            # GameManager.new_game returns {"error": ..., "illegal": True} if
            # the FEN is malformed or the position fails python-chess's
            # is_valid() check — surface that as a 400 so the editor can show
            # the reason and stay open.
            start_fen = (body.get("start_fen") or "").strip() or None
            game, lock = self._game_for_request()
            with lock:
                result = game.new_game(elo, color, start_fen=start_fen)
            if isinstance(result, dict) and result.get("illegal"):
                self._send_json(400, result)
            else:
                self._send_json(200, result)
        elif path == "/api/undo":
            game, lock = self._game_for_request()
            with lock:
                result = game.undo()
            self._send_json(200, result)
        elif path == "/api/coach":
            self._handle_coach()
        elif path == "/api/hint":
            self._handle_hint(int(body.get("level", 1)))
        elif path == "/api/ask":
            self._handle_ask((body.get("question") or "").strip())
        elif path == "/api/review":
            self._handle_review()
        elif path == "/api/sandbox/start":
            game, lock = self._game_for_request()
            with lock:
                result = game.sandbox_start()
            self._send_json(200, result)
        elif path == "/api/sandbox/move":
            frm = body.get("from")
            to = body.get("to")
            promo = body.get("promotion")
            if not frm or not to:
                self._send_json(400, {"error": "from/to required"})
                return
            game, lock = self._game_for_request()
            with lock:
                result = game.sandbox_move(frm, to, promo)
            self._send_json(200, result)
        elif path == "/api/sandbox/undo":
            game, lock = self._game_for_request()
            with lock:
                result = game.sandbox_undo()
            self._send_json(200, result)
        elif path == "/api/sandbox/reset":
            game, lock = self._game_for_request()
            with lock:
                result = game.sandbox_reset()
            self._send_json(200, result)
        elif path == "/api/sandbox/exit":
            game, lock = self._game_for_request()
            with lock:
                result = game.sandbox_exit()
            self._send_json(200, result)
        elif path == "/api/sandbox/coach":
            self._handle_sandbox_coach()
        elif path == "/api/tactics/new":
            self._handle_tactics_new(body)
        elif path == "/api/tactics/move":
            self._handle_tactics_move(body)
        elif path == "/api/tactics/hint":
            self._handle_tactics_hint(int(body.get("level", 1)))
        elif path == "/api/tactics/skip":
            self._handle_tactics_skip()
        elif path == "/api/tactics/clear":
            tactics, lock = self._tactics_for_request()
            with lock:
                tactics.clear()
                self._send_json(200, {"active": False})
        elif path == "/api/tactics/explain":
            self._handle_tactics_explain()
        elif path == "/api/tactics/ask":
            self._handle_tactics_ask((body.get("question") or "").strip())
        else:
            self.send_error(404)

    # ---- tactics handlers ------------------------------------------------
    def _ts(self):
        """Tactics session/lock for this request."""
        return self._tactics_for_request()

    def _handle_tactics_new(self, body: dict) -> None:
        theme = body.get("theme") or None      # None/"" => mixed
        max_rating = body.get("max_rating")
        if max_rating in ("", None):
            max_rating = None
        else:
            try:
                max_rating = int(max_rating)
            except (TypeError, ValueError):
                max_rating = None
        # Recently-seen ids let us avoid serving the same puzzle twice in a row.
        exclude = body.get("exclude_ids") or []
        if not isinstance(exclude, list):
            exclude = []
        puzzle = tactics_trainer.pick_puzzle(
            theme=theme, max_rating=max_rating, exclude_ids=exclude)
        if not puzzle:
            self._send_json(200, {
                "active": False,
                "error": "No puzzle matches that filter — try another theme or "
                         "raise the difficulty cap.",
            })
            return
        tactics, lock = self._ts()
        with lock:
            state = tactics.start(puzzle)
        self._send_json(200, state)

    def _handle_tactics_move(self, body: dict) -> None:
        frm = body.get("from")
        to = body.get("to")
        promo = body.get("promotion")
        if not frm or not to:
            self._send_json(400, {"error": "from/to required"})
            return
        tactics, lock = self._ts()
        with lock:
            result = tactics.submit_move(frm, to, promo)
            if result.get("solved") or result.get("failed"):
                tactics.record_if_done()
        self._send_json(200, result)

    def _handle_tactics_hint(self, level: int) -> None:
        tactics, lock = self._ts()
        with lock:
            result = tactics.hint(level)
            result["state"] = tactics.state()
        self._send_json(200, result)

    def _handle_tactics_skip(self) -> None:
        tactics, lock = self._ts()
        with lock:
            result = tactics.skip()
            tactics.record_if_done()
        self._send_json(200, result)

    # ---- Coach: explain the current puzzle / answer free-form questions ----
    # Both run the LLM call OUTSIDE the tactics lock so the user can keep
    # interacting with the board (drag, hint, skip) while Claude is composing.

    def _handle_tactics_explain(self) -> None:
        """Generate a deeper, position-aware explanation of the current puzzle
        using SAP AI Core. The user-facing 'Solved!' templated text is fine
        for a first impression but doesn't actually teach the position; this
        endpoint asks the LLM for the WHY in plain English."""
        tactics, lock = self._ts()
        with lock:
            facts = tactics_trainer.coaching_facts(
                tactics.puzzle, tactics.board,
                move_idx=tactics.move_idx,
                solved=tactics.solved, failed=tactics.failed,
                attempts=tactics.attempts, hints_used=tactics.hints_used)
        if not facts:
            self._send_json(200, {"explanation": None, "source": "no-puzzle"})
            return
        # Synthesise a concrete coaching question. Using `answer_question`
        # (rather than minting a new sap_coach function) keeps this endpoint
        # zero-touch for the existing prompt + safety machinery.
        if facts.get("puzzle_solved") or facts.get("puzzle_failed"):
            question = ("This puzzle is finished. Walk me through the full "
                        "combination — name the motif, explain why each move "
                        "in the canonical line is forced or strongest, and "
                        "tell me the pattern I should remember next time. "
                        "Use plain language, light **bold** on key moves.")
        else:
            question = ("Without giving away the answer, explain what's going "
                        "on in this position: who has the threats, what "
                        "tactical pattern the puzzle is asking me to spot, "
                        "and what kind of moves I should be considering. Two "
                        "or three sentences.")
        text, source = sap_coach.answer_question(facts, question)
        if not text:
            text = (facts.get("puzzle_explanation")
                    or "I can't reach the coaching model right now.")
            source = source or "heuristic"
        self._send_json(200, {"explanation": text, "source": source,
                              "facts_summary": {
                                  "theme": facts.get("puzzle_theme"),
                                  "motif": facts.get("puzzle_motif"),
                                  "rating": facts.get("puzzle_rating"),
                              }})

    def _handle_tactics_ask(self, question: str) -> None:
        """Free-form Q&A about the current puzzle position."""
        if not question:
            self._send_json(400, {"error": "question required"})
            return
        tactics, lock = self._ts()
        with lock:
            facts = tactics_trainer.coaching_facts(
                tactics.puzzle, tactics.board,
                move_idx=tactics.move_idx,
                solved=tactics.solved, failed=tactics.failed,
                attempts=tactics.attempts, hints_used=tactics.hints_used)
        if not facts:
            self._send_json(200, {
                "answer": "Start a puzzle first, then I can answer questions "
                          "about the position.",
                "source": "no-puzzle",
            })
            return
        # If the puzzle is still in progress, gently steer the LLM away from
        # spoiling the canonical solution unless the user explicitly asks.
        guarded_question = question
        if not (facts.get("puzzle_solved") or facts.get("puzzle_failed")):
            guarded_question = (
                f"{question}\n\n(Note: the student hasn't solved this puzzle "
                f"yet. Don't reveal the canonical solution moves unless the "
                f"student is explicitly asking for the answer.)")
        text, source = sap_coach.answer_question(facts, guarded_question)
        if not text:
            text = ("I can't reach the coaching model right now, but check "
                    "what each piece is attacking, where your king is safe, "
                    "and which of your pieces are loose.")
            source = source or "heuristic"
        self._send_json(200, {"answer": text, "source": source})

    def _handle_sandbox_coach(self) -> None:
        # Snapshot the sim facts under the lock, then call the LLM WITHOUT it.
        game, lock = self._game_for_request()
        with lock:
            ctx = game.sandbox_context()
        if not ctx:
            self._send_json(200, {"verdict": None, "source": "none"})
            return
        text, source = sap_coach.coach_simulation(ctx)
        if not text:
            text = self._sim_fallback(ctx)
            source = "heuristic"
        self._send_json(200, {"verdict": text, "source": source})

    @staticmethod
    def _sim_fallback(ctx: dict) -> str:
        """Templated verdict when the coaching model is unavailable."""
        move = ctx.get("move_san")
        best = ctx.get("best_move_san")
        line = ctx.get("best_line")
        cp = ctx.get("cp_loss")
        pred = ctx.get("predicted_line")
        hang = ctx.get("hanging")
        cls = ctx.get("classification")
        if ctx.get("is_best") or cls in ("best", "great"):
            msg = f"Looks sound — {move} is at or near the engine's top choice."
            if pred:
                msg += f" Likely continuation: {pred}."
            return msg
        if cls == "good":
            tail = f" Expected line: {line}." if line else ""
            return (f"Reasonable. {best} was a touch sharper, but {move} holds "
                    f"the position.{tail}")
        hint = ""
        if hang:
            hint = f" It also leaves your {hang['piece']} on {hang['square']} loose."
        tail = f" ({line})" if line else ""
        return (f"This probably doesn't make sense here — it gives up about "
                f"{cp}cp.{hint} The engine prefers {best}{tail}.")

    def _handle_review(self) -> None:
        game, lock = self._game_for_request()
        with lock:
            review = game.build_review()
            cards = game.create_review_cards() if review else {"created": 0}
        if not review:
            self._send_json(200, {"error": "Game is not over yet."})
            return
        summary, moments = review["summary"], review["moments"]
        # Narrate the summary and each key moment outside the lock (network).
        summary_text, source = sap_coach.coach_review(summary)
        if not summary_text:
            summary_text = (f"Final accuracy {summary['accuracy']}% with "
                            f"{summary['blunders']} blunders and "
                            f"{summary['mistakes']} mistakes. Review the moments "
                            f"below, then play again!")
        for mo in moments:
            txt, _ = sap_coach.coach_moment(mo, summary)
            mo["explanation"] = txt or (
                f"You played {mo['played']} ({mo['classification']}). The engine "
                f"preferred {mo['best']} ({mo['best_line']}).")
        self._send_json(200, {
            "summary": summary, "summary_text": summary_text,
            "moments": moments, "cards_created": cards.get("created", 0),
            "source": source,
        })

    def _handle_coach(self) -> None:
        # Snapshot facts + analyze the resulting position under the lock, then
        # call the LLM WITHOUT the lock (the network round-trip must not block
        # engine/poll calls).
        game, lock = self._game_for_request()
        with lock:
            ctx = game.coaching_context()
            if ctx and not ctx.get("game_over"):
                ctx.update(game.situation_facts())
            fallback = game.coaching
        if not ctx:
            self._send_json(200, {"coaching": fallback, "source": "heuristic"})
            return
        text, source = sap_coach.coach_move(ctx)
        if text:
            with lock:
                game.set_coaching(text)
            self._send_json(200, {"coaching": text, "source": source})
        else:
            self._send_json(200, {"coaching": fallback, "source": source})

    def _handle_hint(self, level: int) -> None:
        level = max(1, min(3, level))
        game, lock = self._game_for_request()
        with lock:
            if game.board.is_game_over():
                self._send_json(200, {"hint": "The game is over — start a new one.",
                                      "level": level, "source": "n/a"})
                return
            facts = game.situation_facts()
            highlight = game.best_move_squares() if level >= 3 else None
        text, source = sap_coach.coach_hint(facts, level)
        if not text:
            # Heuristic fallback so the button always does something useful.
            cands = facts.get("candidates") or []
            if level >= 3 and cands:
                text = f"Try {cands[0]['san']} — the engine's top choice here."
            elif cands:
                opts = ", ".join(c["san"] for c in cands[:3])
                text = f"Strong candidate moves: {opts}. Which fits your plan?"
            else:
                text = "Look for your most active, safe developing move."
        self._send_json(200, {"hint": text, "level": level,
                              "highlight": highlight, "source": source})

    def _handle_ask(self, question: str) -> None:
        if not question:
            self._send_json(400, {"error": "question required"})
            return
        # Wrap the whole handler in try/except so any failure (Stockfish
        # crash, httpx error, malformed response, etc.) surfaces as a JSON
        # body the caller can read instead of dying mid-write and giving
        # the upstream proxy a content-length-0 502. This is essential on
        # memory-constrained hosts where partial-write failures look
        # indistinguishable from "unrelated container crash" otherwise.
        try:
            game, lock = self._game_for_request()
            with lock:
                facts = game.situation_facts()
            answer, source = sap_coach.answer_question(facts, question)
            if not answer:
                answer = ("I can't reach the coaching model right now, but check "
                          "whether any of your pieces are hanging and what your "
                          "opponent threatens.")
            self._send_json(200, {"answer": answer, "source": source})
        except Exception as exc:  # noqa: BLE001
            import traceback
            self._send_json(500, {
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "trace": traceback.format_exc()[-1500:],
                "stage": "ask",
            })

    # ---- helpers ---------------------------------------------------------
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, code: int, obj) -> None:
        payload = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self._session_set_cookie()
        self.end_headers()
        self.wfile.write(payload)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404, f"File not found: {path.name}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # Static assets the frontend re-requests every page load — let the
        # browser cache them for a few minutes but not forever, so quick fixes
        # to voice_control.js or the manifest land without a hard reload.
        if path.suffix in (".js", ".webmanifest"):
            self.send_header("Cache-Control", "public, max-age=300")
        self._cors_headers()
        self._session_set_cookie()
        self.end_headers()
        self.wfile.write(data)

    def _serve_json(self, path: Path) -> None:
        payload = path.read_bytes() if path.exists() else json.dumps(None).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self._session_set_cookie()
        self.end_headers()
        self.wfile.write(payload)

    def _cors_headers(self) -> None:
        # When the frontend is hosted on a different origin (Vercel) the
        # browser sends Origin and expects an explicit allow-list match
        # plus Access-Control-Allow-Credentials so the session cookie can
        # round-trip. Locally we still allow * for ease of testing.
        origin = self.headers.get("Origin", "")
        if _CORS_ALLOWED:
            if origin.rstrip("/") in _CORS_ALLOWED:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Credentials", "true")
            # else: leave the header unset → browser blocks the response
        else:
            # Dev fallback: mirror Origin so credentials work; * blocks them.
            self.send_header("Access-Control-Allow-Origin", origin or "*")
            if origin:
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def _session_set_cookie(self) -> None:
        """Emit Set-Cookie for the session id we issued or refreshed.

        Same-site=None is required for cross-origin (Vercel→Fly) credentialed
        requests; Secure is required by every browser that allows that combo.
        """
        sid = getattr(self, "_session_id", None)
        if not sid:
            return
        # We always emit so that clock skew / cookie eviction never leaves a
        # browser without the cookie after its first POST.
        same_site = "None" if _CORS_ALLOWED else "Lax"
        secure = "; Secure" if same_site == "None" else ""
        ttl = _SESSION_TTL_SECONDS
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE}={sid}; Path=/; Max-Age={ttl}; "
            f"HttpOnly; SameSite={same_site}{secure}"
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Chess Rocket Dashboard Server")
    # Default port 8088 locally; Fly.io sets PORT=8080 in the container env.
    default_port = int(os.environ.get("PORT", "8088"))
    parser.add_argument("--port", type=int, default=default_port,
                        help=f"Port (default: {default_port})")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="Bind address (use 0.0.0.0 in containers)")
    args = parser.parse_args()

    # On Fly we listen on 0.0.0.0; locally on 127.0.0.1. The session-game
    # registry warms up lazily on the first request; we DON'T pre-create
    # GameManager here anymore (would force-load Stockfish + AI Core for an
    # empty session no one's using yet).
    print(f"Chess Rocket Dashboard: http://{args.host}:{args.port}")
    if _CORS_ALLOWED:
        print(f"  CORS allow-list: {', '.join(_CORS_ALLOWED)}")
    server = http.server.ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
