"""replay_trainer.py — 手持ちの分足でデイトレを「手で」練習するリプレイツール。

コードに売買させない。あなたが1本ずつ足を送り、自分で建てて、自分で決済する。
未来の足はサーバ側に留めてブラウザへ送らないので、覗き見はできない。

使い方:
  python replay_trainer.py                       # ランダム出題(5分足)をブラウザで開く
  python replay_trainer.py --symbol 7203.T       # 銘柄を固定してランダムな日
  python replay_trainer.py --date 2026-03-14     # 日を固定
  python replay_trainer.py --interval 1          # 1分足で練習 (既定は5分足)
  python replay_trainer.py --demo                # データが無い環境でも動く合成足
  python replay_trainer.py --report              # 練習ログの集計を表示
  python replay_trainer.py --report --days 30    # 直近30日ぶんだけ集計

データの場所 (CLAUDE.md「★ データ場所メモ」と同じ解決):
  5分足 : 環境変数 MINUTE_5M_DIR → 隣接 stock_5min      (<コード>0.pkl)
  1分足 : 環境変数 MINUTE_1M_DIR → ~/.jquants_cache/minute (<コード>0_1m.pkl)

練習ログ:
  replay_practice_log.csv に1トレード1行で追記される。--report で集計。

⚠ 測れないこと (CLAUDE.md §1.3):
  足の中の高安の順序は分からないので、同一足内で完結する売買は再現できない。
  板・歩み値も存在しないので、約定できるか/スプレッドは練習に入らない。
  同一足で損切と利確の両方に触れた場合は **損切り優先(悲観)** で判定する。
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import pickle
import random
import socketserver
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

LOG_PATH = Path(__file__).resolve().parent / "replay_practice_log.csv"
SESSION_END = "15:25"     # ここまでに決済しなければタイムカット
ATR_PERIOD = 14
DEFAULT_QTY = 100

LOG_COLUMNS = [
    "practiced_at", "symbol", "trade_date", "interval", "side",
    "entry_time", "entry_px", "stop_px", "target_px",
    "exit_time", "exit_px", "exit_reason", "qty", "pnl",
    "r_multiple", "atr_at_entry", "bars_held", "pattern", "rule_ok", "note",
]


# ────────────────────────────────────────────────────────────
# データ解決 (daytrade_data / tenkan_sim と同じ規約。単体でも動くよう自前実装)
# ────────────────────────────────────────────────────────────

def find_minute_dirs() -> tuple[Path | None, Path | None]:
    """(5分足DIR, 1分足DIR) を返す。環境変数 → 既定パスの順。"""
    d1 = None
    env1 = os.environ.get("MINUTE_1M_DIR", "").strip()
    if env1:
        d1 = Path(env1)
    else:
        p = Path.home() / ".jquants_cache" / "minute"
        if p.exists():
            d1 = p

    d5 = None
    env5 = os.environ.get("MINUTE_5M_DIR", "").strip()
    if env5:
        d5 = Path(env5)
    if d5 is None or not d5.exists():
        here = Path(__file__).resolve().parent
        for c in [here / "data" / "minute_5m",
                  here.parent / "stock_5min",
                  here.parent / "stock_5min" / "data" / "minute_5m",
                  here / "data" / "stock_5min"]:
            try:
                if c.exists() and any(c.glob("*.pkl")):
                    d5 = c
                    break
            except Exception:
                pass
    return d5, d1


def _code(symbol: str) -> str:
    """7203.T → 72030"""
    c = symbol.strip().upper().replace(".T", "")
    return c + "0" if len(c) == 4 else c


def load_bars(symbol: str, interval: int) -> pd.DataFrame | None:
    """pkl を読んで [open, high, low, close, volume] / tz-naive JST index に正規化。"""
    d5, d1 = find_minute_dirs()
    if interval == 1:
        path = (d1 / f"{_code(symbol)}_1m.pkl") if d1 else None
    else:
        path = (d5 / f"{_code(symbol)}.pkl") if d5 else None
    if path is None or not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    need = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in need):
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    try:
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    except Exception:
        pass
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df.sort_index()


def available_symbols(interval: int) -> list[str]:
    d5, d1 = find_minute_dirs()
    out: list[str] = []
    if interval == 1 and d1 and d1.exists():
        for p in d1.glob("*_1m.pkl"):
            code = p.name.replace("_1m.pkl", "")
            if len(code) == 5 and code.endswith("0"):
                out.append(code[:4] + ".T")
    elif d5 and d5.exists():
        for p in d5.glob("*.pkl"):
            code = p.stem
            if len(code) == 5 and code.endswith("0"):
                out.append(code[:4] + ".T")
    return sorted(set(out))


def make_demo_bars(seed: int = 0) -> pd.DataFrame:
    """データが無い環境でも動作確認できる合成足 (2日ぶんの5分足)。"""
    rng = np.random.default_rng(seed)
    rows, px = [], 2000.0
    base = datetime(2026, 3, 13, 9, 0)
    for day in range(2):
        d0 = base + timedelta(days=day)
        px *= 1 + rng.normal(0, 0.004)
        for i in range(61):                       # 09:00〜15:00 の5分足
            t = d0 + timedelta(minutes=5 * i)
            if 11 * 60 + 30 <= t.hour * 60 + t.minute < 12 * 60 + 30:
                continue                          # 昼休み
            drift = rng.normal(0, 0.0016)
            o = px
            c = o * (1 + drift)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0009)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.0009)))
            rows.append((t, o, h, lo, c, float(rng.integers(3000, 60000))))
            px = c
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close", "volume"])
    return df.set_index("dt")


# ────────────────────────────────────────────────────────────
# 出題の組み立て
# ────────────────────────────────────────────────────────────

def atr_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


class Question:
    """1銘柄 × 1営業日ぶんの出題。未来の足はここに留めてブラウザへ渡さない。"""

    def __init__(self, symbol: str, interval: int, df: pd.DataFrame, trade_date):
        self.symbol = symbol
        self.interval = interval
        self.trade_date = pd.Timestamp(trade_date).date()

        atr = atr_series(df)
        day_mask = df.index.normalize() == pd.Timestamp(self.trade_date)
        self.day = df.loc[day_mask]
        prior = df.loc[df.index.normalize() < pd.Timestamp(self.trade_date)]

        self.prev_close = float(prior["close"].iloc[-1]) if len(prior) else float(self.day["open"].iloc[0])
        prev_day = prior.loc[prior.index.normalize() == prior.index.normalize().max()] if len(prior) else prior
        self.prev_high = float(prev_day["high"].max()) if len(prev_day) else self.prev_close
        self.prev_low = float(prev_day["low"].min()) if len(prev_day) else self.prev_close
        self.atr = atr.reindex(self.day.index).ffill().fillna(atr.iloc[-1] if len(atr) else 0.0)

        self.n = len(self.day)
        self.i = -1                                # 直近に開示したバーの添字
        # VWAP は開示済みバーだけで逐次計算する (未来を含めない)
        self._cum_pv = 0.0
        self._cum_v = 0.0

    def meta(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "trade_date": str(self.trade_date),
            "prev_close": round(self.prev_close, 2),
            "prev_high": round(self.prev_high, 2),
            "prev_low": round(self.prev_low, 2),
            "total_bars": self.n,
            "session_end": SESSION_END,
        }

    def reveal(self) -> dict | None:
        """次の1本を開示する。終わりなら None。"""
        if self.i + 1 >= self.n:
            return None
        self.i += 1
        ts = self.day.index[self.i]
        row = self.day.iloc[self.i]
        typ = (row["high"] + row["low"] + row["close"]) / 3.0
        self._cum_pv += typ * max(row["volume"], 0.0)
        self._cum_v += max(row["volume"], 0.0)
        vwap = (self._cum_pv / self._cum_v) if self._cum_v > 0 else float(row["close"])
        return {
            "i": self.i,
            "t": ts.strftime("%H:%M"),
            "o": round(float(row["open"]), 2),
            "h": round(float(row["high"]), 2),
            "l": round(float(row["low"]), 2),
            "c": round(float(row["close"]), 2),
            "v": float(row["volume"]),
            "vwap": round(vwap, 2),
            "atr": round(float(self.atr.iloc[self.i]), 2),
            "last": self.i + 1 >= self.n,
            "past_end": ts.strftime("%H:%M") >= SESSION_END,
        }

    def bar(self, i: int) -> pd.Series:
        return self.day.iloc[i]

    def time_at(self, i: int) -> str:
        return self.day.index[i].strftime("%H:%M")


def build_question(symbol: str | None, interval: int, date: str | None,
                   days: int, demo: bool) -> Question:
    if demo:
        df = make_demo_bars(random.randrange(10_000))
        sym = symbol or "DEMO.T"
        dates = sorted(set(df.index.normalize()))
        return Question(sym, interval, df, dates[-1])

    syms = [symbol] if symbol else available_symbols(interval)
    if not syms:
        raise SystemExit(
            "分足データが見つかりません。\n"
            "  5分足: 環境変数 MINUTE_5M_DIR か 隣接 stock_5min フォルダ\n"
            "  1分足: 環境変数 MINUTE_1M_DIR か ~/.jquants_cache/minute\n"
            "動作確認だけなら --demo を付けてください。"
        )
    random.shuffle(syms)
    for sym in syms[:60]:
        df = load_bars(sym, interval)
        if df is None or len(df) < 200:
            continue
        dates = sorted({d.date() for d in df.index.normalize()})
        if len(dates) < 2:
            continue
        pool = dates[1:]                            # 初日は前日が無いので除外
        if days > 0:
            pool = pool[-days:]
        if date:
            want = pd.Timestamp(date).date()
            if want not in pool:
                continue
            pick = want
        else:
            pick = random.choice(pool)
        q = Question(sym, interval, df, pick)
        if q.n >= 20:
            return q
    raise SystemExit("条件に合う銘柄×日が見つかりませんでした。--symbol / --date / --days を見直してください。")


# ────────────────────────────────────────────────────────────
# 約定判定 (悲観側。同一バーで損切と利確の両方に触れたら損切り)
# ────────────────────────────────────────────────────────────

def check_exit(side: str, bar: pd.Series, stop: float, target: float) -> tuple[float, str] | None:
    hi, lo = float(bar["high"]), float(bar["low"])
    if side == "long":
        hit_stop = stop is not None and lo <= stop
        hit_tgt = target is not None and hi >= target
    else:
        hit_stop = stop is not None and hi >= stop
        hit_tgt = target is not None and lo <= target
    if hit_stop:
        return stop, "stop"                          # 両方なら損切り優先
    if hit_tgt:
        return target, "target"
    return None


def append_log(row: dict) -> None:
    df = pd.DataFrame([{c: row.get(c, "") for c in LOG_COLUMNS}])
    header = not LOG_PATH.exists()
    df.to_csv(LOG_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")


# ────────────────────────────────────────────────────────────
# 集計レポート
# ────────────────────────────────────────────────────────────

def report(days: int) -> None:
    if not LOG_PATH.exists():
        print("まだ練習ログがありません。先に練習してください。")
        return
    df = pd.read_csv(LOG_PATH, encoding="utf-8-sig")
    df = df[df["exit_reason"].notna() & (df["exit_reason"] != "")]
    if days > 0:
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
        df = df[pd.to_datetime(df["practiced_at"], errors="coerce") >= cutoff]
    if df.empty:
        print("対象期間に決済済みのトレードがありません。")
        return

    df["r_multiple"] = pd.to_numeric(df["r_multiple"], errors="coerce")
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df["bars_held"] = pd.to_numeric(df["bars_held"], errors="coerce")

    def block(name: str, g: pd.DataFrame) -> str:
        n = len(g)
        wins = g[g["pnl"] > 0]
        losses = g[g["pnl"] <= 0]
        wr = len(wins) / n * 100
        gp, gl = wins["pnl"].sum(), -losses["pnl"].sum()
        pf = (gp / gl) if gl > 0 else float("inf")
        exp_r = g["r_multiple"].mean()
        return (f"{name:<14} {n:>4}件  勝率{wr:5.1f}%  "
                f"PF{pf:6.2f}  期待値{exp_r:+6.2f}R  "
                f"損益{g['pnl'].sum():+11,.0f}円  平均{g['bars_held'].mean():4.1f}本")

    print(f"\n=== 練習レポート ({len(df)}件) ===\n")
    print(block("全体", df))

    # 最重要指標: 損切りを外さなかったか
    ok = df["rule_ok"].astype(str).str.upper().isin(["Y", "YES", "TRUE", "1"])
    print(f"\n★ ルール遵守率  {ok.sum()}/{len(df)} = {ok.mean() * 100:.1f}%"
          f"   {'← ここが100%でない限り他の数字は信用できない' if ok.mean() < 1 else ''}")
    if (~ok).any():
        broke = df[~ok]
        print(f"  ルールを外した {len(broke)}件の損益: {broke['pnl'].sum():+,.0f}円 "
              f"(期待値 {broke['r_multiple'].mean():+.2f}R)")
        print(f"  守った       {ok.sum()}件の損益: {df[ok]['pnl'].sum():+,.0f}円 "
              f"(期待値 {df[ok]['r_multiple'].mean():+.2f}R)")

    for key, label in [("pattern", "型別"), ("side", "方向別")]:
        sub = df[df[key].astype(str).str.len() > 0]
        if sub.empty:
            continue
        print(f"\n--- {label} ---")
        for name, g in sorted(sub.groupby(sub[key].astype(str)), key=lambda kv: -len(kv[1])):
            if len(g) >= 1:
                print(block(str(name)[:14], g))

    print("\n--- 時間帯別 (エントリー時刻) ---")
    hh = pd.to_datetime(df["entry_time"], format="%H:%M", errors="coerce").dt.hour
    for h, g in df.groupby(hh):
        if pd.notna(h):
            print(block(f"{int(h):02d}時台", g))

    print("\n--- 決済理由 ---")
    for name, g in df.groupby("exit_reason"):
        print(block(str(name), g))
    print()


# ────────────────────────────────────────────────────────────
# ブラウザUI (ローカルHTTPサーバー)
# ────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>replay trainer</title>
<style>
 :root{--bg:#12151c;--fg:#e8ecf2;--dim:#8a93a3;--up:#26a69a;--dn:#ef5350;--line:#2a3040}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,"Segoe UI",sans-serif}
 header{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid var(--line)}
 header b{font-size:15px}
 .dim{color:var(--dim)}
 canvas{display:block;width:100%;background:#0d1017}
 #bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;border-top:1px solid var(--line)}
 button{background:#222836;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:7px 13px;font:inherit;cursor:pointer}
 button:hover{background:#2c3446}
 button.buy{background:#14524b;border-color:#1c6f65}
 button.sell{background:#5c2422;border-color:#7d302d}
 button.flat{background:#4a4326;border-color:#6b6136}
 input,select{background:#1a1f2b;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:6px;font:inherit}
 input[type=number]{width:62px}
 #pos{padding:8px 12px;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
 #log{max-height:150px;overflow:auto;padding:6px 12px;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
 #log div{padding:1px 0}
 .win{color:var(--up)} .loss{color:var(--dn)}
 kbd{background:#222836;border:1px solid var(--line);border-radius:3px;padding:1px 5px;font-size:11px}
</style>
<header>
  <b id="sym">-</b><span class="dim" id="meta"></span>
  <span class="dim">|</span>
  <label class="dim">損切 <input type="number" id="sm" value="1.5" step="0.1" min="0.1"></label>
  <label class="dim">利確 <input type="number" id="tm" value="3.0" step="0.1" min="0.1"></label>
  <span class="dim">×ATR</span>
  <label class="dim">型 <select id="pattern">
    <option>前日終値ブレイク</option><option>下ブレイク(lss型)</option><option>ORB</option>
    <option>VWAP乖離</option><option>VWAP反発</option><option>前日高安ブレイク</option><option>その他</option>
  </select></label>
  <span class="dim" id="clock"></span>
</header>
<canvas id="cv" height="430"></canvas>
<div id="bar">
  <button id="next">次の足 <kbd>→</kbd></button>
  <button id="run">連続 <kbd>R</kbd></button>
  <button class="buy" id="buy">買い <kbd>B</kbd></button>
  <button class="sell" id="sell">売り <kbd>S</kbd></button>
  <button class="flat" id="flat">決済 <kbd>X</kbd></button>
  <button id="again">次の出題 <kbd>N</kbd></button>
  <span class="dim">未来の足はサーバ側にあり、覗けません</span>
</div>
<div id="pos" class="dim">ノーポジション</div>
<div id="log"></div>
<script>
let M=null,B=[],pos=null,done=false,timer=null;
const $=id=>document.getElementById(id);
const f=n=>n==null?'-':n.toLocaleString(undefined,{minimumFractionDigits:1,maximumFractionDigits:1});

async function api(p,body){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})});return r.json();}

async function load(){const d=await api('/api/new');M=d.meta;B=[];pos=null;done=false;
  $('sym').textContent=M.symbol;
  $('meta').textContent=`${M.trade_date}  ${M.interval}分足  前日終値 ${f(M.prev_close)}  前日高 ${f(M.prev_high)} / 安 ${f(M.prev_low)}`;
  $('pos').textContent='ノーポジション';$('pos').className='dim';
  for(let i=0;i<3;i++) await next();}

async function next(){
  if(done) return;
  const d=await api('/api/next');
  if(d.end){done=true;stopRun();$('clock').textContent='大引け。'+(pos?'':'次の出題へ');return;}
  B.push(d.bar); $('clock').textContent=d.bar.t;
  if(d.exit) closed(d.exit);
  if(pos) updatePos(d.bar);
  draw();
}

function updatePos(b){
  const s=pos.side==='long'?1:-1, up=(b.c-pos.entry_px)*s*pos.qty;
  $('pos').innerHTML=`<b>${pos.side==='long'?'買い':'売り'}</b> ${pos.qty}株 @${f(pos.entry_px)}　`+
    `損切 ${f(pos.stop_px)}　利確 ${f(pos.target_px)}　`+
    `<span class="${up>=0?'win':'loss'}">含み ${up>=0?'+':''}${Math.round(up).toLocaleString()}円</span>　${pos.pattern}`;
  $('pos').className='';
}

async function enter(side){
  if(pos||done) return;
  const d=await api('/api/enter',{side,sm:+$('sm').value,tm:+$('tm').value,pattern:$('pattern').value});
  if(d.error){alert(d.error);return;} pos=d.trade; updatePos(B[B.length-1]); draw();
}

async function flat(){ if(!pos) return; const d=await api('/api/exit'); if(d.exit) closed(d.exit); draw(); }

function closed(t){
  pos=null; $('pos').textContent='ノーポジション'; $('pos').className='dim';
  const ok=confirm(`決済: ${t.exit_reason} ${f(t.exit_px)}  損益 ${Math.round(t.pnl).toLocaleString()}円 (${t.r_multiple>=0?'+':''}${t.r_multiple.toFixed(2)}R)\n\n`+
    `決めたルール通りに実行できましたか？\n[OK]=はい  [キャンセル]=いいえ(損切りをずらした/根拠なく入った 等)`);
  api('/api/annotate',{rule_ok:ok?'Y':'N'});
  const e=document.createElement('div');
  e.className=t.pnl>=0?'win':'loss';
  e.textContent=`${t.entry_time}→${t.exit_time} ${t.side==='long'?'買':'売'} ${f(t.entry_px)}→${f(t.exit_px)} `+
    `${t.exit_reason} ${Math.round(t.pnl).toLocaleString()}円 ${t.r_multiple>=0?'+':''}${t.r_multiple.toFixed(2)}R ${ok?'':'⚠ルール違反'}`;
  $('log').prepend(e);
}

function startRun(){ if(timer)return; timer=setInterval(next,600); $('run').textContent='停止 R'; }
function stopRun(){ if(timer)clearInterval(timer); timer=null; $('run').textContent='連続 R'; }

function draw(){
  const cv=$('cv'),ctx=cv.getContext('2d');
  const w=cv.width=cv.clientWidth*devicePixelRatio, h=cv.height=430*devicePixelRatio;
  ctx.scale(1,1); ctx.clearRect(0,0,w,h);
  if(!B.length) return;
  const padL=8*devicePixelRatio,padR=66*devicePixelRatio,padT=8*devicePixelRatio;
  const volH=h*0.18, ch=h-padT-volH-8*devicePixelRatio;
  // 開示済みバーだけでスケール(未来を漏らさない)
  let lo=Math.min(...B.map(b=>b.l),M.prev_close), hi=Math.max(...B.map(b=>b.h),M.prev_close);
  const pad=(hi-lo)*0.06||1; lo-=pad; hi+=pad;
  const n=Math.max(B.length,30), bw=(w-padL-padR)/n;
  const Y=p=>padT+(hi-p)/(hi-lo)*ch;
  const line=(p,col,dash)=>{ctx.save();ctx.strokeStyle=col;ctx.setLineDash(dash||[]);ctx.lineWidth=devicePixelRatio;
    ctx.beginPath();ctx.moveTo(padL,Y(p));ctx.lineTo(w-padR,Y(p));ctx.stroke();
    ctx.fillStyle=col;ctx.font=`${11*devicePixelRatio}px sans-serif`;ctx.fillText(f(p),w-padR+4*devicePixelRatio,Y(p)+4*devicePixelRatio);ctx.restore();};
  line(M.prev_close,'#7a8399',[4,4]);
  const vmax=Math.max(...B.map(b=>b.v))||1;
  B.forEach((b,i)=>{
    const x=padL+i*bw+bw/2, up=b.c>=b.o;
    ctx.strokeStyle=ctx.fillStyle=up?'#26a69a':'#ef5350';
    ctx.lineWidth=devicePixelRatio;
    ctx.beginPath();ctx.moveTo(x,Y(b.h));ctx.lineTo(x,Y(b.l));ctx.stroke();
    const y1=Y(Math.max(b.o,b.c)),y2=Y(Math.min(b.o,b.c));
    ctx.fillRect(x-bw*0.32,y1,Math.max(bw*0.64,1),Math.max(y2-y1,devicePixelRatio));
    ctx.globalAlpha=.45;ctx.fillRect(x-bw*0.32,h-b.v/vmax*volH,Math.max(bw*0.64,1),b.v/vmax*volH);ctx.globalAlpha=1;
  });
  ctx.strokeStyle='#c9a227';ctx.lineWidth=devicePixelRatio;ctx.beginPath();
  B.forEach((b,i)=>{const x=padL+i*bw+bw/2;i?ctx.lineTo(x,Y(b.vwap)):ctx.moveTo(x,Y(b.vwap));});ctx.stroke();
  if(pos){line(pos.stop_px,'#ef5350',[2,3]);line(pos.target_px,'#26a69a',[2,3]);line(pos.entry_px,'#e8ecf2',[1,4]);}
}

$('next').onclick=()=>next(); $('buy').onclick=()=>enter('long'); $('sell').onclick=()=>enter('short');
$('flat').onclick=flat; $('again').onclick=()=>{stopRun();load();};
$('run').onclick=()=>timer?stopRun():startRun();
addEventListener('keydown',e=>{const k=e.key.toLowerCase();
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')return;
  if(e.key==='ArrowRight'||e.key===' '){e.preventDefault();next();}
  else if(k==='b')enter('long'); else if(k==='s')enter('short');
  else if(k==='x')flat(); else if(k==='n'){stopRun();load();} else if(k==='r')timer?stopRun():startRun();});
addEventListener('resize',draw);
load();
</script>
"""


