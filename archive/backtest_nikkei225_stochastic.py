"""
日経225 全銘柄 A6: ストキャスティクス バックテスト & 利益ランキング
backtest_toyota_stochastic.py のロジックをそのまま流用し、
225銘柄をループして収益率順にランキングHTMLを出力する。

使い方:
  python backtest_nikkei225_stochastic.py
"""

# ── 既存の toyota スクリプトから全関数をそのままインポート ──────
from backtest_toyota_stochastic import (
    fetch_data, add_indicators, run_backtest,
    YEARS, INITIAL_CASH, ATR_PERIOD, ATR_STOP_MULT,
    STOCH_K, STOCH_SM, STOCH_D, RISK_PER_TRADE,
)

import base64, io, os, subprocess, sys, warnings
warnings.filterwarnings("ignore")
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

OUTPUT_HTML = os.path.join(os.path.dirname(__file__), "backtest_nikkei225_stochastic.html")
WORKERS = 20   # 並列ダウンロード数

# ── 日経225 銘柄リスト ────────────────────────────────────────
NIKKEI225 = [
    ("1332.T", "ニッスイ"),
    ("1333.T", "マルハニチロ"),
    ("1605.T", "INPEX"),
    ("1721.T", "コムシスHD"),
    ("1801.T", "大成建設"),
    ("1802.T", "大林組"),
    ("1803.T", "清水建設"),
    ("1808.T", "長谷工コーポレーション"),
    ("1812.T", "鹿島建設"),
    ("1925.T", "大和ハウス工業"),
    ("1928.T", "積水ハウス"),
    ("1963.T", "日揮HD"),
    ("2002.T", "日清製粉グループ本社"),
    ("2269.T", "明治HD"),
    ("2282.T", "日本ハム"),
    ("2413.T", "エムスリー"),
    ("2432.T", "ディー・エヌ・エー"),
    ("2501.T", "サッポロHD"),
    ("2502.T", "アサヒグループHD"),
    ("2503.T", "キリンHD"),
    ("2531.T", "宝HD"),
    ("2768.T", "双日"),
    ("2801.T", "キッコーマン"),
    ("2802.T", "味の素"),
    ("2871.T", "ニチレイ"),
    ("2914.T", "日本たばこ産業"),
    ("3086.T", "J.フロント リテイリング"),
    ("3099.T", "三越伊勢丹HD"),
    ("3105.T", "日清紡HD"),
    ("3289.T", "東急不動産HD"),
    ("3382.T", "セブン&アイHD"),
    ("3401.T", "帝人"),
    ("3402.T", "東レ"),
    ("3405.T", "クラレ"),
    ("3407.T", "旭化成"),
    ("3436.T", "SUMCO"),
    ("3861.T", "王子HD"),
    ("3863.T", "日本製紙"),
    ("4004.T", "レゾナック・HD"),
    ("4005.T", "住友化学"),
    ("4021.T", "日産化学"),
    ("4042.T", "東ソー"),
    ("4043.T", "トクヤマ"),
    ("4061.T", "デンカ"),
    ("4063.T", "信越化学工業"),
    ("4183.T", "三井化学"),
    ("4188.T", "三菱ケミカルグループ"),
    ("4208.T", "UBE"),
    ("4272.T", "日本化薬"),
    ("4307.T", "野村総合研究所"),
    ("4324.T", "電通グループ"),
    ("4502.T", "武田薬品工業"),
    ("4503.T", "アステラス製薬"),
    ("4506.T", "住友ファーマ"),
    ("4507.T", "塩野義製薬"),
    ("4519.T", "中外製薬"),
    ("4523.T", "エーザイ"),
    ("4543.T", "テルモ"),
    ("4568.T", "第一三共"),
    ("4578.T", "大塚HD"),
    ("4631.T", "DIC"),
    ("4689.T", "LINEヤフー"),
    ("4704.T", "トレンドマイクロ"),
    ("4751.T", "サイバーエージェント"),
    ("4755.T", "楽天グループ"),
    ("4901.T", "富士フイルムHD"),
    ("4902.T", "コニカミノルタ"),
    ("5019.T", "出光興産"),
    ("5020.T", "ENEOSホールディングス"),
    ("5101.T", "横浜ゴム"),
    ("5108.T", "ブリヂストン"),
    ("5201.T", "AGC"),
    ("5214.T", "日本電気硝子"),
    ("5232.T", "住友大阪セメント"),
    ("5233.T", "太平洋セメント"),
    ("5301.T", "東海カーボン"),
    ("5332.T", "TOTO"),
    ("5333.T", "日本碍子"),
    ("5401.T", "日本製鉄"),
    ("5406.T", "神戸製鋼所"),
    ("5411.T", "JFEホールディングス"),
    ("5631.T", "日本製鋼所"),
    ("5703.T", "日本軽金属HD"),
    ("5706.T", "三井金属鉱業"),
    ("5707.T", "東邦亜鉛"),
    ("5711.T", "三菱マテリアル"),
    ("5713.T", "住友金属鉱山"),
    ("5714.T", "DOWAホールディングス"),
    ("5741.T", "UACJ"),
    ("5801.T", "古河電気工業"),
    ("5802.T", "住友電気工業"),
    ("5803.T", "フジクラ"),
    ("6098.T", "リクルートHD"),
    ("6103.T", "オークマ"),
    ("6113.T", "アマダ"),
    ("6178.T", "日本郵政"),
    ("6273.T", "SMC"),
    ("6301.T", "小松製作所"),
    ("6302.T", "住友重機械工業"),
    ("6305.T", "日立建機"),
    ("6326.T", "クボタ"),
    ("6361.T", "荏原製作所"),
    ("6367.T", "ダイキン工業"),
    ("6370.T", "栗田工業"),
    ("6383.T", "ダイフク"),
    ("6406.T", "フジテック"),
    ("6412.T", "平和"),
    ("6417.T", "SANKYO"),
    ("6471.T", "日本精工"),
    ("6472.T", "NTN"),
    ("6473.T", "ジェイテクト"),
    ("6479.T", "ミネベアミツミ"),
    ("6501.T", "日立製作所"),
    ("6503.T", "三菱電機"),
    ("6504.T", "富士電機"),
    ("6506.T", "安川電機"),
    ("6526.T", "ソシオネクスト"),
    ("6532.T", "ベイカレント・コンサルティング"),
    ("6586.T", "マキタ"),
    ("6594.T", "ニデック"),
    ("6645.T", "オムロン"),
    ("6674.T", "GSユアサ"),
    ("6701.T", "NEC"),
    ("6702.T", "富士通"),
    ("6723.T", "ルネサスエレクトロニクス"),
    ("6724.T", "セイコーエプソン"),
    ("6752.T", "パナソニックHD"),
    ("6753.T", "シャープ"),
    ("6754.T", "アンリツ"),
    ("6758.T", "ソニーグループ"),
    ("6762.T", "TDK"),
    ("6770.T", "アルプスアルパイン"),
    ("6773.T", "パイオニア"),
    ("6794.T", "フォスター電機"),
    ("6796.T", "クラリオン"),
    ("6806.T", "ヒロセ電機"),
    ("6841.T", "横河電機"),
    ("6857.T", "アドバンテスト"),
    ("6861.T", "キーエンス"),
    ("6902.T", "デンソー"),
    ("6952.T", "カシオ計算機"),
    ("6954.T", "ファナック"),
    ("6971.T", "京セラ"),
    ("6981.T", "村田製作所"),
    ("6988.T", "日東電工"),
    ("7003.T", "三井E&S"),
    ("7011.T", "三菱重工業"),
    ("7012.T", "川崎重工業"),
    ("7013.T", "IHI"),
    ("7201.T", "日産自動車"),
    ("7202.T", "いすゞ自動車"),
    ("7203.T", "トヨタ自動車"),
    ("7205.T", "日野自動車"),
    ("7211.T", "三菱自動車工業"),
    ("7261.T", "マツダ"),
    ("7267.T", "本田技研工業"),
    ("7270.T", "SUBARU"),
    ("7272.T", "ヤマハ発動機"),
    ("7731.T", "ニコン"),
    ("7733.T", "オリンパス"),
    ("7735.T", "SCREENホールディングス"),
    ("7741.T", "HOYA"),
    ("7751.T", "キヤノン"),
    ("7752.T", "リコー"),
    ("7762.T", "シチズン時計"),
    ("7832.T", "バンダイナムコHD"),
    ("7951.T", "ヤマハ"),
    ("8001.T", "伊藤忠商事"),
    ("8002.T", "丸紅"),
    ("8015.T", "豊田通商"),
    ("8031.T", "三井物産"),
    ("8035.T", "東京エレクトロン"),
    ("8053.T", "住友商事"),
    ("8058.T", "三菱商事"),
    ("8233.T", "高島屋"),
    ("8252.T", "丸井グループ"),
    ("8253.T", "クレディセゾン"),
    ("8267.T", "イオン"),
    ("8303.T", "新生銀行"),
    ("8304.T", "あおぞら銀行"),
    ("8306.T", "三菱UFJフィナンシャルG"),
    ("8308.T", "りそなHD"),
    ("8309.T", "三井住友トラストHD"),
    ("8316.T", "三井住友フィナンシャルG"),
    ("8331.T", "千葉銀行"),
    ("8354.T", "ふくおかFG"),
    ("8355.T", "静岡銀行"),
    ("8377.T", "ほくほくFG"),
    ("8411.T", "みずほフィナンシャルG"),
    ("8601.T", "大和証券グループ本社"),
    ("8604.T", "野村HD"),
    ("8628.T", "松井証券"),
    ("8630.T", "SOMPOホールディングス"),
    ("8697.T", "日本取引所グループ"),
    ("8725.T", "MS&ADインシュアランスG"),
    ("8750.T", "第一生命HD"),
    ("8752.T", "三井住友海上グループHD"),
    ("8766.T", "東京海上HD"),
    ("8795.T", "T&Dホールディングス"),
    ("8801.T", "三井不動産"),
    ("8802.T", "三菱地所"),
    ("8804.T", "東京建物"),
    ("8830.T", "住友不動産"),
    ("9001.T", "東武鉄道"),
    ("9005.T", "東急"),
    ("9007.T", "小田急電鉄"),
    ("9008.T", "京王電鉄"),
    ("9009.T", "京成電鉄"),
    ("9020.T", "東日本旅客鉄道"),
    ("9021.T", "西日本旅客鉄道"),
    ("9022.T", "東海旅客鉄道"),
    ("9064.T", "ヤマトHD"),
    ("9101.T", "日本郵船"),
    ("9104.T", "商船三井"),
    ("9107.T", "川崎汽船"),
    ("9202.T", "ANAホールディングス"),
    ("9301.T", "三菱倉庫"),
    ("9432.T", "日本電信電話"),
    ("9433.T", "KDDI"),
    ("9434.T", "ソフトバンク"),
    ("9501.T", "東京電力HD"),
    ("9502.T", "中部電力"),
    ("9503.T", "関西電力"),
    ("9531.T", "東京ガス"),
    ("9532.T", "大阪ガス"),
    ("9602.T", "東宝"),
    ("9613.T", "NTTデータグループ"),
    ("9735.T", "セコム"),
    ("9766.T", "コナミグループ"),
    ("9983.T", "ファーストリテイリング"),
    ("9984.T", "ソフトバンクグループ"),
]


