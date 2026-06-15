"""Tactics-trainer: load tactical puzzles, validate moves, track progress.

Self-contained; uses the existing JSON puzzle files under `puzzles/` (one
file per theme). Multi-move puzzles auto-play scripted opponent replies
between the player's moves, so the player only has to find their own moves.

Persisted state: `data/tactics_progress.json` (totals + per-theme stats +
recent history). No engine call is required for validation — the puzzle's
canonical `solution_moves` are the source of truth.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Optional

import chess

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PUZZLES_DIR = _REPO_ROOT / "puzzles"
_PROGRESS_FILE = _REPO_ROOT / "data" / "tactics_progress.json"

# Pretty labels for known themes (filename stem -> human label).
# Order roughly tracks pedagogical progression: starter motifs first, advanced
# combinations later. The trainer sorts dropdowns alphabetically by stem, so
# this map is just for display strings.
_THEME_LABELS = {
    # Starter motifs (curated, hand-built)
    "back-rank":          "Back-Rank Mate",
    "beginner-endgames":  "Beginner Endgames",
    "checkmate-patterns": "Checkmate Patterns",
    "forks":              "Forks",
    "from-games":         "Real-game Tactics",
    "opening-moves":      "Opening Moves",
    "opening-traps":      "Opening Traps",
    "pins":               "Pins",
    "skewers":            "Skewers",
    # Core tactical motifs (Lichess-mined, see scripts/build_lichess_tactics.py)
    "discovered-attack":  "Discovered Attack",
    "double-check":       "Double Check",
    "deflection":         "Deflection",
    "decoy":              "Decoy / Attraction",
    "x-ray":              "X-Ray Attack",
    "interference":       "Interference",
    "zwischenzug":        "Zwischenzug (Intermezzo)",
    "zugzwang":           "Zugzwang",
    "trapped-piece":      "Trapped Piece",
    "sacrifice":          "Sacrifice",
    "clearance":          "Clearance",
    # Mating combinations
    "smothered-mate":     "Smothered Mate",
    "mate-in-2":          "Mate in 2",
    "mate-in-3":          "Mate in 3",
    # Strategic-tactical attacks
    "kingside-attack":    "Kingside Attack",
    "queenside-attack":   "Queenside Attack",
}

# Keep one in-memory copy of the puzzle bank so /api/tactics/new is instant.
_PUZZLES_CACHE: dict[str, list[dict]] | None = None
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Puzzle bank loading + selection

def _load_all() -> dict[str, list[dict]]:
    """Read every puzzles/*.json once and cache it. Filename stem == theme."""
    global _PUZZLES_CACHE
    with _CACHE_LOCK:
        if _PUZZLES_CACHE is not None:
            return _PUZZLES_CACHE
        cache: dict[str, list[dict]] = {}
        if _PUZZLES_DIR.exists():
            for fp in sorted(_PUZZLES_DIR.glob("*.json")):
                try:
                    data = json.loads(fp.read_text())
                except (OSError, ValueError):
                    continue
                if not isinstance(data, list):
                    continue
                cache[fp.stem] = [p for p in data if _looks_like_puzzle(p)]
        _PUZZLES_CACHE = cache
        return cache


def _looks_like_puzzle(p: object) -> bool:
    return (
        isinstance(p, dict)
        and isinstance(p.get("fen"), str)
        and isinstance(p.get("solution_moves"), list)
        and len(p["solution_moves"]) >= 1
    )


def list_themes() -> list[dict]:
    """Return a manifest the UI can render in a dropdown."""
    out = []
    for theme, puzzles in _load_all().items():
        if not puzzles:
            continue
        ratings = [
            int(p["difficulty_rating"])
            for p in puzzles
            if isinstance(p.get("difficulty_rating"), (int, float))
        ]
        out.append({
            "theme": theme,
            "label": _THEME_LABELS.get(theme, theme.replace("-", " ").title()),
            "count": len(puzzles),
            "min_rating": min(ratings) if ratings else None,
            "max_rating": max(ratings) if ratings else None,
        })
    out.sort(key=lambda r: r["theme"])
    return out


def pick_puzzle(theme: Optional[str] = None,
                max_rating: Optional[int] = None,
                exclude_ids: Optional[list[str]] = None) -> Optional[dict]:
    """Return one puzzle matching the filter, with `id` and `theme` injected."""
    bank = _load_all()
    if theme and theme != "mixed":
        if theme not in bank:
            return None
        candidates: list[tuple[str, dict]] = [(theme, p) for p in bank[theme]]
    else:
        candidates = [(t, p) for t, ps in bank.items() for p in ps]
    if max_rating is not None:
        candidates = [
            (t, p) for (t, p) in candidates
            if (p.get("difficulty_rating") or 0) <= max_rating
        ]
    if not candidates:
        return None
    exclude = set(exclude_ids or [])
    if exclude:
        filtered = [(t, p) for (t, p) in candidates
                    if _puzzle_id(t, p) not in exclude]
        # If everything has been seen, recycle the unfiltered set.
        candidates = filtered or candidates
    theme_pick, puzzle = random.choice(candidates)
    return _decorate(theme_pick, puzzle)


def _puzzle_id(theme: str, p: dict) -> str:
    """Stable id derived from the FEN + first solution move (no UUID needed)."""
    sol = p.get("solution_moves") or [""]
    fen = p.get("fen", "")
    return f"{theme}:{fen[:24]}:{sol[0]}"


def _decorate(theme: str, p: dict) -> dict:
    out = dict(p)
    out["id"] = _puzzle_id(theme, p)
    out["theme"] = theme
    out["theme_label"] = _THEME_LABELS.get(theme, theme.replace("-", " ").title())
    return out


# ---------------------------------------------------------------------------
# In-progress puzzle session

class TacticsSession:
    """Tracks one puzzle in progress: the board, remaining solution, attempts.

    The puzzle's `solution_moves` alternates player/opponent starting with
    the player (the side to move in `fen`). Even indices (0, 2, 4, ...) are
    player moves, odd indices are scripted opponent replies that we apply
    automatically once the player gets the previous move right.
    """

    def __init__(self) -> None:
        self.puzzle: Optional[dict] = None
        self.board: Optional[chess.Board] = None
        self.move_idx: int = 0          # next solution_moves index to play
        self.attempts: int = 0           # wrong tries on the current ply
        self.hints_used: int = 0         # max hint level used in this puzzle
        self.solved: bool = False
        self.failed: bool = False
        self.recorded: bool = False      # outcome already written to progress

    # ---- lifecycle ------------------------------------------------------

    def start(self, puzzle: dict) -> dict:
        self.puzzle = puzzle
        self.board = chess.Board(puzzle["fen"])
        self.move_idx = 0
        self.attempts = 0
        self.hints_used = 0
        self.solved = False
        self.failed = False
        self.recorded = False
        return self.state()

    def clear(self) -> None:
        self.__init__()

    # ---- state snapshot for the UI -------------------------------------

    def state(self) -> dict:
        if not self.puzzle or self.board is None:
            return {"active": False}
        sol = self.puzzle.get("solution_moves") or []
        total_player_moves = (len(sol) + 1) // 2  # ceil for odd length
        return {
            "active": True,
            "puzzle_id": self.puzzle["id"],
            "theme": self.puzzle["theme"],
            "theme_label": self.puzzle.get("theme_label"),
            "fen": self.board.fen(),
            "side_to_move": "white" if self.board.turn == chess.WHITE else "black",
            "player_color": _player_color_for(self.puzzle),
            "motif": self.puzzle.get("motif"),
            "difficulty": self.puzzle.get("difficulty"),
            "difficulty_rating": self.puzzle.get("difficulty_rating"),
            "explanation": self.puzzle.get("explanation"),
            "total_player_moves": total_player_moves,
            "current_player_move_index": min(
                self.move_idx // 2 + 1, total_player_moves),
            "attempts": self.attempts,
            "hints_used": self.hints_used,
            "solved": self.solved,
            "failed": self.failed,
            "legal_moves_map": _legal_moves_map(self.board),
            "last_move_uci": self._last_move_uci(),
            "check_square": _check_square(self.board),
            "is_player_turn": (
                not self.solved and not self.failed
                and (self.board.turn == chess.WHITE) == (
                    _player_color_for(self.puzzle) == "white")
            ),
        }

    def _last_move_uci(self) -> Optional[str]:
        if not self.board or not self.board.move_stack:
            return None
        return self.board.move_stack[-1].uci()

    # ---- player action: submit a move ----------------------------------

    def submit_move(self, frm: str, to: str, promo: Optional[str]) -> dict:
        """Validate one player move; auto-play the scripted reply on success."""
        if not self.puzzle or self.board is None:
            return {"active": False, "error": "no active puzzle"}
        if self.solved or self.failed:
            return {**self.state(), "correct": False,
                    "error": "puzzle already finished"}
        sol = self.puzzle.get("solution_moves") or []
        if self.move_idx >= len(sol):
            self.solved = True
            return {**self.state(), "correct": True, "finished": True}

        # Build + sanity-check the candidate move.
        player_uci = f"{frm}{to}{promo or ''}".strip().lower()
        try:
            move = chess.Move.from_uci(player_uci)
        except ValueError:
            return {**self.state(), "correct": False, "error": "invalid uci"}
        if move not in self.board.legal_moves:
            # Try without promotion if caller forgot it on a pawn promotion.
            if not promo:
                for cand in self.board.legal_moves:
                    if (cand.from_square == move.from_square
                            and cand.to_square == move.to_square
                            and cand.promotion):
                        move = cand
                        break
            if move not in self.board.legal_moves:
                return {**self.state(), "correct": False, "error": "illegal"}

        # Compare against the puzzle's expected move.
        expected_uci = sol[self.move_idx]
        try:
            expected = chess.Move.from_uci(expected_uci)
        except ValueError:
            expected = None
        if expected is None or move != expected:
            self.attempts += 1
            return {**self.state(), "correct": False, "expected": None}

        # Apply the player's move.
        played_san = self.board.san(move)
        played_uci = move.uci()
        self.board.push(move)
        self.move_idx += 1
        self.attempts = 0

        # Auto-play the scripted opponent reply, if any remain.
        opponent_san: Optional[str] = None
        opponent_uci: Optional[str] = None
        if self.move_idx < len(sol):
            try:
                opp = chess.Move.from_uci(sol[self.move_idx])
                if opp in self.board.legal_moves:
                    opponent_san = self.board.san(opp)
                    opponent_uci = opp.uci()
                    self.board.push(opp)
                    self.move_idx += 1
            except (ValueError, AssertionError):
                opponent_san = None

        finished = (
            self.move_idx >= len(sol)
            or self.board.is_game_over()
        )
        if finished:
            self.solved = True
        out = {
            **self.state(),
            "correct": True,
            "played_san": played_san,
            "played_uci": played_uci,
            "opponent_san": opponent_san,
            "opponent_uci": opponent_uci,
            "finished": finished,
        }
        # When the puzzle is over, ship the full canonical line and an
        # explanation that names the actual mating/decisive move — not just
        # the puzzle's first move. The UI relies on these fields to render
        # the "Solution" panel for both solved and skipped puzzles, plus
        # `replay_fens` so the user can re-watch the combination on the board.
        if finished:
            out["revealed_solution_san"] = self.puzzle.get("solution_san") or []
            out["revealed_solution_uci"] = self.puzzle.get("solution_moves") or []
            out["solved_explanation"] = _build_solved_explanation(
                self.puzzle, self.board)
            out["replay_fens"] = _build_replay_fens(self.puzzle)
        return out

    # ---- player action: ask for a hint ---------------------------------

    def hint(self, level: int) -> dict:
        """Graduated hint: 1=motif, 2=piece-and-square, 3=exact move."""
        if not self.puzzle or self.board is None:
            return {"text": "No active puzzle.", "level": 0}
        if self.solved or self.failed:
            return {"text": "Puzzle already finished.", "level": 0}
        level = max(1, min(3, level))
        self.hints_used = max(self.hints_used, level)
        sol = self.puzzle.get("solution_moves") or []
        san_list = self.puzzle.get("solution_san") or []
        if self.move_idx >= len(sol):
            return {"text": "Puzzle is already complete.", "level": level}
        expected_uci = sol[self.move_idx]
        san_now = (san_list[self.move_idx]
                   if self.move_idx < len(san_list) else "")
        try:
            move = chess.Move.from_uci(expected_uci)
        except ValueError:
            return {
                "text": "Look for the most forcing move (check, capture, threat).",
                "level": level,
            }
        piece = self.board.piece_at(move.from_square)
        piece_name = chess.piece_name(piece.piece_type) if piece else "piece"
        from_sq = chess.square_name(move.from_square)

        if level == 1:
            motif = (self.puzzle.get("motif") or "tactic").replace("_", " ")
            return {
                "text": (f"Look for a **{motif}**. Check every piece your "
                         f"move attacks — and every piece you leave hanging."),
                "level": 1,
            }
        if level == 2:
            return {
                "text": f"Move your **{piece_name}** on **{from_sq}**.",
                "level": 2,
            }
        # level 3 reveals the exact move and a board highlight.
        return {
            "text": f"Play **{san_now or move.uci()}**.",
            "level": 3,
            "highlight": {
                "from": from_sq,
                "to": chess.square_name(move.to_square),
            },
        }

    # ---- player action: give up / reveal -------------------------------

    def skip(self) -> dict:
        """Mark the puzzle failed and reveal the full solution.

        We also rewind the board to the puzzle's starting FEN and ship a
        per-ply FEN list (`replay_fens`) so the UI can animate the solution
        from the start instead of leaving the board frozen on whatever
        position the player got stuck at.
        """
        if not self.puzzle:
            return {"active": False}
        self.failed = True
        # Rewind the trainer's own board to the start so any post-skip state
        # snapshot reflects the position the UI will animate from.
        try:
            self.board = chess.Board(self.puzzle["fen"])
        except (ValueError, KeyError):
            pass
        return {
            **self.state(),
            "revealed_solution_san": self.puzzle.get("solution_san") or [],
            "revealed_solution_uci": self.puzzle.get("solution_moves") or [],
            "replay_fens": _build_replay_fens(self.puzzle),
        }

    # ---- mark progress once per terminal puzzle outcome ---------------

    def record_if_done(self) -> Optional[dict]:
        if not self.puzzle:
            return None
        if self.recorded or not (self.solved or self.failed):
            return None
        self.recorded = True
        return record_outcome(
            puzzle_id=self.puzzle["id"],
            theme=self.puzzle["theme"],
            solved=self.solved,
            attempts=self.attempts,
            hints_used=self.hints_used,
        )


# ---------------------------------------------------------------------------
# Helpers

def _legal_moves_map(board: chess.Board) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in board.legal_moves:
        out.setdefault(chess.square_name(m.from_square), []).append(
            chess.square_name(m.to_square))
    return out


def _check_square(board: chess.Board) -> Optional[str]:
    if not board.is_check():
        return None
    king_sq = board.king(board.turn)
    return chess.square_name(king_sq) if king_sq is not None else None


def _player_color_for(puzzle: dict) -> str:
    """The player owns the side to move in the puzzle's start FEN."""
    fen = puzzle.get("fen", "")
    parts = fen.split(" ")
    return "white" if (len(parts) > 1 and parts[1] == "w") else "black"


def _build_solved_explanation(puzzle: dict, board_after: chess.Board) -> str:
    """A short post-solve narration that names the *final* (decisive) move
    and explains the outcome — much more useful than the puzzle's generic
    `explanation` field, which only references the first move.

    This is what the trainer puts on the "Solved!" panel so the student
    sees, for example, **Qxb7#** (instead of just "Rf8+ is the key…").
    """
    san_list = puzzle.get("solution_san") or []
    if not san_list:
        return puzzle.get("explanation") or "Solved."
    first = san_list[0]
    last = san_list[-1]
    motif = (puzzle.get("motif") or "tactic").replace("_", " ")

    # The post-move board has the side TO MOVE flipped, so a mate detected
    # via `is_checkmate()` is mate against that side.
    if board_after.is_checkmate():
        loser = "Black" if board_after.turn == chess.BLACK else "White"
        if len(san_list) == 1:
            return (f"**{last}** — checkmate. {loser} is mated; the king has "
                    f"no legal escape.")
        return (f"**{last}** — checkmate! The line **{_format_line(san_list)}** "
                f"forces {loser}'s king into a corner with no defence.")

    if board_after.is_stalemate():
        return (f"**{last}** — stalemate, saving the half-point. Line: "
                f"**{_format_line(san_list)}**.")

    if len(san_list) == 1:
        return (f"**{last}** — the {motif} works. Notice how it solves the "
                f"position in a single move.")
    return (f"**{first}** sets it up; **{last}** finishes it. Full line: "
            f"**{_format_line(san_list)}**. The {motif} is the theme.")


def _format_line(san_list: list[str]) -> str:
    """Render a SAN list as '1.Rf8+ Kd7 2.Qb5+ c6 3.Qxb7#' style."""
    if not san_list:
        return ""
    out: list[str] = []
    for i, san in enumerate(san_list):
        if i % 2 == 0:
            out.append(f"{i // 2 + 1}.{san}")
        else:
            out.append(san)
    return " ".join(out)


def coaching_facts(puzzle: Optional[dict],
                   board: Optional[chess.Board],
                   *,
                   move_idx: int = 0,
                   solved: bool = False,
                   failed: bool = False,
                   attempts: int = 0,
                   hints_used: int = 0) -> dict:
    """Pack the puzzle context into the facts dict shape `sap_coach` expects.

    The trainer holds different state than the live game (no Stockfish, no
    eval), so we synthesize what we can from the puzzle metadata. The LLM
    will treat fields like `eval_white` / `recent` as best-effort hints —
    `answer_question` is forgiving about missing keys.
    """
    if not puzzle or board is None:
        return {}
    sol_san = puzzle.get("solution_san") or []
    sol_uci = puzzle.get("solution_moves") or []
    motif = (puzzle.get("motif") or "tactic").replace("_", " ")
    color = _player_color_for(puzzle)
    rating = puzzle.get("difficulty_rating") or 0
    level = puzzle.get("difficulty") or "intermediate"
    # The portion of the line the student has already played out, plus whats
    # left, in numbered notation. Helps the LLM reason about context.
    recent_san = sol_san[:move_idx] if move_idx else []
    upcoming_san = sol_san[move_idx:]
    return {
        # Student profile (sap_coach reads these to size up the student).
        "level": level,
        "elo": rating,
        "your_color": color,
        # Position context.
        "fen": board.fen(),
        "move_number": (move_idx // 2) + 1,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "opening": None,                  # puzzles aren't typically book moves
        "eval_white": None,               # we don't run Stockfish here
        "recent": _format_line(recent_san) or None,
        # Tactical metadata — synthesised so the LLM can ground its answer in
        # the puzzle's actual canonical solution rather than guessing.
        "puzzle_theme": puzzle.get("theme_label") or puzzle.get("theme"),
        "puzzle_motif": motif,
        "puzzle_difficulty": level,
        "puzzle_rating": rating,
        "puzzle_full_solution_san": sol_san,
        "puzzle_remaining_solution_san": upcoming_san,
        "puzzle_solution_uci": sol_uci,
        "puzzle_solved": solved,
        "puzzle_failed": failed,
        "puzzle_attempts": attempts,
        "puzzle_hints_used": hints_used,
        "puzzle_explanation": puzzle.get("explanation"),
        "puzzle_lichess_id": puzzle.get("lichess_id"),
    }


def _build_replay_fens(puzzle: dict) -> list[str]:
    """Position-per-ply FEN list so the UI can animate the full solution.

    `fens[0]` is the puzzle's starting position; `fens[i+1]` is the
    position after `solution_moves[i]`. Stops early on any illegal move
    rather than throwing, so a malformed puzzle just truncates the replay.
    """
    fens: list[str] = []
    try:
        board = chess.Board(puzzle["fen"])
    except (ValueError, KeyError):
        return fens
    fens.append(board.fen())
    for u in puzzle.get("solution_moves") or []:
        try:
            mv = chess.Move.from_uci(u)
        except ValueError:
            break
        if mv not in board.legal_moves:
            break
        board.push(mv)
        fens.append(board.fen())
    return fens


# ---------------------------------------------------------------------------
# Persistent progress tracking

def record_outcome(puzzle_id: str, theme: str, solved: bool,
                   attempts: int, hints_used: int) -> dict:
    """Append one outcome to data/tactics_progress.json. Returns the updated doc."""
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        prog = json.loads(_PROGRESS_FILE.read_text()) if _PROGRESS_FILE.exists() else {}
        if not isinstance(prog, dict):
            prog = {}
    except (OSError, ValueError):
        prog = {}

    perfect = bool(solved and attempts <= 0 and hints_used == 0)

    totals = prog.setdefault("totals", {"attempted": 0, "solved": 0, "perfect": 0})
    totals["attempted"] += 1
    if solved:
        totals["solved"] += 1
    if perfect:
        totals["perfect"] += 1

    by_theme = prog.setdefault("by_theme", {})
    rec = by_theme.setdefault(theme, {"attempted": 0, "solved": 0, "perfect": 0})
    rec["attempted"] += 1
    if solved:
        rec["solved"] += 1
    if perfect:
        rec["perfect"] += 1

    history = prog.setdefault("history", [])
    history.append({
        "puzzle_id": puzzle_id,
        "theme": theme,
        "solved": solved,
        "attempts": attempts,
        "hints_used": hints_used,
    })
    if len(history) > 500:
        prog["history"] = history[-500:]

    _PROGRESS_FILE.write_text(json.dumps(prog, indent=2))
    return prog


def progress_summary() -> dict:
    """Read-only snapshot for the UI."""
    if not _PROGRESS_FILE.exists():
        return {
            "totals": {"attempted": 0, "solved": 0, "perfect": 0},
            "by_theme": {},
        }
    try:
        data = json.loads(_PROGRESS_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("totals", {"attempted": 0, "solved": 0, "perfect": 0})
        data.setdefault("by_theme", {})
        return data
    except (OSError, ValueError):
        return {
            "totals": {"attempted": 0, "solved": 0, "perfect": 0},
            "by_theme": {},
        }