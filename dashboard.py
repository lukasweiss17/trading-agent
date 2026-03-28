#!/usr/bin/env python3
"""
Alpaca Options Bot — Trading Dashboard
Production monitoring dashboard served via Python stdlib http.server.
No external Python dependencies required.
"""

import json
import time
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = 8050
BASE_DIR = Path(__file__).resolve().parent
ROI_FILE = BASE_DIR / "roi_tracker.json"
TRADE_LOG_FILE = BASE_DIR / "trade_log.json"
BOT_LOG_FILE = BASE_DIR / "trading-bot.log"
BOT_ALIVE_SECONDS = 120  # consider bot "running" if log touched within this

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _bot_status() -> str:
    try:
        mtime = BOT_LOG_FILE.stat().st_mtime
        return "running" if (time.time() - mtime) < BOT_ALIVE_SECONDS else "stopped"
    except FileNotFoundError:
        return "stopped"


def build_api_payload() -> dict:
    roi = _read_json(ROI_FILE) or {}
    trades = _read_json(TRADE_LOG_FILE) or []

    # Derive ROI fields from trade_log if roi_tracker.json missing/empty
    closed = [t for t in trades if t.get("action") == "CLOSE"]
    opened = [t for t in trades if t.get("action") == "OPEN"]

    # Build set of symbols that have been closed (match by order_id or symbol)
    closed_symbols = set()
    for c in closed:
        closed_symbols.add(c.get("symbol"))

    # Open positions = OPEN entries whose symbol has no matching CLOSE yet
    # We track by order_id if available, otherwise symbol
    open_order_ids = {}
    closed_order_ids = set()
    for t in trades:
        if t.get("action") == "OPEN":
            oid = t.get("order_id", t.get("symbol", "") + t.get("timestamp", ""))
            open_order_ids[oid] = t
        elif t.get("action") == "CLOSE":
            oid = t.get("order_id", t.get("symbol", "") + t.get("timestamp", ""))
            closed_order_ids.add(oid)

    # Deduplicate: keep only most-recent OPEN per symbol, skip symbols with a CLOSE
    close_count_by_symbol: dict[str, int] = {}
    for c in closed:
        sym = c.get("symbol", "")
        close_count_by_symbol[sym] = close_count_by_symbol.get(sym, 0) + 1

    # Last OPEN entry per symbol wins (handles restart-duplicate bug in old logs)
    latest_open_by_symbol: dict[str, dict] = {}
    for o in opened:
        sym = o.get("symbol", "")
        latest_open_by_symbol[sym] = o  # overwrite → last entry is canonical

    open_positions = [
        o for sym, o in latest_open_by_symbol.items()
        if close_count_by_symbol.get(sym, 0) == 0
    ]

    return {
        "roi": roi,
        "trades": trades,
        "closed_trades": closed,
        "open_positions": open_positions,
        "bot_status": _bot_status(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# HTML — complete dashboard
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alpaca Options Bot — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── Reset & Base ─────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080d1a;--card:#0c1425;--border:rgba(79,139,255,0.12);
  --accent:#4f8bff;--green:#22c55e;--red:#ef4444;--yellow:#eab308;
  --text:#e2eafc;--text2:#8898b8;--radius:14px;
  --font-text:'Inter',system-ui,sans-serif;
  --font-mono:'JetBrains Mono','Fira Code',monospace;
}
html{font-size:14px}
body{background:var(--bg);color:var(--text);font-family:var(--font-text);
  min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}

/* ── Scrollbar ────────────────────────────────────────────── */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(79,139,255,0.2);border-radius:3px}

/* ── Layout ───────────────────────────────────────────────── */
.shell{max-width:1440px;margin:0 auto;padding:16px 24px 40px}

/* ── Header ───────────────────────────────────────────────── */
.header{display:flex;align-items:center;justify-content:space-between;
  padding:14px 24px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:20px;flex-wrap:wrap;gap:12px}
.header-brand{display:flex;align-items:center;gap:10px;font-weight:700;
  font-size:1.05rem;letter-spacing:.03em}
