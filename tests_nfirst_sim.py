"""nfirst 変種の検算。**本体の _newgap_sim をそのまま呼ぶ**(写しではない)。

見るもの:
  ① N を全部先に建て、余った予算だけ鏡像に回るか
  ② watch上限が **側ごと** に掛かるか(混ぜて上位N を取っていないか)
  ③ 明細に side が入るか
  ④ side_col を渡さない既定の挙動が **1ミリも変わっていない**か
"""
import sys
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from newgap_core import _newgap_sim          # noqa: E402

FAIL = []


def ck(name, got, want):
    ok = got == want
    print(f"{'OK ' if ok else '⛔ '} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        FAIL.append(name)


def row(sym, side, ret1, gap, px, pnl, liq):
    return {"date": "2026-09-14", "symbol": sym, "ng_side": side,
            "ret1": ret1, "gap_bp": gap, "entry_p": px, "pnl": pnl, "liq": liq}


# ══ ① N優先 — 予算がちょうど N 2件 + 鏡像1件ぶん ═══════════════════
#   建値2,000円 × 100株 = 20万/件。予算60万 = 3件ぶん。
#   N が3件・鏡像が3件 合格しているので、**N が3件とも先に入って
#   鏡像は0件** になるのが正しい。
R = [row("N1", 1, 5.0, 300, 2000, +5000, 9e9),
     row("N2", 1, 4.0, 200, 2000, +4000, 8e9),
     row("N3", 1, 3.0, 150, 2000, +3000, 7e9),
     row("M1", -1, 5.0, 400, 2000, +9000, 9.5e9),   # 鏡像は符号反転済みの想定
     row("M2", -1, 4.0, 350, 2000, +8000, 8.5e9),
     row("M3", -1, 3.0, 120, 2000, +7000, 7.5e9)]
df = pd.DataFrame(R)

s = _newgap_sim(df, 60, 0, 100.0, 1.753, order="nfirst", side_col="ng_side")
ck("N優先: N を3件、鏡像は0件", list(s["det"]["symbol"]), ["N1", "N2", "N3"])
ck("N優先: side は全部 +1", sorted(set(s["det"]["side"])), [1])

# 予算を 100万 = 5件ぶんにすると、N3件 + 鏡像2件(ギャップ降順)
s2 = _newgap_sim(df, 100, 0, 100.0, 1.753, order="nfirst", side_col="ng_side")
ck("余りが出たら鏡像が入る", list(s2["det"]["symbol"]),
   ["N1", "N2", "N3", "M1", "M2"])
ck("side が両方入る", sorted(set(s2["det"]["side"])), [-1, 1])

# ⛔ 対照: gap 降順(既定)だと **鏡像が先に枠を取る**
s3 = _newgap_sim(df, 60, 0, 100.0, 1.753, order="gap", side_col="ng_side")
ck("対照: gap降順だと鏡像が先に取る", list(s3["det"]["symbol"]),
   ["M1", "M2", "N1"])

# ══ ② watch上限が側ごとか ═════════════════════════════════════════
#   各側3件、watch=2。混ぜて上位2件を取ると流動性トップの M1/N1 だけ。
#   側ごとなら N1,N2 と M1,M2 の計4件が見られる。
s4 = _newgap_sim(df, 1000, 2, 100.0, 1.753, order="nfirst", side_col="ng_side")
ck("watch2 が側ごとに効く(各側2件=計4件)",
   list(s4["det"]["symbol"]), ["N1", "N2", "M1", "M2"])
ck("watched は4件", int(s4["days"]["watched"].iloc[0]), 4)

# 側を渡さないと混ざる = 上位2件だけ(M1, N1)
s5 = _newgap_sim(df.drop(columns=["ng_side"]), 1000, 2, 100.0, 1.753,
                 order="gap")
ck("side_col なしなら混ざって2件", sorted(s5["det"]["symbol"]), ["M1", "N1"])

# ══ ③ 既定の挙動が変わっていないか(N だけ / side_col なし) ═════════
NONLY = pd.DataFrame([r for r in R if r["ng_side"] > 0]).drop(columns=["ng_side"])
s6 = _newgap_sim(NONLY, 60, 0, 100.0, 1.753, order="gap")
ck("既定: N だけなら今までどおり3件", list(s6["det"]["symbol"]),
   ["N1", "N2", "N3"])
# ⛔⛔ 片側のときは **side 列を付けない**。付けると鏡像タブ(side="long")の
#   det が全行 +1 になり、行ごと判定が「売り」と読んで **買いの明細が
#   全部ショート表記**になる(§18.63 と同じ形)。列が無いことが
#   「呼び出し側の side を使え」の合図。
ck("⛔ 片側では side 列を付けない", "side" in s6["det"].columns, False)
ck("既定: 損益", float(s6["days"]["pnl"].iloc[0]), 12000.0)

# 鏡像タブ(片側 long)でも side 列が無いこと
MONLY = pd.DataFrame([r for r in R if r["ng_side"] < 0]).drop(columns=["ng_side"])
s8 = _newgap_sim(MONLY, 1000, 0, 100.0, 1.753, order="gap")
ck("⛔ 鏡像タブ(片側)でも side 列なし", "side" in s8["det"].columns, False)
ck("鏡像タブは3件", len(s8["det"]), 3)

# ══ ④ ギャップ判定で落ちる行は建たない ════════════════════════════
#   M3 は gap 120 で合格、N3 は 150 で合格。gap閾値を 200 にすると
#   N1,N2 と M1,M2 だけ
s7 = _newgap_sim(df, 1000, 0, 200.0, 1.753, order="nfirst", side_col="ng_side")
ck("gap閾値で落ちる", list(s7["det"]["symbol"]), ["N1", "N2", "M1", "M2"])

# ══ ⑤ 損益は side に依らず pnl 列をそのまま使う(符号は上流で反転済み) ═
ck("損益の合計", float(s2["days"]["pnl"].iloc[0]),
   5000.0 + 4000 + 3000 + 9000 + 8000)

print("\n" + ("⛔ 失敗 " + ", ".join(FAIL) if FAIL else "✅ 全部通りました"))
sys.exit(1 if FAIL else 0)
