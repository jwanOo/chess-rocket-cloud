// Browser-side Stockfish wrapper for Chess Rocket.
//
// Why this file exists:
//   Render's free tier (512 MB) OOM-killed every /api/ask|hint|coach because
//   running Stockfish multipv-3 + Python + httpx + the dashboard server in
//   one container blew past 512 MB. We can't shrink Stockfish enough to
//   reliably fit, and the only paid-plan alternatives (Cloudflare Containers
//   Workers Paid, Render Starter) all cost money.
//
//   Browser-side WASM Stockfish is the right answer: each user's machine
//   already has gigabytes of RAM and runs the same Stockfish via WebAssembly.
//   Lichess does this for the same reasons. The compute moves to the user;
//   the server stays a thin coordinator that fits in 512 MB easily.
//
// Loader notes:
//   - Uses Stockfish 10 (asm.js/wasm hybrid) from jsdelivr. Single-threaded,
//     no SharedArrayBuffer required, no COOP/COEP headers needed → works on
//     plain Render-hosted HTTP without any infra changes. Reaches depth ~14
//     multipv 3 in ~1-2s on a 2020-era laptop, which is plenty for coaching
//     hints. Newer threaded Stockfish needs cross-origin isolation that
//     would require Render middleware changes — not worth it.
//   - The engine itself runs in a Web Worker so it doesn't block the UI.
//   - We're a thin UCI driver: send commands, parse `info depth … pv …`
//     and `bestmove`, return the structured facts the Render coach prompt
//     expects (candidates, threat, your_hanging, best_motif, best_uci).

const STOCKFISH_CDN =
  "https://cdn.jsdelivr.net/npm/stockfish.js@10.0.2/stockfish.js";

const DEFAULT_DEPTH = 14;     // ~1-2s on a typical laptop
const DEFAULT_MULTIPV = 3;
const ANALYSIS_TIMEOUT_MS = 15000;

// ─── Worker singleton ──────────────────────────────────────────────────
// One engine per page is plenty. We lazy-init on first use so simply
// loading the dashboard with no coach interaction doesn't pay the cost.

let _worker = null;
let _ready = null;
let _busy = Promise.resolve();   // serializes calls so commands don't interleave

async function _getWorker() {
  if (_worker) {
    await _ready;
    return _worker;
  }
  _worker = new Worker(STOCKFISH_CDN);
  _ready = new Promise((resolve, reject) => {
    const t = setTimeout(
      () => reject(new Error("Stockfish init timed out (>15s)")), 15000);
    function onMsg(ev) {
      const line = typeof ev.data === "string" ? ev.data : "";
      if (line === "uciok") {
        clearTimeout(t);
        _worker.removeEventListener("message", onMsg);
        resolve();
      }
    }
    _worker.addEventListener("message", onMsg);
    _worker.addEventListener("error", e => {
      clearTimeout(t);
      reject(new Error(`Stockfish worker error: ${e.message || e}`));
    });
    _worker.postMessage("uci");
  });
  await _ready;
  return _worker;
}

// ─── Single analysis call ──────────────────────────────────────────────

