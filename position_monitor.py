"""
Position Monitor + Dashboard
=============================
1. SL/TP monitor — closes positions when stop/target is hit (paper + live).
2. Auto-close — closes stale positions after MAX_HOLD_HOURS etc.
3. Web dashboard at /dashboard/ with KPIs, live PnL, chart, CSV export.
"""

import csv
import io
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from flask import Blueprint, render_template_string, jsonify, Response

import config

logger = logging.getLogger(__name__)


# ======================================================================
# PUBLIC PRICE FETCHING (no API keys — safe to call every 30s)
# ======================================================================
_public_client = None
_public_lock = threading.Lock()
_price_cache: Dict[str, Dict[str, Any]] = {}


def _get_public():
    global _public_client
    if _public_client is None:
        with _public_lock:
            if _public_client is None:
                import ccxt
                _public_client = ccxt.binanceusdm({
                    'enableRateLimit': True,
                    'options': {'defaultType': 'future'},
                })
                logger.info("Monitor: public ccxt client initialized")
    return _public_client


def _sym(symbol: str) -> str:
    return symbol.replace('/', '').upper()


def get_price(symbol: str) -> Optional[float]:
    """Fetch price with 30s cache."""
    now = datetime.now()
    cached = _price_cache.get(symbol)
    if cached and (now - cached['at']).total_seconds() < 30:
        return cached['price']

    if config.MONITOR_USE_PUBLIC_PRICE:
        try:
            ex = _get_public()
            t = ex.fetch_ticker(_sym(symbol))
            p = float(t.get('last') or t.get('close') or 0)
            if p > 0:
                _price_cache[symbol] = {'price': p, 'at': now}
                return p
        except Exception as e:
            logger.debug(f"Price fetch failed {symbol}: {e}")

    return cached['price'] if cached else None


def get_prices_bulk(symbols: List[str]) -> Dict[str, float]:
    out = {}
    for s in symbols:
        p = get_price(s)
        if p is not None:
            out[s] = p
    return out


# ======================================================================
# SL/TP CHECKER
# ======================================================================
def _check_sl_tp(pos: Dict, price: float) -> Optional[str]:
    side = pos.get('side')
    sl = float(pos.get('stop_loss') or 0)
    tp = float(pos.get('take_profit') or 0)
    if not sl and not tp:
        return None
    if side == 'long':
        if sl > 0 and price <= sl:
            return 'stop_loss_hit'
        if tp > 0 and price >= tp:
            return 'take_profit_hit'
    elif side == 'short':
        if sl > 0 and price >= sl:
            return 'stop_loss_hit'
        if tp > 0 and price <= tp:
            return 'take_profit_hit'
    return None


def _check_auto_close(pos: Dict, price: float) -> Optional[str]:
    if not config.AUTO_CLOSE_ENABLED:
        return None
    try:
        opened = datetime.fromisoformat(pos['opened_at'])
    except Exception:
        return None
    age_h = (datetime.now() - opened).total_seconds() / 3600.0

    entry = float(pos['entry_price'])
    qty = float(pos['quantity'])
    side = pos['side']
    pnl_usd = (price - entry) * qty if side == 'long' else (entry - price) * qty

    if age_h >= config.MAX_HOLD_HOURS:
        return f'auto_close_max_hold_{age_h:.1f}h'
    if pnl_usd < 0 and age_h >= config.LOSS_CUT_HOURS:
        return f'auto_close_loss_timeout_{age_h:.1f}h'
    if pnl_usd > 0 and age_h >= config.PROFIT_KEEP_HOURS:
        return f'auto_close_profit_timeout_{age_h:.1f}h'
    return None


def _monitor_iteration():
    try:
        from storage import PositionStore
        from position_manager import handle_exit
    except Exception as e:
        logger.error(f"Monitor import failed: {e}")
        return

    try:
        open_positions = PositionStore.get_open()
    except Exception as e:
        logger.error(f"Monitor get_open failed: {e}")
        return

    if not open_positions:
        return

    for pos in open_positions:
        symbol = pos.get('symbol')
        if not symbol:
            continue
        try:
            price = get_price(symbol)
            if price is None:
                continue

            reason = _check_sl_tp(pos, price)
            if reason:
                logger.info(f"Monitor: {symbol} hit {reason} @ {price}")
                handle_exit({'symbol': symbol, 'reason': reason})
                continue

            reason = _check_auto_close(pos, price)
            if reason:
                logger.info(f"Monitor: {symbol} auto-close: {reason}")
                handle_exit({'symbol': symbol, 'reason': reason})
        except Exception as e:
            logger.error(f"Monitor error for {symbol}: {e}")