class TrainerState:
    def __init__(self, args):
        self.args = args
        self.q: Question | None = None
        self.pos: dict | None = None
        self.last: dict | None = None      # 直近に決済したトレード (annotate 用)
        self.lock = threading.Lock()

    def new_question(self) -> dict:
        self.q = build_question(self.args.symbol, self.args.interval,
                                self.args.date, self.args.days, self.args.demo)
        self.pos = None
        self.last = None
        return {"meta": self.q.meta()}

    # ── 1本進める。開示直後に保有中ポジションの損切/利確/タイムカットを判定 ──
    def step(self) -> dict:
        bar = self.q.reveal()
        if bar is None:
            if self.pos:                                   # データ終端は終値で強制決済
                return {"end": True, "exit": self._close(self.q.n - 1,
                                                         float(self.q.bar(self.q.n - 1)["close"]), "timecut")}
            return {"end": True}
        out = {"bar": bar}
        if self.pos:
            i = bar["i"]
            hit = check_exit(self.pos["side"], self.q.bar(i),
                             self.pos["stop_px"], self.pos["target_px"])
            if hit:
                out["exit"] = self._close(i, hit[0], hit[1])
            elif bar["past_end"]:
                out["exit"] = self._close(i, float(self.q.bar(i)["close"]), "timecut")
        return out

    def enter(self, side: str, sm: float, tm: float, pattern: str) -> dict:
        if self.pos:
            return {"error": "すでに建玉があります"}
        i = self.q.i
        if i < 0:
            return {"error": "まだ足が出ていません"}
        px = float(self.q.bar(i)["close"])
        atr = float(self.q.atr.iloc[i]) or px * 0.003
        sgn = 1 if side == "long" else -1
        self.pos = {
            "side": side, "qty": self.args.qty, "pattern": pattern,
            "entry_i": i, "entry_time": self.q.time_at(i), "entry_px": round(px, 2),
            "stop_px": round(px - sgn * atr * sm, 2),
            "target_px": round(px + sgn * atr * tm, 2),
            "atr": round(atr, 2),
        }
        return {"trade": self.pos}

    def manual_exit(self) -> dict:
        if not self.pos:
            return {}
        i = self.q.i
        return {"exit": self._close(i, float(self.q.bar(i)["close"]), "manual")}

    def _close(self, i: int, px: float, reason: str) -> dict:
        p = self.pos
        sgn = 1 if p["side"] == "long" else -1
        pnl = (px - p["entry_px"]) * sgn * p["qty"]
        risk = abs(p["entry_px"] - p["stop_px"]) * p["qty"]
        t = {
            "practiced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": self.q.symbol, "trade_date": str(self.q.trade_date),
            "interval": self.q.interval, "side": p["side"],
            "entry_time": p["entry_time"], "entry_px": p["entry_px"],
            "stop_px": p["stop_px"], "target_px": p["target_px"],
            "exit_time": self.q.time_at(i), "exit_px": round(px, 2),
            "exit_reason": reason, "qty": p["qty"], "pnl": round(pnl, 1),
            "r_multiple": round(pnl / risk, 3) if risk > 0 else 0.0,
            "atr_at_entry": p["atr"], "bars_held": i - p["entry_i"],
            "pattern": p["pattern"], "rule_ok": "", "note": "",
        }
        self.pos = None
        self.last = t
        return t

    def annotate(self, rule_ok: str) -> dict:
        if self.last is None:
            return {}
        self.last["rule_ok"] = "Y" if str(rule_ok).upper().startswith("Y") else "N"
        append_log(self.last)
        self.last = None
        return {"saved": True}