function _runAnalysis(worker, { fen, depth, multipv }) {
  return new Promise((resolve, reject) => {
    // We accumulate the latest `info depth N` line per multipv index, then
    // resolve when `bestmove` arrives. The last `info` for each multipv slot
    // is the deepest one Stockfish managed before bestmove.
    const lines = new Map();   // multipv index → {score_cp, mate, pv (UCI list)}
    let bestUci = null;
    const t = setTimeout(() => {
      reject(new Error(`Stockfish analysis timed out (>${ANALYSIS_TIMEOUT_MS}ms)`));
      worker.removeEventListener("message", onMsg);
    }, ANALYSIS_TIMEOUT_MS);

    function onMsg(ev) {
      const text = typeof ev.data === "string" ? ev.data : "";
      if (!text) return;
      // "info depth 14 seldepth 19 multipv 1 score cp 24 ... pv e2e4 e7e5 ..."
      if (text.startsWith("info ") && text.includes(" pv ")) {
        const mpvMatch = text.match(/\bmultipv (\d+)/);
        const mpv = mpvMatch ? parseInt(mpvMatch[1], 10) : 1;
        const cpMatch = text.match(/\bscore cp (-?\d+)/);
        const mateMatch = text.match(/\bscore mate (-?\d+)/);
        const pvMatch = text.match(/\bpv (.+)$/);
        if (!pvMatch) return;
        lines.set(mpv, {
          score_cp: cpMatch ? parseInt(cpMatch[1], 10) : null,
          mate: mateMatch ? parseInt(mateMatch[1], 10) : null,
          pv_uci: pvMatch[1].trim().split(/\s+/),
        });
      } else if (text.startsWith("bestmove")) {
        // "bestmove e2e4" or "bestmove e2e4 ponder e7e5"
        const m = text.match(/^bestmove (\S+)/);
        bestUci = m && m[1] !== "(none)" ? m[1] : null;
        clearTimeout(t);
        worker.removeEventListener("message", onMsg);
        // Sort by multipv index ascending and resolve.
        const ordered = [...lines.entries()]
          .sort((a, b) => a[0] - b[0])
          .map(([_, v]) => v);
        resolve({ best_uci: bestUci, lines: ordered });
      }
    }
    worker.addEventListener("message", onMsg);

    worker.postMessage("ucinewgame");
    worker.postMessage(`setoption name MultiPV value ${multipv}`);
    worker.postMessage(`position fen ${fen}`);
    worker.postMessage(`go depth ${depth}`);
  });
}

// ─── Convert UCI moves to SAN using the current chess.js board ─────────
// dashboard.html already loads chess.js. We use it for SAN conversion + the
// hanging-piece + threat heuristics so we don't ship an extra rules library.

function _uciListToSan(fen, uciList) {
  /* global Chess */
  const sans = [];
  if (!Array.isArray(uciList) || !uciList.length) return sans;
  const board = new Chess(fen);
  for (const u of uciList) {
    const move = board.move({
      from: u.slice(0, 2),
      to: u.slice(2, 4),
      promotion: u.length > 4 ? u[4] : undefined,
    });
    if (!move) break;
    sans.push(move.san);
  }
  return sans;
}

function _hangingPieceFor(fen) {
  // Mirrors the server-side heuristic: side-to-move piece (non-king) where
  // attackers on it outnumber its defenders. Returns the first one we find.
  const board = new Chess(fen);
  const stm = board.turn();          // 'w' or 'b'
  const opp = stm === "w" ? "b" : "w";
  const SQS = [];
  for (let r = 1; r <= 8; r++) {
    for (const f of "abcdefgh") SQS.push(f + r);
  }
  for (const sq of SQS) {
    const piece = board.get(sq);
    if (!piece || piece.color !== stm || piece.type === "k") continue;
    // chess.js attackers/defenders surrogate: count legal captures of `sq`
    // by the opponent + count of own-side defenders attacking `sq`.
    const attackers = board.attackers ? board.attackers(sq, opp).length
                                      : _countAttackers(board, sq, opp);
    const defenders = board.attackers ? board.attackers(sq, stm).length
                                      : _countAttackers(board, sq, stm);
    if (attackers > 0 && defenders < attackers) {
      const pieceName = {
        p: "pawn", n: "knight", b: "bishop", r: "rook",
        q: "queen", k: "king",
      }[piece.type];
      return { piece: pieceName, square: sq };
    }
  }
  return null;
}

function _countAttackers(board, sq, color) {
  // chess.js < 1.0 doesn't expose attackers(); brute-force by trying captures
  // from every opposing piece. Slow but only a couple dozen squares.
  let n = 0;
  const oppMoves = board.moves({ verbose: true });
  // moves() returns moves for side-to-move; if we want attackers of color X,
  // and X is side-to-move, just count captures landing on sq.
  if (board.turn() === color) {
    n += oppMoves.filter(m => m.to === sq && m.flags.includes("c")).length;
  } else {
    // Need to ask the OTHER side. chess.js can't directly. Fallback:
    // make a null move to flip side-to-move (no public API), so we just
    // approximate: zero. The server-side check is more accurate; if we get
    // here we'll skip the hanging-piece hint, not break the prompt.
    n += 0;
  }
  return n;
}

