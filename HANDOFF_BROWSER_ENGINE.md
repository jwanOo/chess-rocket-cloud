# Handoff: Move engine move to browser (Option B)

## Why

Render free tier (512 MB) OOM-kills on `/api/move` because `GameManager.player_move()` calls `ChessEngine.get_move()`, which spawns Stockfish via python-chess UCI. Confirmed by direct probe:

```
POST /api/new   → 200  (no engine call)
POST /api/move  → 502  (engine spawn → OOM)
```

The two prior commits (`984c570` chess.js URL fix, `a186cb7` BrokenPipeError fix) are correct and live; the only remaining failure is the Stockfish RSS overhead. Moving the engine move to the browser eliminates server-side Stockfish entirely — zero ongoing cost, no upgrade.

## Architecture after the change

```
Browser (Stockfish.wasm via client_engine.js)            Render (Python, ~80 MB)
┌────────────────────────────────────────┐               ┌──────────────────────────┐
│ User makes move on board               │               │                          │
│ chess.js validates legally             │               │                          │
│ pickEngineMove(fen, elo) →             │   POST        │ _handle_move:            │
│   {bestUci, ponder?}                   │ ─────────────►│   board.push(player_uci) │
│ POST /api/move {                       │               │   board.push(engine_uci) │
│   from, to, promotion,                 │               │   compute classification │
│   engine_uci  ◄── new field            │               │   return state_dict()    │
│ }                                      │   200         │                          │
│ Update board with engine reply         │ ◄─────────────│                          │
└────────────────────────────────────────┘               └──────────────────────────┘
                          ▲                                          │
                          │  (already implemented in 98962f2)        │
                          ▼                                          ▼
                  /api/coach POST with {facts}            SAP AI Core (Cloudflare Worker proxy)
```

No server-side Stockfish anywhere. Same architecture for `/api/coach`, `/api/hint`, `/api/ask`, `/api/review`, `/api/sandbox/*` (already done).

## Files to change

### 1. `chess-rocket-cloud/scripts/client_engine.js`

Add `pickEngineMove(fen, elo)` next to existing `analyzeFen()`. Maps elo to Stockfish Skill Level so the engine still feels appropriate for beginner mode.

```js
// elo → Stockfish "Skill Level" (0..20). Crude mapping but plays a credible
// sub-2000 game; depth caps prevent the worker from hogging the main thread.
const _ELO_TO_SKILL = [
  [400,  0,  3],   // [maxElo, skillLevel, maxDepth]
  [800,  3,  6],
  [1200, 7,  9],
  [1600, 12, 12],
  [2000, 16, 14],
  [9999, 20, 16],
];

function _eloParams(elo) {
  const e = (elo === "auto" || elo == null) ? 800 : Number(elo);
  for (const [maxE, skill, depth] of _ELO_TO_SKILL) {
    if (e <= maxE) return { skill, depth };
  }
  return { skill: 20, depth: 16 };
}

export async function pickEngineMove(fen, elo = 800) {
  const release = await _acquire();
  try {
    const worker = await _getWorker();
    const { skill, depth } = _eloParams(elo);
    return await new Promise((resolve, reject) => {
      let bestUci = null;
      const t = setTimeout(() => {
        worker.removeEventListener("message", onMsg);
        reject(new Error("engine move timed out (>10s)"));
      }, 10000);
      function onMsg(ev) {
        const text = typeof ev.data === "string" ? ev.data : "";
        if (text.startsWith("bestmove")) {
          const m = text.match(/^bestmove (\S+)/);
          bestUci = m && m[1] !== "(none)" ? m[1] : null;
          clearTimeout(t);
          worker.removeEventListener("message", onMsg);
          resolve(bestUci);
        }
      }
      worker.addEventListener("message", onMsg);
      worker.postMessage("ucinewgame");
      worker.postMessage(`setoption name Skill Level value ${skill}`);
      worker.postMessage(`setoption name MultiPV value 1`);
      worker.postMessage(`position fen ${fen}`);
      worker.postMessage(`go depth ${depth}`);
    });
  } finally {
    release();
  }
}

if (typeof window !== "undefined") {
  window.ChessRocketEngine = { analyzeFen, disposeEngine, pickEngineMove };
}
```

