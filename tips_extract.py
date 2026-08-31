"""
tips_extract.py  ―  字幕テキストから「株の売買見解 (calls) と Tips」を構造化抽出
=================================================================================
youtube_tips.py の下請け。字幕の生テキスト (タイムスタンプ付き) を投げると、
売買判断の材料として使える形の JSON を返す。

【設計方針】
  1. **発言 (speaker_claim) と AI の推測 (ai_note) を必ず分離する。**
     動画で言っていないことを「言った」ことにしない。
  2. **銘柄コードは推測しない。** symbol_lookup.py のマスタで裏取りし、
     取れなければ code_verified=False で人間確認に回す。
  3. **信頼度は LLM の気分ではなく決定的なルーブリックで採点する。**
     LLM には「根拠があるか」等の真偽フラグだけ答えさせ、点数は Python で計算
     (score_call)。基準を変えたら過去データも同じ式で再計算できる。
  4. **抽出エンジンは差し替え可能。** Claude / 任意の外部 CLI (codex 等) /
     オフラインのキーワード抽出を同じインターフェースで扱う。

【バックエンド】 --backend で指定。auto は api → cli → heuristic の順に自動選択
  api       : anthropic SDK + ANTHROPIC_API_KEY  (pip install anthropic)
  cli       : `claude -p` (Claude Code CLI。API キー不要)
  cmd       : 任意の外部コマンド。プロンプトを stdin、JSON を stdout で受け取る
              例) --backend cmd --llm-cmd "codex exec -"
                  export TIPS_LLM_CMD="codex exec -"
  heuristic : LLM 無しのキーワード抽出 (オフライン。精度は落ちるが必ず動く)

【相互チェック (2エンジン合議)】
  extract_tips(..., cross_check="cmd") のように 2 つ目のエンジンを渡すと、
  両者の結果を銘柄単位で突き合わせ、
    ・両方が同じ銘柄に同じスタンス → agreement="一致"  (信頼度 +10)
    ・スタンスが割れた             → agreement="不一致" (信頼度 -20, 要確認)
    ・片方だけ                     → agreement="片側"   (据え置き)
  を付ける。Claude と codex を突き合わせる用途を想定。

【出力スキーマ】
  {
    "summary": ["3行要約", ...],
    "market_view": "強気|弱気|中立 — 一言",
    "calls": [{
       "ticker","company","code_verified","stance","action","time_horizon",
       "entry_condition","target_price","stop_condition",
       "catalysts":[],"risks":[],"evidence_type":[],
       "speaker_claim",          # 動画内で実際に述べられたこと
       "ai_note",                # AI の補足・推測 (発言ではない)
       "timestamp_seconds","flags":{...},"quote_confidence",
       "reliability"             # 0-100 (score_call の出力)
    }],
    "tips": [{"category","tip","detail","timestamp","actionable","confidence"}],
    "noise": false, "promo": false,
    "backend","model"
  }

【単体テスト】
  python tips_extract.py --file youtube_tips_data/transcripts/<id>.json
  python tips_extract.py --file x.json --backend heuristic
  python tips_extract.py --file x.json --backend cli --cross-check cmd --llm-cmd "codex exec -"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

import symbol_lookup

DEFAULT_MODEL = "claude-sonnet-5"   # 日次で本数を回すのでコスト重視。
                                    # 精度優先なら --model claude-opus-5
CHUNK_CHARS   = 24_000              # 1 回の投入上限 (超えたら分割 → マージ)
MAX_TIPS      = 12                  # 1 動画あたりの Tips 上限
MAX_CALLS     = 10                  # 1 動画あたりの銘柄見解 上限
CLI_TIMEOUT   = 420

STANCES  = ("強気", "弱気", "中立")
HORIZONS = ("数日", "数週間", "数ヶ月", "不明")
EVIDENCE = ("業績", "テクニカル", "需給", "マクロ", "材料", "思惑")
CATS     = ("エントリー", "利確", "損切り", "資金管理", "メンタル", "マクロ", "銘柄", "ツール")

# ── 信頼度ルーブリック (点数はここだけで決まる) ────────────────────────
RUBRIC = {
    "has_evidence":           +15,   # 具体的な根拠がある
    "has_entry_exit":         +15,   # エントリー条件と撤退条件がある
    "has_verifiable_numbers": +10,   # 決算数値など検証可能な情報がある
    "overclaiming":           -15,   # 「絶対に上がる」等の断定・煽り
    "promotional":            -15,   # サロン/アフィリエイト誘導
    "hindsight_only":         -10,   # 事後解説だけで、これからの条件が無い
}
BASE_SCORE = 50


def score_call(flags: dict, quote_confidence: float = 0.6,
               channel_bonus: float = 0.0) -> int:
    """
    銘柄見解の信頼度 (0-100) を決定的に計算する。

      base 50
      + ルーブリック加減点 (RUBRIC)
      + (字幕の聞き取り確度 - 0.5) * 20     … 自動字幕の誤変換リスク
      + channel_bonus                      … 発信者の過去実績 (tips_track.py が算出)

    ※ この値は「売買シグナル」ではなく「この発言をどれだけ真に受けてよいか」。
    """
    s = BASE_SCORE
    for k, pt in RUBRIC.items():
        if flags.get(k):
            s += pt
    s += (float(quote_confidence or 0.5) - 0.5) * 20
    s += float(channel_bonus or 0.0)
    return int(max(0, min(100, round(s))))


# ── プロンプト ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "あなたは日本株の個人投資家を支援するアナリストです。"
    "YouTube 動画の字幕から、売買判断の材料になる情報だけを抽出します。"
    "最重要ルール: 『動画内で実際に述べられたこと』と『あなたの推測・補足』を必ず分離すること。"
    "前者は speaker_claim、後者は ai_note に書き、speaker_claim に推測を混ぜてはいけません。"
    "証券コードは確信が持てないなら空文字にすること (推測でコードを作らない)。"
    "字幕は自動生成のため誤変換が多い (例: 『損切り』→『そんぎり』、『日経』→『日経い』)。"
    "文脈で補正し、聞き取りが怪しい箇所は quote_confidence を下げること。"
    "出力は JSON のみ。前置き・説明文・コードフェンスは付けないでください。"
)

USER_PROMPT = """以下は YouTube 動画の字幕です。