_stop_event = threading.Event()


def _monitor_loop():
    logger.info(f"PositionMonitor started (interval={config.MONITOR_INTERVAL_SEC}s)")
    time.sleep(15)
    while not _stop_event.is_set():
        try:
            _monitor_iteration()
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        for _ in range(config.MONITOR_INTERVAL_SEC):
            if _stop_event.is_set():
                break
            time.sleep(1)
    logger.info("PositionMonitor stopped")


def start_monitor():
    t = threading.Thread(target=_monitor_loop, daemon=True, name="PositionMonitor")
    t.start()


def stop_monitor():
    _stop_event.set()


# ======================================================================
# DASHBOARD
# ======================================================================
dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')


def _enrich_open(pos: Dict, price: Optional[float]) -> Dict:
    r = dict(pos)
    r['current_price'] = price
    r['unrealized_pnl_usd'] = None
    r['unrealized_pnl_pct'] = None
    r['age_minutes'] = None
    try:
        opened = datetime.fromisoformat(pos['opened_at'])
        r['age_minutes'] = round((datetime.now() - opened).total_seconds() / 60.0, 1)
    except Exception:
        pass
    if price is None:
        return r
    try:
        entry = float(pos['entry_price'])
        qty = float(pos['quantity'])
        side = pos['side']
        pnl_usd = (price - entry) * qty if side == 'long' else (entry - price) * qty
        notional = entry * qty
        r['unrealized_pnl_usd'] = round(pnl_usd, 4)
        r['unrealized_pnl_pct'] = round((pnl_usd / notional * 100.0) if notional else 0.0, 4)
    except Exception:
        pass
    return r


