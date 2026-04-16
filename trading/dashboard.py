"""
Live dashboard for the Wheel Strategy agent.

Run alongside wheel_agent.py:
    python dashboard.py

Then open: http://localhost:5001
Refreshes automatically every 30 seconds.
"""

import glob
import json
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
BASE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Stage metadata – human-readable explanations for each wheel stage
# ---------------------------------------------------------------------------
STAGE_META = {
    "IDLE": {
        "label": "Idle — Ready to Start",
        "color": "#64748b",
        "step": 0,
        "action": "Waiting to sell the next put option",
        "what_now": (
            "The wheel is at rest. There are no open positions and no open orders. "
            "On the next market check the bot will scan for a put option to sell."
        ),
        "what_next": (
            "The bot will sell a <strong>cash-secured put</strong> roughly 10% below "
            "the current stock price with a 2–4 week expiration. "
            "Enough cash will always be reserved to cover a full assignment."
        ),
    },
    "SELL_PUT": {
        "label": "Stage 1 — Short Put Open",
        "color": "#3b82f6",
        "step": 1,
        "action": "Short put is open — collecting time-value premium",
        "what_now": (
            "A <strong>cash-secured put</strong> has been sold. The bot is now waiting "
            "for the option to expire or for the stock to be assigned. "
            "Time decay (theta) works in your favour every day."
        ),
        "what_next": (
            "<strong>If the stock stays above the strike at expiry:</strong> the option "
            "expires worthless, you keep the full premium, and the bot sells another put — "
            "restarting Stage 1.<br><br>"
            "<strong>If the stock falls below the strike:</strong> you are assigned "
            "100 shares at the strike price. Your effective cost is lower than the strike "
            "because the premium is subtracted from it."
        ),
    },
    "HAVE_SHARES": {
        "label": "Transition — Shares Assigned",
        "color": "#f59e0b",
        "step": 2,
        "action": "Put was assigned — preparing covered call",
        "what_now": (
            "The put was assigned and 100 shares have just been received. "
            "The effective cost basis is already below the assignment price "
            "because the put premium collected is subtracted from it."
        ),
        "what_next": (
            "The bot will immediately sell a <strong>covered call</strong> roughly 10% "
            "above the effective cost basis. This locks in a minimum profit if the shares "
            "are eventually called away, while generating additional income."
        ),
    },
    "SELL_CALL": {
        "label": "Stage 2 — Short Call Open",
        "color": "#10b981",
        "step": 3,
        "action": "Short call is open — earning income on owned shares",
        "what_now": (
            "A <strong>covered call</strong> has been sold against the 100 shares. "
            "The bot is generating additional premium income while holding the position. "
            "The call strike is always above the cost basis, guaranteeing a profit if "
            "the shares are called away."
        ),
        "what_next": (
            "<strong>If the stock stays below the strike at expiry:</strong> the call "
            "expires worthless, you keep the premium and keep the shares, and the bot "
            "sells another call — repeating Stage 2.<br><br>"
            "<strong>If the stock rises above the strike:</strong> the shares are sold "
            "(called away) at the strike price, locking in the stock gain plus both "
            "premiums. The cycle is complete and the bot restarts at Stage 1."
        ),
    },
}

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    f = BASE / "state.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {
        "stage": "IDLE",
        "ticker": "TSLA",
        "shares_owned": 0,
        "cost_basis_per_share": 0.0,
        "current_option_symbol": "",
        "current_option_sell_price": 0.0,
        "put_premium_this_cycle": 0.0,
        "call_premium_this_cycle": 0.0,
        "total_premiums_collected": 0.0,
        "cycle_count": 0,
        "last_summary_date": "",
    }


def load_recent_logs(n: int = 30) -> list:
    log_files = sorted(glob.glob(str(BASE / "logs" / "wheel_*.log")), reverse=True)
    lines = []
    for lf in log_files[:2]:
        try:
            with open(lf, encoding="utf-8") as fh:
                lines.extend(fh.readlines())
        except Exception:
            pass
    cleaned = [ln.rstrip() for ln in lines if ln.strip()]
    return cleaned[-n:]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    state = load_state()
    stage = state.get("stage", "IDLE")
    meta = STAGE_META.get(stage, STAGE_META["IDLE"])
    logs = load_recent_logs()
    return jsonify({"state": state, "meta": meta, "logs": logs})