.header-brand svg{width:20px;height:20px}
.header-right{display:flex;align-items:center;gap:20px}
.status-pill{display:inline-flex;align-items:center;gap:6px;
  font-size:.82rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.status-dot{width:8px;height:8px;border-radius:50%}
.status-dot.running{background:var(--green);box-shadow:0 0 6px var(--green)}
.status-dot.stopped{background:var(--red);box-shadow:0 0 6px var(--red)}
.clock{font-family:var(--font-mono);font-size:.92rem;color:var(--text2)}
.refresh-wrap{position:relative;width:28px;height:28px;cursor:pointer}
.refresh-ring{transform:rotate(-90deg)}
.refresh-ring circle{fill:none;stroke-width:2.5}
.refresh-ring .track{stroke:rgba(79,139,255,0.15)}
.refresh-ring .progress{stroke:var(--accent);stroke-linecap:round;
  transition:stroke-dashoffset .3s linear}

/* ── Metric Cards ─────────────────────────────────────────── */
.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:20px}
@media(max-width:1100px){.metrics{grid-template-columns:repeat(3,1fr)}}
@media(max-width:640px){.metrics{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 20px;transition:border-color .2s,box-shadow .2s}
.card:hover{border-color:rgba(79,139,255,0.28);
  box-shadow:0 0 20px rgba(79,139,255,0.06)}
.card-label{font-size:.75rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.07em;color:var(--text2);margin-bottom:8px}
.card-value{font-family:var(--font-mono);font-size:1.55rem;font-weight:700;line-height:1.1}
.card-sub{font-family:var(--font-mono);font-size:.78rem;margin-top:6px;color:var(--text2)}
.positive{color:var(--green)}.negative{color:var(--red)}

/* ── Middle Row ───────────────────────────────────────────── */
.mid-row{display:grid;grid-template-columns:1fr 340px;gap:14px;margin-bottom:20px}
@media(max-width:900px){.mid-row{grid-template-columns:1fr}}
.section{background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px 22px}
.section-title{font-size:.8rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;color:var(--text2);margin-bottom:14px;
  display:flex;align-items:center;gap:8px}
.section-title::before{content:'';width:3px;height:14px;border-radius:2px;
  background:var(--accent)}

/* ── Open Position Cards ──────────────────────────────────── */
.pos-list{display:flex;flex-direction:column;gap:10px;max-height:320px;
  overflow-y:auto;padding-right:4px}
.pos-card{background:rgba(79,139,255,0.04);border:1px solid var(--border);
  border-radius:10px;padding:14px 16px}
.pos-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.pos-ticker{font-weight:700;font-size:1rem;display:flex;align-items:center;gap:8px}
.pos-badge{font-size:.65rem;font-weight:700;padding:2px 7px;border-radius:4px;
  text-transform:uppercase;letter-spacing:.04em}
.badge-put{background:rgba(239,68,68,0.15);color:var(--red)}
.badge-call{background:rgba(34,197,94,0.15);color:var(--green)}
.badge-expiry-today{background:rgba(239,68,68,0.2);color:var(--red);animation:pulse 2s infinite}
.badge-expiry-tomorrow{background:rgba(234,179,8,0.18);color:var(--yellow)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.pos-details{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;
  font-size:.78rem;color:var(--text2)}
.pos-details span.val{font-family:var(--font-mono);color:var(--text)}

/* ── Chart containers ─────────────────────────────────────── */
.chart-wrap{position:relative;width:100%;height:260px}
.chart-wrap canvas{width:100%!important;height:100%!important}

/* ── P&L Bar section ──────────────────────────────────────── */
.pnl-section{margin-bottom:20px}
.pnl-chart-wrap{position:relative;width:100%;height:220px}

/* ── Trade History ────────────────────────────────────────── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.82rem}
thead th{text-align:left;padding:10px 12px;color:var(--text2);font-weight:600;
  font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;
  border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)}
tbody tr{border-bottom:1px solid rgba(79,139,255,0.06);transition:background .15s}
tbody tr:hover{background:rgba(79,139,255,0.04)}
tbody td{padding:9px 12px;font-family:var(--font-mono);font-size:.8rem;white-space:nowrap}
.row-win{border-left:3px solid var(--green)}
.row-loss{border-left:3px solid var(--red)}
.reason-badge{font-size:.65rem;font-weight:700;padding:2px 8px;border-radius:4px;
  text-transform:uppercase;letter-spacing:.03em;display:inline-block}
.reason-STOP_LOSS{background:rgba(239,68,68,0.15);color:var(--red)}
.reason-TRAILING_STOP{background:rgba(234,179,8,0.15);color:var(--yellow)}
.reason-EXPIRY_CLOSE{background:rgba(79,139,255,0.15);color:var(--accent)}
.reason-TAKE_PROFIT{background:rgba(34,197,94,0.15);color:var(--green)}
.reason-MANUAL{background:rgba(139,139,139,0.15);color:var(--text2)}
.win-label{font-weight:700;font-size:.65rem;padding:2px 6px;border-radius:3px;
  background:rgba(34,197,94,0.12);color:var(--green)}
.loss-label{font-weight:700;font-size:.65rem;padding:2px 6px;border-radius:3px;
  background:rgba(239,68,68,0.12);color:var(--red)}

/* ── Empty state ──────────────────────────────────────────── */
.empty{display:flex;align-items:center;justify-content:center;min-height:160px;
  color:var(--text2);font-size:.88rem;font-style:italic}

/* ── Utility ──────────────────────────────────────────────── */
.mono{font-family:var(--font-mono)}
.text-right{text-align:right}
</style>
</head>
<body>
<div class="shell">

<!-- ═══ HEADER ═══ -->
<header class="header">
  <div class="header-brand">
    <svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M11 1L3 12h6l-1 7 8-11h-6l1-7z" stroke="#4f8bff" stroke-width="1.5"
            stroke-linecap="round" stroke-linejoin="round" fill="rgba(79,139,255,.15)"/>
    </svg>
    ALPACA OPTIONS BOT
  </div>
  <div class="header-right">
    <div class="status-pill">
      <span class="status-dot" id="statusDot"></span>
      <span id="statusLabel">—</span>
    </div>
    <div class="clock" id="clock">--:--:--</div>
    <div class="refresh-wrap" id="refreshWrap" title="Auto-refresh countdown">
      <svg class="refresh-ring" viewBox="0 0 28 28">
        <circle class="track" cx="14" cy="14" r="11"/>
        <circle class="progress" id="ringProgress" cx="14" cy="14" r="11"
                stroke-dasharray="69.115" stroke-dashoffset="0"/>
      </svg>
    </div>
  </div>
</header>

<!-- ═══ METRIC CARDS ═══ -->
<div class="metrics">
  <div class="card">
    <div class="card-label">Capital</div>
    <div class="card-value mono" id="mCapital">—</div>
    <div class="card-sub" id="mCapitalDelta">—</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&amp;L</div>
    <div class="card-value mono" id="mPnl">—</div>
    <div class="card-sub" id="mPnlArrow">—</div>
  </div>
  <div class="card">
    <div class="card-label">ROI</div>
    <div class="card-value mono" id="mRoi">—</div>
    <div class="card-sub">&nbsp;</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value mono" id="mWinRate">—</div>
    <div class="card-sub" id="mWL">—</div>
  </div>
  <div class="card">
    <div class="card-label">Trades</div>
    <div class="card-value mono" id="mTrades">—</div>
    <div class="card-sub" id="mTradesToday">—</div>
  </div>
  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value mono" id="mOpen">—</div>
    <div class="card-sub" id="mNextExpiry">—</div>
  </div>
</div>

<!-- ═══ MID ROW — chart + open positions ═══ -->
<div class="mid-row">
  <div class="section">
    <div class="section-title">Capital Over Time</div>
    <div class="chart-wrap"><canvas id="capitalChart"></canvas></div>
  </div>
  <div class="section">
    <div class="section-title">Open Positions</div>
    <div class="pos-list" id="posList"><div class="empty">No open positions</div></div>
  </div>
</div>

<!-- ═══ P&L BAR CHART ═══ -->
<div class="section pnl-section">
  <div class="section-title">P&amp;L Per Trade</div>
  <div class="pnl-chart-wrap"><canvas id="pnlChart"></canvas></div>
</div>

<!-- ═══ TRADE HISTORY ═══ -->
<div class="section">
  <div class="section-title">Trade History</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Time</th><th>Ticker</th><th>Dir</th><th>Strike</th>
          <th class="text-right">Entry</th><th class="text-right">Exit</th>
          <th class="text-right">P&amp;L $</th><th class="text-right">P&amp;L %</th>
          <th>Reason</th><th></th>
        </tr>
      </thead>
      <tbody id="tradeBody"></tbody>
    </table>
    <div class="empty" id="tradeEmpty">No closed trades yet</div>
  </div>
</div>

</div><!-- .shell -->

<script>
/* ══════════════════════════════════════════════════════════════
   DASHBOARD LOGIC
   ══════════════════════════════════════════════════════════════ */

// ── Helpers ──────────────────────────────────────────────────
const $ = s => document.getElementById(s);
const fmt$ = (n, decimals=2) => {
  if (n == null || isNaN(n)) return '—';
  const abs = Math.abs(n);
  const s = abs.toLocaleString('en-US',{minimumFractionDigits:decimals,maximumFractionDigits:decimals});
  return (n >= 0 ? '+$' : '-$') + s;
};
const fmtMoney = (n, decimals=2) => {
  if (n == null || isNaN(n)) return '—';
  return '$' + Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:decimals,maximumFractionDigits:decimals});
};
const fmtPct = (n) => {
  if (n == null || isNaN(n)) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
};
const cls = (n) => n >= 0 ? 'positive' : 'negative';

