"""
run_signals_holdout_all.py — nikkei_analysis.py のシグナル・損益タブと
完全同一フォーマットで、複数ホールドアウト設定を1画面で確認する。

WATCHLISTの優先順:
  1. ホールドアウト専用CSV  walkforward_{strat}_holdout{N}d_*.csv
  2. 標準WF CSV            walkforward_{strat}_*.csv  (holdoutなし)
  3. フォールバック         check_signals_stop/breakout の WATCHLIST

使い方:
  python run_signals_holdout_all.py
  python run_signals_holdout_all.py --workers 8
  python run_signals_holdout_all.py --no-browser
  python run_signals_holdout_all.py --date 2026-06-09
  python run_signals_holdout_all.py --min-score 60
  python run_signals_holdout_all.py --days 180   # 最初に表示する期間 (デフォルト180)
  python run_signals_holdout_all.py --short --force  # 当日キャッシュを無視して再生成

当日キャッシュ:
  同一パラメータ(--short/--symbol/--date)の出力HTMLが当日分すでに存在すれば、
  重いバックテストをスキップしてそのファイルを開いて即終了する。
  フィルター(--max-price/--min-price/--days 等)を変えた場合や強制再計算したい
  場合は --force を付ける。
"""
from __future__ import annotations

import argparse
import copy as _copy
import csv
import importlib as _importlib
import os
import sys

# OpenBLAS/MKLのスレッド数を1に制限（メモリ不足エラー回避）
# importより前に設定する必要がある
for _blas_env in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_blas_env, "1")
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

_JST_TZ = timezone(timedelta(hours=9))

def _report_date():
    """レポート基準日: 15時以降なら当日、それ以前なら前営業日（深夜実行時に翌日付けになるのを防ぐ）"""
    now   = datetime.now(_JST_TZ)
    today = now.date()
    wd    = today.weekday()  # 0=Mon...6=Sun
    if wd == 5:  # 土
        return today - timedelta(days=1)
    if wd == 6:  # 日
        return today - timedelta(days=2)
    if now.hour >= 15:
        return today  # 引け後 → 当日
    if wd == 0:  # 月
        return today - timedelta(days=3)  # 前金曜
    return today - timedelta(days=1)

# ── 引数先読み ────────────────────────────────────────────────────────────────
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--workers",    type=int,   default=1)
_pre.add_argument("--no-browser", action="store_true")
_pre.add_argument("--date",       type=str,   default=None)
_pre.add_argument("--min-score",  type=int,   default=0)
_pre.add_argument("--wf-dir",     type=Path,  default=Path("walkforward_results"))
_pre.add_argument("--auto-scan",  action="store_true")
_pre.add_argument("--max-price",  type=float, default=10000.0)
_pre.add_argument("--min-price",  type=float, default=0.0,
                  help="最新終値の下限 (円/株). 低位株除外 (例: 1000)")
_pre.add_argument("--days",       type=int,   default=180,
                  help="損益タブで最初に表示する期間 (30/60/90/120/150/180)")
_pre.add_argument("--symbol",     type=str,   default=None,
                  help="指定銘柄の期間別取引詳細を追加表示 (例: 8050.T)")
_pre.add_argument("--short",      action="store_true",
                  help="ショート戦略(A7_S/RSI2_S/MACD_S/DON_S/MOM_S/GAP_S/VOL_S)で出力")
_pre.add_argument("--force",      action="store_true", default=True,
                  help="当日の生成済みHTMLがあっても無視して再生成する(既定ON)")
_pre.add_argument("--no-force",    dest="force", action="store_false",
                  help="当日キャッシュがあれば再生成しない")
_pre.add_argument("--entry-days", type=int, default=None,
                  help="取引明細をエントリー日ベースで絞り込む日数 (例: 7=直近1週間エントリーのみ)")
_pre.add_argument("--both",       action="store_true",
                  help="ロング+ショート両方を実行して1つのHTMLにまとめる")
_pre.add_argument("--price-ranges", type=str, default=None,
                  help="複数の株価上限をカンマ区切りで指定 (例: 6000,10000). --bothと組み合わせて使用")
_pre.add_argument("--output-suffix", type=str, default="",
                  help="出力HTMLファイル名にサフィックスを付ける (内部用・--bothから自動設定)")
_pre.add_argument("--rolling", type=int, default=0,
                  help="ローリング逆指値: 未約定時に終値で注文価格を更新する最大回数 (例: --rolling 2)")
_pre.add_argument("--wf-until", type=str, default=None,
                  help="WF歴史検証の最新基準日 (YYYY-MM-DD). 省略時は6ヶ月前を自動設定")
_pre.add_argument("--wf-periods", type=int, default=4,
                  help="WF歴史検証の期間数・6ヶ月間隔 (デフォルト: 4期間 = 約2年分). 0を指定するとスキップ")
_pre.add_argument("--no-wf-history", action="store_true", default=True,
                  help="WF歴史検証タブをスキップ（高速化・既定ON）")
_pre.add_argument("--wf-history", dest="no_wf_history", action="store_false",
                  help="WF歴史検証タブを計算する")
_pre.add_argument("--no-preoos", action="store_true", default=True,
                  help="OOS前BTスコア計算をスキップ（高速化・既定ON）")
_pre.add_argument("--preoos", dest="no_preoos", action="store_false",
                  help="OOS前BTスコア計算を行う")
_pre.add_argument("--wf-universe", type=str, default=None,
                  help="WF歴史検証で使うユニバースファイル (例: symbols_all.py=N225, symbols_listed_prime.py=プライム全体). デフォルト: 自動検出")
_pre.add_argument("--oos-until", type=str, default=None,
                  help="OOS検証終了日 (YYYY-MM-DD). 指定時のみOOS検証HTMLを生成する")
_pre.add_argument("--oos-days",  type=int, default=365,
                  help="OOS検証期間（日数）. デフォルト365日")
_pre.add_argument("--max-holds", type=str, default=None,
                  help="比較する最大保有日数をカンマ区切りで指定 (例: 7,15,20). --bothと組み合わせて使用")
_pre.add_argument("--recalc-analysis", action="store_true",
                  help="保有期間比較・押し目買い比較・寄り確認など日々変わらない構造分析を"
                       "強制再計算する (既定: 最新キャッシュを日付跨ぎで再利用。--force では再計算しない)")
_pre.add_argument("--minute-dir", default=None,
                  help="⑭寄り確認タブ用の5分足CSVディレクトリ ({code}.csv)。"
                       "無指定はyfinance(約60日)")
_pre.add_argument("--open-confirm", action="store_true",
                  help="⑭寄り確認タブを計算する(5分足を読むので重い)。"
                       "未指定でもキャッシュがあれば表示。既定OFFで日次レポートを軽量化")
_pre.add_argument("--fill-timing", action="store_true",
                  help="⑮約定タイミングタブを計算する。未指定でもキャッシュがあれば表示")
_pre.add_argument("--serve", action="store_true", default=True,
                  help="レポート生成後に発注サーバ(order_server.py)を起動する (既定ON)")
_pre.add_argument("--serve-execute", action="store_true", default=True,
                  help="--serve 時に実発注で起動する (既定ON)")
_pre.add_argument("--serve-prod", action="store_true", default=True,
                  help="--serve 時に本番口座(18080)で起動する (既定ON)")
_pre.add_argument("--serve-genbutsu", action="store_true",
                  help="--serve 時にロングを現物で起動する (未指定なら信用新規)")
# ── 安全解除フラグ (既定=本番自動発注ON のため、止める/デモにする手段を用意) ──
_pre.add_argument("--no-serve", dest="serve", action="store_false",
                  help="発注サーバを起動しない(レポート生成のみ)")
_pre.add_argument("--demo", action="store_true",
                  help="デモ口座(18081)・実発注で起動 (本番=18080 を回避)")
_pre.add_argument("--dry", action="store_true",
                  help="発注サーバを dry-run で起動 (実発注しない)")
_pre.add_argument("--no-minute-update", action="store_true",
                  help="5分足の自動更新(1日1回)を行わない")
_args, _ = _pre.parse_known_args()
# --demo / --dry の反映
if getattr(_args, "demo", False):
    _args.serve_prod = False
if getattr(_args, "dry", False):
    _args.serve_execute = False


def _maybe_serve_orders():
    """--serve 指定時、レポート生成後に発注サーバを前面で起動する。
    Ctrl+C で停止するまでブロックする。"""
    if not getattr(_args, "serve", False):
        return
    import subprocess as _sp2
    from pathlib import Path as _P2
    _srv = _P2(__file__).resolve().parent / "order_server.py"
    _cmd = [sys.executable, str(_srv)]
    if _args.serve_execute:
        _cmd.append("--execute")
    if _args.serve_prod:
        _cmd.append("--prod")
    if _args.serve_genbutsu:
        _cmd.append("--genbutsu")
    _is_prod_live = _args.serve_execute and _args.serve_prod
    print("\n" + "=" * 65)
    if _is_prod_live:
        print("⚠⚠⚠  本番口座(18080)・実発注モードで発注サーバを起動します  ⚠⚠⚠")
        print("⚠  接続時に本番の自動発注(利確補完など)が走ります。誤発注に注意。")
        print("⚠  止める: --no-serve / デモ: --demo / 実発注しない: --dry")
    else:
        _lbl = ("デモ(18081)" if not _args.serve_prod else "本番(18080)")
        _ex = "実発注" if _args.serve_execute else "dry-run(発注なし)"
        print(f"発注サーバを起動します（{_lbl} / {_ex}）")
    print("  " + " ".join(_cmd))
    print("  停止するには Ctrl+C")
    print("=" * 65)
    try:
        _sp2.run(_cmd)
    except KeyboardInterrupt:
        pass