# ---------------------------------------------------------------------------
# Dashboard HTML (single-file, no external dependencies)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wheel Strategy Dashboard</title>
<style>
  :root {
    --bg:        #0a0f1e;
    --surface:   #111827;
    --surface2:  #1f2937;
    --border:    #374151;
    --text:      #f1f5f9;
    --muted:     #9ca3af;
    --idle:      #64748b;
    --sell-put:  #3b82f6;
    --assigned:  #f59e0b;
    --sell-call: #10b981;
    --danger:    #ef4444;
    --radius:    12px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    font-size: 15px;
    line-height: 1.6;
    min-height: 100vh;
    padding-bottom: 40px;
  }

  /* ── Header ──────────────────────────────────────────── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  header h1 { font-size: 1.2rem; font-weight: 700; letter-spacing: 0.02em; }
  header h1 span { color: var(--muted); font-weight: 400; font-size: 0.95rem; }
  .header-right { display: flex; align-items: center; gap: 16px; font-size: 0.85rem; color: var(--muted); }
  .live-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
    animation: pulse 2s infinite;
    display: inline-block; margin-right: 6px;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  /* ── Layout ──────────────────────────────────────────── */
  main { max-width: 1200px; margin: 0 auto; padding: 28px 24px; display: grid; gap: 24px; }

  /* ── Section headings ────────────────────────────────── */
  .section-title {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin-bottom: 14px;
  }

  /* ── Cards ───────────────────────────────────────────── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
  }
  .card-label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
  .card-value { font-size: 1.5rem; font-weight: 700; }
  .card-sub   { font-size: 0.8rem; color: var(--muted); margin-top: 4px; }

  .cards-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }

  /* ── Wheel diagram ───────────────────────────────────── */
  .wheel-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 28px 24px;
  }

  .wheel-grid {
    display: grid;
    grid-template-columns: 1fr 56px 1fr;
    grid-template-rows: 1fr 56px 1fr;
    gap: 0;
    max-width: 600px;
    margin: 0 auto;
  }

  /* Positions in the 3x3 grid:
       [SELL PUT]       col 1-3, row 1
       [←]             col 1, row 2
       [hub]           col 2, row 2
       [→]             col 3, row 2
       [IDLE]   [SELL CALL]   col 1+3, row 3
  */

  .wstep {
    border: 2px solid var(--border);
    border-radius: 10px;
    padding: 16px 14px;
    text-align: center;
    transition: all 0.4s ease;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    cursor: default;
    user-select: none;
  }
  .wstep.active {
    border-color: var(--active-color, #3b82f6);
    background: color-mix(in srgb, var(--active-color, #3b82f6) 12%, transparent);
    box-shadow: 0 0 24px color-mix(in srgb, var(--active-color, #3b82f6) 35%, transparent);
  }
  .wstep .badge {
    width: 26px; height: 26px; border-radius: 50%;
    background: var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 0.75rem; font-weight: 700;
    transition: background 0.4s;
  }
  .wstep.active .badge { background: var(--active-color, #3b82f6); }
  .wstep .sname { font-weight: 600; font-size: 0.9rem; }
  .wstep .sdesc { font-size: 0.75rem; color: var(--muted); line-height: 1.4; }
  .wstep.active .sdesc { color: var(--text); }

  /* Grid placement */
  #ws-sell-put    { grid-column: 1; grid-row: 1; --active-color: var(--sell-put); }
  #ws-assigned    { grid-column: 3; grid-row: 1; --active-color: var(--assigned); }
  #ws-sell-call   { grid-column: 3; grid-row: 3; --active-color: var(--sell-call); }
  #ws-idle        { grid-column: 1; grid-row: 3; --active-color: var(--idle); }

  /* Hub */
  .wheel-hub {
    grid-column: 2; grid-row: 2;
    display: flex; align-items: center; justify-content: center;
    position: relative;
  }
  .hub-circle {
    width: 48px; height: 48px; border-radius: 50%;
    background: var(--surface2);
    border: 2px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 0.8rem; color: var(--text);
    z-index: 2; position: relative;
  }

  /* Arrows — SVG connectors */
  .wheel-arrow {
    display: flex; align-items: center; justify-content: center;
    color: var(--border);
    font-size: 1.4rem;
    user-select: none;
  }
  /* Top-right corner arrow (sell put → assigned) */
  .wa-tr { grid-column: 2; grid-row: 1; }
  /* Right arrow (assigned → sell call) */
  .wa-r  { grid-column: 3; grid-row: 2; }  /* unused slot */
  /* Bottom-right corner (sell call ← ) */
  .wa-br { grid-column: 2; grid-row: 3; }
  /* Left arrow (idle → sell put) */
  .wa-l  { grid-column: 1; grid-row: 2; }

  /* SVG connector lines */
  .connector-svg {
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    pointer-events: none;
    overflow: visible;
  }

  .wheel-wrapper {
    position: relative;
    max-width: 600px;
    margin: 0 auto;
  }

  /* ── Current status banner ───────────────────────────── */
  .status-banner {
    border-radius: var(--radius);
    padding: 18px 22px;
    border: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px;
    transition: background 0.4s, border-color 0.4s;
  }
  .status-dot { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
  .status-label { font-size: 1.05rem; font-weight: 700; }
  .status-action { font-size: 0.85rem; color: var(--muted); }

  /* ── Explanation cards ───────────────────────────────── */
  .explain-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 640px) { .explain-grid { grid-template-columns: 1fr; } }

  .explain-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
  }
  .explain-card h3 { font-size: 0.85rem; font-weight: 700; margin-bottom: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .explain-card p  { font-size: 0.9rem; color: var(--text); line-height: 1.7; }

  /* ── Premium tracker ─────────────────────────────────── */
  .premium-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; }

  /* ── Option detail ───────────────────────────────────── */
  .option-detail {
    background: var(--surface2);
    border-radius: 8px;
    padding: 14px 16px;
    font-size: 0.85rem;
    line-height: 1.8;
  }
  .option-detail .row { display: flex; justify-content: space-between; }
  .option-detail .row .k { color: var(--muted); }
  .option-detail .row .v { font-weight: 600; }

  .profit-bar-bg {
    height: 6px; border-radius: 3px; background: var(--border);
    margin-top: 10px; overflow: hidden;
  }
  .profit-bar-fill { height: 100%; border-radius: 3px; background: #22c55e; transition: width 0.6s ease; }

  /* ── Activity log ────────────────────────────────────── */
  .log-box {
    background: #060c18;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px;
    font-family: "SF Mono", "Fira Code", "Consolas", monospace;
    font-size: 0.78rem;
    line-height: 1.7;
    color: #94a3b8;
    max-height: 360px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-box .log-warn  { color: #f59e0b; }
  .log-box .log-error { color: #ef4444; }
  .log-box .log-info  { color: #94a3b8; }

  /* ── Helpers ─────────────────────────────────────────── */
  .green  { color: #22c55e; }
  .yellow { color: #f59e0b; }
  .red    { color: #ef4444; }
  .blue   { color: #60a5fa; }
  .pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 99px;
    font-size: 0.75rem;
    font-weight: 600;
    background: var(--surface2);
    border: 1px solid var(--border);
  }

  /* ── Empty state ─────────────────────────────────────── */
  .empty { color: var(--muted); font-style: italic; font-size: 0.85rem; }
</style>
</head>
<body>

<header>
  <h1>Wheel Strategy &nbsp;<span id="header-ticker">—</span></h1>
  <div class="header-right">
    <span><span class="live-dot"></span>Live</span>
    <span id="last-updated">Loading…</span>
    <span id="paper-badge" class="pill">Paper Trading</span>
  </div>
</header>

<main>

  <!-- ── Status banner ───────────────────────────────────────── -->
  <div id="status-banner" class="status-banner">
    <div id="status-dot" class="status-dot"></div>
    <div>
      <div id="status-label" class="status-label">Loading…</div>
      <div id="status-action" class="status-action"></div>
    </div>
  </div>

  <!-- ── Wheel diagram ───────────────────────────────────────── -->
  <div class="wheel-section">
    <div class="section-title">The Wheel — Current Stage</div>
    <div class="wheel-wrapper">
      <div class="wheel-grid">

        <!-- Step 1: Sell Put (top-left) -->
        <div class="wstep" id="ws-sell-put">
          <div class="badge">1</div>
          <div class="sname">Sell Put</div>
          <div class="sdesc">Collect premium<br>~10% below price</div>
        </div>

        <!-- Arrow: top row center -->
        <div class="wheel-arrow wa-tr">→</div>

        <!-- Step 2: Assigned (top-right) -->
        <div class="wstep" id="ws-assigned">
          <div class="badge">2</div>
          <div class="sname">Assigned</div>
          <div class="sdesc">Receive 100 shares<br>at strike price</div>
        </div>

        <!-- Arrow: left side center -->
        <div class="wheel-arrow wa-l">↑</div>

        <!-- Hub -->
        <div class="wheel-hub">
          <div class="hub-circle" id="hub-ticker">—</div>
        </div>

        <!-- Arrow: right side center -->
        <div class="wheel-arrow" style="grid-column:3;grid-row:2;">↓</div>

        <!-- Step 0: Idle (bottom-left) -->
        <div class="wstep" id="ws-idle">
          <div class="badge">↺</div>
          <div class="sname">Idle / Restart</div>
          <div class="sdesc">Cycle complete<br>ready for next put</div>
        </div>

        <!-- Arrow: bottom row center -->
        <div class="wheel-arrow wa-br">←</div>

        <!-- Step 3: Sell Call (bottom-right) -->
        <div class="wstep" id="ws-sell-call">
          <div class="badge">3</div>
          <div class="sname">Sell Call</div>
          <div class="sdesc">Earn income on<br>owned shares</div>
        </div>

      </div>
    </div>
  </div>

  <!-- ── Account & position cards ───────────────────────────── -->
  <div>
    <div class="section-title">Account Snapshot</div>
    <div class="cards-row">
      <div class="card">
        <div class="card-label">Cash Available</div>
        <div class="card-value" id="card-cash">—</div>
        <div class="card-sub">Available to sell puts</div>
      </div>
      <div class="card">
        <div class="card-label">Cycles Completed</div>
        <div class="card-value" id="card-cycles">—</div>
        <div class="card-sub">Full wheel turns</div>
      </div>
      <div class="card">
        <div class="card-label">Total Premiums</div>
        <div class="card-value green" id="card-premiums">—</div>
        <div class="card-sub">Across all cycles</div>
      </div>
      <div class="card" id="card-position-wrap">
        <div class="card-label">Stock Position</div>
        <div class="card-value" id="card-position">None</div>
        <div class="card-sub" id="card-cost-basis"></div>
      </div>
    </div>
  </div>

  <!-- ── Open option ─────────────────────────────────────────── -->
  <div id="option-section" style="display:none">
    <div class="section-title">Open Option Contract</div>
    <div class="card">
      <div class="option-detail" id="option-detail"></div>
    </div>
  </div>

  <!-- ── Explanation ─────────────────────────────────────────── -->
  <div>
    <div class="section-title">Strategy Explanation</div>
    <div class="explain-grid">
      <div class="explain-card">
        <h3>What's Happening Now</h3>
        <p id="explain-now">—</p>
      </div>
      <div class="explain-card">
        <h3>What Happens Next</h3>
        <p id="explain-next">—</p>
      </div>
    </div>
  </div>

  <!-- ── This cycle premiums ─────────────────────────────────── -->
  <div>
    <div class="section-title">This Cycle</div>
    <div class="premium-row">
      <div class="card">
        <div class="card-label">Put Premium</div>
        <div class="card-value green" id="cycle-put">$0.00</div>
        <div class="card-sub">Collected from Stage 1</div>
      </div>
      <div class="card">
        <div class="card-label">Call Premium</div>
        <div class="card-value green" id="cycle-call">$0.00</div>
        <div class="card-sub">Collected from Stage 2</div>
      </div>
      <div class="card">
        <div class="card-label">Cycle Total</div>
        <div class="card-value green" id="cycle-total">$0.00</div>
        <div class="card-sub">Put + call this cycle</div>
      </div>
    </div>
  </div>

  <!-- ── Activity log ────────────────────────────────────────── -->
  <div>
    <div class="section-title">Recent Activity</div>
    <div class="log-box" id="log-box">Waiting for log data…</div>
  </div>

</main>

<script>
  // Stage → DOM id map
  const STEP_IDS = {
    IDLE:        "ws-idle",
    SELL_PUT:    "ws-sell-put",
    HAVE_SHARES: "ws-assigned",
    SELL_CALL:   "ws-sell-call",
  };

  const STAGE_COLORS = {
    IDLE:        "#64748b",
    SELL_PUT:    "#3b82f6",
    HAVE_SHARES: "#f59e0b",
    SELL_CALL:   "#10b981",
  };

  function fmt(n) {
    if (n == null || n === 0) return "$0.00";
    return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function updateWheel(stage) {
    Object.keys(STEP_IDS).forEach(s => {
      document.getElementById(STEP_IDS[s]).classList.remove("active");
    });
    const el = document.getElementById(STEP_IDS[stage]);
    if (el) el.classList.add("active");
  }

  function updateBanner(meta, stage) {
    const color = STAGE_COLORS[stage] || "#64748b";
    const banner = document.getElementById("status-banner");
    banner.style.background = `color-mix(in srgb, ${color} 10%, var(--surface))`;
    banner.style.borderColor = color;
    document.getElementById("status-dot").style.background = color;
    document.getElementById("status-label").textContent = meta.label || stage;
    document.getElementById("status-action").textContent = meta.action || "";
  }

  function updateCards(state) {
    const ticker = state.ticker || "—";
    document.getElementById("header-ticker").textContent = ticker;
    document.getElementById("hub-ticker").textContent = ticker;

    // Account cards (cash not in state.json — shown if available)
    document.getElementById("card-cycles").textContent = state.cycle_count ?? 0;
    document.getElementById("card-premiums").textContent = fmt(state.total_premiums_collected);

    // Position
    const shares = state.shares_owned || 0;
    const posEl = document.getElementById("card-position");
    const basisEl = document.getElementById("card-cost-basis");
    if (shares > 0) {
      posEl.textContent = shares + " shares";
      posEl.className = "card-value green";
      basisEl.textContent = "Cost basis: " + fmt(state.cost_basis_per_share) + " / share";
    } else {
      posEl.textContent = "None";
      posEl.className = "card-value";
      basisEl.textContent = "No stock position open";
    }

    // Cash placeholder (not in state.json; shown as N/A)
    document.getElementById("card-cash").textContent = "See Alpaca";

    // Cycle premiums
    const putPrem  = state.put_premium_this_cycle  || 0;
    const callPrem = state.call_premium_this_cycle || 0;
    document.getElementById("cycle-put").textContent  = fmt(putPrem);
    document.getElementById("cycle-call").textContent = fmt(callPrem);
    document.getElementById("cycle-total").textContent = fmt(putPrem + callPrem);

    // Open option
    const sym = state.current_option_symbol;
    const optSection = document.getElementById("option-section");
    if (sym) {
      optSection.style.display = "";
      const sellPrice = state.current_option_sell_price || 0;
      const maxProfit = sellPrice * 100;
      // We don't have the current price in state, so show what we know
      const html = `
        <div class="row"><span class="k">Symbol</span><span class="v">${sym}</span></div>
        <div class="row"><span class="k">Type</span>
          <span class="v">${sym.includes("P0") ? "Put (Stage 1)" : "Call (Stage 2)"}</span></div>
        <div class="row"><span class="k">Sold At</span><span class="v">${fmt(sellPrice)} / share</span></div>
        <div class="row"><span class="k">Max Premium</span><span class="v">${fmt(maxProfit)} (1 contract)</span></div>
        <div class="row"><span class="k">Early Close At</span>
          <span class="v">${fmt(sellPrice * 0.5)} / share (50% target)</span></div>
      `;
      document.getElementById("option-detail").innerHTML = html;
    } else {
      optSection.style.display = "none";
    }
  }

  function updateExplanation(meta) {
    document.getElementById("explain-now").innerHTML  = meta.what_now  || "—";
    document.getElementById("explain-next").innerHTML = meta.what_next || "—";
  }

  function updateLog(logs) {
    const box = document.getElementById("log-box");
    if (!logs || logs.length === 0) {
      box.innerHTML = '<span class="empty">No log entries yet. Start wheel_agent.py to begin.</span>';
      return;
    }
    const html = logs.slice().reverse().map(line => {
      const lvl = line.includes("[ERROR") ? "log-error"
                : line.includes("[WARNING") ? "log-warn"
                : "log-info";
      return `<span class="${lvl}">${escapeHtml(line)}</span>`;
    }).join("\n");
    box.innerHTML = html;
  }

  function escapeHtml(s) {
    return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  async function refresh() {
    try {
      const r = await fetch("/api/status");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const { state, meta, logs } = data;
      const stage = state.stage || "IDLE";

      updateWheel(stage);
      updateBanner(meta, stage);
      updateCards(state);
      updateExplanation(meta);
      updateLog(logs);

      const now = new Date();
      document.getElementById("last-updated").textContent =
        "Updated " + now.toLocaleTimeString();
    } catch (e) {
      console.error("Refresh failed:", e);
      document.getElementById("last-updated").textContent = "Update failed — retrying…";
    }
  }

  refresh();
  setInterval(refresh, 30000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 50)
    print("  Wheel Strategy Dashboard")
    print("  http://localhost:5001")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False)
