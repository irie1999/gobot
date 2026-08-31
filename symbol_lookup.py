"""
symbol_lookup.py  ―  企業名 ⇄ 証券コード の名寄せ
====================================================
YouTube 字幕から拾った「トヨタ」「ソフトバンクG」のような表記を、
リポジトリ内の銘柄マスタと突き合わせて 4 桁コードに確定させる。
**推測でコードを作らない** のが目的 (LLM は平気で存在しないコードを出す)。

【マスタの探索順】
  1. symbols_listed_all.py      (全上場 ~4000)   ← fetch_listed_symbols.py で生成
  2. symbols_listed_standard.py
  3. symbols_listed_prime.py    (プライム ~1800)
  4. symbols_all.py             (日経225)        ← フォールバック
  いずれも `SYMBOLS = [("1332.T", "ニッスイ"), ...]` 形式。

【使い方】
  from symbol_lookup import resolve
  resolve("7203", "トヨタ")     -> ("7203", "トヨタ自動車", True)
  resolve("",     "トヨタ")     -> ("7203", "トヨタ自動車", True)
  resolve("9999", "謎の会社")   -> ("9999", "謎の会社", False)   # マスタに無い

  python symbol_lookup.py トヨタ ソフトバンクG 7203   # CLI で確認
"""

from __future__ import annotations

import re
import sys
import unicodedata

MASTER_MODULES = ("symbols_listed_all", "symbols_listed_standard",
                  "symbols_listed_prime", "symbols_all")

_CODE2NAME: dict[str, str] = {}
_EXACT2CODE: dict[str, str] = {}          # 軽い正規化 (表記ゆれのみ吸収)
_LOOSE2CODES: dict[str, set] = {}         # 強い正規化 (HD/グループ等も除去)
_LOADED = False

# 軽い正規化で落とす語 (会社形態のみ)
_LIGHT_DROP = ("株式会社", "(株)", "（株）", " ", "\u3000", "・")
# 強い正規化で落とす語 (別法人と衝突しうるので一意判定必須)
_LOOSE_DROP = _LIGHT_DROP + ("ホールディングス", "ホールディング", "グループ本社",
                             "グループ", "HD", "&", "＆")


def _norm(s: str, words: tuple = _LIGHT_DROP) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).upper()
    for w in words:
        s = s.replace(unicodedata.normalize("NFKC", w).upper(), "")
    return s.strip()


def _loose(s: str) -> str:
    return _norm(s, _LOOSE_DROP)


def load_master() -> dict[str, str]:
    """{4桁コード: 銘柄名} を返す (初回のみ読み込み)。"""
    global _LOADED
    if _LOADED:
        return _CODE2NAME
    for mod in MASTER_MODULES:
        try:
            m = __import__(mod)
        except Exception:
            continue
        for sym, name in getattr(m, "SYMBOLS", []):
            code = str(sym).split(".")[0]
            if not re.fullmatch(r"\d{4}", code):
                continue
            _CODE2NAME.setdefault(code, name)
            n = _norm(name)
            if n:
                _EXACT2CODE.setdefault(n, code)
            l = _loose(name)
            if l:
                _LOOSE2CODES.setdefault(l, set()).add(code)
    _LOADED = True
    return _CODE2NAME


def resolve(code: str = "", name: str = "") -> tuple[str, str, bool]:
    """
    (code, name, verified) を返す。
      verified=True  … マスタで裏取りできた
      verified=False … マスタに無い / 特定できない (発注前に人間が確認すべき)
    """
    load_master()
    code = re.sub(r"\D", "", str(code or ""))[:4]
    name = str(name or "").strip()

    if code and code in _CODE2NAME:
        return code, _CODE2NAME[code], True

    if name:
        # 1) 表記ゆれのみ吸収した完全一致 (ソフトバンクG と ソフトバンク を混同しない)
        n = _norm(name)
        if n in _EXACT2CODE:
            c = _EXACT2CODE[n]
            return c, _CODE2NAME[c], True
        # 2) HD/グループ等も落とした一致。複数法人に当たったら確定させない
        l = _loose(name)
        if l and len(_LOOSE2CODES.get(l, ())) == 1:
            c = next(iter(_LOOSE2CODES[l]))
            return c, _CODE2NAME[c], True
        # 3) 前方/部分一致。候補が 1 社に絞れるときだけ採用
        if len(l) >= 3:
            hits = {c for k, cs in _LOOSE2CODES.items()
                    if k.startswith(l) or l in k for c in cs}
            if len(hits) == 1:
                c = next(iter(hits))
                return c, _CODE2NAME[c], True

    return code, name, False


def name_of(code: str) -> str:
    load_master()
    return _CODE2NAME.get(re.sub(r"\D", "", str(code or ""))[:4], "")


if __name__ == "__main__":
    load_master()
    print(f"マスタ {len(_CODE2NAME)} 銘柄")
    for q in sys.argv[1:]:
        if re.fullmatch(r"\d{4}", q):
            print(q, "→", resolve(code=q))
        else:
            print(q, "→", resolve(name=q))