function parseOCC(sym) {
  // OCC format: TICKER + YYMMDD + P/C + 00000000 (strike * 1000)
  if (!sym || sym.length < 15) return null;
  // Find where date starts: scan for 6 digits followed by P or C
  const m = sym.match(/^([A-Z]+)(\d{6})([PC])(\d{8})$/);
  if (!m) return null;
  const ticker = m[1];
  const dateStr = m[2]; // YYMMDD
  const type = m[3] === 'P' ? 'PUT' : 'CALL';
  const strike = parseInt(m[4], 10) / 1000;
  const yy = parseInt(dateStr.slice(0,2),10);
  const mm = parseInt(dateStr.slice(2,4),10);
  const dd = parseInt(dateStr.slice(4,6),10);
  const expiry = new Date(2000+yy, mm-1, dd);
  return {ticker, expiry, type, strike,
    expiryStr: `${(2000+yy)}-${String(mm).padStart(2,'0')}-${String(dd).padStart(2,'0')}`};
}

function todayStr(){ return new Date().toISOString().slice(0,10); }
function tomorrowStr(){
  const d = new Date(); d.setDate(d.getDate()+1);
  return d.toISOString().slice(0,10);
}
function formatTS(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const dd = String(d.getDate()).padStart(2,'0');
  const hh = String(d.getHours()).padStart(2,'0');
  const mi = String(d.getMinutes()).padStart(2,'0');
  return `${mm}/${dd} ${hh}:${mi}`;
}

