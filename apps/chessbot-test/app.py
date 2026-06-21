#!/usr/bin/env python3
import time
from dataclasses import dataclass, field

from flask import Flask, jsonify, request, render_template_string
import chess
import chess.engine

# Path to system Stockfish on your Pi
STOCKFISH_PATH = "/usr/games/stockfish"

app = Flask(__name__)


# ---------- GAME STATE ----------

@dataclass
class GameState:
    board: chess.Board = field(default_factory=chess.Board)
    eval_cp: float = 0.0           # evaluation in pawns from White POV
    mate: int | None = None        # mate in N (for the side to move)
    engine_line: str = "-"         # principal variation (SAN)
    nodes: int = 0
    depth: int = 0
    time_used: float = 0.0
    opening: str = "Starting position"

    last_move_uci: str | None = None
    last_move_quality: str = "-"
    last_move_comment: str = "-"

    move_log: list[str] = field(default_factory=list)

    # Captured pieces (symbols like 'p', 'P', 'q', etc.)
    white_captures: list[str] = field(default_factory=list)  # pieces White has captured
    black_captures: list[str] = field(default_factory=list)  # pieces Black has captured

    difficulty_rating: int = 1100   # slider rating
    think_time: float = 0.4         # seconds engine thinks per move / analysis


game_state = GameState()


# ---------- ENGINE & ANALYSIS HELPERS ----------

def rating_to_think_time(rating: int) -> float:
    """
    Map slider rating (~800-2400) to think time.
    Lower rating => faster / weaker; higher rating => slower / stronger.
    """
    r = max(800, min(2400, int(rating)))
    # 800 -> 0.15 s, 2400 -> 1.5 s
    frac = (r - 800) / (2400 - 800)
    return 0.15 + frac * (1.5 - 0.15)


def get_opening_name(board: chess.Board) -> str:
    """Very simple opening label just so the UI has something to show."""
    return "Starting position" if not board.move_stack else "Custom line / Unknown opening"


def classify_move_quality(prev_eval: float, new_eval: float, mover: chess.Color) -> str:
    """
    Rough quality label based on how much the eval changed for the mover.
    prev_eval/new_eval are from *White's* point of view (pawns).
    """
    if mover == chess.WHITE:
        diff = new_eval - prev_eval
    else:
        diff = prev_eval - new_eval

    if diff <= -2.0:
        return "Blunder"
    if diff <= -1.0:
        return "Mistake"
    if diff <= -0.5:
        return "Inaccuracy"
    if diff >= 0.5:
        return "Excellent / Best"
    return "Solid"


# Global engine instance
engine: chess.engine.SimpleEngine | None = None


def get_engine() -> chess.engine.SimpleEngine:
    global engine
    if engine is None:
        engine = chess.engine.SimpleEngine.popen_uci([STOCKFISH_PATH])
    return engine


def recompute_captures_from_history() -> None:
    """
    Rebuild capture lists (white_captures / black_captures) from the move history.
    This handles undo/new_game correctly without us having to micro-track per move.
    """
    global game_state
    base = chess.Board()
    white_caps: list[str] = []
    black_caps: list[str] = []

    for mv in game_state.board.move_stack:
        if base.is_capture(mv):
            captured = base.piece_at(mv.to_square)
            if captured:
                # base.turn is the side about to move, i.e. the side making mv
                if base.turn == chess.WHITE:
                    white_caps.append(captured.symbol())
                else:
                    black_caps.append(captured.symbol())
        base.push(mv)

    game_state.white_captures = white_caps
    game_state.black_captures = black_caps