### 2. `chess-rocket-cloud/scripts/dashboard.html`

In the `sendMove(from, to, promotion)` function, before the `/api/move` POST, run:

```js
async function sendMove(from, to, promo) {
  busy = true;
  setControlsDisabled(true);
  setCoach('<span class="thinking">Engine is thinking</span>');
  try {
    // 1. Apply player move locally so we can derive the post-player FEN
    //    that the engine is supposed to reply to.
    if (!window.Chess || !currentState) throw new Error("chess.js not ready");
    const tmp = new Chess(currentState.fen);
    const playerMove = tmp.move({ from, to, promotion: promo || "q" });
    if (!playerMove) throw new Error("illegal");

    // 2. Have the browser engine pick black's reply (skip if game over).
    let engineUci = null;
    if (!(tmp.isGameOver?.() || tmp.in_checkmate?.() || tmp.in_stalemate?.())) {
      try {
        engineUci = await window.ChessRocketEngine.pickEngineMove(
          tmp.fen(), currentState.target_elo);
      } catch (e) {
        console.warn("[engine] pickEngineMove failed; server will fall back", e);
      }
    }

    // 3. POST player+engine moves; server records both, computes annotation.
    const state = await postJSON('/api/move',
      { from, to, promotion: promo, engine_uci: engineUci });
    if (state && state.illegal) {
      if (currentState) board.position(currentState.fen);
      setCoach('That move is not legal — try another.');
      return;
    }
    resetMoveAids();
    applyState(state);
    renderTactical(state);
    /* … rest unchanged … */
  } catch (e) {
    if (currentState) board.position(currentState.fen);
    setCoach('Move failed: ' + (e.message || e));
  } finally {
    busy = false;
    setControlsDisabled(false);
  }
}
```

### 3. `chess-rocket-cloud/scripts/game_manager.py`

Add a `record_player_move(from_sq, to_sq, promotion, engine_uci)` method that is the OOM-free twin of `player_move`. It:

1. Pushes the player move to `self.board`.
2. If `engine_uci` was provided, validates it's legal and pushes it.
3. Computes `move_annotations`, `eval_score` from the *resulting* FEN using cheap python-chess only (no `ChessEngine`).
4. Returns the same `state_dict()` shape `player_move` returns.

Skeleton (pseudocode — actual diff goes into `chess-rocket-cloud/scripts/game_manager.py`):

```python
def record_player_move(self, from_sq, to_sq, promotion=None, engine_uci=None):
    """Player + browser-supplied engine move recorder. No Stockfish.
    Mirrors player_move()'s return shape so dashboard_server doesn't care."""
    move = chess.Move.from_uci(f"{from_sq}{to_sq}{promotion or ''}")
    if move not in self.board.legal_moves:
        return {"illegal": True, "error": "illegal move"}

    # Player move
    self.board.push(move)
    self._after_move(move, by_player=True)

    # Engine reply (if provided + still legal)
    engine_san = None
    if engine_uci and not self.board.is_game_over():
        try:
            em = chess.Move.from_uci(engine_uci)
            if em in self.board.legal_moves:
                engine_san = self.board.san(em)
                self.board.push(em)
                self._after_move(em, by_player=False)
        except (ValueError, AssertionError):
            pass  # fall through; client will see "no reply" and retry

    state = self.state_dict()
    state["engine_move"] = engine_san
    return state
```

`_after_move(move, by_player)` should encapsulate whatever `player_move`/`engine_move` currently do *minus* the Stockfish call: update accuracy via heuristic only, opening lookup, captured-piece tracking, checkmate detection. The exact body needs to be lifted from the existing `player_move`/`_engine_reply` methods and stripped of any `self.engine.*` calls.