// ── Chart instances ──────────────────────────────────────────
let capitalChartInst = null;
let pnlChartInst = null;

function buildCapitalChart(closed, roi) {
  const ctx = $('capitalChart').getContext('2d');
  const startCap = (roi && roi.starting_capital) ? roi.starting_capital : 10000;
  // Build cumulative capital series from closed trades
  let points = [{x: 0, y: startCap, label: 'Start'}];
  let running = startCap;
  const sorted = [...closed].sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp));
  sorted.forEach((t, i) => {
    const pnl = t.pnl_dollar || 0;
    running += pnl;
    points.push({x: i+1, y: running, label: formatTS(t.timestamp), ticker: t.ticker || ''});
  });

  const labels = points.map(p => p.label);
  const data = points.map(p => p.y);
  const finalAboveStart = running >= startCap;

  if (capitalChartInst) { capitalChartInst.destroy(); }

  capitalChartInst = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        fill: true,
        borderColor: finalAboveStart ? '#22c55e' : '#ef4444',
        borderWidth: 2,
        backgroundColor: function(context) {
          const chart = context.chart;
          const {ctx: c, chartArea} = chart;
          if (!chartArea) return 'transparent';
          const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          if (finalAboveStart) {
            g.addColorStop(0, 'rgba(34,197,94,0.25)');
            g.addColorStop(1, 'rgba(34,197,94,0.01)');
          } else {
            g.addColorStop(0, 'rgba(239,68,68,0.25)');
            g.addColorStop(1, 'rgba(239,68,68,0.01)');
          }
          return g;
        },
        tension: 0.35,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: finalAboveStart ? '#22c55e' : '#ef4444',
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor: '#0c1425',
          borderColor: 'rgba(79,139,255,0.3)',
          borderWidth: 1,
          titleFont: {family: "'Inter'", size: 12},
          bodyFont: {family: "'JetBrains Mono'", size: 12},
          padding: 12,
          callbacks: {
            title: function(items) { return items[0].label || ''; },
            label: function(item) {
              const val = item.parsed.y;
              const diff = val - startCap;
              return [
                'Capital: $' + val.toLocaleString('en-US',{minimumFractionDigits:2}),
                'P&L: ' + (diff>=0?'+':'') + '$' + diff.toLocaleString('en-US',{minimumFractionDigits:2})
              ];
            }
          }
        }
      },
      scales: {
        x: {display: false},
        y: {
          grid: {color: 'rgba(79,139,255,0.06)', drawBorder: false},
          ticks: {
            font: {family: "'JetBrains Mono'", size: 11},
            color: '#8898b8',
            callback: v => '$' + v.toLocaleString()
          },
          border: {display: false}
        }
      }
    }
  });
}