def serve(args) -> None:
    state = TrainerState(args)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):            # アクセスログは黙らせる
            pass

        def _send(self, obj):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            with state.lock:
                try:
                    if self.path == "/api/new":
                        self._send(state.new_question())
                    elif self.path == "/api/next":
                        self._send(state.step())
                    elif self.path == "/api/enter":
                        self._send(state.enter(data.get("side", "long"),
                                               float(data.get("sm", 1.5)),
                                               float(data.get("tm", 3.0)),
                                               str(data.get("pattern", ""))))
                    elif self.path == "/api/exit":
                        self._send(state.manual_exit())
                    elif self.path == "/api/annotate":
                        self._send(state.annotate(data.get("rule_ok", "N")))
                    else:
                        self.send_error(404)
                except Exception as e:            # ブラウザを固まらせない
                    self._send({"error": f"{type(e).__name__}: {e}"})

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as srv:
        url = f"http://127.0.0.1:{args.port}/"
        print(f"リプレイ練習を開始します: {url}")
        print("  →/Space=次の足  B=買い  S=売り  X=決済  N=次の出題  R=連続再生")
        print(f"  記録先: {LOG_PATH}")
        print("  Ctrl+C で終了")
        if not args.no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n終了しました。`python replay_trainer.py --report` で集計できます。")


def main() -> None:
    ap = argparse.ArgumentParser(description="手持ちの分足でデイトレを手で練習するリプレイツール")
    ap.add_argument("--symbol", help="銘柄 (例 7203.T)。省略でランダム")
    ap.add_argument("--date", help="日付 YYYY-MM-DD。省略でランダム")
    ap.add_argument("--interval", type=int, default=5, choices=[1, 5], help="足種 (既定5分足)")
    ap.add_argument("--days", type=int, default=0, help="直近N営業日から出題 (0=全期間)")
    ap.add_argument("--qty", type=int, default=DEFAULT_QTY, help="株数 (既定100)")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--demo", action="store_true", help="データが無い環境でも動く合成足")
    ap.add_argument("--report", action="store_true", help="練習ログを集計して表示")
    args = ap.parse_args()

    if args.report:
        report(args.days)
        return
    serve(args)


if __name__ == "__main__":
    main()
