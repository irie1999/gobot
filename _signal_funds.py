"""_signal_funds.py — shared --funds helpers for run_signals*.py scripts."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from backtest_limit_entry import compute_period_result, FIXED_QTY

JST = timezone(timedelta(hours=9))


_MIN_TRADES_365 = 5   # 365日間の最低取引回数（これ未満は除外）


def filter_items(items: list[dict], min_trades: int = _MIN_TRADES_365) -> list[dict]:
    """Remove items with non-positive 365-day PnL or too few 365-day trades."""
    kept, removed = [], []
    for item in items:
        pr365 = (item.get("period_results") or {}).get(365) or {}
        reason = None
        if pr365.get("total_pnl", 0) <= 0:
            reason = f"365d PnL={pr365.get('total_pnl',0):+,.0f}円"
        elif pr365.get("trades", 0) < min_trades:
            reason = f"365d 取引={pr365.get('trades',0)}回"
        if reason:
            removed.append((item["symbol"], item["name"], item["strategy"], reason))
        else:
            kept.append(item)
    if removed:
        print(f"  [filter] 除外 {len(removed)}件:", flush=True)
        for sym, name, strat, reason in removed:
            print(f"    {sym} {name} [{strat}] — {reason}", flush=True)
    return kept


def collect_fund_rows(all_item_lists: list[list[dict]], days: int) -> list[dict]:
    """Collect de-duped fund rows from backtest results for the given period."""
    fund_rows: list[dict] = []
    seen: set = set()
    _today_d = datetime.now(JST).date()
    for items in all_item_lists:
        for item in items:
            pr = compute_period_result(item, days)
            for t in pr.get("trade_log", []):
                key = (item["symbol"], item["strategy"], t["signal_dt"].date())
                if key in seen:
                    continue
                seen.add(key)
                reason   = t.get("reason") or "保有中"
                entry_dt = t["entry_dt"].date() if t.get("entry_dt") else t["signal_dt"].date()
                exit_dt  = t["exit_dt"].date()  if t.get("exit_dt")  else _today_d
                if reason == "保有中":
                    exit_dt = _today_d
                fund_rows.append({
                    "symbol":       item["symbol"],
                    "name":         item["name"],
                    "strategy":     item["strategy"],
                    "signal_dt":    t["signal_dt"].date(),
                    "entry_dt":     entry_dt,
                    "exit_dt":      exit_dt,
                    "signal_price": t["signal_price"],
                    "required":     t["signal_price"] * FIXED_QTY,
                    "reason":       reason,
                    "pnl":          t.get("pnl", 0),
                })
    fund_rows.sort(key=lambda x: x["entry_dt"])
    return fund_rows


def print_fund_summary(fund_rows: list[dict], days: int) -> None:
    """Print funds summary to stdout."""
    if not fund_rows:
        return
    holding  = [r for r in fund_rows if r["reason"] == "保有中"]
    closed   = [r for r in fund_rows if r["reason"] != "保有中"]
    now_tied = sum(r["required"] for r in holding)

    events = []
    _today_d = datetime.now(JST).date()
    for r in fund_rows:
        events.append((r.get("entry_dt", r["signal_dt"]), +r["required"], r))
        events.append((r.get("exit_dt",  _today_d),       -r["required"], r))
    events.sort(key=lambda e: e[0])
    running = peak = 0
    peak_dt = None
    for ev_dt, delta, _ in events:
        running += delta
        if running > peak:
            peak, peak_dt = running, ev_dt

    print()
    print("=" * 80)
    print(f"  必要資金集計（{days}日間・全シグナルを順番に投資した場合）")
    print("=" * 80)
    print(f"  シグナル件数      : {len(fund_rows)}件"
          f"  （保有中: {len(holding)}件  /  決済済み: {len(closed)}件）")
    print(f"  ★ 最低必要資金   : {peak:>12,.0f}円  （{peak/10000:.0f}万円）"
          f"  ← ピーク同時保有日: {peak_dt}")
    print(f"  現在の拘束資金    : {now_tied:>12,.0f}円  （保有中分）")
    print()

    if holding:
        print(f"【保有中】{len(holding)}件  拘束資金 {now_tied:,.0f}円")
        print(f"  {'約定日':<12} {'銘柄':<22} {'戦略':<6} {'株価':>8}  {'拘束額':>10}")
        print("  " + "-" * 65)
        for r in holding:
            print(f"  {str(r['entry_dt']):<12} {r['name']:<22} {r['strategy']:<6}"
                  f" {r['signal_price']:>8,.0f}円  {r['required']:>9,.0f}円")
        print()

    if closed:
        print(f"【決済済み】{len(closed)}件（売却で資金は復活）")
        print(f"  {'約定日':<12} {'売却日':<12} {'銘柄':<22} {'戦略':<6} {'株価':>8}  {'結果':<8}  {'損益'}")
        print("  " + "-" * 85)
        for r in closed:
            pnl_str = f"{r['pnl']:+,.0f}円" if r["pnl"] != 0 else ""
            print(f"  {str(r['entry_dt']):<12} {str(r['exit_dt']):<12} {r['name']:<22}"
                  f" {r['strategy']:<6} {r['signal_price']:>8,.0f}円  {r['reason']:<8}  {pnl_str}")

    print()
    print(f"  ※ 必要資金 = 約定価格 × {FIXED_QTY}株")
    print(f"  ※ 売却後は資金が戻るため、ピーク同時保有額が最低限必要な資金")
    print("=" * 80)
    print()


def fund_html(fund_rows: list[dict], show_days: int) -> str:
    """Generate collapsible funds summary box HTML."""
    if not fund_rows:
        return ""
    holding  = [r for r in fund_rows if r["reason"] == "保有中"]
    closed   = [r for r in fund_rows if r["reason"] != "保有中"]
    now_tied = sum(r["required"] for r in holding)

    events = []
    _today_d = datetime.now(JST).date()
    for r in fund_rows:
        events.append((r.get("entry_dt", r["signal_dt"]), +r["required"]))
        events.append((r.get("exit_dt",  _today_d),       -r["required"]))
    events.sort(key=lambda e: e[0])
    running = peak = 0
    peak_dt = None
    for ev_dt, delta in events:
        running += delta
        if running > peak:
            peak, peak_dt = running, ev_dt

    def reason_cls(reason: str) -> str:
        if reason == "目標達成": return "profit"
        if reason == "損切り":   return "loss"
        if reason == "保有中":   return "holding"
        return ""

    holding_rows_html = "".join(
        f'<tr>'
        f'<td>{r["signal_dt"]}</td>'
        f'<td>{r["name"]}<br><small style="color:#94a3b8">{r["symbol"]}</small></td>'
        f'<td><span class="tag-{r["strategy"].lower()}">{r["strategy"]}</span></td>'
        f'<td style="text-align:right">{r["signal_price"]:,.0f}円</td>'
        f'<td style="text-align:right;color:#f59e0b;font-weight:600">{r["required"]:,.0f}円</td>'
        f'</tr>'
        for r in holding
    )

    def _pnl_cell(r: dict) -> str:
        pnl_cls = "profit" if r["pnl"] > 0 else "loss" if r["pnl"] < 0 else ""
        pnl_txt = f'{r["pnl"]:+,.0f}円' if r["pnl"] != 0 else ""
        return (f'<td class="{reason_cls(r["reason"])}">{r["reason"]}</td>'
                f'<td class="{pnl_cls}">{pnl_txt}</td>')

    closed_rows_html = "".join(
        f'<tr>'
        f'<td>{r["signal_dt"]}</td>'
        f'<td>{r["name"]}<br><small style="color:#94a3b8">{r["symbol"]}</small></td>'
        f'<td><span class="tag-{r["strategy"].lower()}">{r["strategy"]}</span></td>'
        f'<td style="text-align:right">{r["signal_price"]:,.0f}円</td>'
        f'<td style="text-align:right">{r["required"]:,.0f}円</td>'
        + _pnl_cell(r) +
        '</tr>'
        for r in closed
    )

    peak_dt_str   = str(peak_dt) if peak_dt else "—"
    holding_label = (f"保有中 {len(holding)}件 &nbsp;／&nbsp; 合計 {now_tied:,.0f}円"
                     if holding else "保有中 0件")
    closed_label  = (f"決済済み {len(closed)}件 &nbsp;／&nbsp; 合計 "
                     f"{sum(r['required'] for r in closed):,.0f}円"
                     if closed else "決済済み 0件")
    open_attr     = "open" if holding else ""

    return f"""
