// Chess Rocket — voice-control module (browser-only, zero dependencies).
//
// Plug-in module the play-/tactics-pages can mount onto an existing board.
// Speech recognition uses the browser's Web Speech API (Chrome, Safari iOS
// 14.5+, Firefox nightly). Audio leaves the device only via whatever cloud
// recognizer the OS has built in — no audio is sent to our backend.
//
// Public surface (everything is on `window.ChessVoice`):
//   ChessVoice.mount({ getState, submitMove, commands }) -> { destroy }
//   ChessVoice.parseUtterance(text, state) -> { kind, … }
//   ChessVoice.speak(text)
//
// `commands` lets the host page wire spoken control words ("undo", "hint",
// "new game", "explore", …) to its own handlers. The mic-button UI is
// rendered automatically as a floating action button in the bottom-right;
// the host page only needs to call `mount()` once and the rest is wired.

(function (global) {
  'use strict';

  // ---------------------------------------------------------------------
  // 1. Phonetic normalisation
  //
  // STT engines are wildly inconsistent on chess vocabulary — "knight" can
  // come back as "night", "mike", or "mic"; "rook" as "crook", "cook",
  // "rock", or even "wrook". This first pass cleans the transcript so the
  // grammar parser below sees something canonical.

  // Words → canonical piece letter. Order matters: longer phrases first.
  const PIECE_ALIASES = [
    [/\b(king|kings|the king|kink|cane)\b/gi, 'king'],
    [/\b(queen|queens|the queen|gwen|quinn|quinne|wean|weenie|kuwait)\b/gi, 'queen'],
    [/\b(rook|rooks|the rook|crook|crooks|wrook|brook|cook|cooks|rock|rocks|hook|hooks|tower|tower's|castle piece)\b/gi, 'rook'],
    [/\b(bishop|bishops|the bishop|b\.? shop|bee shop|bee\-shop|fish up|biship)\b/gi, 'bishop'],
    [/\b(knight|knights|the knight|night|nights|nite|nites|mike|mic|kanye|horse|horsie|horses)\b/gi, 'knight'],
    [/\b(pawn|pawns|the pawn|porn|porns|paun|pon|pawns?)\b/gi, 'pawn'],
  ];

  // Spelled-out files (NATO + plain letters spoken phonetically).
  const FILE_ALIASES = {
    a: ['a', 'alpha', 'ay', 'eh', 'apple'],
    b: ['b', 'bravo', 'bee', 'be'],
    c: ['c', 'charlie', 'see', 'sea'],
    d: ['d', 'delta', 'dee', 'd.'],
    e: ['e', 'echo', 'ee', 'eh'],
    f: ['f', 'foxtrot', 'ef', 'eff'],
    g: ['g', 'golf', 'gee', 'g.'],
    h: ['h', 'hotel', 'aitch', 'h.'],
  };

  const RANK_WORDS = {
    one: 1, two: 2, three: 3, four: 4, for: 4, fore: 4,
    five: 5, six: 6, sex: 6, seven: 7, eight: 8, ate: 8,
  };

  const PROMOTION_ALIASES = [
    [/\b(promote to queen|queen|q)\b/i, 'q'],
    [/\b(promote to rook|rook|r)\b/i, 'r'],
    [/\b(promote to bishop|bishop|b)\b/i, 'b'],
    [/\b(promote to knight|knight|n)\b/i, 'n'],
  ];

  // Build a regex that matches any spoken file alias preceded by a word
  // boundary, capturing the canonical letter via a lookup.
  function _buildFileAliasRegex() {
    const parts = [];
    for (const letter of Object.keys(FILE_ALIASES)) {
      for (const alias of FILE_ALIASES[letter]) {
        if (alias.length === 1) continue;            // single letters handled below
        parts.push(`(?<f_${letter}_${alias.replace(/\W/g, '_')}>\\b${alias}\\b)`);
      }
    }
    return new RegExp(parts.join('|'), 'gi');
  }
  const _FILE_ALIAS_RE = _buildFileAliasRegex();

  function normalize(raw) {
    if (!raw) return '';
    let s = ' ' + String(raw).toLowerCase() + ' ';
    s = s.replace(/[.,!?]/g, ' ');
    s = s.replace(/\s+/g, ' ');

    // Pieces.
    for (const [re, canonical] of PIECE_ALIASES) {
      s = s.replace(re, canonical);
    }

    // Spelled-out file names → letter ("alpha 4" → "a4").
    s = s.replace(_FILE_ALIAS_RE, (match) => {
      const lower = match.toLowerCase();
      for (const letter of Object.keys(FILE_ALIASES)) {
        if (FILE_ALIASES[letter].includes(lower)) return ' ' + letter + ' ';
      }
      return match;
    });

    // Rank words.
    s = s.replace(/\b(one|two|three|four|for|fore|five|six|sex|seven|eight|ate)\b/gi,
                  (m) => ' ' + RANK_WORDS[m.toLowerCase()] + ' ');

    // Castling.
    s = s.replace(/\b(castle|castling) (long|queen ?side|queens ?side)\b/g, ' o-o-o ');
    s = s.replace(/\b(castle|castling) (short|king ?side|kings ?side)\b/g, ' o-o ');
    s = s.replace(/\bkingside castle\b/g, ' o-o ');
    s = s.replace(/\bqueenside castle\b/g, ' o-o-o ');

    // "takes/captures/x/captures the" → "x"
    s = s.replace(/\b(takes|capture|captures|capturing|takes the|captures the)\b/g, ' x ');

    // "to" between piece+square is filler, drop it.
    s = s.replace(/\b(to|onto|on|moves to|move to|goes to|go to|moving to)\b/g, ' ');

    // Squeeze "<file> <rank>" tokens together so "e 4" becomes "e4".
    s = s.replace(/\b([a-h])\s+([1-8])\b/g, '$1$2');

    return s.replace(/\s+/g, ' ').trim();
  }

  // ---------------------------------------------------------------------
  // 2. Move parser
  //
  // Given the normalised text + a state snapshot containing
  // `legal_moves_map` and `fen` (whatever the host page already polls), find
  // a unique legal move. Returns one of:
  //   { kind: 'move', from, to, promotion? }
  //   { kind: 'ambiguous', candidates: [{from,to,san}, …], piece }
  //   { kind: 'illegal', reason }
  //   { kind: 'command', name }      // mapped from `commands` keys
  //   { kind: 'noise' }               // nothing we can do with it
  //
  // The parser is intentionally forgiving — STT will swallow words and
  // duplicate them; we extract any piece + any square mention in any order
  // ("knight to e4", "to e4 with the knight", "e4 knight" all work).

  // Map a piece word to the chessboard.js piece-letter prefix (uppercase
  // for white, lowercase for black). We need to know whose turn it is to
  // pick the right side; that comes from state.turn.
  const PIECE_LETTER = {
    king: 'k', queen: 'q', rook: 'r', bishop: 'b', knight: 'n', pawn: 'p',
  };

  // Convert UCI (e2e4, e7e8q) to {from,to,promotion?}.
  function _parseUci(uci) {
    if (!uci || uci.length < 4) return null;
    const m = /^([a-h][1-8])([a-h][1-8])([qrbn]?)$/.exec(uci);
    return m ? { from: m[1], to: m[2], promotion: m[3] || null } : null;
  }

  // Run through the legal-move map and find moves whose `from` square holds
  // a piece of the requested type owned by the side to move. We piggyback on
  // the host's polled FEN to know what's on each square.
  function _piecesOnSquare(fen, square) {
    if (!fen || !square) return null;
    const board = fen.split(' ')[0];
    const ranks = board.split('/');
    const file = square.charCodeAt(0) - 97;     // a=0
    const rank = 8 - parseInt(square[1], 10);   // rank 1 → idx 7
    const row = ranks[rank];
    if (!row) return null;
    let col = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) col += parseInt(ch, 10);
      else { if (col === file) return ch; col += 1; }
    }
    return null;
  }

  function _piecesByType(fen, pieceLetter, sideWhite) {
    // Returns the list of squares occupied by the given piece type for the
    // active side. pieceLetter is canonical lowercase ('n', 'r', …).
    const out = [];
    const board = (fen || '').split(' ')[0];
    if (!board) return out;
    const ranks = board.split('/');
    for (let r = 0; r < 8; r++) {
      let f = 0;
      for (const ch of ranks[r] || '') {
        if (/\d/.test(ch)) { f += parseInt(ch, 10); continue; }
        const isWhitePiece = ch === ch.toUpperCase();
        if (ch.toLowerCase() === pieceLetter && isWhitePiece === !!sideWhite) {
          out.push(String.fromCharCode(97 + f) + (8 - r));
        }
        f += 1;
      }
    }
    return out;
  }

  // Extract the first square (a1..h8) and any square-like mention from text.
  function _extractSquares(text) {
    const re = /\b([a-h][1-8])\b/g;
    const out = [];
    let m;
    while ((m = re.exec(text)) !== null) out.push(m[1]);
    return out;
  }

  function _extractPiece(text) {
    for (const word of ['king', 'queen', 'rook', 'bishop', 'knight', 'pawn']) {
      if (new RegExp('\\b' + word + '\\b').test(text)) return word;
    }
    return null;
  }

  function _extractPromotion(text) {
    // After the destination square comes "queen"/"knight"/etc. — but we
    // already lowercased "queen" via the piece pass, so look for the word
    // appearing AFTER any [a-h][18] in the original normalized string.
    const after = text.match(/\b[a-h][18]\b\s+(queen|rook|bishop|knight|q|r|b|n)\b/i);
    if (!after) return null;
    const w = after[1].toLowerCase();
    return ({ queen: 'q', rook: 'r', bishop: 'b', knight: 'n',
              q: 'q', r: 'r', b: 'b', n: 'n' })[w] || null;
  }

  function parseUtterance(rawText, state, commands) {
    const text = normalize(rawText || '');
    if (!text) return { kind: 'noise', text };

    // 0. Voice-control commands (always allowed).
    if (commands) {
      for (const name of Object.keys(commands)) {
        // Each command name is matched as a phrase ANYWHERE in the
        // utterance — STT often pads with "please" / "can you" / etc.
        if (new RegExp('\\b' + name.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&') + '\\b').test(text)) {
          return { kind: 'command', name, text };
        }
      }
    }

    if (!state || !state.legal_moves_map) {
      return { kind: 'illegal', reason: 'no game in progress', text };
    }
    const legal = state.legal_moves_map;
    const sideWhite = (state.turn || 'white') === 'white';

    // 1. Castling.
    if (/\bo-?o-?o\b/.test(text)) {
      const fromSq = sideWhite ? 'e1' : 'e8';
      const toSq = sideWhite ? 'c1' : 'c8';
      if ((legal[fromSq] || []).includes(toSq))
        return { kind: 'move', from: fromSq, to: toSq, text };
      return { kind: 'illegal', reason: 'castling queenside is not legal here', text };
    }
    if (/\bo-?o\b/.test(text)) {
      const fromSq = sideWhite ? 'e1' : 'e8';
      const toSq = sideWhite ? 'g1' : 'g8';
      if ((legal[fromSq] || []).includes(toSq))
        return { kind: 'move', from: fromSq, to: toSq, text };
      return { kind: 'illegal', reason: 'castling kingside is not legal here', text };
    }

    const squares = _extractSquares(text);
    const piece = _extractPiece(text);
    const promo = _extractPromotion(text);

    // 2. Two squares: pure from→to, e.g. "e2 to e4".
    if (squares.length >= 2) {
      const from = squares[0];
      const to = squares[1];
      if ((legal[from] || []).includes(to))
        return { kind: 'move', from, to, promotion: promo, text };
      return { kind: 'illegal', reason: `${from} to ${to} isn't legal`, text };
    }

    // 3. Piece + destination: walk the legal map and find the unique legal
    // origin square holding that piece.
    if (squares.length === 1 && piece) {
      const target = squares[0];
      const wantLetter = PIECE_LETTER[piece];
      const candidates = [];
      for (const [from, tos] of Object.entries(legal)) {
        if (!tos.includes(target)) continue;
        const occ = _piecesOnSquare(state.fen, from);
        if (!occ) continue;
        const isWhite = occ === occ.toUpperCase();
        if (occ.toLowerCase() !== wantLetter || isWhite !== sideWhite) continue;
        candidates.push({ from, to: target });
      }
      if (candidates.length === 1)
        return { kind: 'move', ...candidates[0], promotion: promo, text };
      if (candidates.length > 1)
        return { kind: 'ambiguous', candidates, piece, target, text };
      return { kind: 'illegal',
               reason: `no ${piece} can move to ${target}`, text };
    }

    // 4. Just a destination square (e.g. "e4"): try to interpret as a pawn
    // move first, otherwise as a uniquely-legal piece move.
    if (squares.length === 1 && !piece) {
      const target = squares[0];
      const candidates = [];
      for (const [from, tos] of Object.entries(legal)) {
        if (!tos.includes(target)) continue;
        const occ = _piecesOnSquare(state.fen, from);
        if (!occ) continue;
        const isWhite = occ === occ.toUpperCase();
        if (isWhite !== sideWhite) continue;
        candidates.push({ from, to: target, piece: occ.toLowerCase() });
      }
      // Prefer pawn moves when "e4" is bare — that's the human-typical
      // shorthand.
      const pawnOnly = candidates.filter(c => c.piece === 'p');
      if (pawnOnly.length === 1)
        return { kind: 'move', from: pawnOnly[0].from, to: target,
                 promotion: promo, text };
      if (candidates.length === 1)
        return { kind: 'move', from: candidates[0].from, to: target,
                 promotion: promo, text };
      if (candidates.length > 1)
        return { kind: 'ambiguous', candidates, target, text };
      return { kind: 'illegal', reason: `nothing legal moves to ${target}`, text };
    }

    return { kind: 'noise', text };
  }

  // ---------------------------------------------------------------------
  // 3. Text-to-speech (host-callable for any narration)

  function speak(text) {
    if (!text) return;
    if (!('speechSynthesis' in window)) return;
    try {
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.05;
      u.pitch = 1.0;
      window.speechSynthesis.cancel();   // pre-empt anything in the queue
      window.speechSynthesis.speak(u);
    } catch (e) { /* swallow — TTS is best-effort */ }
  }

  // ---------------------------------------------------------------------
  // 4. SpeechRecognition wrapper
  //
  // iOS Safari, Chrome, and Edge expose `webkitSpeechRecognition`. Firefox
  // has it gated behind a flag in stable, so we treat it as missing there.

  function _SpeechRecognition() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
  }

  function isSupported() {
    return _SpeechRecognition() !== null;
  }

  function _listenOnce({ onResult, onError, onEnd, lang = 'en-US' }) {
    const SR = _SpeechRecognition();
    if (!SR) { onError && onError(new Error('SpeechRecognition not supported')); return null; }
    const r = new SR();
    r.lang = lang;
    r.continuous = false;     // tap-once mode (we agreed on this earlier)
    r.interimResults = false; // we want a single final transcript
    r.maxAlternatives = 3;    // pick the best below

    r.onresult = (ev) => {
      const result = ev.results[0];
      if (!result) return;
      // Pick the alternative with the highest confidence; many engines
      // return the most-confident first but we sort defensively.
      const alts = Array.from({ length: result.length }, (_, i) => result[i])
                        .sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
      onResult && onResult(alts[0].transcript || '', alts.map(a => a.transcript));
    };
    r.onerror = (ev) => onError && onError(ev.error || ev);
    r.onend   = () => onEnd && onEnd();

    try { r.start(); } catch (e) { onError && onError(e); return null; }
    return r;
  }

  // ---------------------------------------------------------------------
  // 5. Mic button widget + transcript banner

  const FAB_HTML = `
    <button class="cr-mic-fab" type="button" aria-label="Voice command">
      <span class="cr-mic-icon" aria-hidden="true">🎤</span>
    </button>
    <div class="cr-mic-banner" role="status" aria-live="polite"></div>
  `;
  const FAB_CSS = `
    .cr-mic-fab {
      position: fixed; right: 18px; bottom: 18px; z-index: 9000;
      width: 60px; height: 60px; border-radius: 50%;
      border: 1px solid rgba(255,255,255,0.15);
      background: #16213e; color: #fff;
      box-shadow: 0 6px 18px rgba(0,0,0,0.45);
      cursor: pointer; font-size: 1.7rem;
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.15s ease, background 0.2s ease;
    }
    .cr-mic-fab:hover { background: #1d2f5d; }
    .cr-mic-fab:active { transform: scale(0.94); }
    .cr-mic-fab.listening { background: #e94560; animation: cr-pulse 1.4s ease-in-out infinite; }
    .cr-mic-fab.thinking  { background: #ff9800; }
    .cr-mic-fab.error     { background: #f44336; }
    .cr-mic-fab[disabled] { opacity: 0.4; cursor: not-allowed; }
    @keyframes cr-pulse {
      0%,100% { box-shadow: 0 0 0 0 rgba(233,69,96,0.7); }
      50%     { box-shadow: 0 0 0 14px rgba(233,69,96,0); }
    }
    .cr-mic-banner {
      position: fixed; left: 50%; bottom: 92px; transform: translateX(-50%);
      max-width: 90vw; padding: 10px 16px; border-radius: 24px;
      background: rgba(22,33,62,0.96); color: #fff; font-size: 0.95rem;
      z-index: 9001; pointer-events: none;
      opacity: 0; transition: opacity 0.2s ease;
      box-shadow: 0 4px 14px rgba(0,0,0,0.4);
    }
    .cr-mic-banner.visible { opacity: 1; }
    .cr-mic-banner.error { background: #b8232f; }
    .cr-mic-banner.ok    { background: #2c6e3a; }
    @media (max-width: 480px) {
      .cr-mic-fab { right: 14px; bottom: 14px; width: 56px; height: 56px; }
    }
  `;

  function _injectStyles() {
    if (document.getElementById('cr-voice-styles')) return;
    const s = document.createElement('style');
    s.id = 'cr-voice-styles';
    s.textContent = FAB_CSS;
    document.head.appendChild(s);
  }

  function _showBanner(bannerEl, text, variant, ms) {
    if (!bannerEl) return;
    bannerEl.className = 'cr-mic-banner visible' + (variant ? ' ' + variant : '');
    bannerEl.textContent = text;
    if (ms) {
      clearTimeout(bannerEl._t);
      bannerEl._t = setTimeout(() => {
        bannerEl.classList.remove('visible');
      }, ms);
    }
  }

  // ---------------------------------------------------------------------
  // 6. Public mount() — the single entry the host pages call

  function mount(opts) {
    const cfg = Object.assign({
      getState: () => null,             // host gives us the latest game state
      submitMove: null,                 // (from, to, promotion) => Promise|void
      commands: {},                     // { 'undo': () => ..., 'hint': ..., 'new game': ... }
      lang: 'en-US',
      announceOpponentMove: true,       // speak the engine's reply
      autoMount: true,                  // whether to inject the FAB now
    }, opts || {});

    if (!isSupported()) {
      console.info('ChessVoice: SpeechRecognition not available — mic disabled.');
      return { destroy: () => {}, isSupported: false };
    }

    _injectStyles();
    const wrap = document.createElement('div');
    wrap.innerHTML = FAB_HTML;
    const fab    = wrap.querySelector('.cr-mic-fab');
    const banner = wrap.querySelector('.cr-mic-banner');
    if (cfg.autoMount) {
      document.body.appendChild(fab);
      document.body.appendChild(banner);
    }

    let recognition = null;
    let lastEngineSan = null;

    function setState(state) {
      fab.classList.remove('listening', 'thinking', 'error');
      if (state === 'listening') fab.classList.add('listening');
      else if (state === 'thinking') fab.classList.add('thinking');
      else if (state === 'error') fab.classList.add('error');
    }

    async function handleTranscript(transcript) {
      _showBanner(banner, '“' + transcript + '”', '', 1800);
      const state = cfg.getState ? cfg.getState() : null;
      const parsed = parseUtterance(transcript, state, cfg.commands);

      if (parsed.kind === 'command') {
        const fn = cfg.commands[parsed.name];
        if (typeof fn === 'function') {
          setState('thinking');
          try { await fn(); } finally { setState('idle'); }
          speak('Okay');
        } else {
          _showBanner(banner, 'Command "' + parsed.name + '" not wired.', 'error', 2200);
          setState('idle');
        }
        return;
      }

      if (parsed.kind === 'move') {
        if (typeof cfg.submitMove !== 'function') {
          _showBanner(banner, 'No move handler on this page.', 'error', 2400);
          setState('idle');
          return;
        }
        setState('thinking');
        try {
          await cfg.submitMove(parsed.from, parsed.to, parsed.promotion || null);
          // Speak our own move + the engine reply if the host stashed it.
          const newState = cfg.getState ? cfg.getState() : null;
          const lastUci = newState && newState.last_move_uci;
          const lastSan = newState && newState.last_move_san;
          if (cfg.announceOpponentMove && lastSan && lastSan !== lastEngineSan) {
            lastEngineSan = lastSan;
            speak('Opponent plays ' + lastSan);
          }
        } catch (e) {
          console.warn('ChessVoice: submitMove failed', e);
          _showBanner(banner, 'Move failed.', 'error', 2400);
        } finally {
          setState('idle');
        }
        return;
      }

      if (parsed.kind === 'ambiguous') {
        const list = (parsed.candidates || []).map(c => c.from).join(', ');
        _showBanner(banner, 'Which one? ' + list, 'error', 3000);
        speak('Which one — say the file letter, like ' +
              (parsed.candidates[0] ? parsed.candidates[0].from[0] : 'a') + '.');
        setState('idle');
        return;
      }

      if (parsed.kind === 'illegal') {
        _showBanner(banner, parsed.reason || 'Illegal.', 'error', 2400);
        setState('idle');
        return;
      }

      _showBanner(banner, "Didn't catch that.", 'error', 2000);
      setState('idle');
    }

    function startListening() {
      if (recognition) return;
      _showBanner(banner, 'Listening…', '', 4000);
      setState('listening');
      recognition = _listenOnce({
        lang: cfg.lang,
        onResult: (transcript) => {
          // The result handler will fire BEFORE onend; we close the mic
          // after handling so the visual state matches reality.
          handleTranscript(transcript);
        },
        onError: (err) => {
          console.warn('ChessVoice: SR error', err);
          const msg = (typeof err === 'string') ? err : (err && err.message) || 'mic error';
          if (/not-allowed|denied/i.test(msg)) {
            _showBanner(banner,
              'Microphone permission denied. Enable it in browser settings.',
              'error', 4500);
          } else if (/no-speech/i.test(msg)) {
            _showBanner(banner, "Didn't hear anything — try again.",
                        'error', 2200);
          } else {
            _showBanner(banner, 'Mic error: ' + msg, 'error', 2800);
          }
          setState('error');
          setTimeout(() => setState('idle'), 1500);
        },
        onEnd: () => { recognition = null; },
      });
    }

    fab.addEventListener('click', () => {
      if (recognition) {
        try { recognition.stop(); } catch (e) { /* ignore */ }
        recognition = null;
        setState('idle');
        return;
      }
      startListening();
    });

    return {
      destroy: () => {
        try { recognition && recognition.stop(); } catch (e) { /* ignore */ }
        fab.remove();
        banner.remove();
      },
      isSupported: true,
      startListening,    // expose so the host can hot-key the mic
      speak,
    };
  }

  // ---------------------------------------------------------------------
  // Public surface
  global.ChessVoice = {
    mount,
    parseUtterance,
    normalize,
    speak,
    isSupported,
  };

})(typeof window !== 'undefined' ? window : globalThis);