# ── 1銘柄バックテスト（並列用） ───────────────────────────────
def backtest_one(symbol: str, name: str, start: str, end: str):
    try:
        df = fetch_data(symbol, start, end)
        if df is None or len(df) < 60:
            return None
        df = add_indicators(df)
        result = run_backtest(df)
        return {
            "symbol":   symbol,
            "name":     name,
            "total_pnl":    result["total_pnl"],
            "return_pct":   result["return_pct"],
            "n_trades":     result["n_trades"],
            "win_rate":     result["win_rate"],
            "profit_factor":result["profit_factor"],
            "max_drawdown": result["max_drawdown"],
        }
    except Exception:
        return None


# ── HTML 出力 ─────────────────────────────────────────────────
def export_ranking_html(rows: list, start: str, end: str) -> None:
    plt.rcParams["font.family"] = [
        "IPAexGothic", "Noto Sans CJK JP", "Hiragino Sans", "MS Gothic", "sans-serif"
    ]

    # 棒グラフ（上位20）
    top20 = rows[:20]
    fig, ax = plt.subplots(figsize=(14, 5), facecolor="#1a1a2e")
    ax.set_facecolor("#16213e")
    colors = ["#00e676" if r["total_pnl"] >= 0 else "#ff5252" for r in top20]
    labels = [f"{r['name']}\n{r['symbol']}" for r in top20]
    vals   = [r["total_pnl"] for r in top20]
    ax.bar(range(len(top20)), vals, color=colors, width=0.7, edgecolor="none")
    ax.set_xticks(range(len(top20)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7.5)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:+,.0f}"))
    ax.axhline(0, color="#888", linewidth=0.6)
    ax.set_title("利益上位20銘柄（総損益）", color="#e0e0e0", fontsize=12, pad=8)
    ax.set_ylabel("総損益 (円)", color="#e0e0e0")
    ax.tick_params(colors="#aaa")
    ax.grid(axis="y", alpha=0.15, color="#555")
    ax.spines[list(ax.spines)].set_color("#333")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    chart_b64 = base64.b64encode(buf.getvalue()).decode()

    # テーブル行
    table_rows = ""
    for i, r in enumerate(rows, 1):
        pf  = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        sgn = "+" if r["total_pnl"] >= 0 else ""
        cls = "profit" if r["total_pnl"] >= 0 else "loss"
        table_rows += (
            f'<tr>'
            f'<td class="num">{i}</td>'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["name"]}</td>'
            f'<td class="num {cls}">{sgn}{r["total_pnl"]:,.0f}</td>'
            f'<td class="num {cls}">{sgn}{r["return_pct"]:.2f}%</td>'
            f'<td class="num">{r["n_trades"]}</td>'
            f'<td class="num">{r["win_rate"]:.1f}%</td>'
            f'<td class="num">{pf}</td>'
            f'<td class="num loss">{r["max_drawdown"]:.2f}%</td>'
            f'</tr>\n'
        )

    profit_cnt = sum(1 for r in rows if r["total_pnl"] > 0)
    loss_cnt   = len(rows) - profit_cnt

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日経225 A6ストキャスティクス 利益ランキング</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:"Yu Gothic UI","Hiragino Sans","Noto Sans JP",sans-serif;
        background:#0d1117;color:#e6edf3;font-size:14px}}
  .header{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);
           padding:28px 36px;border-bottom:1px solid #30363d}}
  .header h1{{font-size:1.6em;margin-bottom:8px;color:#fff}}
  .header p{{opacity:.8;font-size:.88em;line-height:1.9;color:#cdd9e5}}
  .section{{margin:24px 36px}}
  h2.sec{{font-size:1.05em;color:#79c0ff;border-left:4px solid #388bfd;
           padding-left:10px;margin-bottom:16px}}
  .kpi-grid{{display:flex;flex-wrap:wrap;gap:14px;margin:24px 36px}}
  .kpi-card{{background:#161b22;border:1px solid #30363d;border-radius:10px;
             padding:16px 22px;min-width:160px;flex:1}}
  .kpi-label{{font-size:.78em;color:#8b949e;margin-bottom:6px}}
  .kpi-value{{font-size:1.35em;font-weight:700;color:#e6edf3}}
  .profit{{color:#3fb950;font-weight:600}}
  .loss{{color:#f85149;font-weight:600}}
  .chart-card{{background:#161b22;border:1px solid #30363d;border-radius:10px;
               padding:16px}}
  .chart-card img{{width:100%;height:auto;border-radius:6px}}
  table{{width:100%;border-collapse:collapse;background:#161b22;
         border-radius:8px;overflow:hidden;border:1px solid #30363d}}
  thead th{{background:#21262d;color:#8b949e;padding:10px 14px;
            text-align:center;font-size:.82em;white-space:nowrap;
            border-bottom:1px solid #30363d;cursor:pointer}}
  thead th:hover{{color:#79c0ff}}
  tbody tr:hover{{background:#1c2128}}
  td{{padding:8px 14px;border-bottom:1px solid #21262d;white-space:nowrap;color:#e6edf3}}
  .num{{text-align:right}}
</style>
</head>
<body>
<div class="header">
  <h1>📊 日経225 — A6: ストキャスティクス 利益ランキング</h1>
  <p>
    バックテスト期間: {start} ～ {end}（直近 {YEARS} 年）&nbsp;|&nbsp;
    初期資金: {INITIAL_CASH:,}円&nbsp;|&nbsp;
    Entry: %K が %D を <strong>30以下</strong>から上抜け&nbsp;
    Exit: %K が %D を <strong>60以上</strong>から下抜け&nbsp;|&nbsp;
    ストップ: ATR({ATR_PERIOD}) × {ATR_STOP_MULT}<br>
    生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}&nbsp;|&nbsp;
    対象銘柄数: {len(rows)} 銘柄
  </p>
</div>

<div class="kpi-grid">
  <div class="kpi-card">
    <div class="kpi-label">対象銘柄数</div>
    <div class="kpi-value">{len(rows)} 銘柄</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">利益プラス銘柄</div>
    <div class="kpi-value profit">{profit_cnt} 銘柄</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">利益マイナス銘柄</div>
    <div class="kpi-value loss">{loss_cnt} 銘柄</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">最高収益率</div>
    <div class="kpi-value profit">+{rows[0]["return_pct"]:.2f}%</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">1位銘柄</div>
    <div class="kpi-value">{rows[0]["name"]}</div>
  </div>
</div>

<div class="section">
  <h2 class="sec">利益上位20銘柄</h2>
  <div class="chart-card">
    <img src="data:image/png;base64,{chart_b64}" alt="上位20銘柄チャート">
  </div>
</div>

<div class="section">
  <h2 class="sec">全銘柄ランキング（{len(rows)} 件）— 収益率順</h2>
  <table id="tbl">
    <thead><tr>
      <th>#</th><th>コード</th><th>銘柄名</th>
      <th>総損益(円)</th><th>収益率</th>
      <th>取引回数</th><th>勝率</th><th>PF</th><th>最大DD</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


# ── メイン ────────────────────────────────────────────────────
def main():
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * YEARS + 60)).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  日経225 A6ストキャスティクス バックテスト")
    print(f"  期間: {start} ～ {end}  銘柄数: {len(NIKKEI225)}")
    print(f"{'='*60}\n")

    results = []
    done = 0
    total = len(NIKKEI225)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {
            ex.submit(backtest_one, sym, name, start, end): (sym, name)
            for sym, name in NIKKEI225
        }
        for fut in as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            sym, name = futures[fut]
            status = f"+{r['return_pct']:+.1f}%" if r else "スキップ"
            print(f"  [{done:3d}/{total}] {sym:<10} {name:<20} {status}")

    # 収益率順にソート
    results.sort(key=lambda x: x["return_pct"], reverse=True)

    print(f"\n  ── 上位10銘柄 ──────────────────────────────")
    for i, r in enumerate(results[:10], 1):
        print(f"  {i:2d}. {r['symbol']:<10} {r['name']:<20} {r['return_pct']:+.2f}%  {r['total_pnl']:+,.0f}円")
    print(f"  ────────────────────────────────────────────")

    print(f"\n  HTML レポート生成中 ...")
    export_ranking_html(results, start, end)
    abs_path = os.path.abspath(OUTPUT_HTML)
    print(f"  保存完了: {abs_path}")

    try:
        if sys.platform == "win32":
            os.startfile(abs_path)
        elif sys.platform == "darwin":
            subprocess.run(["open", abs_path], check=False)
        else:
            subprocess.run(["xdg-open", abs_path], check=False)
    except Exception:
        pass

    print(f"\n{'='*60}")
    print("  完了！HTMLレポートを確認してください。")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