<details class="funds-box" style="
  margin:12px 16px 0;
  background:#0f1117;border:1px solid #f59e0b;border-radius:8px">
  <summary style="
    padding:14px 20px;cursor:pointer;list-style:none;
    display:flex;align-items:center;flex-wrap:wrap;gap:32px">
    <span style="color:#f59e0b;font-size:15px;font-weight:700;white-space:nowrap">
      💰 必要資金集計（{show_days}日間）
    </span>
    <span style="display:flex;flex-wrap:wrap;gap:32px;align-items:flex-end">
      <span>
        <span style="color:#64748b;font-size:11px;display:block;margin-bottom:2px">★ 最低必要資金（ピーク同時保有）</span>
        <span style="color:#f59e0b;font-size:24px;font-weight:800">{peak:,.0f}円</span>
        <span style="color:#94a3b8;font-size:14px;margin-left:8px">（{peak/10000:.0f}万円）</span>
        <span style="color:#64748b;font-size:11px;margin-left:12px">ピーク日: {peak_dt_str}</span>
      </span>
      <span>
        <span style="color:#64748b;font-size:11px;display:block;margin-bottom:2px">現在の拘束資金（保有中）</span>
        <span style="color:#dde1ec;font-size:18px;font-weight:700">{now_tied:,.0f}円</span>
      </span>
      <span style="font-size:12px;color:#64748b">
        全{len(fund_rows)}件 &nbsp;／&nbsp;
        保有中 {len(holding)}件 ／ 決済済み {len(closed)}件
      </span>
    </span>
    <span style="color:#64748b;font-size:12px;margin-left:auto">▶ クリックで詳細展開</span>
  </summary>
  <div style="padding:0 20px 16px">
    <div style="font-size:11px;color:#475569;margin-bottom:14px;border-top:1px solid #1e2235;padding-top:10px">
      ※ 売却で資金は復活するため、合計額ではなくピーク同時保有額が最低限必要な資金です &nbsp;／&nbsp;
      必要資金 = 約定株価 × {FIXED_QTY}株
    </div>
    <details style="margin-bottom:12px" {open_attr}>
      <summary style="cursor:pointer;color:#f59e0b;font-weight:700;font-size:14px;margin-bottom:8px">
        {holding_label} ▶クリックで展開
      </summary>
      <table style="width:auto;min-width:500px;margin-top:8px">
        <thead><tr>
          <th>シグナル日</th><th>銘柄</th><th>戦略</th>
          <th style="text-align:right">株価</th>
          <th style="text-align:right">必要資金</th>
        </tr></thead>
        <tbody>{holding_rows_html}</tbody>
      </table>
    </details>
    <details>
      <summary style="cursor:pointer;color:#94a3b8;font-size:13px">
        {closed_label} ▶クリックで展開
      </summary>
      <table style="width:auto;min-width:640px;margin-top:8px">
        <thead><tr>
          <th>シグナル日</th><th>銘柄</th><th>戦略</th>
          <th style="text-align:right">株価</th>
          <th style="text-align:right">必要資金</th>
          <th>結果</th><th>損益</th>
        </tr></thead>
        <tbody>{closed_rows_html}</tbody>
      </table>
    </details>
  </div>
</details>"""
