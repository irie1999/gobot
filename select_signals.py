"""
select_signals.py  ―  日経平均分析 → 使用シグナル自動判定

日経225の現在の相場環境を分析して、今日使うべきシグナルスクリプトを
推奨・警告・停止の3段階でHTML表示する。

判定ルール（analyze_nolimit_regime.py の実績データから導出）:

  株価制限なし: 上昇相場専用（5日騰落>=+2%, 20日>=+3%, trend=up）
  プライム全銘柄: 上昇・横ばい高ボラで有効
  WF conservative/aggressive: 上昇・横ばいで有効
  既存版 conservative: 全相場で基本使用
  下落相場時: 既存版 conservative のみ

Usage:
    python select_signals.py              # HTML生成 & ブラウザ表示
    python select_signals.py --no-browser # HTML生成のみ
    python select_signals.py --run        # 推奨スクリプトを順次実行
    python select_signals.py --run --days 90  # 期間指定で実行
    python select_signals.py --signal-only    # --signal-only 付きで実行
"""
from __future__ import annotations
import argparse
import subprocess
import webbrowser
from pathlib import Path
from datetime import timedelta, timezone, datetime

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()

# ── 相場レジーム取得 ─────────────────────────────────────────────────────────

def get_regime() -> dict | None:
    """日経225から相場レジームを取得（MA200含む）"""
    if not _HAS_YF:
        print("[WARN] yfinance が利用できません。regime_cache.json を使います。")
        return _load_cache()
    try:
        df = yf.download("^N225", period="18mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 30:
            return _load_cache()
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close = close.dropna()

        cur   = float(close.iloc[-1])
        rets  = close.pct_change().dropna()
        vol14 = float(rets.tail(14).std() * 100)
        ma10  = float(close.rolling(10).mean().iloc[-1])
        ma25  = float(close.rolling(25).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        mom5  = (cur / float(close.iloc[-6])  - 1) * 100
        mom20 = (cur / float(close.iloc[-21]) - 1) * 100
        # 過去30日の最大1日下落（クラッシュ判定用）
        max_1d_drop = float(rets.tail(30).min() * 100)

        # トレンド判定
        if cur > ma10 and ma10 > ma25:
            trend = "up"
        elif cur < ma10 and ma10 < ma25:
            trend = "down"
        else:
            trend = "sideways"

        # ボラ判定
        if vol14 > 1.5:
            vol_level = "high"
        elif vol14 > 0.8:
            vol_level = "mid"
        else:
            vol_level = "low"

        above_ma200 = (cur >= ma200) if ma200 else True

        return {
            "cur": cur, "ma10": ma10, "ma25": ma25, "ma200": ma200,
            "vol": vol14, "vol_level": vol_level,
            "trend": trend,
            "mom5": mom5, "mom20": mom20,
            "above_ma200": above_ma200,
            "max_1d_drop": max_1d_drop,
        }
    except Exception as e:
        print(f"[WARN] 日経データ取得失敗: {e}")
        return _load_cache()


def _load_cache() -> dict | None:
    """regime_cache.json からフォールバック読み込み"""
    import json, pathlib
    p = pathlib.Path(__file__).parent / "regime_cache.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data.get("current")
    except Exception:
        return None


# ── シグナル判定ルール ────────────────────────────────────────────────────────

# 各スクリプトの設定
SCRIPTS = [
    {
        "cmd":      "python run_signals_nolimit.py",
        "label":    "株価制限なし",
        "sublabel": "aggressive sm=1.5 tm=2.0 / 84銘柄",
        "color":    "\033[93m",   # 黄色
        "risk":     "高",
        "note":     "上昇相場専用。高株価銘柄は急落時の損失も大きい。",
    },
    {
        "cmd":      "python run_signals_prime.py",
        "label":    "プライム全銘柄",
        "sublabel": "aggressive sm=1.5 tm=2.0 / 84銘柄",
        "color":    "\033[35m",   # マゼンタ
        "risk":     "中高",
        "note":     "上昇・横ばい高ボラで有効。",
    },
    {
        "cmd":      "python run_signals_wf.py --aggressive",
        "label":    "WF aggressive",
        "sublabel": "aggressive / WF選定 59銘柄",
        "color":    "\033[91m",   # 赤
        "risk":     "中",
        "note":     "上昇相場で最も効率が良い（30日PF 3.24）。",
    },
    {
        "cmd":      "python run_signals_wf.py --conservative",
        "label":    "WF conservative",
        "sublabel": "conservative / WF選定",
        "color":    "\033[36m",   # シアン
        "risk":     "低中",
        "note":     "横ばい相場でも安定。逆指値Bが中心。",
    },
    {
        "cmd":      "python run_signals.py",
        "label":    "既存版 conservative",
        "sublabel": "conservative / デフォルト 31+24銘柄",
        "color":    "\033[94m",   # 青
        "risk":     "低",
        "note":     "全相場で使用可能なベースライン。",
    },
    {
        "cmd":      "python run_signals_merged.py",
        "label":    "WF+既存統合",
        "sublabel": "conservative / 統合WATCHLIST",
        "color":    "\033[92m",   # 緑
        "risk":     "低中",
        "note":     "上昇・横ばいで有効。WFと既存のマージ。",
    },
]


def judge(script: dict, r: dict | None) -> tuple[str, str, str]:
    """
    Returns (status, reason, advice)
    status: "✅ 推奨" / "⚠️ 注意" / "❌ 停止"
    """
    if r is None:
        # 相場データなし → 保守的にデフォルト推奨
        if "nolimit" in script["cmd"] or "prime" in script["cmd"]:
            return "⚠️ 注意", "相場データ取得失敗", "保守的運用を推奨"
        return "✅ 推奨", "相場データなし（デフォルト推奨）", ""

    trend   = r["trend"]
    vol     = r["vol_level"]
    mom5    = r["mom5"]
    mom20   = r["mom20"]
    above   = r["above_ma200"]
    drop    = r["max_1d_drop"]
    cmd     = script["cmd"]

    # ── 共通: 急落検知（過去30日で-3%超の日が あった）──
    crash_risk = drop < -3.0

    # ── 株価制限なし ───────────────────────────────────────────
    if "nolimit" in cmd:
        if trend == "down":
            return "❌ 停止", f"トレンド下落 (MA10<MA25)", "相場回復まで使用停止"
        if not above:
            return "❌ 停止", f"日経 < MA200 (長期下落トレンド)", "既存版のみに絞る"
        if mom5 < -2.0:
            return "❌ 停止", f"5日騰落 {mom5:+.1f}% (急落中)", "反発確認後に再開"
        if mom5 >= 2.0 and mom20 >= 3.0 and trend == "up":
            return "✅ 推奨", f"5日 {mom5:+.1f}% / 20日 {mom20:+.1f}% / 上昇トレンド", "条件を全て満たす"
        if mom5 >= 0.0 and trend == "up":
            return "⚠️ 注意", f"5日 {mom5:+.1f}% (上昇は弱い)", "半数に絞るか WF に切替"
        return "⚠️ 注意", f"5日 {mom5:+.1f}% / 20日 {mom20:+.1f}%", "条件未達。プライムを優先"

    # ── プライム全銘柄 ─────────────────────────────────────────
    if "prime" in cmd:
        if trend == "down" and vol == "high":
            return "❌ 停止", "下落×高ボラ (急落相場)", "損切り連発のリスク大"
        if trend == "down":
            return "⚠️ 注意", "下落トレンド", "WF conservative に切り替え推奨"
        if trend == "up" or (trend == "sideways" and vol in ("mid", "high")):
            return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "相場環境と合致"
        return "⚠️ 注意", f"低ボラ横ばい", "シグナルが少ない可能性"

    # ── WF aggressive ──────────────────────────────────────────
    if "--aggressive" in cmd:
        if trend == "down" and vol == "high":
            return "❌ 停止", "下落×高ボラ", "DON戦略の損失が拡大しやすい"
        if trend == "down":
            return "⚠️ 注意", "下落トレンド", "conservative に切り替え"
        return "✅ 推奨", f"トレンド={trend}", "上昇・横ばいで有効"

    # ── WF conservative / 既存版 / 統合 ───────────────────────
    if trend == "down" and vol == "high" and crash_risk:
        return "⚠️ 注意", f"下落×高ボラ×急落リスク(過去30日最大1日 {drop:+.1f}%)", "シグナルを精査して選択的に発注"
    return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "安定した相場適合"


# ── 表示 ─────────────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD  = "\033[1m"
RED   = "\033[91m"
GRN   = "\033[92m"
YLW   = "\033[93m"
CYN   = "\033[36m"
GRY   = "\033[90m"

STATUS_COLOR = {
    "✅ 推奨": GRN,
    "⚠️ 注意": YLW,
    "❌ 停止": RED,
}


def _regime_summary(r: dict | None) -> None:
    if r is None:
        print(f"  {YLW}相場データ取得失敗{RESET} (判定は保守的ルールで実施)")
        return
    trend_color = GRN if r["trend"] == "up" else (RED if r["trend"] == "down" else YLW)
    trend_ja    = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[r["trend"]]
    vol_color   = RED if r["vol_level"] == "high" else (YLW if r["vol_level"] == "mid" else GRN)
    vol_ja      = {"high": "高ボラ", "mid": "中ボラ", "low": "低ボラ"}[r["vol_level"]]
    ma200_str   = f"{r['ma200']:,.0f}" if r.get("ma200") else "N/A"
    above_str   = f"{GRN}上{RESET}" if r["above_ma200"] else f"{RED}下{RESET}"

    print(f"  日経225  : {BOLD}{r['cur']:>8,.0f}円{RESET}")
    print(f"  MA200    : {ma200_str}円 → MA200 {above_str}")
    print(f"  トレンド : {trend_color}{trend_ja}{RESET}  (MA10={r['ma10']:,.0f} / MA25={r['ma25']:,.0f})")
    print(f"  ボラ     : {vol_color}{vol_ja}{RESET}  ({r['vol']:.2f}% / 14日)")
    mom5c  = GRN if r["mom5"]  >= 0 else RED
    mom20c = GRN if r["mom20"] >= 0 else RED
    print(f"  5日騰落  : {mom5c}{r['mom5']:>+6.2f}%{RESET}")
    print(f"  20日騰落 : {mom20c}{r['mom20']:>+6.2f}%{RESET}")
    print(f"  過去30日最大1日下落: {r['max_1d_drop']:+.2f}%")


def _loss_risk_warning(r: dict | None) -> None:
    """株価制限なしが大損するシナリオの警告"""
    if r is None:
        return
    risks = []
    if r["max_1d_drop"] < -3.0:
        risks.append(f"過去30日に {r['max_1d_drop']:+.1f}% の急落あり → 複数ポジションが同時損切りリスク")
    if r["trend"] == "up" and r["vol_level"] == "high" and r["mom5"] > 5:
        risks.append(f"急騰後の高ボラ ({r['mom5']:+.1f}%) → 利益確定売りで急反落しやすい")
    if not r["above_ma200"]:
        risks.append(f"日経 < MA200 → 長期トレンドが下落。逆指値が全滅するリスク")
    if risks:
        print(f"\n{YLW}【⚠️ 株価制限なし 大損リスク要因】{RESET}")
        for rk in risks:
            print(f"  ・{rk}")
        print(f"  ヒント: 1銘柄あたり損失 = ATR × 1.5 × 100株")
        print(f"          5,000円株・ATR200円 → 損切り -30,000円/銘柄")
        print(f"          10,000円株・ATR400円 → 損切り -60,000円/銘柄")


def print_recommendation(r: dict | None) -> list[dict]:
    """推奨表を表示し、推奨スクリプトのリストを返す"""
    print()
    print(f"{BOLD}{'='*68}{RESET}")
    print(f"{BOLD}  シグナル選択レポート  {_TODAY}{RESET}")
    print(f"{BOLD}{'='*68}{RESET}")
    print()
    print(f"{BOLD}【現在の相場環境】{RESET}")
    _regime_summary(r)
    _loss_risk_warning(r)

    print()
    print(f"{BOLD}【スクリプト判定】{RESET}")
    print(f"  {'スクリプト':22} {'判定':10} {'根拠'}")
    print(f"  {'-'*64}")

    recommended = []
    for s in SCRIPTS:
        status, reason, advice = judge(s, r)
        sc = STATUS_COLOR.get(status, "")
        print(f"  {s['label']:22} {sc}{status}{RESET}  {GRY}{reason}{RESET}")
        if advice:
            print(f"  {'':22} {'':10}  → {advice}")
        if status == "✅ 推奨":
            recommended.append(s)

    print()
    print(f"{BOLD}【本日の推奨コマンド】{RESET}")
    if not recommended:
        print(f"  {RED}全スクリプト停止推奨。様子見を。{RESET}")
    else:
        for s in recommended:
            print(f"  {GRN}{s['cmd']}{RESET}")
            print(f"    {GRY}→ {s['sublabel']}  リスク: {s['risk']}{RESET}")

    # 株価制限なしが停止の場合、その理由を強調
    nolimit_s, nolimit_r, _ = judge(SCRIPTS[0], r)
    if nolimit_s != "✅ 推奨":
        print()
        print(f"{BOLD}【株価制限なし が推奨外の理由】{RESET}")
        print(f"  {nolimit_r}")
        print(f"  → 直近30日の成績の95%は日経急騰局面に依存。")
        print(f"     条件: 5日騰落>=+2% / 20日騰落>=+3% / 上昇トレンド / MA200上")
        if r:
            _trend_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[r["trend"]]
            _ma200_pos = "上" if r["above_ma200"] else "下"
            print(f"     現状: 5日 {r['mom5']:+.1f}% / 20日 {r['mom20']:+.1f}% / {_trend_ja} / MA200{_ma200_pos}")

    print()
    print(f"{BOLD}{'='*68}{RESET}")
    return recommended


def _add_args(cmd: str, days: int | None, signal_only: bool, no_browser: bool) -> str:
    extra = []
    if days:
        extra.append(f"--days {days}")
    if signal_only:
        extra.append("--signal-only")
    if no_browser:
        extra.append("--no-browser")
    return cmd + (" " + " ".join(extra) if extra else "")


# ── HTML生成 ─────────────────────────────────────────────────────────────────

def _regime_html(r: dict | None) -> str:
    if r is None:
        return '<p style="color:#fbbf24;padding:12px">⚠️ 相場データ取得失敗（yfinance接続不可）。保守的ルールで判定しています。</p>'

    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[r["trend"]]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[r["trend"]]
    vol_color   = {"high": "#f87171", "mid": "#fbbf24", "low": "#4ade80"}[r["vol_level"]]
    vol_ja      = {"high": "高ボラ", "mid": "中ボラ", "low": "低ボラ"}[r["vol_level"]]
    ma200_str   = f"{r['ma200']:,.0f}" if r.get("ma200") else "N/A"
    ma200_color = "#4ade80" if r["above_ma200"] else "#f87171"
    ma200_pos   = f'<span style="color:{ma200_color}">{"▲ 上" if r["above_ma200"] else "▼ 下"}</span>'
    mom5_c  = "#4ade80" if r["mom5"]  >= 0 else "#f87171"
    mom20_c = "#4ade80" if r["mom20"] >= 0 else "#f87171"
    drop_c  = "#f87171" if r["max_1d_drop"] < -3 else "#94a3b8"

    items = [
        ("日経225",        f'<strong style="font-size:1.2rem">{r["cur"]:,.0f}円</strong>'),
        ("トレンド",       f'<span style="color:{trend_color};font-weight:700">{trend_ja}</span>'),
        ("ボラ (14日)",    f'<span style="color:{vol_color}">{vol_ja} ({r["vol"]:.2f}%)</span>'),
        ("5日騰落",        f'<span style="color:{mom5_c};font-weight:600">{r["mom5"]:+.2f}%</span>'),
        ("20日騰落",       f'<span style="color:{mom20_c};font-weight:600">{r["mom20"]:+.2f}%</span>'),
        ("MA200",          f'{ma200_str}円 → {ma200_pos}'),
        ("過去30日最大下落", f'<span style="color:{drop_c}">{r["max_1d_drop"]:+.2f}%</span>'),
    ]
    cells = "".join(
        f'<div class="regime-item"><span class="regime-label">{lbl}</span>'
        f'<span class="regime-val">{val}</span></div>'
        for lbl, val in items
    )
    return f'<div class="regime-panel">{cells}</div>'


def _risk_warning_html(r: dict | None) -> str:
    if r is None:
        return ""
    risks = []
    if r["max_1d_drop"] < -3.0:
        risks.append(f"過去30日に <strong>{r['max_1d_drop']:+.1f}%</strong> の急落あり → 複数ポジションが同時損切りリスク")
    if r["trend"] == "up" and r["vol_level"] == "high" and r["mom5"] > 5:
        risks.append(f"急騰後の高ボラ ({r['mom5']:+.1f}%) → 利益確定売りで急反落しやすい局面")
    if not r["above_ma200"]:
        risks.append("日経 &lt; MA200 → 長期下落トレンド。逆指値が連続損切りするリスク大")
    if not risks:
        return ""
    li = "".join(f"<li>{rk}</li>" for rk in risks)
    return f"""
<div class="warn-box">
  <div style="font-weight:700;margin-bottom:8px">⚠️ 株価制限なし 大損リスク要因</div>
  <ul style="padding-left:1.4em;line-height:1.9">{li}</ul>
  <div style="margin-top:10px;color:#94a3b8;font-size:0.82rem">
    損失目安: ATR × 1.5 × 100株 / 銘柄<br>
    例) 5,000円株・ATR200円 → 損切り −30,000円/銘柄 &nbsp;|&nbsp;
        10,000円株・ATR400円 → 損切り −60,000円/銘柄
  </div>
</div>"""


STATUS_META = {
    "✅ 推奨": ("推奨",  "#4ade80", "#052e16", "#166534"),
    "⚠️ 注意": ("注意",  "#fbbf24", "#2d1f00", "#92400e"),
    "❌ 停止": ("停止",  "#f87171", "#2d0a0a", "#991b1b"),
}

RISK_COLOR = {"高": "#f87171", "中高": "#fb923c", "中": "#fbbf24", "低中": "#86efac", "低": "#4ade80"}


def _script_cards_html(r: dict | None) -> str:
    recommended_cmds = []
    cards = []
    for s in SCRIPTS:
        status, reason, advice = judge(s, r)
        label_ja, fg, bg, border = STATUS_META[status]
        risk_c = RISK_COLOR.get(s["risk"], "#94a3b8")
        advice_html = f'<div style="color:#94a3b8;font-size:0.8rem;margin-top:4px">→ {advice}</div>' if advice else ""
        cards.append(f"""
<div class="script-card" style="border-color:{border};background:{bg}">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="status-badge" style="background:{border};color:{fg}">{label_ja}</span>
    <span style="font-weight:700;font-size:1.05rem">{s['label']}</span>
    <span style="color:#64748b;font-size:0.8rem">{s['sublabel']}</span>
    <span style="margin-left:auto;font-size:0.78rem;color:{risk_c}">リスク: {s['risk']}</span>
  </div>
  <code class="cmd-box">{s['cmd']}</code>
  <div style="color:#94a3b8;font-size:0.82rem;margin-top:6px">{reason}</div>
  {advice_html}
</div>""")
        if status == "✅ 推奨":
            recommended_cmds.append(s["cmd"])
    return "\n".join(cards), recommended_cmds


def _recommended_section_html(recommended_cmds: list[str]) -> str:
    if not recommended_cmds:
        return '<div class="warn-box" style="border-color:#991b1b">❌ 全スクリプト停止推奨。相場が回復するまで様子見を。</div>'
    rows = "".join(
        f'<div style="margin:6px 0"><code class="cmd-box" style="display:inline-block">{c}</code></div>'
        for c in recommended_cmds
    )
    return f'<div style="background:#052e16;border:1px solid #166534;border-radius:8px;padding:16px">{rows}</div>'


def build_html(r: dict | None) -> str:
    today_str = str(_TODAY)
    trend_label = "N/A"
    vol_label   = "N/A"
    if r:
        trend_label = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[r["trend"]]
        vol_label   = {"high": "高ボラ", "mid": "中ボラ", "low": "低ボラ"}[r["vol_level"]]
    subtitle = f"相場環境: {trend_label} / {vol_label}" if r else "相場データ取得失敗"

    regime_html     = _regime_html(r)
    warn_html       = _risk_warning_html(r)
    cards_html, recommended_cmds = _script_cards_html(r)
    rec_section     = _recommended_section_html(recommended_cmds)

    # 株価制限なし停止理由
    nolimit_s, nolimit_r, _ = judge(SCRIPTS[0], r)
    nolimit_block = ""
    if nolimit_s != "✅ 推奨" and r:
        _trend_ja  = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[r["trend"]]
        _ma200_pos = "上" if r["above_ma200"] else "下"
        nolimit_block = f"""
<h2>株価制限なし が推奨外の理由</h2>
<div class="warn-box">
  <div><strong>{nolimit_r}</strong></div>
  <div style="margin-top:8px;color:#94a3b8;font-size:0.85rem">
    使用条件: 5日騰落 ≥ +2% ／ 20日騰落 ≥ +3% ／ 上昇トレンド ／ 日経 &gt; MA200<br>
    現　　状: 5日 <strong>{r['mom5']:+.1f}%</strong> ／
              20日 <strong>{r['mom20']:+.1f}%</strong> ／
              {_trend_ja} ／ MA200{_ma200_pos}<br>
    根　　拠: 直近30日利益の95%が「日経急騰局面」に集中。それ以外の期間は1件あたり平均+782円。
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>シグナル選択レポート — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif;
          background:#0f172a; color:#e2e8f0; padding:24px; max-width:900px; margin:0 auto; }}
  h1 {{ color:#60a5fa; font-size:1.6rem; margin-bottom:4px; }}
  h2 {{ color:#60a5fa; font-size:1.1rem; margin:28px 0 12px;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:24px; }}

  /* 相場環境パネル */
  .regime-panel {{ display:flex; flex-wrap:wrap; gap:14px;
                   background:#0d1424; border:1px solid #1e3a5f;
                   border-radius:10px; padding:18px; margin-bottom:16px; }}
  .regime-item  {{ display:flex; flex-direction:column; min-width:110px; }}
  .regime-label {{ font-size:0.72rem; color:#64748b; margin-bottom:3px; }}
  .regime-val   {{ font-size:0.95rem; font-weight:600; }}

  /* 警告ボックス */
  .warn-box {{ background:#2d1f00; border:1px solid #92400e;
               border-radius:8px; padding:14px 18px; margin-bottom:16px;
               color:#fde68a; font-size:0.88rem; line-height:1.7; }}

  /* スクリプトカード */
  .script-card {{ border:1px solid; border-radius:10px; padding:14px 18px;
                  margin-bottom:10px; transition:filter .15s; }}
  .script-card:hover {{ filter:brightness(1.1); }}
  .status-badge {{ display:inline-block; padding:2px 10px; border-radius:99px;
                   font-size:0.78rem; font-weight:700; }}
  .cmd-box {{ display:block; margin-top:8px; background:#0f172a;
              padding:6px 12px; border-radius:6px; color:#38bdf8;
              font-size:0.85rem; font-family:monospace; }}
</style>
</head>
<body>
<h1>シグナル選択レポート</h1>
<p class="subtitle">生成日: {today_str} ／ {subtitle}</p>

<h2>現在の相場環境（日経225）</h2>
{regime_html}
{warn_html}

<h2>スクリプト判定</h2>
{cards_html}

<h2>本日の推奨コマンド</h2>
{rec_section}
{nolimit_block}

<p style="color:#334155;font-size:0.75rem;margin-top:32px">
  ※ 判定ルールは analyze_nolimit_regime.py の過去バックテスト実績から導出。将来を保証しません。<br>
  ※ 株価制限なし条件: 5日騰落≥+2% / 20日騰落≥+3% / 上昇トレンド / 日経&gt;MA200
</p>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="日経平均分析 → 使用シグナル自動判定")
    parser.add_argument("--run",          action="store_true", help="推奨スクリプトを順次実行")
    parser.add_argument("--days",         type=int,            help="--days N を各スクリプトに渡す")
    parser.add_argument("--signal-only",  action="store_true", help="--signal-only を各スクリプトに渡す")
    parser.add_argument("--no-browser",   action="store_true", help="--no-browser を各スクリプトに渡す")
    args = parser.parse_args()

    regime = get_regime()

    # ── HTML生成 & ブラウザ表示 ───────────────────────────────────────────────
    html_path = Path(f"select_signals_{_TODAY}.html")
    html_path.write_text(build_html(regime), encoding="utf-8")
    print(f"生成: {html_path}")

    if not args.no_browser:
        webbrowser.open(html_path.resolve().as_uri())

    # ── コンソール出力（サマリーのみ）────────────────────────────────────────
    print_recommendation(regime)

    if not args.run:
        print(f"{GRY}  ヒント: --run を付けると推奨スクリプトを順次実行します{RESET}")
        print(f"{GRY}  例: python select_signals.py --run --days 90 --no-browser{RESET}")
        print()
        return

    _, recommended_cmds = _script_cards_html(regime)
    if not recommended_cmds:
        print(f"{RED}推奨スクリプトなし。実行をスキップします。{RESET}")
        return

    print(f"{BOLD}【実行開始】{RESET}")
    for cmd_base in recommended_cmds:
        cmd = _add_args(cmd_base, args.days, args.signal_only, args.no_browser)
        print()
        print(f"{GRN}▶ {cmd}{RESET}")
        print("-" * 60)
        result = subprocess.run(cmd, shell=True)
        if result.returncode != 0:
            print(f"{YLW}  [WARN] 終了コード {result.returncode}{RESET}")

    print()
    print(f"{BOLD}全推奨スクリプトの実行完了。{RESET}")


if __name__ == "__main__":
    main()