function buildPnlChart(closed) {
  const ctx = $('pnlChart').getContext('2d');
  const sorted = [...closed].sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp));
  const labels = sorted.map(t => {
    const d = new Date(t.timestamp);
    const mm = String(d.getMonth()+1).padStart(2,'0');
    const dd = String(d.getDate()).padStart(2,'0');
    return (t.ticker||'?') + ' ' + mm + '/' + dd;
  });
  const data = sorted.map(t => t.pnl_dollar || 0);
  const colors = data.map(v => v >= 0 ? 'rgba(34,197,94,0.75)' : 'rgba(239,68,68,0.75)');
  const hoverColors = data.map(v => v >= 0 ? '#22c55e' : '#ef4444');

  if (pnlChartInst) { pnlChartInst.destroy(); }

  pnlChartInst = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors,
        hoverBackgroundColor: hoverColors,
        borderRadius: 4,
        maxBarThickness: 40,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor: '#0c1425',
          borderColor: 'rgba(79,139,255,0.3)',
          borderWidth: 1,
          titleFont: {family: "'Inter'", size: 12},
          bodyFont: {family: "'JetBrains Mono'", size: 12},
          padding: 12,
          callbacks: {
            title: items => items[0].label,
            label: function(item) {
              const t = sorted[item.dataIndex];
              const lines = [];
              lines.push('P&L: ' + fmt$(t.pnl_dollar));
              if (t.pnl_pct != null) lines.push('P&L %: ' + fmtPct(t.pnl_pct));
              if (t.entry_price != null) lines.push('Entry: $' + t.entry_price.toFixed(2));
              if (t.exit_price != null) lines.push('Exit: $' + t.exit_price.toFixed(2));
              if (t.reason) lines.push('Reason: ' + t.reason);
              return lines;
            }
          }
        }
      },
      scales: {
        x: {
          grid: {display: false},
          ticks: {
            font: {family: "'JetBrains Mono'", size: 10},
            color: '#8898b8',
            maxRotation: 45,
          },
          border: {display: false}
        },
        y: {
          grid: {color: 'rgba(79,139,255,0.06)', drawBorder: false},
          ticks: {
            font: {family: "'JetBrains Mono'", size: 11},
            color: '#8898b8',
            callback: v => (v>=0?'+$':'-$') + Math.abs(v).toLocaleString()
          },
          border: {display: false}
        }
      }
    }
  });
}