def analyse_current_position() -> None:
    """
    Run a short Stockfish analysis on the current board and update game_state
    (eval, PV, nodes, depth, etc.), and recompute captured pieces.
    """
    global game_state
    board = game_state.board

    game_state.opening = get_opening_name(board)

    # Also recompute captures from the move history
    recompute_captures_from_history()

    if board.is_game_over():
        game_state.eval_cp = 0.0
        game_state.mate = None
        game_state.engine_line = "-"
        game_state.nodes = 0
        game_state.depth = 0
        game_state.time_used = 0.0
        return

    eng = get_engine()
    info_raw = eng.analyse(
        board,
        chess.engine.Limit(time=game_state.think_time),
        multipv=1,
    )

    # python-chess returns either a single InfoDict or a list of them
    info = info_raw[0] if isinstance(info_raw, list) else info_raw

    score_obj = info.get("score")
    eval_cp = 0.0
    mate = None

    if score_obj is not None:
        # Always convert to White's POV so positive = better for White
        pov = score_obj.pov(chess.WHITE)
        if pov.is_mate():
            mate = pov.mate()
            eval_cp = 0.0
        else:
            eval_cp = (pov.score(mate_score=100000) or 0) / 100.0

    # Build SAN PV line
    pv_moves = info.get("pv")
    if pv_moves:
        tmp_board = board.copy()
        san_moves = []
        for m in pv_moves:
            san_moves.append(tmp_board.san(m))
            tmp_board.push(m)
        line = " ".join(san_moves)
    else:
        line = "-"

    game_state.eval_cp = eval_cp
    game_state.mate = mate
    game_state.engine_line = line
    game_state.nodes = int(info.get("nodes", 0))
    game_state.depth = int(info.get("depth", 0))
    game_state.time_used = float(info.get("time", 0.0))


# ---------- HTML / FRONTEND ----------