# 動画情報
タイトル: {title}
チャンネル: {channel}
公開日: {upload_date}
長さ: {duration_min} 分
URL: {url}

# 抽出する 2 種類
(A) calls … 個別銘柄への売買見解。最大 {max_calls} 件。
(B) tips  … 銘柄に依らない再現性のある手法・ルール。最大 {max_tips} 件。

# ルール
- 実況・感想・自己紹介・グッズ販売・サロン勧誘は無視する。宣伝ばかりなら promo=true。
- 中身が無い動画は noise=true にして calls/tips は空でよい。
- 数値 (%, 円, 日数, 指標名) が語られていれば必ず含める。語られていない項目は空文字。
- ticker は動画内で明示された 4 桁コードのみ。分からなければ "" (企業名は company に書く)。
- stance は {stances} のいずれか。time_horizon は {horizons} のいずれか。
- evidence_type は {evidence} から該当するものを配列で。
- timestamp_seconds は字幕の [mm:ss] を秒に直した整数。
- flags は事実の有無だけを true/false で答える (点数は付けない):
    has_evidence           具体的な根拠 (数値・チャート・材料) を挙げているか
    has_entry_exit         入る条件と撤退/損切り条件の両方に触れているか
    has_verifiable_numbers 決算値・株価水準など後から検証できる数値があるか
    overclaiming           「絶対」「確実に」等の断定や煽りがあるか
    promotional            サロン・情報商材・アフィリエイトへの誘導があるか
    hindsight_only         事後解説のみで、これからの行動条件が無いか
- quote_confidence は「字幕からその発言をどれだけ正確に読み取れたか」0.0-1.0。

# 出力 JSON スキーマ (このキー構成を厳守)
{{"summary":["要約1","要約2","要約3"],
  "market_view":"強気|弱気|中立 — 一言",
  "calls":[{{"ticker":"7203","company":"トヨタ自動車","stance":"強気",
             "action":"押し目買い候補","time_horizon":"数週間",
             "entry_condition":"2800円付近まで調整","target_price":"3100円",
             "stop_condition":"直近安値割れ","catalysts":["円安"],"risks":["為替反転"],
             "evidence_type":["業績","テクニカル"],
             "speaker_claim":"動画内で実際に述べられたこと",
             "ai_note":"AIの補足 (発言ではない)",
             "timestamp_seconds":423,
             "flags":{{"has_evidence":true,"has_entry_exit":true,
                      "has_verifiable_numbers":true,"overclaiming":false,
                      "promotional":false,"hindsight_only":false}},
             "quote_confidence":0.7}}],
  "tips":[{{"category":"エントリー","tip":"...","detail":"...","timestamp":"12:34",
            "actionable":true,"confidence":0.8}}],
  "noise":false,"promo":false}}
  ※ tips の category は {cats} から選ぶ。