// ── Open Positions Renderer ──────────────────────────────────
function renderPositions(positions) {
  const el = $('posList');
  if (!positions || positions.length === 0) {
    el.innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const today = todayStr();
  const tomorrow = tomorrowStr();
  el.innerHTML = positions.map(p => {
    const occ = parseOCC(p.symbol);
    const ticker = occ ? occ.ticker : (p.ticker || '???');
    const type = occ ? occ.type : (p.direction || '').toUpperCase();
    const strike = occ ? occ.strike.toFixed(2) : '—';
    const expiryStr = occ ? occ.expiryStr : '—';
    const badgeType = type === 'PUT' ? 'badge-put' : 'badge-call';
    let expiryBadge = '';
    if (occ) {
      if (occ.expiryStr === today) expiryBadge = '<span class="pos-badge badge-expiry-today">EXPIRY TODAY</span>';
      else if (occ.expiryStr === tomorrow) expiryBadge = '<span class="pos-badge badge-expiry-tomorrow">EXPIRY TOMORROW</span>';
    }
    const qty = p.qty || 1;
    const entry = p.entry_price != null ? '$' + p.entry_price.toFixed(2) : '—';
    const cost = p.cost != null ? '$' + p.cost.toLocaleString('en-US',{minimumFractionDigits:2}) : '—';
    return `<div class="pos-card">
      <div class="pos-header">
        <div class="pos-ticker">${ticker} <span class="pos-badge ${badgeType}">${type}</span> $${strike} ${expiryBadge}</div>
      </div>
      <div class="pos-details">
        <span>Expiry</span><span class="val">${expiryStr}</span>
        <span>Qty</span><span class="val">${qty} lot${qty>1?'s':''}</span>
        <span>Entry</span><span class="val">${entry}</span>
        <span>Cost</span><span class="val">${cost}</span>
      </div>
    </div>`;
  }).join('');
}

// ── Trade History Renderer ───────────────────────────────────
function renderTradeHistory(closed) {
  const tbody = $('tradeBody');
  const emptyEl = $('tradeEmpty');
  if (!closed || closed.length === 0) {
    tbody.innerHTML = '';
    emptyEl.style.display = 'flex';
    return;
  }
  emptyEl.style.display = 'none';
  // Most recent first, max 30
  const sorted = [...closed].sort((a,b) => new Date(b.timestamp) - new Date(a.timestamp)).slice(0, 30);
  tbody.innerHTML = sorted.map(t => {
    const isWin = (t.pnl_dollar || 0) >= 0;
    const rowCls = isWin ? 'row-win' : 'row-loss';
    const occ = parseOCC(t.symbol);
    const strike = occ ? '$' + occ.strike.toFixed(0) : '—';
    const dir = (t.direction || '').toUpperCase();
    const entry = t.entry_price != null ? '$' + t.entry_price.toFixed(2) : '—';
    const exit = t.exit_price != null ? '$' + t.exit_price.toFixed(2) : '—';
    const pnl$ = t.pnl_dollar != null ? fmt$(t.pnl_dollar) : '—';
    const pnlPct = t.pnl_pct != null ? fmtPct(t.pnl_pct) : '—';
    const pnlCls = cls(t.pnl_dollar || 0);
    const reason = t.reason || '—';
    const reasonCls = 'reason-' + reason;
    const wlLabel = isWin
      ? '<span class="win-label">WIN</span>'
      : '<span class="loss-label">LOSS</span>';
    return `<tr class="${rowCls}">
      <td>${formatTS(t.timestamp)}</td>
      <td style="font-weight:600">${t.ticker || (occ?occ.ticker:'—')}</td>
      <td>${dir}</td>
      <td>${strike}</td>
      <td class="text-right">${entry}</td>
      <td class="text-right">${exit}</td>
      <td class="text-right ${pnlCls}">${pnl$}</td>
      <td class="text-right ${pnlCls}">${pnlPct}</td>
      <td><span class="reason-badge ${reasonCls}">${reason.replace(/_/g,' ')}</span></td>
      <td>${wlLabel}</td>
    </tr>`;
  }).join('');
}

// ── Metric Cards Updater ─────────────────────────────────────
function updateMetrics(data) {
  const roi = data.roi || {};
  const closed = data.closed_trades || [];
  const openPos = data.open_positions || [];
  const allTrades = data.trades || [];

  const startCap = roi.starting_capital || 10000;
  // Compute values from closed trades if roi fields missing
  const totalPnl = roi.total_pnl != null ? roi.total_pnl : closed.reduce((s,t)=>s+(t.pnl_dollar||0),0);
  const currentCap = roi.current_capital != null ? roi.current_capital : startCap + totalPnl;
  const totalTrades = roi.total_trades != null ? roi.total_trades : closed.length;
  const wins = roi.winning_trades != null ? roi.winning_trades : closed.filter(t=>(t.pnl_dollar||0)>=0).length;
  const losses = roi.losing_trades != null ? roi.losing_trades : closed.filter(t=>(t.pnl_dollar||0)<0).length;
  const roiPct = roi.roi_pct != null ? roi.roi_pct : (startCap ? (totalPnl/startCap)*100 : 0);
  const winRate = totalTrades > 0 ? (wins/totalTrades)*100 : 0;

  // Today's trades
  const today = todayStr();
  const todayTrades = closed.filter(t => t.timestamp && t.timestamp.slice(0,10) === today).length;

  // Next expiry from open positions
  let nextExpiry = null;
  openPos.forEach(p => {
    const occ = parseOCC(p.symbol);
    if (occ) {
      if (!nextExpiry || occ.expiryStr < nextExpiry) nextExpiry = occ.expiryStr;
    }
  });

  // Capital
  $('mCapital').textContent = fmtMoney(currentCap);
  const delta = currentCap - startCap;
  $('mCapitalDelta').textContent = fmt$(delta);
  $('mCapitalDelta').className = 'card-sub ' + cls(delta);

  // P&L
  $('mPnl').textContent = fmt$(totalPnl);
  $('mPnl').className = 'card-value mono ' + cls(totalPnl);
  $('mPnlArrow').innerHTML = totalPnl >= 0
    ? '<span class="positive">&#9650; profit</span>'
    : '<span class="negative">&#9660; loss</span>';

  // ROI
  $('mRoi').textContent = fmtPct(roiPct);
  $('mRoi').className = 'card-value mono ' + cls(roiPct);

  // Win Rate
  $('mWinRate').textContent = winRate.toFixed(1) + '%';
  $('mWinRate').className = 'card-value mono ' + (winRate >= 50 ? 'positive' : winRate > 0 ? 'negative' : '');
  $('mWL').textContent = wins + 'W / ' + losses + 'L';

  // Trades
  $('mTrades').textContent = totalTrades;
  $('mTradesToday').textContent = 'today: ' + todayTrades;

  // Open
  $('mOpen').textContent = openPos.length;
  $('mNextExpiry').textContent = nextExpiry ? 'next expiry: ' + nextExpiry.slice(5) : '—';
}

// ── Status & Clock ───────────────────────────────────────────
function updateStatus(status) {
  const dot = $('statusDot');
  const label = $('statusLabel');
  if (status === 'running') {
    dot.className = 'status-dot running';
    label.textContent = 'RUNNING';
    label.style.color = '#22c55e';
  } else {
    dot.className = 'status-dot stopped';
    label.textContent = 'STOPPED';
    label.style.color = '#ef4444';
  }
}

function tickClock() {
  const now = new Date();
  $('clock').textContent = now.toLocaleTimeString('en-US',{hour12:false});
}
setInterval(tickClock, 1000);
tickClock();

// ── Refresh ring ─────────────────────────────────────────────
const INTERVAL = 30; // seconds
const CIRCUMFERENCE = 2 * Math.PI * 11; // r=11
let countdown = INTERVAL;
let ringTimer = null;

function updateRing() {
  const frac = countdown / INTERVAL;
  const offset = CIRCUMFERENCE * (1 - frac);
  $('ringProgress').style.strokeDashoffset = offset;
}

function startRingTimer() {
  countdown = INTERVAL;
  updateRing();
  if (ringTimer) clearInterval(ringTimer);
  ringTimer = setInterval(() => {
    countdown--;
    updateRing();
    if (countdown <= 0) {
      fetchData();
      countdown = INTERVAL;
    }
  }, 1000);
}

// ── Fetch & Render ───────────────────────────────────────────
async function fetchData() {
  try {
    const res = await fetch('/api/data');
    const data = await res.json();
    updateStatus(data.bot_status);
    updateMetrics(data);
    buildCapitalChart(data.closed_trades || [], data.roi || {});
    buildPnlChart(data.closed_trades || []);
    renderPositions(data.open_positions || []);
    renderTradeHistory(data.closed_trades || []);
  } catch (e) {
    console.error('Fetch error:', e);
  }
}

// ── Init ─────────────────────────────────────────────────────
fetchData();
startRingTimer();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the dashboard HTML and JSON API."""

    def do_GET(self):
        if self.path == "/":
            self._send(200, "text/html", HTML.encode())
        elif self.path == "/api/data":
            payload = build_api_payload()
            self._send(200, "application/json", json.dumps(payload).encode())
        else:
            self._send(404, "text/plain", b"Not Found")

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Quieter logging: single-line with timestamp
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {args[0]}" if args else "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"{'='*56}")
    print(f"  Alpaca Options Bot Dashboard")
    print(f"  http://localhost:{PORT}")
    print(f"{'='*56}")
    print(f"  ROI file:   {ROI_FILE}")
    print(f"  Trade log:  {TRADE_LOG_FILE}")
    print(f"  Bot log:    {BOT_LOG_FILE}")
    print(f"{'='*56}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