HTML_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Dustys Robo Chess Lounge</title>
  <style>
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top left, #3b2b1a, #050303 60%);
      color: #f7e7c6;
    }
    .app-frame {
      max-width: 1200px;
      margin: 16px auto;
      padding: 12px 16px 24px;
      border-radius: 16px;
      background: radial-gradient(circle at top left, #2b1d12, #000);
      box-shadow: 0 0 40px rgba(0,0,0,0.7);
    }
    .app-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 8px 14px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .title {
      font-size: 26px;
      letter-spacing: 0.12em;
    }
    .mode-label {
      font-size: 12px;
      text-transform: uppercase;
      opacity: 0.7;
    }
    .main-row {
      display: grid;
      grid-template-columns: 3fr 2fr;
      gap: 12px;
      margin-top: 14px;
    }
    .board-shell {
      padding: 10px;
      border-radius: 16px;
      background: radial-gradient(circle at top left, #302015, #050303 70%);
      box-shadow: 0 0 20px rgba(0,0,0,0.6);
    }
    .board-wrapper {
      position: relative;
      width: 100%;
      max-width: 640px;
      margin: 0 auto;
    }
    .board-grid {
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      grid-template-rows: repeat(8, 1fr);
      aspect-ratio: 1 / 1;  /* keeps the board square */
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 0 12px rgba(0,0,0,0.7);
    }
    .square {
      position: relative;
      width: 100%;
      height: 100%;
      box-sizing: border-box;
      transition: box-shadow 0.12s ease, outline 0.12s ease;
    }
    .square.light { background: #f0e0c0; }
    .square.dark  { background: #b58763; }

    .square.highlight-from {
      box-shadow: inset 0 0 0 3px rgba(255, 215, 0, 0.9);
    }
    .square.highlight-to {
      box-shadow: inset 0 0 0 3px rgba(0, 191, 255, 0.9);
    }
    .square.selected {
      outline: 3px solid #f5d15f;
      outline-offset: -3px;
    }
    .square.legal-target {
      box-shadow: inset 0 0 0 3px rgba(0, 200, 120, 0.9);
    }
    .square.illegal {
      box-shadow: inset 0 0 0 3px rgba(220, 20, 60, 0.95);
    }

    .piece {
      position: absolute;
      inset: 4px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 32px;
      font-weight: 700;
      text-shadow: 0 0 4px rgba(0,0,0,0.6);
      user-select: none;
    }
    .piece.white {
      color: #ffffff;  /* solid white */
      filter: drop-shadow(0 0 3px rgba(0,0,0,0.8));
    }
    .piece.black {
      color: #111111;
      filter: drop-shadow(0 0 3px rgba(255,255,255,0.25));
    }

    .engine-main-row {
      margin-top: 10px;
    }
    .btn {
      border-radius: 999px;
      padding: 6px 14px;
      border: 1px solid rgba(255,255,255,0.25);
      background: radial-gradient(circle at top left, #f5a623, #8b4c00 70%);
      color: #1b1209;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      cursor: pointer;
    }
    .btn-engine {
      width: 100%;
      padding: 10px 18px;
      font-size: 14px;
      letter-spacing: 0.16em;
    }
    .btn-secondary {
      background: transparent;
      color: #f7e7c6;
    }
    .btn:disabled {
      opacity: 0.35;
      cursor: default;
    }

    /* Right column */
    .panel {
      border-radius: 12px;
      padding: 10px 12px;
      background: radial-gradient(circle at top left, #241710, #050303 70%);
      box-shadow: 0 0 20px rgba(0,0,0,0.6);
      margin-bottom: 10px;
    }
    .panel-title {
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      margin-bottom: 4px;
    }
    .label-small {
      font-size: 11px;
      opacity: 0.8;
    }
    .value-strong {
      font-size: 15px;
      font-weight: 600;
    }
    .slider-row {
      margin-top: 6px;
    }
    input[type=range] {
      width: 100%;
    }
    .row-between {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
    }
    .button-row {
      margin-top: 8px;
      display: flex;
      gap: 8px;
    }

    .engine-details-toggle {
      font-size: 11px;
      cursor: pointer;
      color: #8abfff;
      margin-top: 4px;
    }
    .engine-details {
      font-size: 11px;
      margin-top: 4px;
      padding-top: 4px;
      border-top: 1px solid rgba(255,255,255,0.08);
      display: none;
    }

    .log-panel {
      font-size: 11px;
      margin-top: 4px;
      max-height: 160px;
      overflow-y: auto;
    }
    .log-entry {
      margin-bottom: 2px;
    }
    .log-panel::-webkit-scrollbar {
      width: 6px;
    }
    .log-panel::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.24);
      border-radius: 3px;
    }

    .captured-row {
      margin-top: 6px;
      font-size: 11px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .captured-label {
      margin-top: 6px;
      font-size: 11px;
      opacity: 0.85;
    }
  </style>
</head>
<body>
  <div class="app-frame">
    <div class="app-header">
      <div class="title">DUSTYS ROBO CHESS LOUNGE</div>
      <div class="mode-label">HUMAN VS. STOCKFISH • LOUNGE MODE</div>
    </div>

    <div class="main-row">
      <!-- BOARD + ENGINE BUTTON -->
      <div class="board-shell">
        <div class="board-wrapper">
          <div id="board" class="board-grid"></div>
        </div>
        <div class="engine-main-row">
          <button class="btn btn-engine" id="engine-move-btn">ENGINE MOVE</button>
        </div>
      </div>

      <!-- RIGHT SIDE PANELS -->
      <div>
        <div class="panel">
          <div class="panel-title">Difficulty</div>
          <div class="row-between">
            <div>
              <div class="label-small">
                Rating: <span id="rating-value">{{ rating }}</span>
              </div>
              <div class="label-small" id="rating-label">Developing player</div>
            </div>
          </div>
          <div class="slider-row">
            <input type="range" id="difficulty-slider"
                   min="800" max="2400" step="50" value="{{ rating }}">
          </div>
        </div>

        <div class="panel">
          <div class="panel-title">Engine Insight</div>
          <div class="label-small">
            Eval: <span class="value-strong" id="eval-display">0.0 (equal)</span>
          </div>
          <div class="label-small">
            Mate: <span id="mate-display">–</span>
          </div>
          <div class="label-small">
            Engine line: <span id="line-display">–</span>
          </div>
          <div class="label-small">
            Opening: <span id="opening-display">Starting position</span>
          </div>
          <div class="label-small">
            Last move quality: <span id="quality-display">–</span>
          </div>

          <div class="engine-details-toggle" id="details-toggle">▾ Engine details</div>
          <div class="engine-details" id="engine-details">
            <div>Depth: <span id="depth-display">0</span></div>
            <div>Nodes: <span id="nodes-display">0</span></div>
            <div>Time: <span id="time-display">0.00 s</span></div>
          </div>

          <div class="button-row">
            <button class="btn btn-secondary" id="undo-btn">Undo last</button>
            <button class="btn btn-secondary" id="newgame-btn">
              Resign / New game
            </button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-title">Move Log</div>
          <div class="label-small" id="turn-label">
            You are White. Tap a piece, then a highlighted square.
          </div>
          <div id="move-log" class="log-panel"></div>
          <div class="captured-label">Captured pieces</div>
          <div class="captured-row">
            <div class="label-small">
              White captured: <span id="white-captures"></span>
            </div>
            <div class="label-small">
              Black captured: <span id="black-captures"></span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const boardEl = document.getElementById('board');
    const moveLogEl = document.getElementById('move-log');
    const turnLabelEl = document.getElementById('turn-label');

    const evalEl = document.getElementById('eval-display');
    const mateEl = document.getElementById('mate-display');
    const lineEl = document.getElementById('line-display');
    const openingEl = document.getElementById('opening-display');
    const qualityEl = document.getElementById('quality-display');
    const depthEl = document.getElementById('depth-display');
    const nodesEl = document.getElementById('nodes-display');
    const timeEl = document.getElementById('time-display');

    const engineBtn = document.getElementById('engine-move-btn');
    const undoBtn = document.getElementById('undo-btn');
    const newBtn = document.getElementById('newgame-btn');
    const slider = document.getElementById('difficulty-slider');
    const ratingVal = document.getElementById('rating-value');
    const ratingLabel = document.getElementById('rating-label');

    const detailsToggle = document.getElementById('details-toggle');
    const detailsPanel = document.getElementById('engine-details');

    const whiteCapsEl = document.getElementById('white-captures');
    const blackCapsEl = document.getElementById('black-captures');

    detailsToggle.addEventListener('click', () => {
      const show = detailsPanel.style.display !== 'block';
      detailsPanel.style.display = show ? 'block' : 'none';
      detailsToggle.textContent = (show ? '▴' : '▾') + ' Engine details';
    });

    function ratingLabelText(r) {
      r = parseInt(r, 10);
      if (r < 900) return "Beginner";
      if (r < 1300) return "Developing player";
      if (r < 1700) return "Club player";
      if (r < 2100) return "Strong club player";
      return "Expert / Master level";
    }

    slider.addEventListener('input', () => {
      ratingVal.textContent = slider.value;
      ratingLabel.textContent = ratingLabelText(slider.value);
    });

    slider.addEventListener('change', () => {
      fetch('/set_difficulty', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rating: parseInt(slider.value)})
      });
    });

    let currentState = null;
    let selectedSquare = null;
    let illegalFrom = null;
    let illegalTo = null;

    const pieceToUnicode = {
      'P': '♙', 'N': '♘', 'B': '♗', 'R': '♖', 'Q': '♕', 'K': '♔',
      'p': '♟', 'n': '♞', 'b': '♝', 'r': '♜', 'q': '♛', 'k': '♚'
    };

    function squareName(file, rank) {
      return String.fromCharCode('a'.charCodeAt(0) + file) + (rank + 1);
    }

    function renderBoard() {
      if (!currentState) return;

      const fen = currentState.fen.split(' ')[0];
      const rows = fen.split('/');

      const lastUci = currentState.last_move_uci || null;
      let lastFrom = null, lastTo = null;
      if (lastUci && lastUci.length >= 4) {
        lastFrom = lastUci.slice(0, 2);
        lastTo = lastUci.slice(2, 4);
      }

      // Build set of legal targets for the currently selected square
      const legalTargets = new Set();
      if (selectedSquare && currentState.legal_moves) {
        currentState.legal_moves.forEach(m => {
          if (m.slice(0, 2) === selectedSquare) {
            legalTargets.add(m.slice(2, 4));
          }
        });
      }

      boardEl.innerHTML = '';

      for (let rank = 7; rank >= 0; rank--) {
        const row = rows[7 - rank];
        let file = 0;
        for (let ch of row) {
          if (ch >= '1' && ch <= '8') {
            const count = parseInt(ch, 10);
            for (let i = 0; i < count; i++) {
              const sqName = squareName(file, rank);
              const squareDiv = document.createElement('div');
              const isLight = (file + rank) % 2 === 0;
              squareDiv.className = 'square ' + (isLight ? 'light' : 'dark');
              squareDiv.dataset.square = sqName;

              if (sqName === selectedSquare) squareDiv.classList.add('selected');
              if (sqName === lastFrom) squareDiv.classList.add('highlight-from');
              if (sqName === lastTo) squareDiv.classList.add('highlight-to');
              if (legalTargets.has(sqName)) squareDiv.classList.add('legal-target');
              if (sqName === illegalFrom || sqName === illegalTo) squareDiv.classList.add('illegal');

              squareDiv.addEventListener('click', onSquareClick);
              boardEl.appendChild(squareDiv);
              file++;
            }
          } else {
            const sqName = squareName(file, rank);
            const squareDiv = document.createElement('div');
            const isLight = (file + rank) % 2 === 0;
            squareDiv.className = 'square ' + (isLight ? 'light' : 'dark');
            squareDiv.dataset.square = sqName;

            if (sqName === selectedSquare) squareDiv.classList.add('selected');
            if (sqName === lastFrom) squareDiv.classList.add('highlight-from');
            if (sqName === lastTo) squareDiv.classList.add('highlight-to');
            if (legalTargets.has(sqName)) squareDiv.classList.add('legal-target');
            if (sqName === illegalFrom || sqName === illegalTo) squareDiv.classList.add('illegal');

            squareDiv.addEventListener('click', onSquareClick);

            const pieceSpan = document.createElement('div');
            pieceSpan.className = 'piece ' + (ch === ch.toUpperCase() ? 'white' : 'black');
            pieceSpan.textContent = pieceToUnicode[ch] || '?';
            squareDiv.appendChild(pieceSpan);

            boardEl.appendChild(squareDiv);
            file++;
          }
        }
      }

      // Engine panel
      const e = currentState.engine || {};
      if (e.mate !== null && e.mate !== undefined) {
        evalEl.textContent = `Mate in ${Math.abs(e.mate)}`;
        mateEl.textContent = e.mate > 0 ? 'White mates' : 'Black mates';
      } else {
        const v = typeof e.eval_cp === 'number' ? e.eval_cp : 0.0;
        const txt = v.toFixed(2);
        let label = '(equal)';
        if (v > 0.4) label = '(white better)';
        else if (v < -0.4) label = '(black better)';
        evalEl.textContent = `${txt} ${label}`;
        mateEl.textContent = '–';
      }
      lineEl.textContent = e.line || '-';
      openingEl.textContent = currentState.opening || 'Starting position';
      qualityEl.textContent = currentState.last_move_quality || '-';
      depthEl.textContent = e.depth || 0;
      nodesEl.textContent = e.nodes || 0;
      timeEl.textContent = ((e.time || 0)).toFixed(2) + ' s';

      // Move log
      moveLogEl.innerHTML = '';
      (currentState.move_log || []).forEach(entry => {
        const div = document.createElement('div');
        div.className = 'log-entry';
        div.textContent = entry;
        moveLogEl.appendChild(div);
      });

      // Captured pieces
      const caps = currentState.captures || {white: [], black: []};
      const whitePieces = (caps.white || []).map(ch => pieceToUnicode[ch] || '').join(' ');
      const blackPieces = (caps.black || []).map(ch => pieceToUnicode[ch] || '').join(' ');
      whiteCapsEl.textContent = whitePieces;
      blackCapsEl.textContent = blackPieces;

      // Turn & engine button
      if (currentState.turn === 'white') {
        turnLabelEl.textContent = 'Your move (White). Tap a piece, then a highlighted square.';
        engineBtn.disabled = true;
      } else {
        turnLabelEl.textContent = 'Engine to move (tap ENGINE MOVE).';
        engineBtn.disabled = false;
      }
    }

    function flashIllegal(from, to) {
      illegalFrom = from;
      illegalTo = to;
      renderBoard();
      setTimeout(() => {
        illegalFrom = null;
        illegalTo = null;
        renderBoard();
      }, 500);
    }

    function onSquareClick(ev) {
      if (!currentState) return;
      if (currentState.turn !== 'white') return;  // human is always White
      const sq = ev.currentTarget.dataset.square;

      if (!selectedSquare) {
        // Only allow selecting a square that actually has a White piece
        const fen = currentState.fen.split(' ')[0];
        // We rely on legal_moves list instead of re-parsing FEN for color;
        // if there are no legal moves from this square, ignore.
        const hasAnyLegal = (currentState.legal_moves || []).some(m => m.slice(0, 2) === sq);
        if (!hasAnyLegal) return;
        selectedSquare = sq;
        renderBoard();
      } else if (selectedSquare === sq) {
        selectedSquare = null;
        renderBoard();
      } else {
        const from = selectedSquare;
        const to = sq;
        selectedSquare = null;

        // Client-side legality check using legal_moves
        const isLegal = (currentState.legal_moves || []).some(m => m === from + to);
        if (!isLegal) {
          flashIllegal(from, to);
          return;
        }

        sendMove(from, to);
      }
    }

    function sendMove(from, to) {
      fetch('/log_move', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({uci: from + to})
      })
      .then(async r => {
        const data = await r.json();
        if (!r.ok || data.error) {
          flashIllegal(from, to);
          return;
        }
        currentState = data;
        illegalFrom = null;
        illegalTo = null;
        renderBoard();
      })
      .catch(() => {
        flashIllegal(from, to);
      });
    }

    engineBtn.addEventListener('click', () => {
      if (!currentState || currentState.turn !== 'black') return;
      engineBtn.disabled = true;
      fetch('/engine_move', {method: 'POST'})
        .then(r => r.json())
        .then(state => {
          currentState = state;
          renderBoard();
        })
        .finally(() => {
          // renderBoard will re-enable if it's still engine's turn
          if (currentState && currentState.turn === 'black') {
            engineBtn.disabled = false;
          }
        });
    });

    undoBtn.addEventListener('click', () => {
      fetch('/undo_last', {method: 'POST'})
        .then(r => r.json())
        .then(state => {
          currentState = state;
          selectedSquare = null;
          illegalFrom = null;
          illegalTo = null;
          renderBoard();
        });
    });

    newBtn.addEventListener('click', () => {
      fetch('/new_game', {method: 'POST'})
        .then(r => r.json())
        .then(state => {
          currentState = state;
          selectedSquare = null;
          illegalFrom = null;
          illegalTo = null;
          renderBoard();
        });
    });

    function fetchState() {
      fetch('/state')
        .then(r => r.json())
        .then(state => {
          currentState = state;
          ratingVal.textContent = state.difficulty_rating;
          slider.value = state.difficulty_rating;
          ratingLabel.textContent = ratingLabelText(state.difficulty_rating);
          renderBoard();
        });
    }

    fetchState();
  </script>
</body>
</html>
"""


# ---------- FLASK ROUTES ----------

def build_state_json():
    """Helper to build the JSON state object the frontend expects."""
    return {
        "fen": game_state.board.fen(),
        "turn": "white" if game_state.board.turn == chess.WHITE else "black",
        "opening": game_state.opening,
        "last_move_uci": game_state.last_move_uci,
        "last_move_quality": game_state.last_move_quality,
        "move_log": game_state.move_log,
        "difficulty_rating": game_state.difficulty_rating,
        "engine": {
            "eval_cp": game_state.eval_cp,
            "mate": game_state.mate,
            "line": game_state.engine_line,
            "depth": game_state.depth,
            "nodes": game_state.nodes,
            "time": game_state.time_used,
        },
        "captures": {
            "white": game_state.white_captures,
            "black": game_state.black_captures,
        },
        "legal_moves": [m.uci() for m in game_state.board.legal_moves],
    }


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, rating=game_state.difficulty_rating)


@app.route("/state")
def state():
    return jsonify(build_state_json())


@app.route("/set_difficulty", methods=["POST"])
def set_difficulty():
    data = request.get_json(force=True, silent=True) or {}
    rating = int(data.get("rating", game_state.difficulty_rating))
    game_state.difficulty_rating = rating
    game_state.think_time = rating_to_think_time(rating)
    return ("", 200)


@app.route("/log_move", methods=["POST"])
def log_move():
    data = request.get_json(force=True, silent=True) or {}
    uci = data.get("uci", "")

    board = game_state.board
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return jsonify({"error": "bad move"}), 400

    if move not in board.legal_moves:
        # Frontend now handles this, but we keep server-side protection.
        return jsonify({"error": "illegal move"}), 400

    prev_eval = game_state.eval_cp
    mover = board.turn
    board.push(move)
    game_state.last_move_uci = move.uci()

    analyse_current_position()

    quality = classify_move_quality(prev_eval, game_state.eval_cp, mover)
    game_state.last_move_quality = quality
    desc = f"{'White' if mover == chess.WHITE else 'Black'} played {move.uci()} ({quality})"
    game_state.move_log.append(desc)

    return jsonify(build_state_json())


@app.route("/engine_move", methods=["POST"])
def engine_move():
    board = game_state.board

    if board.is_game_over() or board.turn != chess.BLACK:
        return jsonify(build_state_json())

    eng = get_engine()
    prev_eval = game_state.eval_cp
    mover = board.turn

    result = eng.play(board, chess.engine.Limit(time=game_state.think_time))
    move = result.move
    board.push(move)
    game_state.last_move_uci = move.uci()

    analyse_current_position()

    quality = classify_move_quality(prev_eval, game_state.eval_cp, mover)
    game_state.last_move_quality = quality
    desc = f"{'White' if mover == chess.WHITE else 'Black'} played {move.uci()} ({quality})"
    game_state.move_log.append(desc)

    return jsonify(build_state_json())


@app.route("/undo_last", methods=["POST"])
def undo_last():
    board = game_state.board
    if board.move_stack:
        board.pop()
        if game_state.move_log:
            game_state.move_log.pop()

    game_state.last_move_uci = None
    game_state.last_move_quality = "-"
    analyse_current_position()
    return jsonify(build_state_json())


@app.route("/new_game", methods=["POST"])
def new_game():
    global game_state
    game_state = GameState()
    game_state.think_time = rating_to_think_time(game_state.difficulty_rating)
    analyse_current_position()
    return jsonify(build_state_json())


# ---------- MAIN ----------

if __name__ == "__main__":
    try:
        game_state.think_time = rating_to_think_time(game_state.difficulty_rating)
        analyse_current_position()
        app.run(host="0.0.0.0", port=5000)
    finally:
        if engine is not None:
            engine.quit()