def _stats() -> Dict:
    from storage import PositionStore
    open_pos = PositionStore.get_open()
    recent = PositionStore.get_recent(limit=100)
    today = PositionStore.get_today_stats()
    closed = [p for p in recent if p.get('status') == 'closed']
    wins = [p for p in closed if (p.get('pnl_usd') or 0) > 0]
    losses = [p for p in closed if (p.get('pnl_usd') or 0) < 0]
    total = sum(float(p.get('pnl_usd') or 0) for p in closed)
    avg_win = (sum(float(p['pnl_usd']) for p in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(float(p['pnl_usd']) for p in losses) / len(losses)) if losses else 0.0

    by_sym: Dict[str, Dict] = {}
    for p in closed:
        s = p['symbol']
        by_sym.setdefault(s, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        by_sym[s]['trades'] += 1
        by_sym[s]['pnl'] += float(p.get('pnl_usd') or 0)
        if float(p.get('pnl_usd') or 0) > 0:
            by_sym[s]['wins'] += 1
    top = sorted([{'symbol': k, **v} for k, v in by_sym.items()],
                 key=lambda x: x['pnl'], reverse=True)[:5]

    return {
        'paper_trading': config.PAPER_TRADING,
        'leverage': config.LEVERAGE,
        'position_size_usd': config.POSITION_SIZE_USD,
        'open_count': len(open_pos),
        'closed_count': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': (len(wins) / len(closed) * 100.0) if closed else 0.0,
        'total_pnl_usd': round(total, 4),
        'avg_win_usd': round(avg_win, 4),
        'avg_loss_usd': round(avg_loss, 4),
        'today': {
            'date': today.get('date'),
            'trades_opened': int(today.get('trades_opened') or 0),
            'trades_closed': int(today.get('trades_closed') or 0),
            'realized_pnl': round(float(today.get('realized_pnl') or 0), 4),
            'wins': int(today.get('wins') or 0),
            'losses': int(today.get('losses') or 0),
        },
        'top_symbols': top,
        'monitor_interval_sec': config.MONITOR_INTERVAL_SEC,
        'auto_close_enabled': config.AUTO_CLOSE_ENABLED,
        'max_hold_hours': config.MAX_HOLD_HOURS,
    }


def _daily_chart(days: int = 14) -> List[Dict]:
    from storage import _conn
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT date, realized_pnl FROM daily_stats "
                "WHERE date >= ? ORDER BY date ASC",
                (since,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"chart data failed: {e}")
        return []


def _webhook_log(limit: int = 30) -> List[Dict]:
    from storage import _conn
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM webhook_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"webhook log failed: {e}")
        return []


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Execution Bot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#0d1117;color:#e6edf3;padding:24px;min-height:100vh}
  h1{font-size:24px;margin-bottom:4px}
  h2{font-size:18px;margin:24px 0 12px;color:#58a6ff}
  .subtitle{color:#8b949e;font-size:13px;margin-bottom:20px}
  .grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:24px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
  .card .label{color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  .card .value{font-size:24px;font-weight:600;margin-top:6px}
  .card .extra{color:#8b949e;font-size:12px;margin-top:4px}
  .pos{color:#3fb950}.neg{color:#f85149}.neutral{color:#8b949e}
  .tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;text-transform:uppercase}
  .tag-paper{background:#1f6feb;color:#fff}.tag-live{background:#da3633;color:#fff}
  .tag-long{background:#238636;color:#fff}.tag-short{background:#a40e26;color:#fff}
  table{width:100%;border-collapse:collapse;font-size:13px;background:#161b22;border-radius:8px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #21262d}
  th{background:#0d1117;color:#8b949e;font-weight:600;font-size:12px;text-transform:uppercase}
  tr:hover td{background:#1c2128}
  .btn{background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:6px 12px;
       border-radius:6px;cursor:pointer;font-size:12px;transition:all .15s}
  .btn:hover{background:#30363d}
  .btn-danger{background:#da3633;color:#fff;border-color:#da3633}
  .btn-danger:hover{background:#f85149}
  .btn-primary{background:#1f6feb;color:#fff;border-color:#1f6feb}
  .btn-primary:hover{background:#388bfd}
  .controls{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  .controls input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;
                  padding:6px 10px;border-radius:6px;font-size:13px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .dot-live{background:#3fb950;animation:pulse 1.5s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .empty{color:#8b949e;padding:20px;text-align:center;font-style:italic}
  .chart-wrap{background:#161b22;border-radius:8px;padding:16px;height:280px;position:relative}
  .badge{display:inline-flex;align-items:center;background:#21262d;padding:4px 10px;
         border-radius:12px;font-size:11px;color:#8b949e}
</style>
</head>
<body>

<h1>Execution Bot Dashboard <span id="mode-badge"></span></h1>
<div class="subtitle">
  <span class="badge"><span class="dot dot-live"></span> Live · refresh every 5s · <span id="last-update">–</span></span>
  <span class="badge" style="margin-left:8px">SL/TP: <strong id="monitor-int">–</strong>s · Auto-close: <strong id="auto-close">–</strong></span>
</div>

<div class="grid" id="kpi-grid"></div>

<div class="controls">
  <button class="btn btn-primary" onclick="refreshAll()">Refresh Now</button>
  <button class="btn" onclick="location.href='/dashboard/api/export.csv'">Export CSV</button>
  <button class="btn btn-danger" onclick="closeAll()">Emergency Close All</button>
  <input id="filter-symbol" type="text" placeholder="Filter symbol..." style="width:140px">
</div>

<h2>Open Positions <span id="open-count" class="neutral"></span></h2>
<div id="open-positions"></div>

<h2>Recent Closed Positions</h2>
<div id="closed-positions"></div>

<h2>Daily Realized PnL (Last 14 Days)</h2>
<div class="chart-wrap"><canvas id="pnl-chart"></canvas></div>

<h2>Top Performing Symbols</h2>
<div id="top-symbols"></div>

<h2>Recent Webhook Activity</h2>
<div id="webhook-log"></div>

<script>
let chart=null;
const REFRESH=5000;

async function fetchJSON(url,opt={}){
  const r=await fetch(url,opt);const ct=r.headers.get('content-type')||'';
  if(!ct.includes('json'))throw new Error('non-JSON');return r.json();
}
function fmtUsd(v){if(v==null)return'–';return(v>=0?'+':'−')+'$'+Math.abs(v).toFixed(2)}
function fmtPct(v){if(v==null)return'–';return(v>=0?'+':'−')+Math.abs(v).toFixed(2)+'%'}
function cls(v){if(v==null)return'neutral';return v>0?'pos':(v<0?'neg':'neutral')}

async function refreshStats(){
  try{
    const d=await fetchJSON('/dashboard/api/stats');
    renderKpis(d.stats);renderOpen(d.open_positions);renderClosed(d.recent_closed);
    renderTop(d.stats.top_symbols);updateChart(d.daily_chart);
    document.getElementById('last-update').textContent=new Date().toLocaleTimeString();
    document.getElementById('monitor-int').textContent=d.stats.monitor_interval_sec;
    document.getElementById('auto-close').textContent=d.stats.auto_close_enabled
      ?('ON ('+d.stats.max_hold_hours+'h)'):'OFF';
  }catch(e){console.warn(e)}
}
async function refreshWebhooks(){
  try{const d=await fetchJSON('/dashboard/api/webhooks');renderLog(d.log)}catch(e){}
}
function refreshAll(){refreshStats();refreshWebhooks()}

function renderKpis(s){
  document.getElementById('mode-badge').innerHTML=s.paper_trading
    ?'<span class="tag tag-paper">PAPER</span>':'<span class="tag tag-live">LIVE</span>';
  const c=[
    {l:'Open Positions',v:s.open_count,e:'Max: 1'},
    {l:'Total PnL',v:fmtUsd(s.total_pnl_usd),c:cls(s.total_pnl_usd),e:s.closed_count+' trades'},
    {l:'Win Rate',v:s.win_rate.toFixed(1)+'%',c:s.win_rate>=50?'pos':'neg',e:s.wins+'W / '+s.losses+'L'},
    {l:'Today PnL',v:fmtUsd(s.today.realized_pnl),c:cls(s.today.realized_pnl),
     e:s.today.trades_opened+' open · '+s.today.trades_closed+' closed'},
    {l:'Avg Win',v:fmtUsd(s.avg_win_usd),c:'pos'},
    {l:'Avg Loss',v:fmtUsd(s.avg_loss_usd),c:'neg'},
    {l:'Leverage',v:s.leverage+'x',e:'Size: $'+s.position_size_usd.toFixed(2)},
  ];
  document.getElementById('kpi-grid').innerHTML=c.map(x=>`
    <div class="card"><div class="label">${x.l}</div>
    <div class="value ${x.c||''}">${x.v}</div>
    ${x.e?`<div class="extra">${x.e}</div>`:''}</div>`).join('');
}

function renderOpen(ps){
  const r=document.getElementById('open-positions');
  document.getElementById('open-count').textContent='('+ps.length+')';
  if(!ps.length){r.innerHTML='<div class="empty">No open positions.</div>';return}
  r.innerHTML=`<table><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th>
    <th>Current</th><th>Qty</th><th>PnL</th><th>Age</th><th>SL / TP</th><th>Actions</th>
    </tr></thead><tbody>${ps.map(p=>`<tr>
    <td><strong>${p.symbol}</strong></td>
    <td><span class="tag tag-${p.side}">${p.side.toUpperCase()}</span></td>
    <td>${p.entry_price}</td><td>${p.current_price??'–'}</td><td>${p.quantity}</td>
    <td class="${cls(p.unrealized_pnl_usd)}">${fmtUsd(p.unrealized_pnl_usd)}<br>
      <small>${fmtPct(p.unrealized_pnl_pct)}</small></td>
    <td>${p.age_minutes!=null?p.age_minutes.toFixed(1)+'m':'–'}</td>
    <td><small>SL: ${p.stop_loss||'–'}<br>TP: ${p.take_profit||'–'}</small></td>
    <td><button class="btn btn-danger" onclick="closePos(${p.id})">Close</button></td>
    </tr>`).join('')}</tbody></table>`;
}

function renderClosed(ps){
  const r=document.getElementById('closed-positions');
  const f=(document.getElementById('filter-symbol').value||'').toLowerCase();
  const fl=f?ps.filter(p=>p.symbol.toLowerCase().includes(f)):ps;
  if(!fl.length){r.innerHTML='<div class="empty">No closed positions.</div>';return}
  r.innerHTML=`<table><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th>
    <th>Exit</th><th>PnL</th><th>Reason</th><th>Closed</th></tr></thead>
    <tbody>${fl.map(p=>`<tr>
    <td><strong>${p.symbol}</strong></td>
    <td><span class="tag tag-${p.side}">${p.side.toUpperCase()}</span></td>
    <td>${p.entry_price}</td><td>${p.exit_price}</td>
    <td class="${cls(p.pnl_usd)}">${fmtUsd(p.pnl_usd)}<br><small>${fmtPct(p.pnl_pct)}</small></td>
    <td><small>${p.exit_reason||'–'}</small></td>
    <td><small>${(p.closed_at||'').replace('T',' ').split('.')[0]}</small></td>
    </tr>`).join('')}</tbody></table>`;
}

function renderTop(sy){
  const r=document.getElementById('top-symbols');
  if(!sy||!sy.length){r.innerHTML='<div class="empty">Not enough data.</div>';return}
  r.innerHTML=`<table><thead><tr><th>Symbol</th><th>Trades</th><th>Wins</th>
    <th>Win Rate</th><th>Total PnL</th></tr></thead><tbody>
    ${sy.map(s=>`<tr><td><strong>${s.symbol}</strong></td><td>${s.trades}</td>
    <td>${s.wins}</td>
    <td class="${s.trades?(s.wins/s.trades>=.5?'pos':'neg'):'neutral'}">
      ${s.trades?((s.wins/s.trades*100).toFixed(0)+'%'):'–'}</td>
    <td class="${cls(s.pnl)}">${fmtUsd(s.pnl)}</td></tr>`).join('')}</tbody></table>`;
}

function renderLog(log){
  const r=document.getElementById('webhook-log');
  if(!log||!log.length){r.innerHTML='<div class="empty">No activity.</div>';return}
  r.innerHTML=`<table><thead><tr><th>Time</th><th>Event</th><th>Symbol</th>
    <th>Action</th><th>Reason</th></tr></thead><tbody>
    ${log.map(e=>`<tr>
    <td><small>${(e.received_at||'').replace('T',' ').split('.')[0]}</small></td>
    <td>${e.event||'–'}</td><td>${e.symbol||'–'}</td>
    <td>${e.action||'–'}</td><td><small>${e.reason||'–'}</small></td>
    </tr>`).join('')}</tbody></table>`;
}

function updateChart(d){
  const labels=d.map(x=>x.date);
  const values=d.map(x=>parseFloat(x.realized_pnl||0));
  const ctx=document.getElementById('pnl-chart').getContext('2d');
  if(chart){chart.data.labels=labels;chart.data.datasets[0].data=values;chart.update('none');return}
  chart=new Chart(ctx,{type:'line',data:{labels,datasets:[{
    label:'Realized PnL (USD)',data:values,
    borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,.1)',
    borderWidth:2,tension:.3,fill:true,pointRadius:3,
    pointBackgroundColor:'#58a6ff'}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{labels:{color:'#e6edf3'}}},
    scales:{x:{ticks:{color:'#8b949e'},grid:{color:'#21262d'}},
            y:{ticks:{color:'#8b949e'},grid:{color:'#21262d'}}}}});
}

async function closePos(id){
  if(!confirm('Close position #'+id+'?'))return;
  try{await fetch('/dashboard/api/close/'+id,{method:'POST'});refreshAll()}
  catch(e){alert('Failed: '+e)}
}
async function closeAll(){
  if(!confirm('EMERGENCY: Close ALL open positions?'))return;
  try{const r=await fetch('/dashboard/api/close_all',{method:'POST'});
    const d=await r.json();alert('Closed '+d.closed+' position(s)');refreshAll()}
  catch(e){alert('Failed: '+e)}
}
document.getElementById('filter-symbol').addEventListener('input',refreshStats);

refreshAll();
setInterval(refreshAll,REFRESH);
</script>
</body>
</html>
"""


@dashboard_bp.route('/')
def dashboard_home():
    return render_template_string(DASHBOARD_HTML)


@dashboard_bp.route('/api/stats')
def dashboard_stats():
    from storage import PositionStore
    open_pos = PositionStore.get_open()
    symbols = list({p['symbol'] for p in open_pos})
    prices = get_prices_bulk(symbols)
    enriched = [_enrich_open(p, prices.get(p['symbol'])) for p in open_pos]
    recent = PositionStore.get_recent(limit=100)
    recent_closed = [p for p in recent if p.get('status') == 'closed'][:20]

    return jsonify({
        'status': 'success',
        'stats': _stats(),
        'open_positions': enriched,
        'recent_closed': recent_closed,
        'daily_chart': _daily_chart(14),
        'timestamp': datetime.now().isoformat(),
    })


@dashboard_bp.route('/api/webhooks')
def dashboard_webhooks():
    return jsonify({'status': 'success', 'log': _webhook_log(30)})


@dashboard_bp.route('/api/close/<int:pid>', methods=['POST'])
def dashboard_close_one(pid):
    from storage import PositionStore
    from position_manager import handle_exit
    pos = None
    for p in PositionStore.get_open():
        if int(p['id']) == int(pid):
            pos = p
            break
    if not pos:
        return jsonify({'status': 'error', 'message': 'not found'}), 404
    try:
        result = handle_exit({'symbol': pos['symbol'],
                              'reason': f'manual_dashboard_{pid}'})
        return jsonify({'status': 'success', 'result': result})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@dashboard_bp.route('/api/close_all', methods=['POST'])
def dashboard_close_all():
    from position_manager import close_all
    try:
        return jsonify(close_all('dashboard_emergency'))
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@dashboard_bp.route('/api/export.csv')
def dashboard_export():
    from storage import PositionStore
    rows = PositionStore.get_recent(limit=1000)
    out = io.StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    else:
        out.write('no data\n')
    return Response(
        out.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=positions.csv'}
    )


def register_dashboard(app):
    app.register_blueprint(dashboard_bp)
    logger.info("Dashboard registered at /dashboard/")