### 4. `chess-rocket-cloud/scripts/dashboard_server.py`

Change `_handle_coach`'s sibling, `do_POST` `/api/move` branch:

```python
elif path == "/api/move":
    frm = body.get("from")
    to = body.get("to")
    promo = body.get("promotion")
    engine_uci = body.get("engine_uci")  # browser-supplied; may be None
    if not frm or not to:
        self._send_json(400, {"error": "from/to required"})
        return
    game, lock = self._game_for_request()
    with lock:
        if engine_uci is not None:
            result = game.record_player_move(frm, to, promo, engine_uci)
        else:
            # Legacy path for old clients still in flight: try in-process
            # engine, but it'll OOM on Render free tier. Acceptable as
            # transition fallback; remove once all browser clients reload.
            result = game.player_move(frm, to, promo)
    self._send_json(200, result)
```

## Smoke test (after deploying)

```bash
# 1. Start a game
curl -sX POST -c /tmp/ck -b /tmp/ck https://chess-rocket-backend.onrender.com/api/new \
  -H "Content-Type: application/json" -d '{"elo":800,"color":"white"}'

# 2. Player e2-e4, engine plays e7-e5 (computed in browser; we hard-code here)
curl -sX POST -c /tmp/ck -b /tmp/ck https://chess-rocket-backend.onrender.com/api/move \
  -H "Content-Type: application/json" \
  -d '{"from":"e2","to":"e4","engine_uci":"e7e5"}'
# Expected: 200, JSON with move_list:["e4","e5"], no 502
```

## Render memory expected after the change

- Python interpreter: ~30 MB
- python-chess + httpx: ~40 MB
- dashboard_server: ~10 MB
- **Total: ~80 MB resident** (was ~480 MB during a Stockfish search)

Comfortably fits 512 MB free tier. No more OOM. No upgrade needed.

## Validation checklist before merging

- [ ] `node --check scripts/client_engine.js` clean
- [ ] `python3 -c "import ast; ast.parse(open('scripts/dashboard_server.py').read())"` clean
- [ ] `python3 -c "import ast; ast.parse(open('scripts/game_manager.py').read())"` clean
- [ ] Local smoke: `uv run python scripts/dashboard_server.py`, hit http://localhost:8088, play 5 moves, no errors
- [ ] Live smoke: curl above returns 200
- [ ] Browser DevTools Network: every `/api/move` body has `engine_uci` field
- [ ] Browser DevTools Memory: `chrome://tasks` shows ~50 MB for the tab (Stockfish.wasm worker)
- [ ] Render Events: no OOMKilled in 24h after deploy

## Estimated effort

About 60-90 minutes of focused work in a fresh session:
- 15 min: add `pickEngineMove` to `client_engine.js` + sanity test in browser console
- 20 min: refactor `sendMove` in `dashboard.html` to call browser engine before POST
- 30 min: write `record_player_move` in `game_manager.py` by lifting code from `player_move` and removing all `self.engine.*` calls; preserve `state_dict()` shape exactly
- 10 min: wire `dashboard_server.py` `/api/move` to the new method when `engine_uci` is present
- 10 min: smoke tests (local + live)
- 5 min: commit + push, watch Render Events tab to confirm no OOM

## What still uses the server (untouched)

- Session cookies + per-session game state (negligible RAM, tens of bytes per session)
- Opening DB lookup (CSV parse, ~5 MB resident, already loaded)
- `/api/coach`, `/api/hint`, `/api/ask`, `/api/review` → SAP AI Core via Cloudflare Worker proxy (no compute on Render)
- `/api/sandbox/*` → also needs Stockfish-free path; skip for this iteration, mark as "Pro feature, browser-only" if anyone hits it on free tier

## What this does NOT change

- Voice control (already client-only)
- chess.js UMD load (already fixed in `984c570`)
- BrokenPipe handling (already fixed in `a186cb7`)
- The OfflineMode shim (still useful as a true fallback when Render is down)
- SAP AI Core integration (untouched; works through the existing Worker proxy)