# 字幕
{transcript}
"""

MERGE_PROMPT = """以下は同一動画を分割して抽出した JSON の配列です。
重複する calls / tips をまとめ、同じスキーマの JSON 1 個だけを出力してください
(calls は最大 {max_calls} 件、tips は最大 {max_tips} 件。説明文なし)。

{parts}
"""


# ── JSON 取り出し ──────────────────────────────────────────────────────
def _loads(text: str) -> dict:
    """LLM 出力から JSON オブジェクトを頑張って取り出す。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError(f"JSON として解釈できません: {text[:200]}")


# ── バックエンド 1: anthropic SDK ─────────────────────────────────────
def _has_api() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _call_api(prompt: str, model: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=8192, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


# ── バックエンド 2: claude CLI (-p) ───────────────────────────────────
def _has_cli() -> bool:
    return shutil.which("claude") is not None


def _call_cli(prompt: str, model: str) -> str:
    # --system-prompt でシステムプロンプトごと差し替える。
    # CLAUDE.md 探索やツール定義が乗らないので、要約タスクとしては最小コストで済む。
    cmd = ["claude", "-p", "--output-format", "json",
           "--model", model,
           "--system-prompt", SYSTEM_PROMPT,
           "--strict-mcp-config",          # MCP サーバを読み込まない
           "--disable-slash-commands"]     # skill 探索をしない
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=CLI_TIMEOUT, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"claude CLI 失敗: {p.stderr.strip()[:300]}")
    try:
        return json.loads(p.stdout).get("result", "")
    except json.JSONDecodeError:
        return p.stdout


# ── バックエンド 3: 任意の外部コマンド (codex など) ───────────────────
def llm_cmd() -> str:
    """外部コマンドのテンプレート。--llm-cmd か環境変数 TIPS_LLM_CMD で指定。"""
    return os.environ.get("TIPS_LLM_CMD", "").strip()


def _has_cmd() -> bool:
    c = llm_cmd()
    return bool(c) and shutil.which(shlex.split(c)[0]) is not None


def _call_cmd(prompt: str, model: str) -> str:
    """
    stdin にプロンプト全文 (システム指示込み) を流し、stdout の JSON を受け取る。
      例: TIPS_LLM_CMD="codex exec -"
          TIPS_LLM_CMD="ollama run qwen2.5"
    """
    c = llm_cmd()
    if not c:
        raise RuntimeError("TIPS_LLM_CMD (または --llm-cmd) が未設定です")
    full = f"{SYSTEM_PROMPT}\n\n{prompt}"
    p = subprocess.run(shlex.split(c), input=full, capture_output=True, text=True,
                       timeout=CLI_TIMEOUT, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"外部コマンド失敗 ({c}): {p.stderr.strip()[:300]}")
    return p.stdout


# ── バックエンド 4: ヒューリスティック (LLM 無し) ─────────────────────
KEYWORDS: dict[str, tuple[str, ...]] = {
    "エントリー": ("エントリー", "買い場", "押し目", "ブレイク", "打診買い", "仕込", "逆指値", "指値"),
    "利確":     ("利確", "利益確定", "目標株価", "決済", "手仕舞い", "利食い"),
    "損切り":   ("損切", "ロスカット", "撤退", "ストップ", "含み損"),
    "資金管理": ("資金管理", "ポジションサイズ", "分散", "枚数", "建玉", "レバレッジ", "余力"),
    "メンタル": ("メンタル", "感情", "焦り", "ルール", "規律", "習慣", "コツコツ"),
    "マクロ":   ("日経", "為替", "ドル円", "金利", "FOMC", "CPI", "雇用統計", "地合い", "需給"),
    "銘柄":     ("決算", "業績", "材料", "上方修正", "増配", "自社株買い", "テーマ"),
    "ツール":   ("スクリーニング", "チャート", "インジケーター", "移動平均", "MACD", "RSI",
                 "出来高", "ボリンジャー", "一目"),
}
_STRONG   = ("重要", "大事", "必ず", "絶対", "ポイントは", "コツは", "おすすめ", "注意",
             "してはいけない", "べき")
