"""In-process game manager for the interactive web dashboard.

Holds a single long-lived Stockfish engine + openings DB and drives a game
entirely from the browser: the player drags pieces, the engine replies, and
beginner-friendly coaching text is generated server-side. State is mirrored to
data/current_game.json so the existing polling UI keeps working.

This is the engine behind dashboard_server.py's /api/move and /api/new.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import chess

from scripts.engine import ChessEngine
from scripts.openings import OpeningsDB
from scripts import sap_coach
from scripts import motif_detector

# Auto-difficulty: recent-accuracy -> engine-Elo adjustment (see CLAUDE.md).
_DIFFICULTY_STEPS = [
    (90.0, 100, "you've been winning comfortably"),
    (80.0, 50, "you're playing well"),
    (65.0, 0, "you're in a good challenge zone"),
    (50.0, -50, "you've been struggling a little"),
    (0.0, -100, "recent games have been tough"),
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_STATE_PATH = _DATA_DIR / "current_game.json"

_PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9,
}
_PIECE_SYMBOLS = {
    chess.PAWN: "P", chess.KNIGHT: "N", chess.BISHOP: "B",
    chess.ROOK: "R", chess.QUEEN: "Q",
}
_STARTING_PIECES = {
    "white": ["P"] * 8 + ["N", "N", "B", "B", "R", "R", "Q"],
    "black": ["P"] * 8 + ["N", "N", "B", "B", "R", "R", "Q"],
}

# Per-classification accuracy weight (used for the running accuracy %)
_ACCURACY_WEIGHT = {
    "best": 100.0, "great": 94.0, "good": 82.0,
    "inaccuracy": 62.0, "mistake": 38.0, "blunder": 12.0,
}

_PIECE_NAMES = {
    chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
    chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king",
}


def _hanging_piece(board: chess.Board, color: bool) -> dict | None:
    """Most valuable piece of `color` that is attacked and under-defended.

    Heuristic (good enough for beginner alerts): a piece is hanging if the
    opponent attacks it and either nothing defends it, or the cheapest
    attacker is worth less than the piece (a favorable trade for the opponent).
    """
    opp = not color
    worst = None
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if not piece or piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(opp, sq)
        if not attackers:
            continue
        defenders = board.attackers(color, sq)
        value = _PIECE_VALUES.get(piece.piece_type, 0)
        min_attacker = min(
            _PIECE_VALUES.get(board.piece_at(a).piece_type, 99) for a in attackers
        )
        if not defenders or min_attacker < value:
            if worst is None or value > worst["value"]:
                worst = {
                    "square": chess.square_name(sq),
                    "piece": _PIECE_NAMES.get(piece.piece_type, "piece"),
                    "value": value,
                    "defended": bool(defenders),
                }
    return worst


_MOTIF_LABELS = {
    "fork": "fork", "pin": "pin", "skewer": "skewer",
    "back_rank_mate": "back-rank mate", "checkmate": "checkmate",
    "double_check": "double check", "discovered_attack": "discovered attack",
    "promotion": "pawn promotion",
}


def _motif_label(motif: str | None) -> str | None:
    if not motif:
        return None
    return _MOTIF_LABELS.get(motif, motif.replace("_", " "))


def _safe_motifs(board: chess.Board, move: chess.Move) -> list[str]:
    """detect_all_motifs that never raises (returns [] on any failure)."""
    try:
        return motif_detector.detect_all_motifs(board, move)
    except (ValueError, AssertionError, IndexError):
        return []


def _count_material(board: chess.Board) -> dict:
    material = {"white": 0, "black": 0}
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece and piece.piece_type != chess.KING:
            color = "white" if piece.color == chess.WHITE else "black"
            material[color] += _PIECE_VALUES.get(piece.piece_type, 0)
    return material


def _get_captured_pieces(board: chess.Board) -> dict:
    current = {"white": [], "black": []}
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece and piece.piece_type != chess.KING:
            color = "white" if piece.color == chess.WHITE else "black"
            current[color].append(_PIECE_SYMBOLS.get(piece.piece_type, "?"))
    captured = {"white": [], "black": []}
    for color in ("white", "black"):
        remaining = list(current[color])
        for p in _STARTING_PIECES[color]:
            if p in remaining:
                remaining.remove(p)
            else:
                opponent = "black" if color == "white" else "white"
                captured[opponent].append(p)
    return captured


def _format_variation(base_fen: str, sans: list[str]) -> str:
    """Render a SAN line with move numbers from a given base position.

    e.g. starting from Black to move on move 12: "12... Nf6 13. e5 Nd5".
    """
    board = chess.Board(base_fen)
    out: list[str] = []
    for san in sans:
        try:
            move = board.parse_san(san)
        except ValueError:
            break
        num = board.fullmove_number
        if board.turn == chess.WHITE:
            out.append(f"{num}. {san}")
        elif not out:
            out.append(f"{num}... {san}")
        else:
            out.append(san)
        board.push(move)
    return " ".join(out)


def _candidates_and_eval(engine, board: chess.Board, *, depth: int = 18,
                         multipv: int = 3, plies: int = 6) -> dict:
    """Full-strength multipv look: white-perspective eval, label, candidates, line.

    Each candidate carries its SAN, UCI, white-perspective eval, a display label
    and the engine's principal variation (SAN, up to `plies` half-moves). Caller
    holds the engine lock. Safe on terminal positions.
    """
    if board.is_game_over():
        if board.is_checkmate():
            ew = -99.0 if board.turn == chess.WHITE else 99.0
            label = "Checkmate"
        else:
            ew, label = 0.0, "Draw"
        return {"eval_white": ew, "label": label, "candidates": [], "best_line": []}

    lines = engine.analyze_position(board, depth=depth, multipv=multipv)
    white_to_move = board.turn == chess.WHITE
    candidates: list[dict] = []
    for ln in lines:
        pv = ln.get("pv") or []
        if not pv:
            continue
        cp = ln.get("score_cp")
        mate = ln.get("mate")
        if mate is not None:
            white_mate = mate if white_to_move else -mate
            ew = 99.0 if white_mate > 0 else -99.0
            label = f"#{abs(mate)}"
        else:
            white_cp = cp if white_to_move else -cp
            ew = round(white_cp / 100.0, 2)
            label = f"{ew:+.2f}"
        try:
            uci = board.parse_san(pv[0]).uci()
        except ValueError:
            uci = None
        candidates.append({"san": pv[0], "uci": uci, "eval_white": ew,
                           "label": label, "pv": pv[:plies]})

    top = candidates[0] if candidates else {"eval_white": 0.0, "label": "0.00", "pv": []}
    return {"eval_white": top["eval_white"], "label": top["label"],
            "candidates": candidates, "best_line": top["pv"]}


class GameManager:
    """Owns the live game, the engine, and coaching generation."""

    def __init__(self) -> None:
        self.engine = ChessEngine()            # plays moves (weakened to target Elo)
        self.analyzer = ChessEngine()          # ALWAYS full strength — for analysis,
        #                                        coaching, hints, threats, grading.
        self.openings = OpeningsDB()
        self.board = chess.Board()
        self.target_elo = 800
        self.player_color = "white"
        self.move_evals: list[dict] = []  # one entry per player move
        self.eval_score = 0.3  # White-perspective, pawns
        self.coaching = "New game — your move. Fight for the center!"
        self.player_elo = self._read_player_elo()
        self.last_tactics: dict | None = None    # tactical alert for last move
        self.last_context: dict | None = None    # facts for the LLM coach
        self._situation: dict | None = None       # cached analysis of current pos
        self._cards_created = False                # SRS cards made for this game?
        self._session_persisted = False            # game-over result written?
        self.suggested = self._suggested_target_elo()  # adaptive Elo recommendation
        self.coach_source = sap_coach.provider_label()
        # ---- what-if sandbox (analysis board, never touches the live game) ----
        self._sandbox: chess.Board | None = None
        self._sandbox_base_fen: str | None = None
        self._sandbox_base_eval: float = 0.0
        self._sandbox_ctx: dict | None = None      # facts for the sim verdict LLM

    # ---- persistence -----------------------------------------------------
    def _read_player_elo(self) -> int:
        try:
            p = json.loads((_DATA_DIR / "progress.json").read_text("utf-8"))
            return int(p.get("current_elo", p.get("estimated_elo", 400)))
        except (OSError, ValueError, json.JSONDecodeError):
            return 400

    def _level(self) -> str:
        if self.player_elo < 600:
            return "beginner"
        if self.player_elo < 1000:
            return "intermediate-beginner"
        return "intermediate"

    # ---- adaptive difficulty (CLAUDE.md "Difficulty Control") -------------
    def _read_progress(self) -> dict:
        try:
            return json.loads((_DATA_DIR / "progress.json").read_text("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _suggested_target_elo(self, base: int | None = None) -> dict:
        """Recommend an engine Elo from recent accuracy (last 3 games).

        Returns {elo, delta, rationale, based_on}. Falls back to the base
        (current target or stored engine Elo or 800) when there's no history.
        """
        prog = self._read_progress()
        base = base or int(prog.get("engine_elo", getattr(self, "target_elo", 800)))
        history = [h for h in prog.get("accuracy_history", []) if isinstance(h, (int, float))]
        recent = history[-3:]
        if not recent:
            return {"elo": base, "delta": 0, "based_on": 0,
                    "rationale": "No game history yet — starting at your chosen level."}
        avg = sum(recent) / len(recent)
        delta, why = 0, ""
        for threshold, d, reason in _DIFFICULTY_STEPS:
            if avg >= threshold:
                delta, why = d, reason
                break
        suggested = max(100, min(3500, base + delta))
        if suggested == base:
            rationale = f"Recent accuracy {avg:.0f}% — {why}; keeping it at {base}."
        else:
            verb = "bumping up" if delta > 0 else "dialing back"
            rationale = (f"Recent accuracy {avg:.0f}% — {why}; "
                         f"{verb} to {suggested} Elo.")
        return {"elo": suggested, "delta": delta, "based_on": len(recent),
                "rationale": rationale}

    def _on_game_over(self) -> None:
        """Persist this game's result so adaptive difficulty has data (once)."""
        if self._session_persisted:
            return
        self._session_persisted = True
        acc = self._accuracy().get(self.player_color)
        if acc is None:
            return
        prog = self._read_progress()
        hist = [h for h in prog.get("accuracy_history", []) if isinstance(h, (int, float))]
        hist.append(round(float(acc), 1))
        prog["accuracy_history"] = hist[-20:]
        prog["sessions_completed"] = int(prog.get("sessions_completed", 0)) + 1
        prog["engine_elo"] = self.target_elo
        try:
            from datetime import datetime, timezone
            prog["last_session"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            pass
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _DATA_DIR / "progress.tmp"
            tmp.write_text(json.dumps(prog, indent=2), encoding="utf-8")
            os.replace(tmp, _DATA_DIR / "progress.json")
        except OSError:
            pass

    # ---- game lifecycle --------------------------------------------------
    def new_game(self, elo=800, color: str = "white",
                 start_fen: str | None = None) -> dict:
        # elo may be an int, or "auto"/None to use the adaptive recommendation.
        if elo in (None, "auto", "Auto"):
            self.target_elo = int(self._suggested_target_elo()["elo"])
        else:
            self.target_elo = int(elo)
        self.player_color = color if color in ("white", "black") else "white"

        # Optional custom starting position (from the /setup editor). If the
        # FEN is malformed or the position is illegal (missing king, side-not-
        # to-move in check, …) we surface a clear error instead of silently
        # falling back to the standard start position.
        if start_fen:
            try:
                board = chess.Board(start_fen.strip())
            except ValueError as exc:
                return {"error": f"Invalid FEN: {exc}", "illegal": True}
            if not board.is_valid():
                # python-chess's reason gives us things like "the side to move
                # is in check"… surface that to the UI.
                return {
                    "error": f"Position is illegal: {board.status().name.lower().replace('_', ' ')}",
                    "illegal": True,
                }
            self.board = board
            self._custom_start_fen = start_fen.strip()
        else:
            self.board = chess.Board()
            self._custom_start_fen = None

        self.move_evals = []
        self.last_tactics = None
        self.last_context = None
        self._situation = None
        self._cards_created = False
        self._session_persisted = False
        # Recommendation stays anchored to the last COMPLETED level (persisted
        # engine_elo) so it doesn't compound off a value we just auto-applied.
        self.suggested = self._suggested_target_elo()
        self.engine.set_difficulty(self.target_elo)
        self.eval_score = 0.3
        if start_fen:
            self.coaching = (
                f"Custom position loaded at {self.target_elo} Elo — you're "
                f"{self.player_color}. Look at piece activity, king safety, "
                f"and weak squares before you play."
            )
        else:
            self.coaching = (
                f"New game at {self.target_elo} Elo — you're "
                f"{self.player_color}. Control the center and develop your pieces."
            )
        # If it's the ENGINE's turn after setup (either standard start with
        # the player as Black, or a custom FEN where the engine's color is
        # to move), fire its first move now so the player gets to respond.
        engine_is_white = self.player_color == "black"
        side_to_move_is_engine = (
            (self.board.turn == chess.WHITE and engine_is_white)
            or (self.board.turn == chess.BLACK and not engine_is_white)
        )
        if side_to_move_is_engine and not self.board.is_game_over():
            self._engine_reply()
        return self.save()

    # ---- LLM coaching (called by /api/coach, outside the engine lock) -----
    def coaching_context(self) -> dict | None:
        """Snapshot of the facts for the most recent player move, or None."""
        return dict(self.last_context) if self.last_context else None

    def set_coaching(self, text: str) -> None:
        self.coaching = text

    # ---- current-position analysis (for proactive coaching / hints) ------
    def _analyze_current(self) -> dict | None:
        """Full-strength look at the position with the player to move.

        Returns candidate moves, the opponent's main threat, and any of the
        player's pieces hanging right now. Cached per-FEN. Caller holds the
        engine lock. Returns None when the game is over.
        """
        board = self.board
        if board.is_game_over():
            return None
        fen = board.fen()
        if self._situation and self._situation.get("fen") == fen:
            return self._situation

        lines = self.analyzer.analyze_position(board, depth=18, multipv=3)
        candidates = []
        for ln in lines:
            pv = ln.get("pv") or []
            if not pv:
                continue
            candidates.append({
                "san": pv[0],
                "score_cp": ln.get("score_cp"),
                "mate": ln.get("mate"),
                "line": " ".join(pv[:6]),
            })
        best_uci = None
        best_motif = None
        if candidates:
            try:
                best_move_obj = board.parse_san(candidates[0]["san"])
                best_uci = best_move_obj.uci()
                best_motif = motif_detector.detect_motif(board, best_move_obj)
            except (ValueError, AssertionError):
                pass

        sit = {
            "fen": fen,
            "candidates": candidates,
            "best_uci": best_uci,
            "best_motif": best_motif,
            "threat": self._threat(board),
            "your_hanging": _hanging_piece(board, board.turn),
            "turn": "white" if board.turn == chess.WHITE else "black",
        }
        self._situation = sit
        return sit

    def _threat(self, board: chess.Board) -> dict | None:
        """What the opponent threatens if the player does nothing (null move).

        Uses the full-strength analyzer and classifies the threat (mate / check
        / capture / positional initiative) with a human description and line —
        not just "is it a capture".
        """
        if board.is_check():
            return None
        probe = board.copy()
        try:
            probe.push(chess.Move.null())
        except (ValueError, AssertionError):
            return None
        lines = self.analyzer.analyze_position(probe, depth=16, multipv=1)
        if not lines or not lines[0].get("pv"):
            return None
        ln = lines[0]
        pv = ln["pv"]
        san = pv[0]
        mate = ln.get("mate")
        cp = ln.get("score_cp")  # opponent's perspective (probe.turn == opponent)
        line = _format_variation(probe.fen(), pv[:6])
        gives_check = is_cap = False
        value, victim_name = 0, "piece"
        try:
            mv = probe.parse_san(san)
            gives_check = probe.gives_check(mv)
            is_cap = probe.is_capture(mv)
            if probe.is_en_passant(mv):
                value, victim_name = 1, "pawn"
            else:
                victim = probe.piece_at(mv.to_square)
                if victim:
                    value = _PIECE_VALUES.get(victim.piece_type, 0)
                    victim_name = _PIECE_NAMES.get(victim.piece_type, "piece")
        except ValueError:
            pass

        if mate is not None and mate > 0:
            typ = "mate"
            desc = f"a forced mate (#{mate}) beginning with {san} ({line})"
        elif is_cap and value >= 2:
            typ = "win-material"
            chk = " with check" if gives_check else ""
            desc = f"{san}{chk}, winning your {victim_name} ({line})"
        elif cp is not None and cp >= 150:
            typ = "initiative"
            desc = (f"{san}, gaining a strong advantage "
                    f"(~{cp/100:.1f} in their favour): {line}")
        elif gives_check:
            typ = "check"
            desc = f"the checking sequence {san}+ ({line})"
        else:
            typ = "quiet"
            desc = f"{san} ({line})"
        return {"san": san, "type": typ, "value": value, "mate": mate,
                "score_cp": cp, "line": line, "desc": desc}

    def situation_facts(self) -> dict:
        """Facts about the CURRENT position for hints / ask / next-move coaching.

        Caller must hold the engine lock (uses the engine).
        """
        board = self.board
        op = self._opening()
        facts = {
            "level": self._level(),
            "elo": self.player_elo,
            "your_color": self.player_color,
            "opening": f"{op['name']} ({op['eco']})" if op else None,
            "move_number": board.fullmove_number,
            "turn": "white" if board.turn == chess.WHITE else "black",
            "eval_white": self.eval_score,
            "recent": self._recent_moves(),
            "fen": board.fen(),
            "game_over": board.is_game_over(),
            "is_check": board.is_check(),
        }
        sit = self._analyze_current()
        if sit:
            facts["candidates"] = sit["candidates"]
            facts["threat"] = sit["threat"]
            facts["your_hanging"] = sit["your_hanging"]
            facts["best_uci"] = sit["best_uci"]
            facts["best_motif"] = sit.get("best_motif")
        return facts

    def best_move_squares(self) -> dict | None:
        sit = self._analyze_current()
        if not sit or not sit.get("best_uci"):
            return None
        uci = sit["best_uci"]
        return {"from": uci[0:2], "to": uci[2:4],
                "san": sit["candidates"][0]["san"] if sit["candidates"] else None}

    # ---- post-game review ------------------------------------------------
    def _fen_before_ply(self, ply: int) -> str:
        """FEN of the position right before the move at the given half-move."""
        temp = chess.Board()
        for i, m in enumerate(self.board.move_stack):
            if i >= ply:
                break
            temp.push(m)
        return temp.fen()

    def build_review(self, max_moments: int = 3, cp_threshold: int = 80) -> dict | None:
        """Summarize a finished game and pick the most instructive moments."""
        if not self.board.is_game_over():
            return None
        counts: dict[str, int] = {}
        for e in self.move_evals:
            counts[e["classification"]] = counts.get(e["classification"], 0) + 1

        moments = []
        for e in sorted(self.move_evals, key=lambda x: x["cp_loss"], reverse=True):
            if e["cp_loss"] < cp_threshold or len(moments) >= max_moments:
                break
            fen_before = self._fen_before_ply(e["ply"])
            played_uci = best_uci = None
            try:
                b = chess.Board(fen_before)
                played_uci = b.parse_san(e["move_san"]).uci()
                best_uci = b.parse_san(e["best_move_san"]).uci()
            except ValueError:
                pass
            moments.append({
                "ply": e["ply"],
                "move_number": e["ply"] // 2 + 1,
                "color": e["color"],
                "fen_before": fen_before,
                "played": e["move_san"],
                "played_uci": played_uci,
                "best": e["best_move_san"],
                "best_uci": best_uci,
                "best_line": " ".join(e.get("best_line", [])[:5]),
                "cp_loss": e["cp_loss"],
                "classification": e["classification"],
            })

        # Order moments chronologically for the walkthrough.
        moments.sort(key=lambda m: m["ply"])

        summary = {
            "result": self.board.result(),
            "result_text": self._result_text(),
            "player_color": self.player_color,
            "target_elo": self.target_elo,
            "level": self._level(),
            "accuracy": self._accuracy().get(self.player_color, 0.0),
            "counts": counts,
            "total_moves": len(self.move_evals),
            "blunders": counts.get("blunder", 0),
            "mistakes": counts.get("mistake", 0),
            "inaccuracies": counts.get("inaccuracy", 0),
        }
        return {"summary": summary, "moments": moments}

    def create_review_cards(self, cp_threshold: int = 80) -> dict:
        """Create SRS flashcards for the player's significant mistakes (once)."""
        if not self.board.is_game_over() or self._cards_created:
            return {"created": 0, "already": self._cards_created}
        from scripts.srs import SRSManager
        srs = SRSManager(str(_DATA_DIR / "srs_cards.json"))
        created = 0
        for e in self.move_evals:
            if e["cp_loss"] < cp_threshold or e["color"] != self.player_color:
                continue
            srs.add_card(
                fen=self._fen_before_ply(e["ply"]),
                player_move=e["move_san"],
                best_move=e["best_move_san"],
                cp_loss=e["cp_loss"],
                classification=e["classification"],
                explanation=(f"You played {e['move_san']} ({e['classification']}, "
                             f"-{e['cp_loss']}cp). The engine preferred "
                             f"{e['best_move_san']}."),
            )
            created += 1
        self._cards_created = True
        return {"created": created}

    def _opening(self) -> dict | None:
        uci = [m.uci() for m in self.board.move_stack]
        if not uci:
            return None
        match = self.openings.identify_opening(uci)
        if not match:
            return None
        return {
            "eco": match["eco"], "name": match["name"],
            "family": match.get("family", ""),
            "moves_matched": match.get("moves_matched", 0),
        }

    def _refresh_eval(self) -> None:
        """White-perspective evaluation in pawns from a quick analysis."""
        if self.board.is_game_over():
            if self.board.is_checkmate():
                self.eval_score = -99.0 if self.board.turn == chess.WHITE else 99.0
            else:
                self.eval_score = 0.0
            return
        lines = self.analyzer.analyze_position(self.board, depth=16, multipv=1)
        if not lines:
            return
        cp = lines[0]["score_cp"]
        mate = lines[0]["mate"]
        # score_cp is from side-to-move perspective; convert to White's.
        white_cp = cp if self.board.turn == chess.WHITE else -cp
        if mate is not None:
            self.eval_score = 99.0 if white_cp > 0 else -99.0
        else:
            self.eval_score = round(white_cp / 100.0, 2)

    # ---- moves -----------------------------------------------------------
    def player_move(self, frm: str, to: str, promotion: str | None = None) -> dict:
        if self.board.is_game_over():
            return self.save(error="The game is already over. Start a new game.")
        uci = f"{frm}{to}" + (promotion or "")
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return self.save(error=f"Could not parse move {uci}.")
        # Auto-promote to queen if a promotion is required but none given.
        if move not in self.board.legal_moves and promotion is None:
            promo_try = chess.Move.from_uci(uci + "q")
            if promo_try in self.board.legal_moves:
                move = promo_try
        if move not in self.board.legal_moves:
            return self.save(error="illegal", illegal=True)

        ev = self.analyzer.evaluate_move(self.board, move)  # full-strength grading
        ply = len(self.board.move_stack)
        mover = "white" if self.board.turn == chess.WHITE else "black"
        mover_bool = self.board.turn
        move_number = self.board.fullmove_number
        recent = self._recent_moves()
        # Named tactical motifs (computed on the PRE-move board) — grounds the
        # coach's explanations instead of letting it guess "fork/pin".
        played_motifs = _safe_motifs(self.board, move)
        best_motif = None
        try:
            best_motif = motif_detector.detect_motif(
                self.board, self.board.parse_san(ev.best_move_san))
        except (ValueError, AssertionError):
            best_motif = None
        self.board.push(move)
        self.move_evals.append({
            "move_san": ev.move_san,
            "best_move_san": ev.best_move_san,
            "cp_loss": ev.cp_loss,
            "classification": ev.classification,
            "is_best": ev.is_best,
            "best_line": ev.best_line,
            "ply": ply,
            "color": mover,
        })
        # Tactical scan at the position right after the player's move (the
        # moment an "undo" is most useful), before the engine punishes it.
        self.last_tactics = self._tactical_alert(ev, mover_bool, best_motif)
        self.coaching = self._coach(ev)  # instant heuristic placeholder
        self._refresh_eval()

        # Engine replies (unless the player's move ended the game).
        engine_san = None
        if not self.board.is_game_over():
            engine_san = self._engine_reply()
        if self.board.is_game_over():
            self._on_game_over()

        # Stash everything the LLM coach needs (consumed by /api/coach).
        op = self._opening()
        self.last_context = {
            "level": self._level(),
            "elo": self.player_elo,
            "opening": f"{op['name']} ({op['eco']})" if op else None,
            "move_number": move_number,
            "mover": mover,
            "move_san": ev.move_san,
            "classification": ev.classification,
            "cp_loss": ev.cp_loss,
            "is_best": ev.is_best,
            "best_move_san": ev.best_move_san,
            "best_line": " ".join(ev.best_line[:6]),
            "eval_before": ev.eval_before,
            "eval_after": ev.eval_after,
            "tactical": self.last_tactics["llm"] if self.last_tactics else None,
            "played_motifs": played_motifs,
            "best_motif": best_motif if not ev.is_best else None,
            "engine_reply": engine_san,
            "game_over": self.board.is_game_over(),
            "result_text": self._result_text(),
            "recent": recent,
            "fen": self.board.fen(),
        }
        return self.save(player_eval=self._eval_payload(ev), engine_move=engine_san)

    def _recent_moves(self) -> str:
        sans = []
        temp = chess.Board()
        for m in self.board.move_stack:
            sans.append(temp.san(m))
            temp.push(m)
        return " ".join(sans[-6:])

    def _result_text(self) -> str | None:
        if not self.board.is_game_over():
            return None
        if self.board.is_checkmate():
            winner = "Black" if self.board.turn == chess.WHITE else "White"
            return f"checkmate, {winner} wins"
        if self.board.is_stalemate():
            return "draw by stalemate"
        return f"draw ({self.board.result()})"

    def _tactical_alert(self, ev, mover_bool: bool,
                        best_motif: str | None = None) -> dict | None:
        """Tactical alert for a bad move, VERIFIED against the engine.

        Anti-credulity: rather than trust the static hanging-piece heuristic
        (which can name the wrong piece), we ask the analyzer for the
        opponent's actual best reply at this position. If that reply captures
        one of the player's pieces, we name THAT engine-true piece/square. If
        the refutation is positional (no capture), we avoid the false "your X
        hangs" claim and show a generic, engine-grounded warning instead.
        """
        if ev.cp_loss < 120:
            return None
        sev = "blunder" if ev.cp_loss >= 300 else "mistake"
        best_label = _motif_label(best_motif)
        missed = (f" The stronger {ev.best_move_san} sets up a {best_label}."
                  if best_label else "")

        # Engine-verify: what does the opponent actually best play now?
        ref_san = None
        verified = None
        try:
            ref = self.analyzer.analyze_position(self.board, depth=12, multipv=1)
            if ref and ref[0].get("pv"):
                ref_san = ref[0]["pv"][0]
                mv = self.board.parse_san(ref_san)
                if self.board.is_capture(mv) and not self.board.is_en_passant(mv):
                    victim = self.board.piece_at(mv.to_square)
                    if victim and victim.color == mover_bool:
                        verified = {
                            "piece": _PIECE_NAMES.get(victim.piece_type, "piece"),
                            "square": chess.square_name(mv.to_square),
                            "value": _PIECE_VALUES.get(victim.piece_type, 0),
                        }
        except (ValueError, AssertionError, IndexError):
            pass

        if verified:
            p, sq = verified["piece"], verified["square"]
            return {
                "severity": sev,
                "text": f"Your {p} on {sq} can be taken by {ref_san} — that loses material.",
                "llm": (f"the student's move hangs their {p} on {sq}; the engine "
                        f"refutes with {ref_san}.{missed}"),
                "best_move": ev.best_move_san, "verified": True,
            }

        # No direct winning capture — fall back to the static hint only if it
        # agrees, otherwise stay generic so we never assert a wrong piece.
        hung = _hanging_piece(self.board, mover_bool)
        if hung and hung["value"] >= 3 and ev.cp_loss >= 150:
            p, sq = hung["piece"], hung["square"]
            return {
                "severity": sev,
                "text": f"That move loses material — your {p} on {sq} looks vulnerable.",
                "llm": (f"the move loses ~{ev.cp_loss}cp; the student's {p} on {sq} "
                        f"is loose.{missed}"),
                "best_move": ev.best_move_san, "verified": False,
            }
        if ev.cp_loss >= 300:
            return {
                "severity": "blunder",
                "text": (f"That move gives up about {ev.cp_loss//100} points — "
                         f"there was something much stronger."),
                "llm": (f"the move loses ~{ev.cp_loss}cp; no single piece simply "
                        f"hangs, so it's a positional or tactical blunder.{missed}"),
                "best_move": ev.best_move_san, "verified": False,
            }
        return None

    def _engine_reply(self) -> str:
        move = self.engine.get_engine_move(self.board)
        san = self.board.san(move)
        self.board.push(move)
        self._refresh_eval()
        if self.board.is_game_over():
            self.coaching = self._coach_gameover()
            self._on_game_over()
        return san

    # ---- coaching --------------------------------------------------------
    def _coach(self, ev) -> str:
        """Beginner-aware coaching text keyed off the move classification."""
        lvl = self._level()
        op = self._opening()
        book = f" You're in book: **{op['name']}** ({op['eco']})." if op else ""
        best = ev.best_move_san
        line = " ".join(ev.best_line[:4])

        c = ev.classification
        if c == "best":
            return f"✓ **{ev.move_san}** — best move! Excellent choice.{book}"
        if c == "great":
            return (f"✓ **{ev.move_san}** — great move, you keep the advantage."
                    f" (Only {ev.cp_loss}cp from the top pick.){book}")
        if c == "good":
            return (f"• **{ev.move_san}** is solid. A touch sharper was "
                    f"**{best}** ({line}).{book}")
        if c == "inaccuracy":
            return (f"?! **{ev.move_san}** is a little inaccurate "
                    f"(−{ev.cp_loss}cp). **{best}** was cleaner: {line}. "
                    f"Think about what your opponent threatens next.")
        if c == "mistake":
            tip = ("In simple terms: that move lets your opponent improve their "
                   "position. " if lvl == "beginner" else "")
            return (f"? **{ev.move_san}** is a mistake (−{ev.cp_loss}cp). {tip}"
                    f"Stronger was **{best}**: {line}. Before moving, check: is "
                    f"anything of mine hanging? what does my opponent want?")
        # blunder
        return (f"?? **{ev.move_san}** is a blunder (−{ev.cp_loss}cp)! "
                f"The much better move was **{best}** ({line}). "
                f"Click **Undo** to take it back and try again — look for "
                f"hanging pieces and captures first.")

    def _coach_gameover(self) -> str:
        if self.board.is_checkmate():
            winner = "Black" if self.board.turn == chess.WHITE else "White"
            you = (self.player_color.capitalize() == winner)
            if you:
                return "🏆 Checkmate — you win! Well played. Start a new game to keep climbing."
            return ("Checkmate — the engine got you this time. Hit New Game and "
                    "we'll go again; every loss is a lesson.")
        if self.board.is_stalemate():
            return "Stalemate — it's a draw. No legal moves but not in check."
        if self.board.is_insufficient_material():
            return "Draw — insufficient material to checkmate."
        return "Game over — it's a draw."

    def _eval_payload(self, ev) -> dict:
        return {
            "move_san": ev.move_san,
            "best_move_san": ev.best_move_san,
            "cp_loss": ev.cp_loss,
            "classification": ev.classification,
            "is_best": ev.is_best,
            "best_line": ev.best_line[:5],
        }

    # ---- state ----------------------------------------------------------
    def _accuracy(self) -> dict:
        acc = {"white": [], "black": []}
        for ev in self.move_evals:
            acc[ev["color"]].append(_ACCURACY_WEIGHT.get(ev["classification"], 50.0))
        out = {}
        for color in ("white", "black"):
            vals = acc[color]
            out[color] = round(sum(vals) / len(vals), 1) if vals else 0.0
        return out

    def state_dict(self) -> dict:
        board = self.board
        move_list = []
        temp = chess.Board()
        for m in board.move_stack:
            move_list.append(temp.san(m))
            temp.push(m)
        last_move_san = move_list[-1] if move_list else None
        last_move_uci = board.move_stack[-1].uci() if board.move_stack else None
        check_square = None
        if board.is_check():
            king_sq = board.king(board.turn)
            if king_sq is not None:
                check_square = chess.square_name(king_sq)
        annotations = [
            {"move": e["move_san"], "classification": e["classification"],
             "cp_loss": e["cp_loss"], "ply": e["ply"], "color": e["color"]}
            for e in self.move_evals
        ]
        legal = {}
        for m in board.legal_moves:
            legal.setdefault(chess.square_name(m.from_square), []).append(
                chess.square_name(m.to_square)
            )
        return {
            "fen": board.fen(),
            "board_display": str(board),
            "move_list": move_list,
            "last_move_san": last_move_san,
            "last_move_uci": last_move_uci,
            "check_square": check_square,
            "eval_score": self.eval_score,
            "player_color": self.player_color,
            "target_elo": self.target_elo,
            "player_elo": self.player_elo,
            "is_game_over": board.is_game_over(),
            "result": board.result() if board.is_game_over() else None,
            "accuracy": self._accuracy(),
            "current_opening": self._opening(),
            "material": _count_material(board),
            "captured_pieces": _get_captured_pieces(board),
            "is_check": board.is_check(),
            "is_checkmate": board.is_checkmate(),
            "is_stalemate": board.is_stalemate(),
            "move_annotations": annotations,
            "coaching": self.coaching,
            "tactical_alert": self.last_tactics,
            "coach_source": self.coach_source,
            "suggested_elo": self.suggested.get("elo"),
            "suggested_delta": self.suggested.get("delta"),
            "suggested_rationale": self.suggested.get("rationale"),
            "legal_moves_map": legal,
            "turn": "white" if board.turn == chess.WHITE else "black",
        }

    def undo(self) -> dict:
        """Take back the player's last move (and the engine reply before it)."""
        # Pop engine reply (if the engine moved last) then the player's move.
        if self.board.move_stack:
            # If it's the player's turn now, the last move was the engine's.
            if self._is_engine_turn_after_pop():
                self.board.pop()
        if self.board.move_stack:
            self.board.pop()
        if self.move_evals:
            self.move_evals.pop()
        self.last_tactics = None
        self.last_context = None
        self._refresh_eval()
        self.coaching = "Took the move back. Try a different idea — your move."
        return self.save()

    def _is_engine_turn_after_pop(self) -> bool:
        """True if the last move on the stack was made by the engine."""
        engine_color = chess.BLACK if self.player_color == "white" else chess.WHITE
        # The side that just moved is the opposite of board.turn.
        side_that_moved = not self.board.turn
        return side_that_moved == engine_color

    # ---- what-if sandbox -------------------------------------------------
    # A scratch board seeded from the live position. The player may push moves
    # for EITHER side to explore "what happens if…", see the engine's eval and
    # predicted continuation at every ply, and get a verdict — all without
    # changing the real game. Caller holds the engine lock for every method.
    def sandbox_start(self) -> dict:
        """Open the sandbox from the current live position."""
        self._sandbox = chess.Board(self.board.fen())
        self._sandbox_base_fen = self.board.fen()
        self._sandbox_ctx = None
        info = _candidates_and_eval(self.analyzer, self._sandbox)
        self._sandbox_base_eval = info["eval_white"]
        return self._sandbox_state(info=info)

    def sandbox_exit(self) -> dict:
        self._sandbox = None
        self._sandbox_base_fen = None
        self._sandbox_ctx = None
        return {"active": False, "exited": True}

    def sandbox_reset(self) -> dict:
        """Rewind the sandbox to the live position it was opened from."""
        if self._sandbox is None or self._sandbox_base_fen is None:
            return self.sandbox_start()
        self._sandbox = chess.Board(self._sandbox_base_fen)
        self._sandbox_ctx = None
        return self._sandbox_state()

    def sandbox_undo(self) -> dict:
        """Take back the last move tried in the sandbox."""
        if self._sandbox is None:
            return self.sandbox_start()
        if self._sandbox.move_stack:
            self._sandbox.pop()
        self._sandbox_ctx = None
        return self._sandbox_state()

    def sandbox_move(self, frm: str, to: str, promotion: str | None = None) -> dict:
        """Try a move in the sandbox; return the new state + a move simulation."""
        if self._sandbox is None:
            self.sandbox_start()
        board = self._sandbox
        if board.is_game_over():
            return self._sandbox_state(error="This line is already over — reset to keep exploring.")
        uci = f"{frm}{to}" + (promotion or "")
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return self._sandbox_state(error=f"Could not parse move {uci}.")
        if move not in board.legal_moves and promotion is None:
            promo_try = chess.Move.from_uci(uci + "q")
            if promo_try in board.legal_moves:
                move = promo_try
        if move not in board.legal_moves:
            out = self._sandbox_state()
            out["illegal"] = True
            return out

        fen_before = board.fen()
        mover = "white" if board.turn == chess.WHITE else "black"
        mover_bool = board.turn
        ev = self.engine.evaluate_move(board, move)
        board.push(move)
        hanging = _hanging_piece(board, mover_bool)
        info = _candidates_and_eval(self.analyzer, board)

        white = mover_bool == chess.WHITE
        eb_white = ev.eval_before if white else -ev.eval_before
        ea_white = ev.eval_after if white else -ev.eval_after
        predicted = _format_variation(board.fen(), info["best_line"])
        sim = {
            "move_san": ev.move_san,
            "mover": mover,
            "cp_loss": ev.cp_loss,
            "classification": ev.classification,
            "is_best": ev.is_best,
            "best_move_san": ev.best_move_san,
            "best_line": ev.best_line[:6],
            "best_line_display": _format_variation(fen_before, ev.best_line[:6]),
            "eval_before_white": round(eb_white, 2),
            "eval_after_white": round(ea_white, 2),
            "swing_cp": round((ev.eval_after - ev.eval_before) * 100),
            "hanging": hanging,
        }
        self._sandbox_ctx = {
            "level": self._level(),
            "elo": self.player_elo,
            "mover": mover,
            "move_san": ev.move_san,
            "classification": ev.classification,
            "cp_loss": ev.cp_loss,
            "is_best": ev.is_best,
            "best_move_san": ev.best_move_san,
            "best_line": " ".join(ev.best_line[:6]),
            "eval_before": ev.eval_before,
            "eval_after": ev.eval_after,
            "hanging": hanging,
            "predicted_line": predicted,
            "recent": self._recent_moves(),
            "fen": board.fen(),
        }
        return self._sandbox_state(info=info, sim=sim)

    def sandbox_context(self) -> dict | None:
        """Facts for the LLM verdict on the most recent sandbox move (or None)."""
        return dict(self._sandbox_ctx) if self._sandbox_ctx else None

    def _sandbox_result_text(self) -> str | None:
        board = self._sandbox
        if board is None or not board.is_game_over():
            return None
        if board.is_checkmate():
            winner = "Black" if board.turn == chess.WHITE else "White"
            return f"checkmate, {winner} wins"
        if board.is_stalemate():
            return "draw by stalemate"
        return f"draw ({board.result()})"

    def _sandbox_state(self, *, info: dict | None = None,
                       sim: dict | None = None, error: str | None = None) -> dict:
        board = self._sandbox
        if board is None:
            return {"active": False, "error": error}
        if info is None:
            info = _candidates_and_eval(self.analyzer, board)
        base_fen = self._sandbox_base_fen or board.fen()

        sans: list[str] = []
        temp = chess.Board(base_fen)
        for m in board.move_stack:
            sans.append(temp.san(m))
            temp.push(m)

        legal: dict[str, list[str]] = {}
        for m in board.legal_moves:
            legal.setdefault(chess.square_name(m.from_square), []).append(
                chess.square_name(m.to_square))

        check_square = None
        if board.is_check():
            king_sq = board.king(board.turn)
            if king_sq is not None:
                check_square = chess.square_name(king_sq)

        cur_fen = board.fen()
        candidates = [{
            "san": c["san"], "uci": c.get("uci"),
            "eval_white": c["eval_white"], "label": c["label"],
            "line": _format_variation(cur_fen, c["pv"]),
        } for c in info["candidates"]]

        out = {
            "active": True,
            "fen": cur_fen,
            "turn": "white" if board.turn == chess.WHITE else "black",
            "base_fen": base_fen,
            "line_san": sans,
            "line_display": _format_variation(base_fen, sans),
            "ply_from_base": len(board.move_stack),
            "legal_moves_map": legal,
            "last_move_uci": board.move_stack[-1].uci() if board.move_stack else None,
            "check_square": check_square,
            "is_game_over": board.is_game_over(),
            "result_text": self._sandbox_result_text(),
            "eval_white": info["eval_white"],
            "eval_label": info["label"],
            "eval_base": self._sandbox_base_eval,
            "predicted_line": _format_variation(cur_fen, info["best_line"]),
            "candidates": candidates,
            "sim": sim,
        }
        if error:
            out["error"] = error
        return out

    def save(self, **extra) -> dict:
        state = self.state_dict()
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _DATA_DIR / "current_game.tmp"
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, _STATE_PATH)
        out = dict(state)
        out.update(extra)
        return out
