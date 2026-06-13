"""
_signal_funds_daytrade.py  ―  シグナル銘柄の必要資金計算
==================================================================
逆指値ロング (_signal_funds.py) を5分足デイトレ用に移植。

【役割】
- シグナル列から必要資金 (注文額) を計算
- 予算内で買える銘柄をフィルタ
- 予算別の同時保有可能銘柄数を表示
"""

from __future__ import annotations


def collect_fund_rows(signals, budget=300_000, max_risk=1_000):
    """シグナルから資金関連情報を集計。

    引数:
        signals: list[dict] (symbol/name/strategy/order_p/stop_p 必須)
        budget: 予算 (円)
        max_risk: 1取引最大リスク (円)

    戻り値:
        list[dict] 各シグナルの (qty, required, max_loss, rr)
    """
    rows = []
    for sig in signals:
        order_p = sig.get("order_p", 0)
        stop_p = sig.get("stop_p", 0)
        target_p = sig.get("target_p", 0)
        if order_p <= 0 or stop_p <= 0:
            continue
        risk_per = abs(order_p - stop_p)
        if risk_per <= 0:
            continue
        qty_by_risk = int(max_risk / risk_per / 100) * 100
        qty_by_budget = int(budget / order_p / 100) * 100
        qty = max(100, min(qty_by_risk, qty_by_budget))
        required = order_p * qty
        max_loss = risk_per * qty
        reward = abs(target_p - order_p) * qty
        rr = reward / max_loss if max_loss > 0 else 0
        rows.append({
            **sig,
            "qty": qty,
            "required": required,
            "max_loss": max_loss,
            "reward": reward,
            "rr": rr,
            "affordable": required <= budget,
        })
    return rows


def filter_items(items, budget):
    """予算内のシグナルだけ抽出。"""
    rows = collect_fund_rows(items, budget=budget)
    return [r for r in rows if r["affordable"]]


def fund_html(rows, budget):
    """必要資金表 HTML。"""
    if not rows:
        return ""
    affordable = sum(1 for r in rows if r["affordable"])
    total_req = sum(r["required"] for r in rows if r["affordable"])
    table_rows = ""
    for r in sorted(rows, key=lambda x: -x.get("score", 0)):
        emoji = "✓" if r["affordable"] else "✗"
        table_rows += f"""
<tr>
  <td>{emoji}</td>
  <td><strong>{r["name"]}</strong><br><small>{r["symbol"]}</small></td>
  <td>{r["strategy"]}</td>
  <td>{r["order_p"]:,.0f}</td>
  <td>{r["qty"]}</td>
  <td>{r["required"]:,.0f}</td>
  <td class="loss">{r["max_loss"]:,.0f}</td>
  <td class="profit">{r["reward"]:,.0f}</td>
  <td>{r["rr"]:.1f}</td>
</tr>"""
    return f"""
<div style="background:#1e293b;padding:14px;border-radius:8px;margin-bottom:12px">
  <strong>必要資金サマリ (予算 {budget:,}円)</strong><br>
  購入可能: <strong style="color:#4ade80">{affordable}件</strong> /
  全シグナル: {len(rows)}件 / 合計必要額: {total_req:,}円
</div>
<table>
  <thead><tr>
    <th>可</th><th>銘柄</th><th>戦略</th><th>注文</th><th>株数</th>
    <th>必要額</th><th>最大損失</th><th>利益</th><th>RR</th>
  </tr></thead>
  <tbody>{table_rows}</tbody>
</table>
"""