_BULL     = ("買い", "上昇", "強い", "上がる", "狙", "妙味")
_BEAR     = ("売り", "下落", "弱い", "下がる", "警戒", "危な")
_CODE_RE  = re.compile(r"(?<!\d)([1-9]\d{3})(?!\d)")


def _heuristic(transcript: str, meta: dict) -> dict:
    """LLM 無しの簡易抽出。文単位でキーワードスコアリングして上位を返す。"""
    body  = re.sub(r"\[\d+:\d{2}(?::\d{2})?\]", "|", transcript)
    sents = [s.strip() for s in re.split(r"[。！？\n|]", body) if len(s.strip()) >= 12]

    scored: list[tuple[float, str, str]] = []
    for s in sents:
        best_cat, hits = "", 0
        for cat, kws in KEYWORDS.items():
            n = sum(1 for k in kws if k in s)
            if n > hits:
                best_cat, hits = cat, n
        if not hits:
            continue
        score = hits + sum(0.5 for w in _STRONG if w in s)
        score += 0.5 if re.search(r"\d+\s*(%|％|円|日|倍)", s) else 0
        scored.append((score, best_cat, s[:200]))
    scored.sort(key=lambda x: -x[0])

    seen: set[str] = set()
    tips: list[dict] = []
    for score, cat, s in scored:
        if s[:24] in seen:
            continue
        seen.add(s[:24])
        tips.append({"category": cat, "tip": s, "detail": "", "timestamp": "",
                     "actionable": score >= 2, "confidence": round(min(score / 4, 0.6), 2)})
        if len(tips) >= MAX_TIPS:
            break

    # 銘柄コードらしき 4 桁 + 同じ文の強弱語からごく粗い call を作る
    calls: list[dict] = []
    for sent in sents:
        for code in _CODE_RE.findall(sent):
            if any(c["ticker"] == code for c in calls):
                continue
            # 「2800円まで押したら」の 2800 のような株価・年号を銘柄と誤認しないよう、
            # マスタで実在が確認できたコードだけ採用する (heuristic は精度優先)
            _c, _n, verified = symbol_lookup.resolve(code=code)
            if not verified:
                continue
            bull = sum(1 for w in _BULL if w in sent)
            bear = sum(1 for w in _BEAR if w in sent)
            calls.append({
                "ticker": code, "company": "", "stance":
                    "強気" if bull > bear else ("弱気" if bear > bull else "中立"),
                "action": "", "time_horizon": "不明", "entry_condition": "",
                "target_price": "", "stop_condition": "", "catalysts": [], "risks": [],
                "evidence_type": [], "speaker_claim": sent[:200],
                "ai_note": "キーワード抽出のみ (LLM 未使用)。文脈は要確認",
                "timestamp_seconds": 0,
                "flags": {"has_evidence": False, "has_entry_exit": False,
                          "has_verifiable_numbers": False, "overclaiming": False,
                          "promotional": False, "hindsight_only": False},
                "quote_confidence": 0.3,
            })
            if len(calls) >= MAX_CALLS:
                break

    return {"summary": [meta.get("title", "")[:100],
                        f"キーワード抽出 tips {len(tips)} 件 / calls {len(calls)} 件 (LLM 未使用)"],
            "market_view": "判定不可 (heuristic)", "calls": calls, "tips": tips,
            "noise": not tips and not calls, "promo": False}


# ── 正規化 ─────────────────────────────────────────────────────────────
def _str(v, n: int = 200) -> str:
    return str(v if v is not None else "").strip()[:n]


def _list(v, n: int = 8, ln: int = 60) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [_str(x, ln) for x in (v or []) if _str(x)][:n]