def _auto_update_minute():
    """WATCHLIST銘柄の5分足を最新化する(1日1回・自動・非致命)。
    ⑭③フェア版が最新5分足を使えるように、日次コマンドの中で自動更新する。
    別コマンド(update_minute_5m)を打たなくて済むように統合。"""
    _mk = Path(".holdout_bt_cache")
    _mk.mkdir(exist_ok=True)
    _flag = _mk / f".minute_updated_{_report_date()}"
    if _flag.exists():
        return                                  # 本日更新済み → スキップ
    # fetcher を姉妹フォルダ探索で特定
    try:
        from update_minute_5m import _find_fetcher
        fetcher = _find_fetcher()
    except Exception:
        fetcher = None
    if fetcher is None:
        return                                  # 見つからなければ黙ってスキップ
    # WATCHLIST 銘柄を集める(ロング+ショート)
    syms: set = set()
    for _mod_name in ("check_signals_stop", "check_signals_breakout",
                      "check_signals_short", "check_signals_short_breakout"):
        try:
            _m = _importlib.import_module(_mod_name)
            for _s, _n, _st in getattr(_m, "WATCHLIST", []):
                syms.add(_s)
        except Exception:
            pass
    if not syms:
        return
    import subprocess as _sp3
    _cmd = [sys.executable, str(fetcher), "--symbols", *sorted(syms)]
    print("=" * 65)
    print(f"5分足を自動更新中 (WATCHLIST {len(syms)}銘柄・本日初回のみ)...", flush=True)
    print("=" * 65)
    try:
        _sp3.run(_cmd, cwd=str(fetcher.parent), timeout=600)
        _flag.write_text("ok", encoding="utf-8")
    except Exception as _e:
        print(f"  ⚠ 5分足自動更新スキップ ({_e})", flush=True)


# 日次コマンドの一部として5分足を自動更新(1日1回)。--no-minute-update で無効化。
if not getattr(_args, "no_minute_update", False):
    _auto_update_minute()

