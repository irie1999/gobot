"""make_lss_universe.py — 『選定を一切しない』lss ユニバースを作る。

なぜ必要か
-----------
今の lss は scan_lss_universe で選んだ提案を累積マージした 1,900ペアで動いている。
だが選定の根拠(BTスコア)には識別力が無いことが分かり(CLAUDE.md 18.12)、
提案マージ自体にも先読みがあった(START_DATES を per-symbol に修正済み)。

そこで最も基本的な問いに戻る:

    **lss のルール自体に、銘柄選定なしでエッジがあるのか?**

  あるなら → 選定は不要かおまけ。予算いっぱいまで機会を広げられる
             (約定率20%問題の解にもなる)
  ないなら → エッジは全部『選定』から来ていることになる。そして選定は
             検証が難しい。戦略の前提を考え直す必要がある

これを測るには、選定を通していないユニバースが要る。このスクリプトは
上場銘柄からランダムに N 銘柄を抜き、全戦略と掛けて SELECTED を書き出す。
**ランダムなので選定バイアスがゼロ**。ここでの成績が「素の期待値」。

使い方
------
  python make_lss_universe.py --n 400
  python make_lss_universe.py --n 400 --seed 7 --out lss_universe_random400.py
  python make_lss_universe.py --n 0            # 全銘柄(重い。5分足の読込が効く)

  # 作ったら compare_lss_rules に食わせる (BTは使わないので --bt-min 0)
  python compare_lss_rules.py --symbols-file lss_universe_random400.py \
      --days 240 --min-price 1000 --max-price 6000 --bt-min 0 --audit delay1 --by-month

なぜランダム抽出でよいか
------------------------
全銘柄を回すと5分足の読込が重い。ランダム抽出は母集団の不偏推定なので、
400銘柄 x 6戦略 = 2,400ペア あれば「素のエッジがプラスかマイナスか」は十分決まる
(現行の選定済みが 1,328ペアなので同規模)。**上位を選んではいけない** —
それをした瞬間に選定バイアスが戻る。
"""
from __future__ import annotations

import argparse
import importlib.util
import random
import sys
from pathlib import Path

# lss が使う6戦略。check_signals_stop / check_signals_breakout の STRATEGY_PARAMS と対応。
STRATEGIES = ["A7", "RSI2", "MACDTF", "DON", "VOLTF", "MOM"]

_UNIVERSE_CANDIDATES = [
    "symbols_listed_prime.py",      # プライム ~1800銘柄 (推奨)
    "symbols_listed_all.py",        # 全上場 ~4000銘柄
    "symbols_listed_standard.py",
    "symbols_all.py",               # 日経225 (フォールバック)
]

ap = argparse.ArgumentParser(description="選定なし(ランダム抽出)の lss ユニバースを作る")
ap.add_argument("--n", type=int, default=400,
                help="抽出する銘柄数 (0=全銘柄)。既定400 x 6戦略 = 2,400ペア")
ap.add_argument("--seed", type=int, default=42, help="乱数シード(再現用)")
ap.add_argument("--symbols", type=str, default=None, help="ユニバースファイルを明示指定")
ap.add_argument("--strategies", type=str, default=",".join(STRATEGIES),
                help="対象戦略をカンマ区切りで")
ap.add_argument("--out", type=str, default="", help="出力ファイル名(省略時は自動)")
args = ap.parse_args()


def _load_universe(explicit: str | None):
    cands = ([explicit] if explicit else []) + _UNIVERSE_CANDIDATES
    for c in cands:
        p = Path(c)
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            syms = getattr(mod, "SYMBOLS", None) or getattr(mod, "SELECTED", None)
            if not syms:
                continue
            # (code, name) or (code, name, strat) を (code, name) に正規化
            out = []
            seen = set()
            for t in syms:
                t = tuple(t)
                code = str(t[0])
                if code in seen:
                    continue
                seen.add(code)
                out.append((code, str(t[1]) if len(t) >= 2 else ""))
            return out, p.name
        except Exception as e:
            print(f"[WARN] {c} を読めません: {e}", file=sys.stderr)
    return [], ""


syms, src = _load_universe(args.symbols)
if not syms:
    sys.exit("[error] ユニバースファイルが見つかりません。\n"
             "        python fetch_listed_symbols.py --market prime を先に実行してください")

strats = [s.strip().upper() for s in args.strategies.split(",") if s.strip()]
print(f"[ユニバース] {src} から {len(syms):,}銘柄")

if args.n and args.n < len(syms):
    rnd = random.Random(args.seed)
    picked = sorted(rnd.sample(syms, args.n), key=lambda t: t[0])
    print(f"[抽出] ランダム {args.n}銘柄 (seed={args.seed})  ※上位選抜ではない=選定バイアスなし")
else:
    picked = sorted(syms, key=lambda t: t[0])
    print(f"[抽出] 全 {len(picked):,}銘柄")

out = args.out or f"lss_universe_random{len(picked)}.py"
lines = [
    '"""自動生成: 選定を通していない lss ユニバース (make_lss_universe.py)。',
    f'ソース={src} / 銘柄={len(picked)} / 戦略={",".join(strats)} / seed={args.seed}',
    'ランダム抽出なので選定バイアスがゼロ。ここでの成績が lss の「素の期待値」。',
    '"""',
    "SELECTED = [",
]
n = 0
for code, name in picked:
    for st in strats:
        lines.append(f"    ({code!r}, {name!r}, {st!r}),")
        n += 1
lines.append("]")
Path(out).write_text("\n".join(lines), encoding="utf-8")

print(f"[出力] {out}  ({len(picked):,}銘柄 x {len(strats)}戦略 = {n:,}ペア)")
print()
print("次:")
print(f"  python compare_lss_rules.py --symbols-file {out} \\")
print(f"      --days 240 --min-price 1000 --max-price 6000 --bt-min 0 "
      f"--audit delay1 --by-month")
print("  ※ --bt-min 0 が重要。BTで絞ると選定バイアスが戻る(BTには識別力が無い)")