def _normalize(d: dict, channel_bonus: float = 0.0) -> dict:
    out = {
        "summary":     [_str(x, 200) for x in (d.get("summary") or []) if _str(x)][:5],
        "market_view": _str(d.get("market_view")),
        "calls": [], "tips": [],
        "noise": bool(d.get("noise")), "promo": bool(d.get("promo")),
    }

    for c in (d.get("calls") or [])[:MAX_CALLS]:
        if not isinstance(c, dict):
            continue
        code, name, verified = symbol_lookup.resolve(c.get("ticker", ""), c.get("company", ""))
        if not code and not name:
            continue
        flags = {k: bool((c.get("flags") or {}).get(k)) for k in RUBRIC}
        try:
            qc = float(c.get("quote_confidence", 0.6))
        except (TypeError, ValueError):
            qc = 0.6
        qc = max(0.0, min(1.0, qc))
        stance   = _str(c.get("stance"), 10)
        horizon  = _str(c.get("time_horizon"), 10)
        try:
            tsec = int(float(c.get("timestamp_seconds") or 0))
        except (TypeError, ValueError):
            tsec = 0
        out["calls"].append({
            "ticker":       code,
            "company":      name or symbol_lookup.name_of(code),
            "code_verified": verified,
            "stance":       stance if stance in STANCES else "中立",
            "action":       _str(c.get("action"), 60),
            "time_horizon": horizon if horizon in HORIZONS else "不明",
            "entry_condition": _str(c.get("entry_condition"), 120),
            "target_price":    _str(c.get("target_price"), 40),
            "stop_condition":  _str(c.get("stop_condition"), 120),
            "catalysts":    _list(c.get("catalysts")),
            "risks":        _list(c.get("risks")),
            "evidence_type": [e for e in _list(c.get("evidence_type")) if e in EVIDENCE],
            "speaker_claim": _str(c.get("speaker_claim"), 400),
            "ai_note":       _str(c.get("ai_note"), 300),
            "timestamp_seconds": max(0, tsec),
            "flags":            flags,
            "quote_confidence": round(qc, 2),
            "reliability":      score_call(flags, qc, channel_bonus),
            "agreement":        "",
        })

    for t in (d.get("tips") or [])[:MAX_TIPS]:
        if not isinstance(t, dict) or not _str(t.get("tip")):
            continue
        cat = _str(t.get("category"), 12)
        try:
            conf = float(t.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out["tips"].append({
            "category":   cat if cat in CATS else "その他",
            "tip":        _str(t.get("tip"), 400),
            "detail":     _str(t.get("detail"), 600),
            "timestamp":  _str(t.get("timestamp"), 12),
            "actionable": bool(t.get("actionable", True)),
            "confidence": round(max(0.0, min(1.0, conf)), 2),
        })
    return out


# ── 相互チェック (2エンジン合議) ──────────────────────────────────────
def merge_cross_check(primary: dict, second: dict) -> dict:
    """
    2 つの抽出結果を銘柄単位で突き合わせ、primary の calls に agreement と
    信頼度補正を付ける。second にしか無い銘柄は「片側(2nd)」として追加する。
    """
    by2 = {c["ticker"] or c["company"]: c for c in second.get("calls", [])}
    for c in primary.get("calls", []):
        key = c["ticker"] or c["company"]
        o   = by2.pop(key, None)
        if o is None:
            c["agreement"] = "片側"
            continue
        if o["stance"] == c["stance"]:
            c["agreement"]   = "一致"
            c["reliability"] = min(100, c["reliability"] + 10)
        else:
            c["agreement"]   = f"不一致(他エンジン:{o['stance']})"
            c["reliability"] = max(0, c["reliability"] - 20)
    for k, o in by2.items():
        o["agreement"]   = "片側(2nd)"
        o["ai_note"]     = (o.get("ai_note", "") + " ※第2エンジンのみが検出").strip()
        o["reliability"] = max(0, o["reliability"] - 10)
        primary.setdefault("calls", []).append(o)

    seen = {t["tip"][:30] for t in primary.get("tips", [])}
    for t in second.get("tips", []):
        if t["tip"][:30] not in seen and len(primary.get("tips", [])) < MAX_TIPS:
            primary.setdefault("tips", []).append(t)
    return primary


# ── メイン API ─────────────────────────────────────────────────────────
def pick_backend(pref: str = "auto") -> str:
    if pref != "auto":
        return pref
    if _has_api():
        return "api"
    if _has_cli():
        return "cli"
    if _has_cmd():
        return "cmd"
    return "heuristic"


def _call(prompt: str, backend: str, model: str) -> str:
    if backend == "api":
        return _call_api(prompt, model)
    if backend == "cli":
        return _call_cli(prompt, model)
    if backend == "cmd":
        return _call_cmd(prompt, model)
    raise ValueError(backend)


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        j = text.rfind("\n", i + int(size * 0.7), i + size)
        j = j if j > i else min(i + size, len(text))
        out.append(text[i:j])
        i = j
    return out


def _run_backend(transcript: str, meta: dict, backend: str, model: str,
                 verbose: bool) -> dict:
    fmt = dict(
        title=meta.get("title", ""), channel=meta.get("channel", ""),
        upload_date=meta.get("upload_date", ""), url=meta.get("url", ""),
        duration_min=round((meta.get("duration") or 0) / 60),
        max_tips=MAX_TIPS, max_calls=MAX_CALLS,
        stances="/".join(STANCES), horizons="/".join(HORIZONS),
        evidence="/".join(EVIDENCE), cats="/".join(CATS),
    )
    parts   = _chunks(transcript, CHUNK_CHARS)
    results = []
    for n, c in enumerate(parts, 1):
        if verbose and len(parts) > 1:
            print(f"    [{backend}] chunk {n}/{len(parts)} ({len(c)}字)", file=sys.stderr)
        results.append(_loads(_call(USER_PROMPT.format(transcript=c, **fmt), backend, model)))
    if len(results) == 1:
        return results[0]
    return _loads(_call(
        MERGE_PROMPT.format(max_tips=MAX_TIPS, max_calls=MAX_CALLS,
                            parts=json.dumps(results, ensure_ascii=False)),
        backend, model))


def extract_tips(transcript: str, meta: dict, backend: str = "auto",
                 model: str = DEFAULT_MODEL, verbose: bool = False,
                 channel_bonus: float = 0.0, cross_check: str = "") -> dict:
    """
    字幕テキスト + 動画メタ → 構造化 JSON。

    LLM 呼び出しに失敗した場合は heuristic に自動フォールバックする
    (日次バッチが 1 本のエラーで止まらないようにするため)。
    cross_check に別バックエンド名を渡すと 2 エンジンで突き合わせる。
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return {**_normalize({"noise": True, "summary": ["字幕が取得できませんでした"]}),
                "backend": "none", "model": ""}

    backend = pick_backend(backend)
    if backend == "heuristic":
        return {**_normalize(_heuristic(transcript, meta), channel_bonus),
                "backend": "heuristic", "model": ""}

    try:
        raw = _run_backend(transcript, meta, backend, model, verbose)
        out = {**_normalize(raw, channel_bonus), "backend": backend, "model": model}
    except Exception as e:
        if verbose:
            print(f"    ! {backend} 失敗 → heuristic: {str(e)[:160]}", file=sys.stderr)
        out = {**_normalize(_heuristic(transcript, meta), channel_bonus),
               "backend": "heuristic", "model": "", "error": f"{backend}: {str(e)[:200]}"}
        return out

    cc = pick_backend(cross_check) if cross_check else ""
    if cc and cc != backend:
        try:
            raw2 = (_heuristic(transcript, meta) if cc == "heuristic"
                    else _run_backend(transcript, meta, cc, model, verbose))
            out  = merge_cross_check(out, _normalize(raw2, channel_bonus))
            out["backend"] = f"{backend}+{cc}"
        except Exception as e:
            if verbose:
                print(f"    ! cross-check({cross_check}) 失敗: {str(e)[:160]}", file=sys.stderr)
            out["cross_check_error"] = str(e)[:200]
    return out


# ── CLI (単体デバッグ用) ───────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="字幕テキスト → 株の見解/Tips 抽出 (デバッグ用)")
    ap.add_argument("--file", required=True, help="字幕テキスト or yt_transcript の JSON")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "api", "cli", "cmd", "heuristic"])
    ap.add_argument("--cross-check", default="",
                    choices=["", "api", "cli", "cmd", "heuristic"],
                    help="2つ目のエンジンで突き合わせる")
    ap.add_argument("--llm-cmd", default="", help='例: "codex exec -"')
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args()

    if a.llm_cmd:
        os.environ["TIPS_LLM_CMD"] = a.llm_cmd
    raw  = open(a.file, encoding="utf-8").read()
    meta: dict = {}
    try:
        d = json.loads(raw)
        meta, raw = d.get("meta", {}), d.get("text", "")
        if not raw and d.get("segments"):
            import yt_transcript
            raw = yt_transcript.segments_to_text(d["segments"])
    except json.JSONDecodeError:
        pass
    r = extract_tips(raw, meta, a.backend, a.model, verbose=True, cross_check=a.cross_check)
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