# ── --both モード: ロング+ショートを統合HTMLに ───────────────────────────────
if _args.both and not _args.short:
    import subprocess as _sp
    _bd   = _args.date or str(_report_date())
    _bout = Path(f"signals_holdout_all_both_{_bd}.html")

    if _bout.exists() and not _args.force:
        print(f"[CACHE] 当日生成済み(both): {_bout.resolve()}")
        print(f"        再生成するには --force を付けてください。")
        if not _args.no_browser:
            from _open_html import open_html
            open_html(_bout.resolve())
        _maybe_serve_orders()
        sys.exit(0)

    # 株価範囲リストを構築
    _min_p_val = _args.min_price if _args.min_price and _args.min_price > 0 else 0.0
    if _args.price_ranges:
        _price_list = [int(x.strip()) for x in _args.price_ranges.split(",") if x.strip()]
    else:
        _price_list = [int(_args.max_price) if _args.max_price < 100000 else 10000]
    _multi_price = len(_price_list) > 1

    # --both / --short / --no-browser / --price-ranges / --output-suffix は渡さず、
    # --max-holds / --force はサブプロセスに伝播させる
    _base_cargs = [a for a in sys.argv[1:]
                   if a not in ("--both", "--short", "--no-browser", "--price-ranges", "--output-suffix",
                                "--serve", "--serve-execute", "--serve-prod", "--serve-genbutsu")
                   and not a.startswith("--price-ranges=")
                   and not a.startswith("--output-suffix=")]
    # --max-price も除去（後で各ループで付け直す）
    _base_cargs_no_price = []
    _skip_next = False
    for _a in _base_cargs:
        if _skip_next:
            _skip_next = False
            continue
        if _a == "--max-price":
            _skip_next = True
            continue
        if _a.startswith("--max-price="):
            continue
        _base_cargs_no_price.append(_a)
    if "--force" not in _base_cargs_no_price:
        _base_cargs_no_price.append("--force")
    _base_cargs_no_price.append("--no-browser")
    # serve は既定ONのため、サブプロセスでは必ず無効化(発注サーバは親が最後に1回だけ起動)
    _base_cargs_no_price.append("--no-serve")

    # 各価格範囲 × ロング/ショートを生成（逐次実行でメモリ使用を抑える）
    # --max-holds はサブプロセスに渡り、損益タブ内の比較セクションとして生成される
    _generated: dict = {}  # (direction, max_p) -> Path
    for _mp in _price_list:
        _mp_suffix = f"_p{_mp}" if _multi_price else ""
        _lf = Path(f"signals_holdout_all_{_bd}{_mp_suffix}.html")
        _sf = Path(f"signals_holdout_all_short_{_bd}{_mp_suffix}.html")
        _cargs_mp = _base_cargs_no_price + ["--max-price", str(_mp), "--output-suffix", _mp_suffix]

        print("=" * 65)
        print(f"=== ロングシグナル生成中 (〜{_mp:,}円) ===")
        print("=" * 65)
        _sp.run([sys.executable, __file__] + _cargs_mp)

        print("=" * 65)
        print(f"=== ショートシグナル生成中 (〜{_mp:,}円) ===")
        print("=" * 65)
        _sp.run([sys.executable, __file__] + _cargs_mp + ["--short"])

        if not _lf.exists() or not _sf.exists():
            print(f"[ERROR] ロング/ショートHTMLの生成に失敗しました (max-price={_mp})")
            sys.exit(1)
        _generated[("long",  _mp)] = _lf
        _generated[("short", _mp)] = _sf

    # ── 最初の価格範囲をデフォルト表示 ──────────────────────────────────────
    _first_mp   = _price_list[0]
    _min_p_disp = int(_min_p_val) if _min_p_val > 0 else None

    def _price_label(mp: int) -> str:
        parts = []
        if _min_p_disp:
            parts.append(f"{_min_p_disp:,}〜")
        parts.append(f"{mp:,}円")
        return "".join(parts)

    # ── ナビゲーションHTML生成 ────────────────────────────────────────────
    _nav_btns = ""
    _frames   = ""

    # ロング/ショートボタン
    for _dir, _lbl_prefix, _dir_cls in [("long", "📈 ロング", "lb"), ("short", "📉 ショート", "sb")]:
        _is_active = _dir == "long"
        _nav_btns += (
            f'  <button class="ls-btn {_dir_cls}{" active" if _is_active else ""}" '
            f'onclick="switchLs(\'{_dir}\')">{_lbl_prefix}</button>\n'
        )

    # 価格帯ボタン（複数の場合のみ、セパレータを挟んで同じ行に追加）
    if _multi_price:
        _nav_btns += '  <span class="nav-sep"></span>\n'
        for _i, _mp in enumerate(_price_list):
            _nav_btns += (
                f'  <button class="pr-btn{" active" if _i==0 else ""}" data-price="{_mp}" '
                f'onclick="switchPr({_mp})">{_price_label(_mp)}</button>\n'
            )
    else:
        _nav_btns += (
            f'  <span style="font-size:0.68rem;font-weight:600;color:#fbbf24;'
            f'background:#292418;border:1px solid #854d0e;border-radius:4px;'
            f'padding:1px 6px;margin-left:8px;align-self:center">'
            f'株価 {_price_label(_price_list[0])}</span>\n'
        )

    # 📌 保有タブ（実際に約定した建玉。close_stop_guard --holdings-html が生成）
    _nav_btns += '  <span class="nav-sep"></span>\n'
    _nav_btns += '  <button class="hold-btn" onclick="switchHoldings(event)">📌 保有</button>\n'
    _holdings_path = Path("holdings_latest.html")
    if not _holdings_path.exists():
        _holdings_path.write_text(
            "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
            "<style>body{background:#0f172a;color:#94a3b8;font-family:sans-serif;padding:30px}"
            "h2{color:#e2e8f0}pre{color:#fbbf24;background:#1e293b;padding:12px;border-radius:8px}</style>"
            "</head><body><h2>📌 保有銘柄</h2>"
            "<p>まだ約定確認をしていません。引け後に次を実行すると、実際に約定した保有銘柄と"
            "タイムカット日が表示されます（このタブを再読み込み）:</p>"
            "<pre>python close_stop_guard.py --holdings-html --kabu --prod</pre>"
            "</body></html>", encoding="utf-8")

    import time as _time_mod
    _cache_bust = int(_time_mod.time())
    for _dir in ("long", "short"):
        for _i, _mp in enumerate(_price_list):
            _frame_id = f"ls-{_dir}-{_mp}"
            _active_fr = " active" if _dir == "long" and _i == 0 else ""
            _src = _generated[(_dir, _mp)].name
            # 遅延ロード: 初期アクティブ(long×先頭価格帯)のみ src を入れ、残りは
            # data-src にして初回クリック時に読む。開いた瞬間に全レポートを同時
            # ロードして重くなるのを防ぐ (5枚→1枚)。
            _is_active_fr = (_dir == "long" and _i == 0)
            _src_attr = (f'src="{_src}?v={_cache_bust}"' if _is_active_fr
                         else f'data-src="{_src}?v={_cache_bust}"')
            _frames += f'<iframe id="{_frame_id}" class="ls-frame{_active_fr}" {_src_attr}></iframe>\n'
    _frames += f'<iframe id="holdings-frame" class="hold-frame" data-src="holdings_latest.html?v={_cache_bust}"></iframe>\n'

    _bout.write_text(f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ホールドアウト全設定 ロング+ショート統合 {_bd} [保有{__import__('backtest_limit_entry').MAX_HOLD}日]</title>
<style>
body{{margin:0;padding:0;background:#0f172a;font-family:sans-serif}}
.ls-nav{{display:flex;gap:0;align-items:flex-end;border-bottom:2px solid #1e293b;background:#0f172a;
  position:sticky;top:0;z-index:9999;padding:8px 16px 0}}
.ls-btn{{padding:11px 28px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:1.05rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.ls-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.ls-btn.active.lb{{color:#34d399;border-bottom:2px solid #34d399;background:#0f172a}}
.ls-btn.active.sb{{color:#f87171;border-bottom:2px solid #f87171;background:#0f172a}}
.pr-btn{{padding:9px 20px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:0.92rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.pr-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.pr-btn.active{{background:#0f172a;border-bottom:2px solid #fbbf24;color:#fbbf24}}
.nav-sep{{width:1px;background:#334155;margin:8px 8px 4px;align-self:stretch}}
.hold-btn{{padding:9px 20px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:0.92rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.hold-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.hold-btn.active{{background:#0f172a;border-bottom:2px solid #60a5fa;color:#60a5fa}}
.ls-frame,.hold-frame{{display:none;width:100%;border:none;height:calc(100vh - 54px)}}
.ls-frame.active,.hold-frame.active{{display:block}}
</style>
</head>
<body>
<div class="ls-nav">
{_nav_btns}</div>
{_frames}
<script>
var _curDir = 'long';
var _curPr  = {_first_mp};
function _ensureLoaded(f) {{
  // data-src だけの iframe を初回表示時に読み込む (遅延ロード)
  if (f && f.dataset && f.dataset.src) {{ f.src = f.dataset.src; f.removeAttribute('data-src'); }}
}}
function _showFrame() {{
  document.querySelectorAll('.ls-frame,.hold-frame').forEach(f => f.classList.remove('active'));
  document.querySelectorAll('.hold-btn').forEach(b => b.classList.remove('active'));
  var f = document.getElementById('ls-' + _curDir + '-' + _curPr);
  if (f) {{ _ensureLoaded(f); f.classList.add('active'); }}
}}
function switchLs(dir) {{
  _curDir = dir;
  document.querySelectorAll('.ls-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  _showFrame();
}}
function switchPr(pr) {{
  _curPr = pr;
  document.querySelectorAll('.pr-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  _showFrame();
}}
function switchHoldings(ev) {{
  document.querySelectorAll('.ls-frame,.hold-frame').forEach(f => f.classList.remove('active'));
  document.querySelectorAll('.hold-btn').forEach(b => b.classList.remove('active'));
  ev.target.classList.add('active');
  var hf = document.getElementById('holdings-frame');
  _ensureLoaded(hf);
  hf.classList.add('active');
}}
</script>
</body>
</html>""", encoding="utf-8")
    print(f"\n統合レポート生成完了: {_bout.resolve()}")
    if not _args.no_browser:
        from _open_html import open_html
        open_html(_bout.resolve())
    _maybe_serve_orders()
    sys.exit(0)

# ── ロング/ショートの戦略セット ──────────────────────────────────────────────
if _args.short:
    _STOP_STRATS = ["A7_S", "RSI2_S", "MACD_S"]      # ショート逆指値系
    _BRK_STRATS  = ["DON_S", "MOM_S", "GAP_S"]       # ショートBRK系
else:
    # VOL/MACD はトレンドフィルタ版(VOLTF/MACDTF, +MA50)を採用。
    # WF比較(compare_filter_variants)で全12設定 PF↑・DD↓・PnL↑ を確認済み。
    # falling knife(MA50割れ, 例:東洋エンジ)のシグナルを除外する。
    _STOP_STRATS = ["MACDTF", "A7", "RSI2"]
    _BRK_STRATS  = ["DON", "VOLTF", "MOM"]

JST   = _JST_TZ
TODAY = _report_date()

# ── 当日キャッシュ: 生成済みHTMLがあれば再計算をスキップ ──────────────────────
# 重いバックテストに入る前に、同一パラメータの出力ファイルが既に存在すれば
# それを開いて即終了する。--force で強制再生成。
_cache_date    = _args.date or str(TODAY)
_cache_short   = "_short" if _args.short else ""
# サイジング/保有日数の設定をキャッシュ名に反映 (設定を変えたら別キャッシュになる)
# これが無いと VOL_PARITY や MAX_HOLD_OVERRIDE を変えても古い結果を読んでしまう
def _settings_sig() -> str:
    parts = []
    if os.environ.get("VOL_PARITY", "0").lower() in ("1", "true", "yes", "on"):
        parts.append("vp" + os.environ.get("RISK_PER_TRADE", "20000"))
    mho = os.environ.get("MAX_HOLD_OVERRIDE")
    if mho:
        parts.append("mh" + mho)
    return ("_" + "_".join(parts)) if parts else ""
_cache_settings = _settings_sig()
_cache_short   = _cache_short + _cache_settings
_cache_symbol  = ""
if _args.symbol:
    _s = _args.symbol.upper()
    if not _s.endswith(".T"):
        _s += ".T"
    _cache_symbol = f"_{_s.replace('.', '')}"
_out_suffix_arg = _args.output_suffix if _args.output_suffix else ""
_cached_out = Path(f"signals_holdout_all{_cache_short}{_cache_symbol}_{_cache_date}{_out_suffix_arg}.html")
if _cached_out.exists() and not _args.force:
    print(f"[CACHE] 当日生成済み: {_cached_out.resolve()}")
    print(f"        再生成するには --force を付けてください。")
    if not _args.no_browser:
        from _open_html import open_html
        open_html(_cached_out.resolve())
    sys.exit(0)

_PNL_PERIODS  = [30, 60, 90, 120, 150, 180, 270, 365]
_DEFAULT_DAYS = _args.days if _args.days in _PNL_PERIODS else 180

HOLDOUT_CONFIGS = [
    (30,  "HO30d",  "#3b82f6", "#60a5fa"),
    (60,  "HO60d",  "#06b6d4", "#67e8f9"),
    (90,  "HO90d",  "#10b981", "#6ee7b7"),
    (120, "HO120d", "#84cc16", "#bef264"),
    (150, "HO150d", "#f59e0b", "#fcd34d"),
    (180, "HO180d", "#ef4444", "#fca5a5"),
]

# ── TRADING_MODE を import 前に設定 ───────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

# ── CSV ヘルパー ──────────────────────────────────────────────────────────────
def _float(v, default=0.0) -> float:
    try:    return float(v)
    except: return default

def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))

def _find_csv(strategy: str, holdout_days: int, wf_dir: Path,
              mode: str = "conservative") -> tuple[Path | None, str]:
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    cands = sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_holdout{holdout_days}d_*.csv"), reverse=True)
    if cands:
        return cands[0], "holdout"
    fallback = [f for f in sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_*.csv"), reverse=True)
                if "holdout" not in f.name]
    if fallback:
        return fallback[0], "standard"
    return None, "none"

def _load_wl_from_csv(csv_path: Path, max_price: float, strategy: str,
                       per_strategy: int = 10, min_price: float = 0.0) -> list[tuple]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    filtered = [r for r in rows if (
        _float(r.get("total_test_pnl", 0)) > 0
        and _float(r.get("max_drawdown_pct", 999)) <= 15.0
        and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
        and (min_price <= 0 or _float(r.get("latest_price", 0)) >= min_price)
    )]
    filtered.sort(key=_composite_score, reverse=True)
    return [(r.get("symbol", ""), r.get("name", ""), strategy)
            for r in filtered[:per_strategy] if r.get("symbol")]

# ── PNL_CONFIGS 構築 ──────────────────────────────────────────────────────────
print("=" * 65)
print(f"run_signals_holdout_all: {TODAY}")
print("=" * 65)

wf_dir = _args.wf_dir
wf_dir.mkdir(exist_ok=True)

# period_days → list of config dicts
_period_configs: dict[int, list[dict]] = {d: [] for d in _PNL_PERIODS}

for holdout_days, ho_label, col_con, col_agg in HOLDOUT_CONFIGS:
    for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
        mode_short = "con" if mode == "conservative" else "agg"
        stop_wl: list[tuple] = []
        brk_wl:  list[tuple] = []
        has_data = False
        for strat in _STOP_STRATS:
            p, _ = _find_csv(strat, holdout_days, wf_dir, mode)
            if p:
                stop_wl.extend(_load_wl_from_csv(p, _args.max_price, strat, min_price=_args.min_price))
                has_data = True
        for strat in _BRK_STRATS:
            p, _ = _find_csv(strat, holdout_days, wf_dir, mode)
            if p:
                brk_wl.extend(_load_wl_from_csv(p, _args.max_price, strat, min_price=_args.min_price))
                has_data = True
        if stop_wl or brk_wl:   # 実際にアイテムがある場合のみ登録
            _period_configs[holdout_days].append({
                "label": f"{ho_label}/{mode_short}",
                "color": color,
                "mode":  mode,
                "sm_tm": None,
                "stop_wl": stop_wl,
                "brk_wl":  brk_wl,
            })

# フォールバック: CSV なし → 現行 WATCHLIST を全期間で共通使用
import check_signals_stop     as _stop
import check_signals_breakout as _brk
if _args.short:
    import check_signals_short          as _fb_stop_mod
    import check_signals_short_breakout as _fb_brk_mod
else:
    _fb_stop_mod = _stop
    _fb_brk_mod  = _brk

_using_fallback = all(len(v) == 0 for v in _period_configs.values())
if _using_fallback:
    print("[INFO] WFスキャンCSVなし → 現行WATCHLISTを全期間で使用")
    _fb_cfg = {
        "label":   "現行WL con",
        "color":   "#3b82f6",
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": list(_fb_stop_mod.WATCHLIST),
        "brk_wl":  list(_fb_brk_mod.WATCHLIST),
    }
    for days in _PNL_PERIODS:
        _period_configs[days] = [_fb_cfg]

# シグナルタブ用: 全設定を重複なしで結合
_seen_cfg_labels: set = set()
_all_configs: list[dict] = []
for cfgs in _period_configs.values():
    for cfg in cfgs:
        if cfg["label"] not in _seen_cfg_labels:
            _seen_cfg_labels.add(cfg["label"])
            _all_configs.append(cfg)

n_items_total = sum(len(c["stop_wl"]) + len(c["brk_wl"]) for c in _all_configs)
print(f"設定数: {len(_all_configs)}件 / アイテム合計: {n_items_total}件")

# ── holdout選定銘柄をエクスポート (デイトレ5分足の同日ショート検証用) ──────────
# 全設定の (銘柄 × 戦略) を重複なしで holdout_selected_symbols.py に出力。
# con/agg はエントリー(em=0)が同一なので (code, strat) で重複排除。
# デイトレ側 verify_sameday_short_intraday.py --symbols-file で読み込む。
try:
    _sel_seen: set = set()
    _sel: list[tuple] = []
    for _c in _all_configs:
        for _code, _name, _strat in list(_c["stop_wl"]) + list(_c["brk_wl"]):
            _k = (_code, _strat)
            if _code and _k not in _sel_seen:
                _sel_seen.add(_k)
                _sel.append((_code, _name, _strat))
    _sel_suffix = "_short" if _args.short else ""
    _sel_path = Path(f"holdout_selected_symbols{_sel_suffix}.py")
    _sel_lines = [
        '"""holdout_selected_symbols.py — run_signals_holdout_all が選定した',
        f'(銘柄, 名前, 戦略) の重複なし一覧。{TODAY} 自動生成。',
        'デイトレ5分足の同日ショート検証用:',
        '  verify_sameday_short_intraday.py --symbols-file holdout_selected_symbols.py',
        '"""',
        "SELECTED = [",
    ]
    for _code, _name, _strat in _sel:
        _sel_lines.append(f"    ({_code!r}, {_name!r}, {_strat!r}),")
    _sel_lines.append("]")
    _sel_path.write_text("\n".join(_sel_lines), encoding="utf-8")
    print(f"[export] holdout選定 {len(_sel)}ペア → {_sel_path}")
except Exception as _e:
    print(f"[export] holdout_selected_symbols 出力失敗: {_e}")

# ── ローリング逆指値: バックテストエンジンにモジュール定数を注入 ──────────────
import backtest_limit_entry as _bte
if _args.rolling > 0:
    _bte.ROLLING_ENTRY = _args.rolling
    print(f"[ROLLING] ローリング逆指値 有効: 最大{_args.rolling}回更新")

# ── nikkei_analysis をインポートして PNL_CONFIGS を注入 ───────────────────────
# nikkei_analysis は import 時に _stop/_brk を reload する。
# WATCHLIST が上書きされないよう、reload 後に元に戻す。
_orig_stop_wl = list(_stop.WATCHLIST)
_orig_brk_wl  = list(_brk.WATCHLIST)

import nikkei_analysis as _na

# reload で上書きされた WATCHLIST を元に戻す
_stop.WATCHLIST[:] = _orig_stop_wl
_brk.WATCHLIST[:]  = _orig_brk_wl

# nikkei_analysis にホールドアウト設定を注入
_na._SIGNALS_AVAILABLE = True
_na._PNL_CONFIGS[:] = _all_configs
# ショートモード: トレンド別成績テーブルの表示順・凡例を反転
_na._IS_SHORT_MODE = _args.short

# ── バックテストキャッシュ (5期間 × 同一銘柄の重複実行を防ぐ) ─────────────────
# 全設定統合(180)+6期間+シグナルタブで同一(銘柄,戦略,モード)が何度も呼ばれるため、
# 1プロセス内で結果をメモ化して重複計算を防ぐ。
# さらにディスクにも永続化し、当日内なら中断・再実行でも再計算しない。
import pickle as _pickle
import atexit as _atexit
import time as _time

_bt_cache: dict[tuple, dict | None] = {}

_bt_cache_dir  = Path(".holdout_bt_cache")
_bt_cache_dir.mkdir(exist_ok=True)
_bt_cache_file = _bt_cache_dir / f"bt{_cache_short}_{_cache_date}.pkl"
# BTキャッシュはキーが日付付き（bt_YYYY-MM-DD.pkl）のため同日内は常に有効。
# --force はHTML再生成のみを強制し、BTキャッシュは削除しない。
if _bt_cache_file.exists():
    try:
        with open(_bt_cache_file, "rb") as _bf:
            _bt_cache = _pickle.load(_bf)
        print(f"[BTキャッシュ] {len(_bt_cache)}件をディスクから復元")
    except Exception:
        _bt_cache = {}

_bt_cache_dirty = {"n": 0}

def _save_bt_cache():
    if _bt_cache_dirty["n"] == 0:
        return
    try:
        with open(_bt_cache_file, "wb") as _bf:
            _pickle.dump(_bt_cache, _bf, protocol=_pickle.HIGHEST_PROTOCOL)
        print(f"[BTキャッシュ] {len(_bt_cache)}件を保存 ({_bt_cache_file})")
    except Exception as _e:
        print(f"[BTキャッシュ] 保存失敗: {_e}")

# 中断(Ctrl-C)・正常終了どちらでも保存し、途中までの計算を次回再利用する
_atexit.register(_save_bt_cache)

def _make_cached_bt(orig_fn):
    _mod_globals = orig_fn.__globals__  # 対象モジュールのグローバル名前空間
    def wrapper(symbol, name, strategy, max_hold=None):
        mode = os.environ.get("TRADING_MODE", "conservative")
        mh_key = ""
        if max_hold is not None:
            mh_key = f"|mh{max_hold}"
            # sm/tm をキーに含めることで、mode切替後に古いキャッシュが返るのを防ぐ
            try:
                params = _mod_globals.get("STRATEGY_PARAMS", {}).get(strategy)
                if params and len(params) >= 4:
                    mh_key += f"|{params[2]:.2f}_{params[3]:.2f}"
            except Exception:
                pass
        key  = f"{symbol}|{strategy}|{mode}{_cache_settings}{mh_key}"
        if key not in _bt_cache:
            _bt_cache[key] = orig_fn(symbol, name, strategy, max_hold)
            _bt_cache_dirty["n"] += 1
            # 100件ごとに途中保存 (長時間実行の中断対策)
            if _bt_cache_dirty["n"] % 100 == 0:
                _save_bt_cache()
        return _bt_cache[key]
    return wrapper

# ロング側 (check_signals_stop / breakout)
_stop.backtest_one = _make_cached_bt(_stop.backtest_one)
_brk.backtest_one  = _make_cached_bt(_brk.backtest_one)

# ショート側 (check_signals_short / short_breakout)。
# nikkei_analysis の _mod_for() はショート戦略をこれらに振り分けるため、
# ここをラップしないとショート実行でキャッシュが全く効かず7〜8倍重くなる。
for _mod_attr in ("_short", "_sbrk"):
    _m = getattr(_na, _mod_attr, None)
    if _m is not None and hasattr(_m, "backtest_one"):
        _m.backtest_one = _make_cached_bt(_m.backtest_one)

# ── シグナルスコアキャッシュ読み込み ─────────────────────────────────────────
# 初回発信時のBTスコアを保存し、以後の実行でも同じスコアを表示する。
# キャッシュキー: "{symbol}::{strategy}::{signal_date}"
import json as _json

_score_cache_path = Path("signal_score_cache.json")
_score_cache: dict = {}
if _score_cache_path.exists():
    try:
        _score_cache = _json.loads(_score_cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass

# キャッシュから (sym, strat) → 最新signal_date & bt_score を取得
_cached_latest: dict[tuple, dict] = {}
for _ck, _cv in _score_cache.items():
    _parts = _ck.split("::")
    if len(_parts) == 3:
        _csym, _cstrat, _csigdate = _parts
        _existing = _cached_latest.get((_csym, _cstrat))
        if _existing is None or _csigdate > _existing["signal_date"]:
            _cached_latest[(_csym, _cstrat)] = {"signal_date": _csigdate,
                                                  "bt_score": _cv.get("bt_score", 0)}

# キャッシュにあるスコアを注入 (signal_date は後で検証)
_na._FROZEN_BT_SCORES.clear()
for (_csym, _cstrat), _info in _cached_latest.items():
    _na._FROZEN_BT_SCORES[(_csym, _cstrat)] = _info["bt_score"]

# シグナル発生時BTスコアを (sym, strat, signal_date_str) → score で注入
# P&Lタブで「シグナル発生時のBTスコア」を正確に表示するために使用
_na._SIGNAL_DATE_BT_SCORES.clear()
for _ck, _cv in _score_cache.items():
    _ck_parts = _ck.split("::")
    if len(_ck_parts) == 3:
        _csym2, _cstrat2, _csigdate2 = _ck_parts
        _na._SIGNAL_DATE_BT_SCORES[(_csym2, _cstrat2, _csigdate2)] = _cv.get("bt_score", 0)

# ── target_date 解決 ─────────────────────────────────────────────────────────
target_date = None
if _args.date:
    from datetime import date as _date_cls
    try:
        target_date = _date_cls.fromisoformat(_args.date)
    except ValueError:
        pass

date_str = _args.date or str(TODAY)

# ── 日経相場環境・トレンド・エントリー分析タブ ─────────────────────────────────
print("日経平均データ取得中 (市場分析)...", flush=True)
_market_tab1_html = _market_tab2_html = _market_tab3_html = ""
try:
    import pandas as _pd
    _na_years  = 5
    _na_end    = target_date if _args.date else TODAY  # 基準日でN225データを揃える
    _na_close  = _na.fetch_n225(_na_years, end_date=_na_end)
    _na_close  = _na_close[_na_close.index <= _pd.Timestamp(_na_end)]
    if not _na_close.empty:
        _na_trend     = _na.label_trend(_na_close)
        _na_r         = _na.get_regime(_na_close)
        _na_ref       = target_date or TODAY
        _na_periods   = _na.extract_periods(_na_close, _na_trend, _na_ref)
        _na_up_p      = _na.extract_up_periods(_na_close, _na_trend, _na_ref)
        _na_all_stats = {
            "up":   _na.calc_stats([p for p in _na_periods if p["trend"] == "up"]),
            "down": _na.calc_stats([p for p in _na_periods if p["trend"] == "down"]),
        }
        print("参考指標取得中...", flush=True)
        _na_indicators = _na.fetch_market_indicators(years=1, end_date=_na_end)
        import traceback as _tb_mkt
        # タブごとに個別 try: 1つ失敗しても他のタブを生かす + 例外を必ず表示
        try:
            _market_tab1_html = _na._tab1_signal_html(
                _na_r, _na_ref, indicators=_na_indicators, periods=_na_periods)
        except Exception as _e1:
            print(f"[WARN] 相場環境タブ失敗: {_e1}\n{_tb_mkt.format_exc()}", flush=True)
        try:
            _market_tab2_html = _na._tab2_trend_html(
                _na_close, _na_trend, _na_periods, _na_years)
        except Exception as _e2t:
            print(f"[WARN] トレンド期間タブ失敗: {_e2t}\n{_tb_mkt.format_exc()}", flush=True)
        try:
            _market_tab3_html = _na._tab3_timing_html(
                _na_close, _na_up_p, _na_all_stats)
        except Exception as _e3t:
            print(f"[WARN] エントリー分析タブ失敗: {_e3t}\n{_tb_mkt.format_exc()}", flush=True)
        print("市場分析タブ生成完了", flush=True)
except Exception as _me:
    import traceback as _tb_mkt2
    print(f"[WARN] 市場分析スキップ: {_me}\n{_tb_mkt2.format_exc()}", flush=True)

if _market_tab1_html:
    _market_tab_btns = (
        "\n  <button class=\"ho-outer-btn\" onclick=\"switchHoTab('mkt1')\">&#x1F4CA; 相場環境</button>"
        "\n  <button class=\"ho-outer-btn\" onclick=\"switchHoTab('mkt2')\">&#x1F4C8; トレンド期間</button>"
        "\n  <button class=\"ho-outer-btn\" onclick=\"switchHoTab('mkt3')\">&#x23F1; エントリー分析</button>"
    )
else:
    _market_tab_btns = ""

# ── 日経バナーは銘柄に依存しないので先に取得 ─────────────────────────────────
try:
    from signal_risk_check import (
        precompute_all       as _precompute_risks,
        render_nikkei_banner as _render_nikkei_banner,
    )
    _nikkei_banner = _render_nikkei_banner()
except Exception as _re:
    print(f"[WARN] リスクチェックスキップ: {_re}", flush=True)
    _precompute_risks = None
    _nikkei_banner = ""

# ── 工程タイミング計測 ────────────────────────────────────────────────────────
_T0 = _time.time()
def _phase(msg: str):
    print(f"  [⏱ {_time.time() - _T0:6.1f}s] {msg}", flush=True)

# ── シグナルタブ HTML (パス1: バッジなし) ─────────────────────────────────────
print("シグナル収集中...", flush=True)
_na._PNL_CONFIGS[:] = _all_configs
_sig_html = _na._tab4_signals_html(
    workers=_args.workers,
    min_score=_args.min_score,
    target_date=target_date,
)
_phase("シグナルタブ完了")

# ── キャッシュ更新・signal_date 検証 ─────────────────────────────────────────
# 新規シグナル保存 & signal_dateが変わった銘柄のスコアを更新
_needs_regen = False
for _sig in _na._last_signals:
    _ssym   = _sig.get("symbol", "")
    _sstrat = _sig.get("strategy", "")
    _ssigdt = str(_sig.get("signal_date", ""))
    _skey   = f"{_ssym}::{_sstrat}::{_ssigdt}"
    _cached = _cached_latest.get((_ssym, _sstrat))

    if _skey not in _score_cache:
        # 新規シグナル: 現在のBTスコアで保存
        _real_bt = _sig.get("rec_score", 0)
        _score_cache[_skey] = {"bt_score": _real_bt, "first_seen": str(TODAY)}
        if _cached and _cached["signal_date"] != _ssigdt:
            # signal_dateが変わった → 古い凍結スコアを使っているので再生成が必要
            _na._FROZEN_BT_SCORES[(_ssym, _sstrat)] = _real_bt
            _needs_regen = True

# signal_dateが変わった銘柄があれば HTML を再生成 (スコア更新のみ、まだバッジなし)
if _needs_regen:
    print("シグナル再生成中 (signal_date更新あり)...", flush=True)
    _sig_html = _na._tab4_signals_html(
        workers=_args.workers,
        min_score=_args.min_score,
        target_date=target_date,
    )

# ── リスク警告・決算日: シグナルが出た銘柄のみ事前計算 ────────────────────────
# _last_signals はここで確定しているので、シグナル銘柄のみに絞り込む
_sig_sym_map: dict[str, str] = {}
for _sig in _na._last_signals:
    _s = _sig.get("symbol", "")
    _n = _sig.get("name", "")
    if _s and _s not in _sig_sym_map:
        _sig_sym_map[_s] = _n

if _precompute_risks and _sig_sym_map:
    try:
        _precompute_risks(
            list(_sig_sym_map.items()),
            workers=_args.workers,
            target_date=target_date,
        )
        # バッジ付きで再生成
        _sig_html = _na._tab4_signals_html(
            workers=_args.workers,
            min_score=_args.min_score,
            target_date=target_date,
        )
    except Exception as _re2:
        print(f"[WARN] リスクチェック失敗: {_re2}", flush=True)
elif not _sig_sym_map:
    print("[INFO] シグナルなし — リスクチェックスキップ", flush=True)

# キャッシュ保存
try:
    _score_cache_path.write_text(
        _json.dumps(_score_cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[INFO] シグナルスコアキャッシュ: {len(_score_cache)}件保存", flush=True)
except Exception as _e:
    print(f"[WARN] キャッシュ保存失敗: {_e}", flush=True)

# ── シグナルを JSON へエクスポート (position_server の /signals が読む) ─────────
# ロング → signals_latest.json / ショート → signals_latest_short.json
# position_server.py からワンクリック登録できるよう、確定シグナルを永続化する。
try:
    _sig_export = []
    for _s in _na._last_signals:
        try:
            _qty = _na._calc_qty(_s.get("order_p", 0), _s.get("stop_p", 0)) \
                   if _s.get("order_p") else 100
        except Exception:
            _qty = 100
        _sig_export.append({
            "symbol":      _s.get("symbol", ""),
            "name":        _s.get("name", ""),
            "strategy":    _s.get("strategy", ""),
            "order_p":     _s.get("order_p", 0),
            "stop_p":      _s.get("stop_p", 0),
            "target_p":    _s.get("target_p", 0),
            "signal_date": str(_s.get("signal_date", "")),
            "score":       _s.get("score", 0),
            "rank":        _s.get("rank", ""),
            "qty":         _qty,
        })
    _sig_out = Path("signals_latest_short.json" if _args.short else "signals_latest.json")

    # 同日に複数の価格レンジで実行した場合はマージ（symbol+strategy でデdup）
    _existing: list[dict] = []
    if _sig_out.exists():
        try:
            _prev = _json.loads(_sig_out.read_text(encoding="utf-8"))
            if str(_prev.get("signal_date", "")) == str(_cache_date):
                _existing = _prev.get("signals", [])
        except Exception:
            pass
    # 既存シグナルに今回の分を追加（symbol+strategy が重複しないよう優先: 今回分）
    _existing_keys = {(s["symbol"], s["strategy"]) for s in _sig_export}
    _merged = _sig_export + [s for s in _existing
                              if (s["symbol"], s["strategy"]) not in _existing_keys]
    _merged.sort(key=lambda s: s.get("score", 0), reverse=True)

    _sig_payload = _json.dumps({
        "generated_at": str(TODAY),
        "signal_date":  _cache_date,
        "mode":         "short" if _args.short else "long",
        "signals":      _merged,
    }, ensure_ascii=False, indent=2)
    _sig_out.write_text(_sig_payload, encoding="utf-8")
    # 日付付きバックアップも保存（翌日の約定登録時に参照できるよう）
    _sig_dated = Path(f"signals_{_cache_date}{'_short' if _args.short else ''}.json")
    _sig_dated.write_text(_sig_payload, encoding="utf-8")
    print(f"[INFO] シグナルJSON書き出し: {_sig_out.name} + {_sig_dated.name} ({len(_merged)}件, うち今回{len(_sig_export)}件)", flush=True)
except Exception as _e:
    print(f"[WARN] シグナルJSON書き出し失敗: {_e}", flush=True)

# ── 損益タブ HTML: 全設定統合 (_DEFAULT_DAYS) + 期間別 ──────────────────────
# 全設定統合: _all_configs でデフォルト期間を一括集計 → デフォルト表示
_na._PNL_CONFIGS[:] = _all_configs
print(f"損益集計中 (全設定統合・直近{_DEFAULT_DAYS}日 / {len(_all_configs)}設定)...", flush=True)
_all_period_html = _na._tab5_pnl_html(_DEFAULT_DAYS, _args.workers, entry_days=_args.entry_days, skip_timing9=True)
# ⑭寄り確認用に、この全設定統合ペインの取引セット(=損益タブの取引明細と同一)を退避
_oc_report_trades = list(getattr(_na, "_LAST_KPI_TRADES", []) or [])
_phase(f"損益タブ({_DEFAULT_DAYS}日/全設定統合)完了")

# トレンド期間タブの一覧表に損益列を追加（損益計算で得たトレードを使って再生成）
if _market_tab2_html:
    try:
        _mkt_trades = getattr(_na, "_LAST_KPI_TRADES", None)
        if _mkt_trades:
            _market_tab2_html = _na._tab2_trend_html(
                _na_close, _na_trend, _na_periods, _na_years, trades=_mkt_trades)
            print("トレンド期間タブに損益列を追加", flush=True)
    except Exception as _e2:
        print(f"[WARN] トレンド期間タブ損益列スキップ: {_e2}", flush=True)

# ── 最大保有日数比較セクション（⑪⑫タブ）日付跨ぎキャッシュ付き ──────────────────
# 保有期間比較・押し目買い比較は長期窓の構造分析で日々ほとんど変わらないため、
# 日付が変わっても最新の既存キャッシュを再利用する (再計算しない)。
# 強制再計算は --recalc-analysis のみ。--force(シグナル/HTML更新)では再計算しない。
import pickle as _mhpk
from pathlib import Path as _MHP
_mh_cache_dir  = _MHP(".holdout_bt_cache")
_mh_cache_dir.mkdir(exist_ok=True)
# short/設定ごとに分けるため suffix (_cache_short = _short + 設定sig) を含める
_ANALYSIS_STALE_DAYS = 30   # これより古い場合のみ自動リフレッシュ

def _latest_analysis_cache(prefix: str):
    """prefix にマッチする最新キャッシュ(日付跨ぎ)を返す。
    無い / _ANALYSIS_STALE_DAYS より古い / --recalc-analysis なら None。"""
    if getattr(_args, "recalc_analysis", False):
        return None
    # 日付(YYYY...)始まりのみに限定。prefix="openconfirm" が
    # "openconfirm_short_*" を誤って拾う(ロングがショートのキャッシュを読む)のを防ぐ。
    cands = sorted(_mh_cache_dir.glob(f"{prefix}_[0-9]*.pkl"), reverse=True)
    if not cands:
        return None
    newest = cands[0]
    # ファイル名末尾の YYYY-MM-DD を取り出して鮮度判定
    try:
        _dstr = newest.stem.rsplit("_", 1)[-1]
        _fdate = datetime.strptime(_dstr, "%Y-%m-%d").date()
        if (TODAY - _fdate).days > _ANALYSIS_STALE_DAYS:
            print(f"[{prefix}] 最新キャッシュ {newest.name} が"
                  f"{_ANALYSIS_STALE_DAYS}日超で古いため再計算", flush=True)
            return None
    except Exception:
        pass
    return newest

_mh_prefix     = f"maxhold_cmp{_cache_short}"
_mh_cache_file = _mh_cache_dir / f"{_mh_prefix}_{TODAY}.pkl"
# --force 時はキャッシュを使わず再計算する(コード修正=HTML構造の変更を反映するため)。
# --no-force なら日付跨ぎでpklを再利用して高速化。
_mh_reuse      = None if _args.force else _latest_analysis_cache(_mh_prefix)

if _mh_reuse is not None:
    try:
        _mh_cached = _mhpk.loads(_mh_reuse.read_bytes())
        _mh_html     = _mh_cached.get("conservative", "")
        _mh_cmp_html = _mh_cached.get("con_agg", "")
        print(f"[最大保有日数比較] キャッシュ再利用(日付跨ぎ可・--no-force): {_mh_reuse.name}", flush=True)
    except Exception:
        _mh_html = _mh_cmp_html = None
else:
    _mh_html = _mh_cmp_html = None

if _mh_html is None:
    _hold_list = [7, 10, 15, 20]
    _na._PNL_CONFIGS[:] = _all_configs
    print(f"最大保有日数比較中 ({_hold_list}, conservative)...", flush=True)
    _mh_html = _na.build_max_hold_comparison_html(_hold_list, _DEFAULT_DAYS, _args.workers, compare_modes=False) or ""
    print(f"最大保有日数比較中 ({_hold_list}, con+agg)...", flush=True)
    _mh_cmp_html = _na.build_max_hold_comparison_html(_hold_list, _DEFAULT_DAYS, _args.workers, compare_modes=True) or ""
    try:
        _mh_cache_file.write_bytes(_mhpk.dumps({"conservative": _mh_html, "con_agg": _mh_cmp_html}))
        print(f"[最大保有日数比較] キャッシュ保存: {_mh_cache_file.name}", flush=True)
    except Exception as _mhe:
        print(f"[最大保有日数比較] キャッシュ保存失敗: {_mhe}", flush=True)

if _mh_html:
    _all_period_html = _all_period_html.replace("<!-- MAXHOLD_SLOT -->", _mh_html, 1)
if _mh_cmp_html:
    _all_period_html = _all_period_html.replace("<!-- MAXHOLD_CMP_SLOT -->", _mh_cmp_html, 1)
_phase("最大保有日数比較完了")

# ── ⑬ 押し目指値買い vs 逆指値ブレイク買い 比較 (詳細分析タブ) ──────────────────
# 「約定後すぐ押す」を活かせるか= 押し目を指値で安く買う方が良いかを本レポートの
# 選定銘柄(=_all_configs)の実トレードで比較。BT70以上のフィルタ表も併記。
# ショートモードでは押し目買いの概念が無いためスキップ。
if not _args.short:
    _na._PNL_CONFIGS[:] = _all_configs
    _pb_list = [0.3, 0.5, 1.0]
    _pb_prefix = f"pullback_cmp{_cache_short}"
    _pb_cache_file = _mh_cache_dir / f"{_pb_prefix}_{TODAY}.pkl"
    _pb_html = None
    # 日付跨ぎで最新キャッシュを再利用 (再計算は --recalc-analysis のみ)
    _pb_reuse = _latest_analysis_cache(_pb_prefix)
    if _pb_reuse is not None:
        try:
            _pb_html = _mhpk.loads(_pb_reuse.read_bytes()).get("html", "")
            print(f"[押し目買い比較] キャッシュ再利用(日付跨ぎ可): {_pb_reuse.name}", flush=True)
        except Exception:
            _pb_html = None
    if _pb_html is None:
        print(f"押し目買い比較中 ({_pb_list}, conservative)...", flush=True)
        try:
            _pb_html = _na.build_pullback_comparison_html(
                _pb_list, _DEFAULT_DAYS, _args.workers) or ""
        except Exception as _pbe:
            print(f"[押し目買い比較] 失敗: {_pbe}", flush=True)
            _pb_html = ""
        if _pb_html:
            try:
                _pb_cache_file.write_bytes(_mhpk.dumps({"html": _pb_html}))
                print(f"[押し目買い比較] キャッシュ保存: {_pb_cache_file.name}", flush=True)
            except Exception:
                pass
    if _pb_html:
        _all_period_html = _all_period_html.replace(
            "<!-- PULLBACK_CMP_SLOT -->", _pb_html, 1)
    _phase("押し目買い比較完了")

# ── ⑭ 寄り後確認エントリー検証 (詳細分析タブ) 日付跨ぎキャッシュ ─────────────────
# 5分足が必要な重い分析なので、構造分析扱いで日付跨ぎ再利用 (--recalc-analysis で再計算)。
# ショートはロング専用のため即ノート表示。
# データ源(yfinance / pkl何個)をキャッシュキーに含める → pkl検出時に自動で再計算
# (yfinance時の古い結果を使い回さない)。
import analyze_open_confirm_entry as _oc
try:
    _oc_dirs = [] if _args.short else _oc.locate_5m_dirs()
except Exception:
    _oc_dirs = []
_oc_src_tok = (f"pkl{len(_oc_dirs)}" if _oc_dirs
               else ("short" if _args.short else "yf"))
_oc_prefix    = f"openconfirmv10_{_oc_src_tok}{_cache_short}"   # v10: ③フェア版を直近60日に限定
_oc_cache_file = _mh_cache_dir / f"{_oc_prefix}_{TODAY}.pkl"
# 本日分キャッシュがあれば再利用(=同日2回目以降スキップ)。無ければ再計算
# (=5分足自動更新後の最新データを反映)。翌日は自動的に作り直す。
_oc_html = None
if _oc_cache_file.exists():
    try:
        _oc_html = _mhpk.loads(_oc_cache_file.read_bytes()).get("html", "")
        print(f"[寄り確認] 本日分キャッシュ再利用(2回目以降スキップ): {_oc_cache_file.name}", flush=True)
    except Exception:
        _oc_html = None
# キャッシュが無い場合のみ計算(=一度計算したら以降スキップ)。①②寄り確認 + ③フェア版。
if _oc_html is None:
    print(f"寄り確認+フェア版 検証中 (初回のみ・5分足源: {_oc_src_tok})...", flush=True)
    try:
        _oc_html = _oc.build_html(minute_dir=_args.minute_dir,
                                  is_short=_args.short,
                                  trades=None if _args.short else _oc_report_trades) or ""
    except Exception as _oce:
        import traceback as _octb
        print(f"[寄り確認] 失敗: {_oce}\n{_octb.format_exc()}", flush=True)
        _oc_html = ""
    # ③ フェア版(後知恵なし・全シグナル)を追記
    try:
        import analyze_fair_market_entry as _fair
        _oc_html += _fair.build_fair_html(is_short=_args.short,
                                          workers=_args.workers,
                                          minute_dir=_args.minute_dir) or ""
    except Exception as _fe:
        import traceback as _fetb
        print(f"[フェア版] 失敗: {_fe}\n{_fetb.format_exc()}", flush=True)
    if _oc_html:
        try:
            _oc_cache_file.write_bytes(_mhpk.dumps({"html": _oc_html}))
            print(f"[寄り確認] キャッシュ保存(以降スキップ): {_oc_cache_file.name}", flush=True)
        except Exception:
            pass
if _oc_html:
    _all_period_html = _all_period_html.replace(
        "<!-- OPENCONFIRM_SLOT -->", _oc_html, 1)
_phase("寄り確認検証完了")

# ── ⑮ 逆指値の約定率・約定タイミング (詳細分析タブ) 日付跨ぎキャッシュ ─────────────
# em=0で約定挙動はcon/agg共通・5分足不要の軽いバックテスト。日付跨ぎ再利用。
_ft_prefix    = f"filltimingv2{_cache_short}"   # v2: BTフィルタ追加
_ft_reuse     = _latest_analysis_cache(_ft_prefix)
_ft_html = None
if _ft_reuse is not None:
    try:
        _ft_html = _mhpk.loads(_ft_reuse.read_bytes()).get("html", "")
        print(f"[約定タイミング] キャッシュ再利用(日付跨ぎ可): {_ft_reuse.name}", flush=True)
    except Exception:
        _ft_html = None
if _ft_html is None:
    print("約定タイミング集計中 (初回のみ・以降スキップ)...", flush=True)
    try:
        import analyze_fill_timing as _ft
        _ft_html = _ft.build_html(is_short=_args.short, workers=_args.workers) or ""
    except Exception as _fte:
        import traceback as _fttb
        print(f"[約定タイミング] 失敗: {_fte}\n{_fttb.format_exc()}", flush=True)
        _ft_html = ""
    if _ft_html:
        try:
            (_mh_cache_dir / f"{_ft_prefix}_{TODAY}.pkl").write_bytes(
                _mhpk.dumps({"html": _ft_html}))
        except Exception:
            pass
if _ft_html:
    _all_period_html = _all_period_html.replace(
        "<!-- FILLTIMING_SLOT -->", _ft_html, 1)
_phase("約定タイミング集計完了")

# 期間別: 各期間のconfigs（⑨Rolling/em比較はスキップして高速化）
# preoos_cutoff_days=days を渡してOOS前BTスコア別成績タブを追加
_period_pane_htmls: dict[int, str] = {}
for days in _PNL_PERIODS:
    cfgs = _period_configs.get(days) or _all_configs
    _na._PNL_CONFIGS[:] = cfgs
    print(f"損益集計中 (直近{days}日 / {len(cfgs)}設定)...", flush=True)
    _skip_preoos = getattr(_args, "no_preoos", False)
    _period_pane_htmls[days] = _na._tab5_pnl_html(
        days, _args.workers, entry_days=_args.entry_days, skip_timing9=True,
        preoos_cutoff_days=None if _skip_preoos else days)
    _phase(f"損益タブ({days}日)完了")

# ── 銘柄詳細タブ HTML (シグナル銘柄ごと) ──────────────────────────────────────
# _last_signals はシグナルタブ生成時に _na 側で設定される
_signal_stocks: list[tuple] = []
_seen_sym: set = set()
# 銘柄 → 本日シグナルが出た戦略の集合 (銘柄詳細タブを「チップのBT=本日の戦略」に絞る)
_sym_today_strats: dict[str, list[str]] = {}
for _sig in _na._last_signals:
    _s = _sig.get("symbol", "")
    if not _s:
        continue
    _st = _sig.get("strategy", "")
    if _st:
        _lst = _sym_today_strats.setdefault(_s, [])
        if _st not in _lst:
            _lst.append(_st)
    if _s not in _seen_sym:
        _seen_sym.add(_s)
        _signal_stocks.append((_s, _sig.get("name", ""), _sig.get("rec_score") or 0))

_sym_tab_nav   = ""
_sym_tab_panes = ""
for _i, (_sym, _sname, _bt) in enumerate(_signal_stocks):
    _tid     = f"sym_{_sym.replace('.','_')}"
    _active  = "active" if _i == 0 else ""
    _display = "block"  if _i == 0 else "none"
    _short   = _sname[:8] if len(_sname) > 8 else _sname
    _sym_tab_nav += (
        f'<button class="sym-tab-btn {_active}" onclick="switchSymTab(\'{_tid}\')">'
        f'<span style="font-size:0.8rem;font-weight:700">{_sym}</span>'
        f'<br><span style="font-size:0.68rem;color:#94a3b8">{_short}</span>'
        f'<br><span style="font-size:0.7rem;color:#fbbf24">BT:{_bt}</span>'
        f'</button>\n'
    )
    _na._PNL_CONFIGS[:] = _all_configs
    # 本日シグナルが出た戦略(=チップのBT)だけに絞る。取引明細・KPIとも一致させる。
    _today_strats = _sym_today_strats.get(_sym) or None
    print(f"銘柄詳細生成中: {_sym} {_sname} (戦略={_today_strats or 'all'})...", flush=True)
    _sym_pnl = _na._tab5_pnl_html(365, _args.workers, symbol_filter=[_sym],
                                  strategy_filter=_today_strats, skip_timing9=True)
    _sym_tab_panes += (
        f'<div id="{_tid}" class="sym-tab-pane" style="display:{_display}">'
        f'{_sym_pnl}</div>\n'
    )

# 後片付け
_na._PNL_CONFIGS[:] = _all_configs

# ── ニュースモデル スコアテーブル HTML ────────────────────────────────────────
# news_model.json が存在する場合のみ、シグナル銘柄のニューススコアを表示する
_news_score_table_html = ""
if _signal_stocks:
    try:
        _model_path = Path("news_model.json")
        if _model_path.exists():
            print("ニュースモデル スコア計算中...", flush=True)
            from fetch_signal_news import load_and_apply_model as _lam
            from datetime import date as _date_cls
            _today_date = _date_cls.fromisoformat(str(TODAY))
            _ns_rows = ""
            for _ns_sym, _ns_name, _ns_bt in _signal_stocks:
                try:
                    _ns_result = _lam(_ns_sym, _ns_name, _today_date, _ns_bt, skip_news=False)
                    _ns_sent   = _ns_result.get("news_sentiment", 0.0)
                    _ns_cnt    = _ns_result.get("news_count", 0)
                    _ns_pred   = _ns_result.get("predicted_win_prob", 0.5)
                    _ns_score  = _ns_result.get("news_score", 0.0)
                    # 感情スコアの色
                    _sent_clr  = "#4ade80" if _ns_sent > 0.1 else "#f87171" if _ns_sent < -0.1 else "#94a3b8"
                    # 予測勝率の色
                    _pred_clr  = "#4ade80" if _ns_pred >= 0.65 else "#facc15" if _ns_pred >= 0.50 else "#f87171"
                    # BTスコアの色
                    _bt_clr    = "#4ade80" if _ns_bt >= 60 else "#facc15" if _ns_bt >= 40 else "#f87171"
                    _ns_rows += (
                        f'<tr>'
                        f'<td><strong>{_ns_sym}</strong></td>'
                        f'<td style="color:#cbd5e1">{_ns_name}</td>'
                        f'<td style="color:{_bt_clr};font-weight:700">{_ns_bt}</td>'
                        f'<td style="color:{_sent_clr};font-weight:700">{_ns_sent:+.2f}</td>'
                        f'<td style="color:#94a3b8">{_ns_cnt}</td>'
                        f'<td style="color:{_pred_clr};font-weight:700">{_ns_pred*100:.1f}%</td>'
                        f'</tr>\n'
                    )
                except Exception as _ns_e:
                    _ns_rows += (
                        f'<tr>'
                        f'<td><strong>{_ns_sym}</strong></td>'
                        f'<td>{_ns_name}</td>'
                        f'<td>{_ns_bt}</td>'
                        f'<td colspan="3" style="color:#64748b">スコア取得失敗: {_ns_e}</td>'
                        f'</tr>\n'
                    )
            _news_score_table_html = f"""
<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:16px;margin:0 0 20px">
  <h3 style="color:#93c5fd;font-size:0.95rem;margin:0 0 10px">
    ニュースモデル スコア付きシグナル
    <span style="font-size:0.72rem;color:#64748b;font-weight:normal;margin-left:8px">
      (news_model.json から予測)
    </span>
  </h3>
  <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
    <thead>
      <tr style="border-bottom:1px solid #334155">
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">コード</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">銘柄名</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">BTスコア</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">ニュース感情</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">記事数</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">予測勝率</th>
      </tr>
    </thead>
    <tbody>{_ns_rows}</tbody>
  </table>
  <p style="color:#64748b;font-size:0.72rem;margin-top:8px">
    予測勝率: モデル訓練済み (news_model.json) のロジスティック回帰による。
    緑≥65%, 黄≥50%, 赤&lt;50%
  </p>
</div>"""
            print(f"ニュースモデルスコア: {len(_signal_stocks)}銘柄 完了", flush=True)
    except Exception as _nst_e:
        print(f"[WARN] ニュースモデルスコア取得失敗: {_nst_e}", flush=True)

# 日経バナー + ニュースモデルスコアをシグナルHTMLの先頭に追加
if _nikkei_banner or _news_score_table_html:
    _sig_html = _nikkei_banner + _news_score_table_html + _sig_html

# ── ニュース・情報タブ HTML ────────────────────────────────────────────────────
try:
    from fetch_signal_news import build_news_html as _build_news_html
    _news_html = _build_news_html(_signal_stocks, workers=_args.workers)
    _news_tab_ok = True
except Exception as _e:
    print(f"[WARN] ニュース取得スキップ: {_e}", flush=True)
    _news_html  = f'<p style="color:#ef4444;padding:24px">ニュース取得エラー: {_e}</p>'
    _news_tab_ok = False

# ── --symbol 指定時: 銘柄別期間別取引詳細タブ ────────────────────────────────
_sym_detail_tab_btn  = ""
_sym_detail_tab_pane = ""

if _args.symbol:
    _sym_arg = _args.symbol.upper()
    if not _sym_arg.endswith(".T"):
        _sym_arg += ".T"

    _sp_btns  = ""
    _sp_panes = ""
    print(f"指定銘柄 {_sym_arg} の期間別取引詳細生成中...", flush=True)
    for days in _PNL_PERIODS:
        cfgs = _period_configs.get(days) or _all_configs
        _na._PNL_CONFIGS[:] = cfgs
        active  = "active" if days == _DEFAULT_DAYS else ""
        display = "block"  if days == _DEFAULT_DAYS else "none"
        _sp_btns += (
            f'<button class="sp-period-btn {active}" '
            f'onclick="switchSpPeriod({days})">{days}日</button>\n'
        )
        print(f"  直近{days}日...", flush=True)
        _sp_html = _na._tab5_pnl_html(days, _args.workers, symbol_filter=[_sym_arg], skip_timing9=True)
        _sp_panes += (
            f'<div id="sp{days}" class="sp-period-pane" style="display:{display}">'
            f'{_sp_html}</div>\n'
        )

    _na._PNL_CONFIGS[:] = _all_configs

    _sym_detail_tab_btn = (
        f'\n  <button class="ho-outer-btn" onclick="switchHoTab(\'sym_detail\')">'
        f'📌 {_sym_arg}</button>'
    )
    _sym_detail_tab_pane = f"""
<div id="ho-sym_detail" class="ho-outer-pane">
  <p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 12px">
    <strong style="color:#e2e8f0">{_sym_arg}</strong> の期間別取引詳細
  </p>
  <div style="margin:0 0 16px">
    <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
    {_sp_btns}
  </div>
  {_sp_panes}
</div>"""

# ── 期間セレクターのHTML部品 ──────────────────────────────────────────────────
# デフォルトは「全設定」ボタン（全10設定の合算）
_period_btns = (
    '<button class="ho-period-btn active" data-days="all" '
    "onclick=\"switchHoPeriod('all')\">全設定 (180日)</button>\n"
)
_period_panes = (
    f'<div id="hdall" class="ho-period-pane" style="display:block">'
    f'{_all_period_html}</div>\n'
)
for days in _PNL_PERIODS:
    _period_btns += (
        f'<button class="ho-period-btn" '
        f'data-days="{days}" onclick="switchHoPeriod({days})">{days}日</button>\n'
    )
    _period_panes += (
        f'<div id="hd{days}" class="ho-period-pane" style="display:none">'
        f'{_period_pane_htmls[days]}</div>\n'
    )

# ── WF歴史検証タブ（常時表示・複数基準日自動生成）────────────────────────────
from datetime import date as _date_cls, timedelta as _td_wfh

# 最新基準日: --wf-until 指定 or 自動（6ヶ月前）
if _args.wf_until:
    try:
        _wfh_latest = _date_cls.fromisoformat(_args.wf_until)
    except ValueError:
        print(f"[WARN] --wf-until の日付形式が不正: {_args.wf_until} → 6ヶ月前を使用")
        _wfh_latest = _date_cls.today() - _td_wfh(days=183)
else:
    _wfh_latest = _date_cls.today() - _td_wfh(days=183)

# 6ヶ月間隔で N 期間さかのぼる
_skip_wf_history = getattr(_args, "no_wf_history", False) or getattr(_args, "wf_periods", 4) == 0
_wfh_periods = max(1, getattr(_args, "wf_periods", 4))
_wfh_dates = []
_d = _wfh_latest
for _ in range(_wfh_periods):
    _wfh_dates.insert(0, _d)
    _d = _d - _td_wfh(days=183)

_wfh_max_price = getattr(_args, "max_price", 0.0) or 0.0
# --price-ranges 指定時はその最大値を WF検証の上限に使う
if getattr(_args, "price_ranges", None):
    _pr_vals = [float(x.strip()) for x in _args.price_ranges.split(",") if x.strip()]
    if _pr_vals:
        _wfh_max_price = max(_pr_vals)
_wfh_min_price = getattr(_args, "min_price", 0.0) or 0.0
_wfh_universe  = getattr(_args, "wf_universe", None)

if _skip_wf_history:
    print("\nWF歴史検証: スキップ (--no-wf-history または --wf-periods 0)", flush=True)
    _wfh_tab_btn  = ""
    _wfh_tab_pane = ""
else:
    print(f"\nWF歴史検証（クロス期間）: {[str(d) for d in _wfh_dates]}", flush=True)
    try:
        _wfh_body = _na._wf_multi_history_html(
            _wfh_dates, workers=_args.workers,
            max_price=_wfh_max_price,
            min_price=_wfh_min_price,
            universe_path=_wfh_universe,
            force=bool(_args.force),
        )
    except Exception as _wfh_e:
        print(f"[WARN] WF歴史検証エラー: {_wfh_e}")
        import traceback; traceback.print_exc()
        _wfh_body = f'<p style="color:#f87171;padding:20px">WF歴史検証エラー: {_wfh_e}</p>'
    _wfh_tab_btn  = '\n  <button class="ho-outer-btn" onclick="switchHoTab(\'wfh\')">📊 WF歴史検証</button>'
    _wfh_tab_pane = f'\n<div id="ho-wfh" class="ho-outer-pane">\n{_wfh_body}\n</div>'
    print("WF歴史検証タブ生成完了", flush=True)

# ── OOS検証タブ (--oos-until 指定時のみ) ─────────────────────────────────────
_oos_tab_btn  = ""
_oos_tab_pane = ""
if _args.oos_until:
    from datetime import date as _date_cls
    try:
        _oos_until_date = _date_cls.fromisoformat(_args.oos_until)
    except ValueError:
        print(f"[ERROR] --oos-until の日付形式が不正です: {_args.oos_until} (YYYY-MM-DD形式で指定してください)")
        _oos_until_date = None

    if _oos_until_date is not None:
        _oos_since = _oos_until_date - timedelta(days=_args.oos_days)
        print(f"\nOOS検証開始: 訓練前データ {_oos_since} 〜 {_oos_until_date} ({_args.oos_days}日間)")
        _na._PNL_CONFIGS[:] = _all_configs
        try:
            _oos_html_body = _na._oos_pnl_html(_oos_until_date, _args.oos_days, _args.workers)
        except Exception as _oos_e:
            print(f"[WARN] OOS検証エラー: {_oos_e}")
            _oos_html_body = f'<p style="color:#f87171;padding:20px">OOS検証エラー: {_oos_e}</p>'

        _oos_tab_btn = '\n  <button class="ho-outer-btn" onclick="switchHoTab(\'oos\')">🔬 OOS検証</button>'
        _oos_tab_pane = f"""
<div id="ho-oos" class="ho-outer-pane">
  <p style="color:#64748b;font-size:0.82rem;margin:8px 0 16px">
    <strong style="color:#38bdf8">OOS検証（訓練前データ）</strong>
    &nbsp;|&nbsp; 期間: {_oos_since} 〜 {_oos_until_date}（{_args.oos_days}日間）
    &nbsp;|&nbsp; WF訓練開始日より前の純粋OOSデータで検証
  </p>
  {_oos_html_body}
</div>"""
        print("OOS検証タブ生成完了")

# ── フル HTML ─────────────────────────────────────────────────────────────────
_extra_css = """
/* run_signals_holdout_all: outer tab overrides */
.ho-outer-nav {
  display:flex; gap:0; margin:16px 0 0;
  border-bottom:2px solid #1e293b; padding-bottom:0;
}
.ho-outer-btn {
  padding:9px 22px; background:#1e293b; border:none;
  border-radius:6px 6px 0 0; color:#94a3b8;
  cursor:pointer; font-size:0.9rem; transition:all .15s;
  border-bottom:2px solid transparent; margin-bottom:-2px;
}
.ho-outer-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.ho-outer-btn.active { background:#0f172a; color:#60a5fa;
  border-bottom:2px solid #60a5fa; font-weight:700; }
.ho-outer-pane { display:none; padding:12px 0; }
.ho-outer-pane.active { display:block; }

/* 期間セレクター */
.ho-period-btn {
  background:#1e293b; border:1px solid #334155; color:#94a3b8;
  padding:5px 14px; border-radius:4px; cursor:pointer;
  font-size:0.82rem; margin-right:4px; transition:all .2s;
}
.ho-period-btn:hover { color:#e2e8f0; border-color:#64748b; }
.ho-period-btn.active { background:#3b82f6; color:#fff;
  border-color:#3b82f6; font-weight:700; }

/* 銘柄別タブ */
.sym-tab-nav {
  display:flex; flex-wrap:wrap; gap:6px; margin:12px 0 16px;
  padding:10px; background:#0f172a; border-radius:8px;
}
.sym-tab-btn {
  padding:6px 14px; background:#1e293b; border:1px solid #334155;
  color:#e2e8f0; border-radius:6px; cursor:pointer;
  font-size:0.82rem; text-align:center; line-height:1.5;
  transition:all .2s; min-width:90px;
}
.sym-tab-btn:hover { background:#263349; border-color:#64748b; }
.sym-tab-btn.active { background:#1d4ed8; border-color:#3b82f6; }
.sym-tab-pane { display:none; }

/* 指定銘柄 期間セレクター */
.sp-period-btn {
  background:#1e293b; border:1px solid #334155; color:#94a3b8;
  padding:5px 14px; border-radius:4px; cursor:pointer;
  font-size:0.82rem; margin-right:4px; transition:all .2s;
}
.sp-period-btn:hover { color:#e2e8f0; border-color:#64748b; }
.sp-period-btn.active { background:#3b82f6; color:#fff;
  border-color:#3b82f6; font-weight:700; }
.sp-period-pane { display:none; }
"""

_extra_js = """
function switchHoTab(tab) {
  document.querySelectorAll('.ho-outer-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.ho-outer-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('ho-' + tab).classList.add('active');
  (event.target.closest('.ho-outer-btn') || event.target).classList.add('active');
}
function switchHoPeriod(days) {
  document.querySelectorAll('.ho-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.ho-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('hd' + days).style.display = 'block';
  (event.target.closest('.ho-period-btn') || event.target).classList.add('active');
}
function switchSymTab(tabId) {
  document.querySelectorAll('.sym-tab-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sym-tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tabId).style.display = 'block';
  (event.target.closest('.sym-tab-btn') || event.target).classList.add('active');
}
function switchSpPeriod(days) {
  document.querySelectorAll('.sp-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sp-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('sp' + days).style.display = 'block';
  (event.target.closest('.sp-period-btn') || event.target).classList.add('active');
}
"""

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ホールドアウト全設定 シグナル・損益{'（ショート）' if _args.short else ''} {date_str} [保有{_bte.MAX_HOLD}日]</title>
<style>
{_na.CSS}
{_extra_css}
</style>
</head>
<body>
<h1>ホールドアウト全設定 シグナル・損益レポート{'（ショート）' if _args.short else ''}</h1>
<p class="subtitle">
  基準日: {date_str} &nbsp;|&nbsp;
  保有期限: {_bte.MAX_HOLD}日 &nbsp;|&nbsp;
  設定数: {len(_all_configs)}件 &nbsp;|&nbsp;
  workers={_args.workers}
</p>

<div class="ho-outer-nav">
  <button class="ho-outer-btn active" onclick="switchHoTab('sig')">📋 シグナル</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('pnl')">💹 損益</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('sym')">📊 銘柄詳細（{len(_signal_stocks)}件）</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('news')">📰 ニュース・情報</button>{_market_tab_btns}{_sym_detail_tab_btn}{_wfh_tab_btn}{_oos_tab_btn}
</div>

<div id="ho-sig" class="ho-outer-pane active">
{_sig_html}
</div>

<div id="ho-pnl" class="ho-outer-pane">
  <div style="margin:12px 0 16px">
    <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
    {_period_btns}
  </div>
  {_period_panes}
</div>

<div id="ho-sym" class="ho-outer-pane">
  <p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 0">
    本日シグナルが出た {len(_signal_stocks)} 銘柄の過去365日取引履歴（BTスコア降順）
  </p>
  <div class="sym-tab-nav">
{_sym_tab_nav}
  </div>
{_sym_tab_panes}
</div>

<div id="ho-news" class="ho-outer-pane">
{_news_html}
</div>

<div id="ho-mkt1" class="ho-outer-pane">
{_market_tab1_html}
</div>

<div id="ho-mkt2" class="ho-outer-pane">
{_market_tab2_html}
</div>

<div id="ho-mkt3" class="ho-outer-pane">
{_market_tab3_html}
</div>
{_sym_detail_tab_pane}
{_wfh_tab_pane}
{_oos_tab_pane}
<script>
{_na.JS}
{_extra_js}
</script>
</body>
</html>"""

_sym_suffix    = f"_{_sym_arg.replace('.', '')}" if _args.symbol else ""
_short_suffix  = "_short" if _args.short else ""
_output_suffix = _args.output_suffix if _args.output_suffix else ""
out_path = Path(f"signals_holdout_all{_short_suffix}{_sym_suffix}_{date_str}{_output_suffix}.html")
out_path.write_text(html, encoding="utf-8")
print(f"\nレポート生成完了: {out_path.resolve()}")

if not _args.no_browser:
    from _open_html import open_html
    open_html(out_path.resolve())

# 単独(非--both)実行でも発注サーバを起動 (--no-serve で無効・既定ON)
_maybe_serve_orders()