// ─── Threat detection (cheap version) ──────────────────────────────────
// Server's null-move analysis isn't easy in pure chess.js. Approximation:
// look at the engine's bestmove FOR THE OPPONENT after our hypothetical pass.
// Cheap & cheerful: we just call the engine again with `position fen <flipped>`
// at low depth. Skip if in check (illegal to pass).

async function _opponentThreat(worker, fen) {
  const board = new Chess(fen);
  if (board.in_check()) return null;
  // Flip side-to-move by editing the FEN's "w"/"b" token.
  const parts = fen.split(" ");
  parts[1] = parts[1] === "w" ? "b" : "w";
  // Reset half-move + clear en-passant target: not strictly correct for
  // legality but good enough for a one-ply lookahead heuristic.
  parts[3] = "-";
  const flipped = parts.join(" ");
  try {
    const result = await _runAnalysis(worker, {
      fen: flipped, depth: 8, multipv: 1,
    });
    const top = result.lines[0];
    if (!top || !top.pv_uci?.length) return null;
    const sans = _uciListToSan(flipped, top.pv_uci.slice(0, 1));
    if (!sans.length) return null;
    return { san: sans[0], desc: `the move ${sans[0]}` };
  } catch (_) {
    return null;
  }
}

// ─── Public API ────────────────────────────────────────────────────────

/**
 * Analyse a FEN and return the situation_facts shape that
 * `scripts/sap_coach.py` prompts expect.
 *
 * @param {string} fen
 * @param {object} [opts]
 * @param {number} [opts.depth=14]
 * @param {number} [opts.multipv=3]
 * @returns {Promise<object>} {candidates, best_uci, threat, your_hanging,
 *                              best_motif (always null here — server can
 *                              still re-run motif detection if desired)}
 */
export async function analyzeFen(fen, opts = {}) {
  const depth = opts.depth ?? DEFAULT_DEPTH;
  const multipv = opts.multipv ?? DEFAULT_MULTIPV;
  // Serialize across calls — sharing one worker means one analysis at a time.
  const release = await _acquire();
  try {
    const worker = await _getWorker();
    const main = await _runAnalysis(worker, { fen, depth, multipv });
    const candidates = main.lines.map(l => {
      const sans = _uciListToSan(fen, l.pv_uci);
      return {
        san: sans[0] || null,
        score_cp: l.score_cp,
        mate: l.mate,
        line: sans.slice(0, 5).join(" "),
      };
    }).filter(c => c.san);

    const facts = {
      candidates,
      best_uci: main.best_uci,
      threat: null,
      your_hanging: _hangingPieceFor(fen),
      // best_motif intentionally null — Python motif_detector lives on the
      // server. The sap_coach prompt handles a missing motif gracefully.
      best_motif: null,
    };
    // Threat is a second engine call — only do it if not in check.
    facts.threat = await _opponentThreat(worker, fen);
    return facts;
  } finally {
    release();
  }
}

// Tiny mutex so two parallel button clicks don't talk to the engine at once.
function _acquire() {
  let release;
  const p = new Promise(r => (release = r));
  const prev = _busy;
  _busy = p;
  return prev.then(() => release);
}

/** Optional: tear down the worker (e.g. on page hide). */
export function disposeEngine() {
  if (_worker) {
    try { _worker.terminate(); } catch (_) { /* noop */ }
    _worker = null;
    _ready = null;
  }
}

// Expose on window for non-module callers (dashboard.html uses inline scripts).
if (typeof window !== "undefined") {
  window.ChessRocketEngine = { analyzeFen, disposeEngine };
}