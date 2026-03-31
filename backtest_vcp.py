"""
VCP (Volatility Contraction Pattern) × 相対強度  銘柄スキャナー（225銘柄）
────────────────────────────────────────────────────────────────────────
【アルゴリズム】
  Entry（翌日始値、全条件必須）:
    1. EMA21 > MA50 > MA200 かつ 終値 > EMA21（短中長期トレンド一致）
    2. 直近5日TR% < 21日平均TR% × 0.80（ボラティリティ収縮中）
    3. 終値 > 前7日間の最高終値（収縮後のブレイクアウト）
    4. 出来高 > 20日平均 × 1.5倍（機関投資家の本格参入）
    5. RSI 40〜65 ／ MA200上 ／ 日経対比RS ≥ -10%

  Exit（いずれか）:
    A. ATR × 2.5 トレイリングストップ
    B. 終値が EMA21 を下抜け（短期トレンド転換）
    C. 損切り -3% ／ 半分利確 +8%

■ 実行方法:
  python backtest_macd_scan.py                    # 直近1ヶ月（デフォルト）
  python backtest_macd_scan.py --months 3         # 直近3ヶ月
  python backtest_macd_scan.py --years 1          # 直近1年
  python backtest_macd_scan.py --years 5          # 直近5年
  python backtest_macd_scan.py --days 45          # 直近45日
  python backtest_macd_scan.py 7203.T --years 2   # 特定銘柄詳細
  python backtest_macd_scan.py --years 3 --top 30 # 上位30件表示
"""

import io
import sys

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── 日経225 全銘柄 ──────────────────────────────────────────
SYMBOLS = [
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
    # ("6773.T", "パイオニア"),     # 上場廃止
    # ("6794.T", "フォスター電機"), # 上場廃止
    # ("6796.T", "クラリオン"),     # 上場廃止
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
    # ("8355.T", "静岡銀行"),  # データ不備
    ("8377.T", "ほくほくFG"),
    ("8411.T", "みずほフィナンシャルG"),
    ("8601.T", "大和証券グループ本社"),
    ("8604.T", "野村HD"),
    ("8628.T", "松井証券"),
    ("8630.T", "SOMPOホールディングス"),
    ("8697.T", "日本取引所グループ"),
    ("8725.T", "MS&ADインシュアランスG"),
    ("8750.T", "第一生命HD"),
    # ("8752.T", "三井住友海上グループHD"), # データ不備
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
    # ("9613.T", "NTTデータグループ"), # データ不備
    ("9735.T", "セコム"),
    ("9766.T", "コナミグループ"),
    ("9983.T", "ファーストリテイリング"),
    ("9984.T", "ソフトバンクグループ"),
]

# ── 業種マップ ─────────────────────────────────────────────
SECTOR = {
    "1332.T":"水産","1333.T":"水産","1605.T":"石油","1721.T":"建設","1801.T":"建設",
    "1802.T":"建設","1803.T":"建設","1808.T":"建設","1812.T":"建設","1925.T":"建設",
    "1928.T":"建設","1963.T":"建設","2002.T":"食品","2269.T":"食品","2282.T":"食品",
    "2413.T":"サービス","2432.T":"サービス","2501.T":"食品","2502.T":"食品",
    "2503.T":"食品","2531.T":"食品","2768.T":"商社","2801.T":"食品","2802.T":"食品",
    "2871.T":"食品","2914.T":"食品","3086.T":"小売","3099.T":"小売","3105.T":"繊維",
    "3289.T":"不動産","3382.T":"小売","3401.T":"繊維","3402.T":"繊維","3405.T":"化学",
    "3407.T":"化学","3436.T":"半導体","3861.T":"紙・パ","3863.T":"紙・パ",
    "4004.T":"化学","4005.T":"化学","4021.T":"化学","4042.T":"化学","4043.T":"化学",
    "4061.T":"化学","4063.T":"化学","4183.T":"化学","4188.T":"化学","4208.T":"化学",
    "4272.T":"化学","4307.T":"IT","4324.T":"サービス","4502.T":"医薬","4503.T":"医薬",
    "4506.T":"医薬","4507.T":"医薬","4519.T":"医薬","4523.T":"医薬","4543.T":"医療機器",
    "4568.T":"医薬","4578.T":"医薬","4631.T":"化学","4689.T":"IT","4704.T":"IT",
    "4751.T":"IT","4755.T":"IT","4901.T":"精密","4902.T":"精密","5019.T":"石油",
    "5020.T":"石油","5101.T":"ゴム","5108.T":"ゴム","5201.T":"ガラス","5214.T":"ガラス",
    "5232.T":"セメント","5233.T":"セメント","5301.T":"化学","5332.T":"窯業",
    "5333.T":"窯業","5401.T":"鉄鋼","5406.T":"鉄鋼","5411.T":"鉄鋼","5631.T":"機械",
    "5703.T":"非鉄","5706.T":"非鉄","5707.T":"非鉄","5711.T":"非鉄","5713.T":"非鉄",
    "5714.T":"非鉄","5741.T":"非鉄","5801.T":"非鉄","5802.T":"非鉄","5803.T":"非鉄",
    "6098.T":"サービス","6103.T":"機械","6113.T":"機械","6178.T":"金融","6273.T":"機械",
    "6301.T":"機械","6302.T":"機械","6305.T":"機械","6326.T":"機械","6361.T":"機械",
    "6367.T":"機械","6370.T":"機械","6383.T":"機械","6406.T":"機械","6412.T":"娯楽",
    "6417.T":"娯楽","6471.T":"機械","6472.T":"機械","6473.T":"機械","6479.T":"電機",
    "6501.T":"電機","6503.T":"電機","6504.T":"電機","6506.T":"電機","6526.T":"半導体",
    "6532.T":"サービス","6586.T":"機械","6594.T":"電機","6645.T":"電機","6674.T":"電機",
    "6701.T":"電機","6702.T":"電機","6723.T":"半導体","6724.T":"精密","6752.T":"電機",
    "6753.T":"電機","6754.T":"電機","6758.T":"電機","6762.T":"電機","6770.T":"電機",
    "6806.T":"電機","6841.T":"電機","6857.T":"半導体","6861.T":"電機","6902.T":"自動車",
    "6952.T":"電機","6954.T":"電機","6971.T":"電機","6981.T":"電機","6988.T":"化学",
    "7003.T":"重工","7011.T":"重工","7012.T":"重工","7013.T":"重工","7201.T":"自動車",
    "7202.T":"自動車","7203.T":"自動車","7205.T":"自動車","7211.T":"自動車",
    "7261.T":"自動車","7267.T":"自動車","7270.T":"自動車","7272.T":"自動車",
    "7731.T":"精密","7733.T":"精密","7735.T":"半導体","7741.T":"精密","7751.T":"精密",
    "7752.T":"精密","7762.T":"精密","7832.T":"娯楽","7951.T":"楽器","8001.T":"商社",
    "8002.T":"商社","8015.T":"商社","8031.T":"商社","8035.T":"半導体","8053.T":"商社",
    "8058.T":"商社","8233.T":"小売","8252.T":"小売","8253.T":"金融","8267.T":"小売",
    "8303.T":"銀行","8304.T":"銀行","8306.T":"銀行","8308.T":"銀行","8309.T":"銀行",
    "8316.T":"銀行","8331.T":"銀行","8354.T":"銀行","8377.T":"銀行","8411.T":"銀行",
    "8601.T":"証券","8604.T":"証券","8628.T":"証券","8630.T":"保険","8697.T":"金融",
    "8725.T":"保険","8750.T":"保険","8766.T":"保険","8795.T":"保険","8801.T":"不動産",
    "8802.T":"不動産","8804.T":"不動産","8830.T":"不動産","9001.T":"陸運","9005.T":"陸運",
    "9007.T":"陸運","9008.T":"陸運","9009.T":"陸運","9020.T":"陸運","9021.T":"陸運",
    "9022.T":"陸運","9064.T":"陸運","9101.T":"海運","9104.T":"海運","9107.T":"海運",
    "9202.T":"空運","9301.T":"倉庫","9432.T":"通信","9433.T":"通信","9434.T":"通信",
    "9501.T":"電力","9502.T":"電力","9503.T":"電力","9531.T":"ガス","9532.T":"ガス",
    "9602.T":"娯楽","9735.T":"サービス","9766.T":"娯楽","9983.T":"小売",
    "9984.T":"IT",
}


BACKTEST_DAYS   = 30            # デフォルト（1ヶ月）
WORKERS         = 16            # 並列バックテスト数

# ── VCP (Volatility Contraction Pattern) × 相対強度 ────────────────
# 【戦略概要】
#   機関投資家の買い集め → 株価がじっくり収縮（ボラ低下）→ 出来高急増で上放れ
#   MACDクロスよりも 1〜2 週間早いシグナル = RSI40〜55・低MA乖離でエントリー可能
#
#   Entry（全条件を満たした翌日始値）:
#     1. EMA21 > MA50 > MA200 かつ 終値 > EMA21（短中長期トレンド一致）
#     2. 直近5日TR% が 21日平均TR% の 80%以下（ボラティリティ収縮中）
#     3. 終値 > 前7日間の最高終値（収縮後のブレイクアウト）
#     4. 出来高 > 20日平均 × 1.5倍（機関投資家の本物の参入）
#     5. MA200 上（長期上昇トレンド確認）
#     6. RSI 40〜65（過熱でも底でもない適切な水準）
#     7. 日経対比リターン ≥ -10%（弱い銘柄を除外）
#
#   Exit（いずれか）:
#     A. ATR × 2.5 トレイリングストップ
#     B. 終値が EMA21 を下抜け（短期トレンド転換）
#     C. -3% ハードストップ
#     D. +8% で半分利確

EMA21_PERIOD    = 21    # 短期EMA（Minervini式: 機関投資家参照基準線）
MA50_PERIOD     = 50    # 中期SMA（トレンド中軸）
ATR_PERIOD      = 14    # ATR（ボラティリティ計測）
VCP_WINDOW      = 3     # ブレイクアウト確認ウィンドウ（3日: 下落相場でも反発を捕捉）
ATR_COMPRESS    = 1.50  # 収縮判定: 事実上無効化（計算・表示のみ）
RS_LOOKBACK     = 40    # 相対強度比較期間（約2ヶ月）
RS_MIN          = -0.30 # 日経対比 -30%以上（事実上無効化）

VOL_MA_PERIOD   = 20    # 出来高移動平均
ATR_TRAIL_MULT  = 2.5   # ATRトレイル係数（高ボラ対応）

INITIAL_CASH    = 500_000
POSITION_SIZE   = 50_000  # 1銘柄あたり購入金額（固定）
MAX_QTY         = 9999

# ── エントリーフィルター ─────────────────────────────────────────
FILTER_MA200_ABOVE   = True   # MA200より上のみ（長期上昇トレンド確認）
FILTER_RSI_MIN       = 35.0   # RSI下限（40→35: 押し目深めも対象に）
FILTER_RSI_MAX       = 70.0   # RSI上限（65→70: 強い銘柄も逃さない）
FILTER_VOL_SURGE     =  1.2   # ブレイクアウト時の出来高倍率（1.3→1.2: 取引増加）

# ── ポートフォリオ管理 ─────────────────────────────────────────
MAX_POSITIONS        = 5     # 同時保有最大銘柄数（地合い良好時）
MAX_POSITIONS_BEAR   = 2     # 日経MA25割れ時は絞る
HALF_PROFIT_PCT      = 8.0   # 半分利確ライン（VCP: より深いトレンドを狙う）
HARD_STOP_PCT        = 3.0   # 即損切りライン（高ボラ対応）

# ── 相場分析に基づくセクター設定【2026年3月 詳細分析版】──────────
#
# 【除外】関税直撃・業績悪化・金利逆風
#   自動車: トヨタ▲6000億、マツダ「サバイバルモード」
#   鉄鋼:  アナリスト最低評価継続
#   小売:  訪日需要剥落、内需低迷
#   海運/非鉄/商社: 前回分析から継続除外
#
EXCLUDE_SECTORS  = set()   # 一時的に全セクター有効化
EXCLUDE_SYMBOLS  = {"6526.T"}   # ソシオネクスト: 1年で4敗0勝 → 除外

# 【優先】構造的強セクター（ポートフォリオ選択スコア+10ボーナス）
#   銀行:  BOJ 0.75%→利上げ継続。MUFG/SMFG/みずほ全社最高益ペース
#   重工:  防衛予算2年前倒しでGDP2%達成。三菱重工+137%（バックログ10.7兆）
#   保険:  金利上昇＋円安で運用益拡大
#   建設:  国内需要・防衛インフラ・震災対応
#   電力/ガス: 内需ディフェンシブ、地政学リスク耐性高
#
PREFER_SECTORS   = {"銀行", "保険", "重工", "建設", "電力", "ガス"}

# 【最重要銘柄】分析で特定した高確信度銘柄（スコア追加+20ボーナス）
#   8316 SMFG     : 当期純利益 +29% YoY、最高値更新
#   7011 三菱重工  : バックログ10.7兆、ATH ¥5,208 からの押し目
#   7013 IHI      : 航空エンジン+防衛、時価総額3.5年で6倍
#   8035 TEL      : PER31x（6857の半値）、CEO「2026年は最高の年」
#   8766 東京海上  : 保険×金利上昇、安定配当
#   8316.T 重複回避のため .T 付きで管理
FOCUS_SYMBOLS    = {"8316.T", "7011.T", "7013.T", "8035.T", "8766.T"}

# 【半導体注意】6857 アドバンテスト: PER54x、3月下落を主導 → 現状は様子見
# PREFER_SECTORSから半導体を除外（高VI時は真っ先に売られる）

# ── インジケーター計算（VCP × 相対強度） ──────────────────────────
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # ── トレンド系 ──────────────────────────────────────────
    ema21 = c.ewm(span=EMA21_PERIOD, adjust=False).mean()
    ma50  = c.rolling(MA50_PERIOD).mean()
    ma75  = c.rolling(75).mean()
    ma200 = c.rolling(200).mean()

    # ── ATR(14) ──────────────────────────────────────────────
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    atr_pct = atr / c.replace(0, np.nan) * 100

    # ── 正規化TR（ボラ収縮判定用） ────────────────────────────
    # tr_norm: 当日のTR を終値で割ったボラ率（ATRと違いスムージングなし）
    tr_norm     = tr / c.replace(0, np.nan)
    # ★ .shift(1) で「前日時点」の平均を使う
    #    ブレイクアウト当日は TR が大きく tr_norm_5d が膨らむため、
    #    シフトしないと compress_ok が必ずFalse になりシグナルが消える
    tr_norm_5d  = tr_norm.rolling(5).mean().shift(1)   # 前日時点の5日平均
    tr_norm_21d = tr_norm.rolling(21).mean().shift(1)  # 前日時点の21日平均
    # compress < ATR_COMPRESS → ブレイクアウト前にボラが収縮していた
    atr_compress = tr_norm_5d / tr_norm_21d.replace(0, np.nan)

    # ── VCPウィンドウ高値 ──────────────────────────────────
    # 前日時点の直近 VCP_WINDOW 日最高終値 → これを本日終値が上抜けたらブレイクアウト
    high_vcp = c.rolling(VCP_WINDOW).max().shift(1)

    # ── 出来高 ────────────────────────────────────────────
    vol_ma    = v.rolling(VOL_MA_PERIOD).mean()
    vol_ratio = v / vol_ma.replace(0, np.nan)

    # ── RSI(14) ───────────────────────────────────────────
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(span=14, adjust=False).mean()
    rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # ── MA乖離率（EMA21基準） ──────────────────────────────
    ma_dev = (c - ema21) / ema21.replace(0, np.nan) * 100

    # ── 25日高値ブレイク（表示用） ─────────────────────────
    new_high25 = c > h.rolling(25).max().shift(1)

    # ── Entry シグナル（VCP ブレイクアウト） ─────────────────
    # 条件1: 長期トレンド（終値 > MA200）
    # ※ MA50>MA200 は修正相場で多くの銘柄がデスクロスするため除外
    trend_ok    = c > ma200
    # 条件2: 短期ブレイクアウト（終値が前VCP_WINDOW日の最高終値を上抜け）
    breakout_ok = c > high_vcp
    # 条件3: 出来高急増（機関投資家の本格参入）
    vol_ok      = v > vol_ma * FILTER_VOL_SURGE
    # ※ compress_ok は計算・表示のみ。シグナルには使わない
    df = df.copy()
    df["entry_sig"] = trend_ok & breakout_ok & vol_ok

    # ── Exit シグナル（EMA21 下抜け） ─────────────────────
    # 終値が EMA21 を下から上抜けていた → 今日下抜け（短期トレンド転換）
    df["exit_sig"] = (c < ema21) & (c.shift(1) >= ema21.shift(1))

    # ── 出力列 ────────────────────────────────────────────
    df["ema21"]        = ema21
    df["ma50"]         = ma50
    df["ma75"]         = ma75
    df["ma200"]        = ma200
    df["atr"]          = atr
    df["atr_pct"]      = atr_pct
    df["atr_compress"] = atr_compress
    df["high_vcp"]     = high_vcp
    df["vol_ma"]       = vol_ma
    df["vol_ratio"]    = vol_ratio
    df["rsi"]          = rsi
    df["ma_dev"]       = ma_dev
    df["new_high25"]   = new_high25
    # 後方互換エイリアス（表示・メタデータ参照用）
    df["ma25"]         = ema21
    df["ma25_dev"]     = ma_dev
    df["above_ma50"]   = c > ma50

    return df


# ── データ取得（メインスレッドから順次） ──────────────────
def fetch_df(symbol: str, backtest_days: int = BACKTEST_DAYS) -> pd.DataFrame | None:
    """バックテスト期間 + 指標計算バッファ分を取得。
    MA200計算のため 200日 + 余裕30日 をバッファとして確保。"""
    buf_days  = 200 + 30          # MA200 が主要なボトルネック
    total_cal = int((backtest_days + buf_days) * 1.5)

    if   total_cal <= 180:  period = "6mo"
    elif total_cal <= 365:  period = "1y"
    elif total_cal <= 730:  period = "2y"
    elif total_cal <= 1095: period = "3y"
    elif total_cal <= 1825: period = "5y"
    else:                   period = "max"

    try:
        raw = yf.download(symbol, period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        min_needed = 200 + VOL_MA_PERIOD + VCP_WINDOW + EMA21_PERIOD
        if len(raw) < min_needed:
            return None
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


# ── 日経平均データ取得 ──────────────────────────────────────
def fetch_nikkei(backtest_days: int) -> pd.DataFrame | None:
    """^N225 を取得し 25日MA と 地合いフラグを付与。"""
    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)
    if   total_cal <= 180:  period = "6mo"
    elif total_cal <= 365:  period = "1y"
    elif total_cal <= 730:  period = "2y"
    elif total_cal <= 1095: period = "3y"
    elif total_cal <= 1825: period = "5y"
    else:                   period = "max"
    try:
        raw = yf.download("^N225", period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["close"]].dropna()
        raw["ma25"] = raw["close"].rolling(25).mean()
        return raw
    except Exception:
        return None


def _nikkei_trend(nikkei_df: pd.DataFrame | None, dt: pd.Timestamp) -> bool | None:
    """指定日の日経平均が 25MA より上か（上=リスクオン）。"""
    if nikkei_df is None:
        return None
    # dtより前の直近行を使用
    past = nikkei_df[nikkei_df.index <= dt]
    if past.empty:
        return None
    row = past.iloc[-1]
    if pd.isna(row["ma25"]):
        return None
    return bool(row["close"] > row["ma25"])


# ── 1銘柄バックテスト ────────────────────────────────────────
def run_backtest(symbol: str, name: str, df: pd.DataFrame,
                 backtest_days: int, nikkei_df: pd.DataFrame | None = None) -> dict | None:
    # セクター除外 / 個別銘柄除外
    if SECTOR.get(symbol, "その他") in EXCLUDE_SECTORS:
        return None
    if symbol in EXCLUDE_SYMBOLS:
        return None

    df = calc_indicators(df)

    # 相対強度（日経対比）を全期間で計算してから期間を切り出す
    if nikkei_df is not None:
        nk_c = nikkei_df["close"].reindex(df.index, method="ffill")
        df["rs_diff"] = (df["close"].pct_change(RS_LOOKBACK)
                         - nk_c.pct_change(RS_LOOKBACK))
    else:
        df["rs_diff"] = 0.0  # Nikkeiデータなし → RSフィルターをスキップ

    cutoff    = pd.Timestamp(datetime.today() - timedelta(days=backtest_days))
    df_target = df[df.index >= cutoff].copy()

    if len(df_target) < 5:
        return None

    in_pos      = False
    cash        = float(INITIAL_CASH)
    trades      = []
    entry_price = trail_stop = 0.0
    entry_dt    = None
    qty         = 0

    for i in range(len(df_target)):
        row  = df_target.iloc[i]
        prev = df_target.iloc[i - 1] if i > 0 else row
        dt   = df_target.index[i]

        if pd.isna(row["ema21"]) or pd.isna(row["atr"]):
            continue

        # ── エグジット ──
        if in_pos:
            exit_p = exit_r = None
            if row["low"] <= trail_stop:
                exit_p = min(float(row["open"]), trail_stop)
                exit_r = "トレイリング"
            elif bool(prev["exit_sig"]):
                exit_p = float(row["open"])
                exit_r = "EMA21割れ"
            if exit_p is not None:
                pnl   = (exit_p - entry_price) * qty
                cash += exit_p * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     dt,
                    "entry_price": entry_price,
                    "exit_price":  exit_p,
                    "qty":         qty,
                    "pnl":         pnl,
                    "hold_days":   (dt - entry_dt).days,
                    "reason":      exit_r,
                    **entry_meta,
                })
                in_pos = False
            else:
                candidate = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
                if candidate > trail_stop:
                    trail_stop = candidate

        # ── エントリー ──
        if not in_pos and bool(prev["entry_sig"]):
            if not _check_entry_filters(prev):
                continue
            atr_v = float(prev["atr"])
            op    = float(row["open"])
            if op > 0:
                q    = max(min(int(POSITION_SIZE / op), MAX_QTY), 0)
                cost = op * q
                if q > 0 and cost <= cash:
                    cash        -= cost
                    entry_price  = float(row["open"])
                    trail_stop   = entry_price - float(row["atr"]) * ATR_TRAIL_MULT
                    entry_dt     = dt
                    qty          = q
                    in_pos       = True
                    # ── エントリー時の詳細メタデータを保存 ──
                    _ep  = entry_price
                    entry_meta = {
                        "sector":       SECTOR.get(symbol, "その他"),
                        "price_tier":   "高位" if _ep >= 5000 else "中位" if _ep >= 1000 else "低位",
                        "ma25_dev":     float(prev["ma_dev"])       if not pd.isna(prev["ma_dev"])       else 0.0,
                        "above_ma75":   bool(_ep > float(prev["ma75"]))  if not pd.isna(prev["ma75"])  else False,
                        "above_ma200":  bool(_ep > float(prev["ma200"])) if not pd.isna(prev["ma200"]) else False,
                        "new_high25":   bool(prev["new_high25"]),
                        "atr_val":      float(prev["atr"]),
                        "atr_pct":      float(prev["atr_pct"])      if not pd.isna(prev["atr_pct"])      else 0.0,
                        "atr_compress": float(prev["atr_compress"]) if not pd.isna(prev["atr_compress"]) else 1.0,
                        "vol":          float(prev["volume"]),
                        "vol_ratio":    float(prev["vol_ratio"])    if not pd.isna(prev["vol_ratio"])    else 0.0,
                        "rs_diff":      float(prev["rs_diff"])      if not pd.isna(prev["rs_diff"])      else 0.0,
                        "rsi":          float(prev["rsi"])           if not pd.isna(prev["rsi"])          else 0.0,
                        "nikkei_up":    _nikkei_trend(nikkei_df, dt),
                    }

    # 未決済を最終日終値で仮決済
    open_pos = None
    if in_pos:
        lp  = float(df_target.iloc[-1]["close"])
        pnl = (lp - entry_price) * qty
        cash += lp * qty
        open_pos = {
            "entry_dt":    entry_dt,
            "exit_dt":     df_target.index[-1],
            "entry_price": entry_price,
            "exit_price":  lp,
            "qty":         qty,
            "pnl":         pnl,
            "hold_days":   (df_target.index[-1] - entry_dt).days,
            "reason":      "保有中（最終日終値）",
            **entry_meta,
        }
        trades.append(open_pos)

    if not trades:
        return None

    total    = cash - INITIAL_CASH
    ret_pct  = total / INITIAL_CASH * 100
    wins     = [t for t in trades if t["pnl"] > 0]
    losses   = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_hold = sum(t["hold_days"] for t in trades) / len(trades)
    pf       = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))

    last      = df_target.iloc[-1]
    last_close = float(last["close"])
    last_hist  = float(last["rs_diff"]) if not pd.isna(last["rs_diff"]) else 0.0
    last_ma25  = float(last["ema21"])     if not pd.isna(last["ema21"])     else 0.0

    return {
        "symbol":    symbol,
        "name":      name,
        "trades":    len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  win_rate,
        "pf":        pf,
        "total":     total,
        "ret_pct":   ret_pct,
        "avg_hold":  avg_hold,
        "trade_log": trades,
        "open_pos":  open_pos is not None,
        "last_close": last_close,
        "last_hist":  last_hist,
        "last_ma25":  last_ma25,
    }


# ── ポートフォリオバックテスト（最大MAX_POSITIONS同時保有）────
def _check_entry_filters(prev: pd.Series) -> bool:
    """VCP エントリーフィルター判定（共通）
    calc_indicators() の entry_sig が True のあと、追加条件を確認する。
    ・MA200 上（長期トレンド確認）
    ・RSI 過熱・売られすぎ排除
    ・相対強度（rs_diff 列が存在する場合）
    """
    _rsi    = float(prev["rsi"])    if not pd.isna(prev["rsi"])    else 0.0
    _close  = float(prev["close"])
    _ma200  = float(prev["ma200"])  if not pd.isna(prev["ma200"])  else None
    _above200 = (_ma200 is not None) and (_close > _ma200)
    # 相対強度フィルター（rs_diff 列は run_backtest/run_portfolio で追加）
    _rs = (float(prev["rs_diff"])
           if ("rs_diff" in prev.index and not pd.isna(prev["rs_diff"]))
           else RS_MIN)  # 列なければギリギリ通過扱い
    return (
        (not FILTER_MA200_ABOVE or _above200) and
        FILTER_RSI_MIN <= _rsi <= FILTER_RSI_MAX and
        _rs >= RS_MIN
    )


def run_portfolio(stock_data_map: dict, backtest_days: int,
                  nikkei_df: pd.DataFrame | None = None) -> dict | None:
    """MAX_POSITIONS 銘柄同時保有のポートフォリオシミュレーション。
    ・優先セクター優先 → 相対強度(RS)高い順で最大N銘柄に入る
    ・日経MA25割れ時は MAX_POSITIONS_BEAR に縮小
    ・+HALF_PROFIT_PCT% で半分利確
    ・-HARD_STOP_PCT% で即損切り"""
    cutoff = pd.Timestamp(datetime.today() - timedelta(days=backtest_days))

    # インジケーター計算 + 期間絞り込み
    dfs: dict[str, tuple[str, pd.DataFrame]] = {}
    for sym, (name, df) in stock_data_map.items():
        if SECTOR.get(sym, "その他") in EXCLUDE_SECTORS:
            continue
        if sym in EXCLUDE_SYMBOLS:
            continue
        df_i = calc_indicators(df)
        # 相対強度（日経対比）を全期間で計算
        if nikkei_df is not None:
            nk_c = nikkei_df["close"].reindex(df_i.index, method="ffill")
            df_i["rs_diff"] = (df_i["close"].pct_change(RS_LOOKBACK)
                               - nk_c.pct_change(RS_LOOKBACK))
        else:
            df_i["rs_diff"] = 0.0
        df_t = df_i[df_i.index >= cutoff]
        if len(df_t) >= 5:
            dfs[sym] = (name, df_t)
    if not dfs:
        return None

    # 全取引日を統合してソート
    all_dates = sorted({d for _, df_t in dfs.values() for d in df_t.index})

    cash      = float(INITIAL_CASH)
    positions: dict = {}   # sym → position dict
    trades    = []

    for dt in all_dates:
        # ── エグジット ──────────────────────────────────────
        for sym in list(positions.keys()):
            name, df_t = dfs[sym]
            if dt not in df_t.index:
                continue
            loc = df_t.index.get_loc(dt)
            if loc == 0:
                continue
            row  = df_t.iloc[loc]
            prev = df_t.iloc[loc - 1]
            pos  = positions[sym]

            if pd.isna(row["ema21"]) or pd.isna(row["atr"]):
                continue

            exit_p = exit_r = None
            lo = float(row["low"])
            op = float(row["open"])

            # 優先①: 即損切り -HARD_STOP_PCT%
            if lo <= pos["hard_stop"]:
                exit_p = min(op, pos["hard_stop"])
                exit_r = f"損切り(-{HARD_STOP_PCT:.0f}%)"
            # 優先②: EMA21 下抜け（短期トレンド転換）
            elif bool(prev["exit_sig"]):
                exit_p = op
                exit_r = "EMA21割れ"
            # 優先③: ATR トレイリング
            elif lo <= pos["trail_stop"]:
                exit_p = min(op, pos["trail_stop"])
                exit_r = "トレイリング"

            if exit_p is not None:
                qty = pos["qty"]
                pnl = (exit_p - pos["entry_price"]) * qty
                cash += exit_p * qty
                trades.append({
                    "symbol": sym, "name": name,
                    "entry_dt": pos["entry_dt"], "exit_dt": dt,
                    "entry_price": pos["entry_price"], "exit_price": exit_p,
                    "qty": qty, "pnl": pnl,
                    "hold_days": (dt - pos["entry_dt"]).days,
                    "reason": exit_r,
                    **pos["meta"],
                })
                del positions[sym]
                continue

            # 半分利確 +HALF_PROFIT_PCT%（クローズで判定）
            if not pos["half_taken"]:
                cl = float(row["close"])
                gain = (cl - pos["entry_price"]) / pos["entry_price"] * 100
                if gain >= HALF_PROFIT_PCT:
                    hq = pos["qty"] // 2
                    if hq > 0:
                        cash += cl * hq
                        pnl_h = (cl - pos["entry_price"]) * hq
                        trades.append({
                            "symbol": sym, "name": name,
                            "entry_dt": pos["entry_dt"], "exit_dt": dt,
                            "entry_price": pos["entry_price"], "exit_price": cl,
                            "qty": hq, "pnl": pnl_h,
                            "hold_days": (dt - pos["entry_dt"]).days,
                            "reason": f"半分利確(+{gain:.1f}%)",
                            **pos["meta"],
                        })
                        pos["qty"] -= hq
                        pos["half_taken"] = True

            # ATR トレイリング更新
            cand = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
            if cand > pos["trail_stop"]:
                pos["trail_stop"] = cand

        # ── エントリー ──────────────────────────────────────
        slots = MAX_POSITIONS - len(positions)
        if slots <= 0:
            continue

        candidates = []
        for sym, (name, df_t) in dfs.items():
            if sym in positions or dt not in df_t.index:
                continue
            loc = df_t.index.get_loc(dt)
            if loc == 0:
                continue
            row  = df_t.iloc[loc]
            prev = df_t.iloc[loc - 1]
            if pd.isna(prev["entry_sig"]) or not bool(prev["entry_sig"]):
                continue
            if pd.isna(row["atr"]):
                continue
            if not _check_entry_filters(prev):
                continue
            # スコアリング: 最重要銘柄(+20) > 優先セクター(+10) > 相対強度(RS×100)
            focus_bonus  = 20.0 if sym in FOCUS_SYMBOLS else 0.0
            sector_bonus = 10.0 if SECTOR.get(sym, "") in PREFER_SECTORS else 0.0
            rs_score = float(prev["rs_diff"]) * 100 if not pd.isna(prev["rs_diff"]) else 0.0
            score = focus_bonus + sector_bonus + rs_score
            candidates.append((score, sym, name, row, prev))

        # 優先セクター → RS高い順 → slots 分だけ入る
        # 日経地合い（MA25割れ）でMAX_POSITIONSを縮小
        nk_now = _nikkei_trend(nikkei_df, dt)
        max_pos = MAX_POSITIONS_BEAR if nk_now is False else MAX_POSITIONS
        slots   = max_pos - len(positions)
        if slots <= 0:
            continue
        candidates.sort(reverse=True, key=lambda x: x[0])
        for _, sym, name, row, prev in candidates[:slots]:
            ep    = float(row["open"])
            atr_v = float(prev["atr"])
            q     = max(min(int(POSITION_SIZE / ep) if ep > 0 else 0, MAX_QTY), 0)
            cost  = ep * q
            if q <= 0 or cost > cash:
                continue
            cash -= cost
            meta = {
                "sector":       SECTOR.get(sym, "その他"),
                "price_tier":   "高位" if ep >= 5000 else "中位" if ep >= 1000 else "低位",
                "ma25_dev":     float(prev["ma_dev"])       if not pd.isna(prev["ma_dev"])       else 0.0,
                "above_ma75":   bool(ep > float(prev["ma75"]))  if not pd.isna(prev["ma75"])  else False,
                "above_ma200":  bool(ep > float(prev["ma200"])) if not pd.isna(prev["ma200"]) else False,
                "new_high25":   bool(prev["new_high25"]),
                "atr_val":      atr_v,
                "atr_pct":      float(prev["atr_pct"])      if not pd.isna(prev["atr_pct"])      else 0.0,
                "atr_compress": float(prev["atr_compress"]) if not pd.isna(prev["atr_compress"]) else 1.0,
                "vol":          float(prev["volume"]),
                "vol_ratio":    float(prev["vol_ratio"])    if not pd.isna(prev["vol_ratio"])    else 0.0,
                "rs_diff":      float(prev["rs_diff"])      if not pd.isna(prev["rs_diff"])      else 0.0,
                "rsi":          float(prev["rsi"])           if not pd.isna(prev["rsi"])          else 0.0,
                "nikkei_up":   _nikkei_trend(nikkei_df, dt),
            }
            positions[sym] = {
                "name":       name,
                "entry_price": ep,
                "qty":         q,
                "hard_stop":   ep * (1 - HARD_STOP_PCT / 100),
                "trail_stop":  ep - atr_v * ATR_TRAIL_MULT,
                "half_taken":  False,
                "entry_dt":    dt,
                "meta":        meta,
            }

    # 未決済 → 最終日終値で仮決済
    for sym, pos in positions.items():
        name, df_t = dfs[sym]
        lp  = float(df_t.iloc[-1]["close"])
        pnl = (lp - pos["entry_price"]) * pos["qty"]
        cash += lp * pos["qty"]
        trades.append({
            "symbol": sym, "name": name,
            "entry_dt": pos["entry_dt"], "exit_dt": df_t.index[-1],
            "entry_price": pos["entry_price"], "exit_price": lp,
            "qty": pos["qty"], "pnl": pnl,
            "hold_days": (df_t.index[-1] - pos["entry_dt"]).days,
            "reason": "保有中（最終日終値）★",
            **pos["meta"],
        })

    if not trades:
        return None

    total    = cash - INITIAL_CASH
    ret_pct  = total / INITIAL_CASH * 100
    wins     = [t for t in trades if t["pnl"] > 0]
    losses   = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_hold = sum(t["hold_days"] for t in trades) / len(trades)
    pf       = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))

    return {
        "trades":    len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  win_rate,
        "pf":        pf,
        "total":     total,
        "ret_pct":   ret_pct,
        "avg_hold":  avg_hold,
        "trade_log": trades,
    }


def print_portfolio(pr: dict, backtest_days: int, label: str) -> None:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")
    sign  = "+" if pr["total"] >= 0 else ""
    pf_s  = "∞" if pr["pf"] == float("inf") else f"{pr['pf']:.2f}"

    trades = pr["trade_log"]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    win_hold  = sum(t["hold_days"] for t in wins)   / len(wins)   if wins   else 0
    loss_hold = sum(t["hold_days"] for t in losses) / len(losses) if losses else 0
    max_win   = max((t["pnl"] for t in wins), default=0)
    max_win_r = max(
        ((t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100 for t in wins),
        default=0,
    )
    nk_up_tr  = [t for t in trades if t.get("nikkei_up") is True]
    nk_dn_tr  = [t for t in trades if t.get("nikkei_up") is False]
    nk_up_win = sum(1 for t in nk_up_tr if t["pnl"] > 0)
    nk_dn_win = sum(1 for t in nk_dn_tr if t["pnl"] > 0)

    print()
    print("═" * 72)
    print(f"  ポートフォリオ結果  最大{MAX_POSITIONS}銘柄同時保有  "
          f"[{since} ～ {today}]（直近{label}）")
    print("═" * 72)
    print(f"  資金: {INITIAL_CASH:,}円  →  {INITIAL_CASH + pr['total']:,.0f}円  "
          f"（{sign}{pr['total']:,.0f}円  {sign}{pr['ret_pct']:.2f}%）")
    print(f"  トレード: {pr['trades']}回  勝: {pr['wins']}  負: {pr['losses']}  "
          f"勝率: {pr['win_rate']:.1f}%  PF: {pf_s}")
    print(f"  平均保有  勝ち: {win_hold:.1f}日  /  負け: {loss_hold:.1f}日")
    print(f"  最大勝ちトレード: {max_win:+,.0f}円  （{max_win_r:+.2f}%）")
    if nk_up_tr or nk_dn_tr:
        up_wr = f"{nk_up_win/len(nk_up_tr)*100:.0f}%" if nk_up_tr else "--"
        dn_wr = f"{nk_dn_win/len(nk_dn_tr)*100:.0f}%" if nk_dn_tr else "--"
        print(f"  エントリー時日経  ↑上昇: {len(nk_up_tr)}回 勝率{up_wr}  "
              f"↓下落: {len(nk_dn_tr)}回 勝率{dn_wr}")
    prefer_s = "、".join(sorted(PREFER_SECTORS))
    excl_s2  = "、".join(sorted(EXCLUDE_SECTORS))
    print(f"  【ルール】 +{HALF_PROFIT_PCT:.0f}%で半分利確  "
          f"-{HARD_STOP_PCT:.0f}%で即損切り  "
          f"地合い良好:{MAX_POSITIONS}銘柄 / 日経MA25割れ:{MAX_POSITIONS_BEAR}銘柄")
    print(f"  【優先】 {prefer_s}")
    print(f"  【除外】 {excl_s2}")
    print()

    if trades:
        print(f"  {'#':<3} {'銘柄':<18} {'エントリー':>10} {'エグジット':>10} "
              f"{'買値':>8} {'売値':>8} {'損益':>10}  決済理由")
        print("  " + "─" * 84)
        for i, t in enumerate(trades, 1):
            pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            label_s = f"{t['name']}({t['symbol']})"
            nk_s    = ("↑" if t.get("nikkei_up") is True
                       else "↓" if t.get("nikkei_up") is False else "-")
            print(f"  {i:<3} {label_s:<18} "
                  f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
                  f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
                  f"{t['entry_price']:>8,.0f} {t['exit_price']:>8,.0f} "
                  f"{t['pnl']:>+10,.0f}({pnl_pct:>+5.1f}%)  "
                  f"{t['reason']}  日経{nk_s}")
            print(f"       業種:{t.get('sector','--'):5s}  "
                  f"MA乖離:{t.get('ma25_dev',0):+4.1f}%  "
                  f"ATR:{t.get('atr_pct',0):.1f}%  "
                  f"収縮:{t.get('atr_compress',1.0):.2f}  "
                  f"出来高比:{t.get('vol_ratio',0):.1f}x  "
                  f"RS:{t.get('rs_diff',0):+.3f}  "
                  f"RSI:{t.get('rsi',0):.0f}  "
                  f"保有{t['hold_days']}日")
        print("  " + "─" * 84)
    print()


# ── 単銘柄詳細表示 ──────────────────────────────────────────
def print_detail(r: dict, backtest_days: int) -> None:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")
    sign  = "+" if r["total"] >= 0 else ""
    pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"

    print()
    print("=" * 68)
    print(f"  {r['name']}({r['symbol']})  [{since} ～ {today}]")
    print("=" * 68)
    trades = r["trade_log"]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    win_avg_hold  = (sum(t["hold_days"] for t in wins)   / len(wins)   if wins   else 0)
    loss_avg_hold = (sum(t["hold_days"] for t in losses) / len(losses) if losses else 0)
    max_win_pnl   = max((t["pnl"] for t in wins), default=0)
    max_win_pct   = max(
        ((t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100 for t in wins),
        default=0
    )

    nk_trades  = [t for t in trades if t.get("nikkei_up") is not None]
    nk_up_tr   = [t for t in nk_trades if t.get("nikkei_up") is True]
    nk_dn_tr   = [t for t in nk_trades if t.get("nikkei_up") is False]
    nk_up_win  = sum(1 for t in nk_up_tr if t["pnl"] > 0)
    nk_dn_win  = sum(1 for t in nk_dn_tr if t["pnl"] > 0)

    print(f"  トレード数       : {r['trades']}回  （勝: {r['wins']}  負: {r['losses']}）")
    print(f"  勝率             : {r['win_rate']:.1f}%    プロフィットファクター: {pf_s}")
    print(f"  損益合計         : {sign}{r['total']:,.0f}円  （{sign}{r['ret_pct']:.2f}%）")
    print(f"  平均保有日数     : 勝ち {win_avg_hold:.1f}日  /  負け {loss_avg_hold:.1f}日"
          f"  （全体 {r['avg_hold']:.1f}日）")
    print(f"  最大勝ちトレード : {max_win_pnl:+,.0f}円  （{max_win_pct:+.2f}%）")
    if nk_trades:
        print(f"  エントリー時日経 : "
              f"↑上昇 {len(nk_up_tr)}回（勝{nk_up_win}勝率{nk_up_win/len(nk_up_tr)*100:.0f}%）  "
              f"↓下落 {len(nk_dn_tr)}回（勝{nk_dn_win}勝率{nk_dn_win/len(nk_dn_tr)*100:.0f}%）"
              if nk_up_tr and nk_dn_tr else
              f"↑上昇 {len(nk_up_tr)}回  ↓下落 {len(nk_dn_tr)}回")
    print(f"  現在値           : {r['last_close']:,.1f}円  "
          f"RS(日経対比): {r['last_hist']:+.2f}  EMA21: {r['last_ma25']:,.1f}  "
          f"トレンド: {'↑' if r['last_close'] > r['last_ma25'] else '↓'}")
    print()

    if r["trade_log"]:
        print(f"  {'#':<3} {'エントリー':>12} {'エグジット':>12} {'買値':>9} {'売値':>9} "
              f"{'株数':>5} {'損益':>10} {'保有日':>5}  決済理由")
        print("  " + "─" * 82)
        for i, t in enumerate(r["trade_log"], 1):
            mark = " ★" if t["reason"] == "保有中（最終日終値）" else ""
            pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            print(f"  {i:<3} {t['entry_dt'].strftime('%Y-%m-%d'):>12} "
                  f"{t['exit_dt'].strftime('%Y-%m-%d'):>12} "
                  f"{t['entry_price']:>9,.1f} {t['exit_price']:>9,.1f} "
                  f"{t['qty']:>5} {t['pnl']:>+10,.0f}({pnl_pct:>+5.1f}%) "
                  f"{t['hold_days']:>3}日  {t['reason']}{mark}")
            # ── エントリー時指標行 ──────────────────────────────
            nk  = ("↑リスクON" if t.get("nikkei_up") is True
                   else "↓リスクOFF" if t.get("nikkei_up") is False
                   else "日経:--")
            ma75_s  = "MA75↑" if t.get("above_ma75")  else "MA75↓"
            ma200_s = "MA200↑" if t.get("above_ma200") else "MA200↓"
            nh25_s  = "25H↑" if t.get("new_high25") else "25H-"
            print(f"       業種:{t.get('sector','--'):5s}  {t.get('price_tier','--'):2s}株"
                  f"  MA乖離:{t.get('ma25_dev', 0):+5.1f}%"
                  f"  {ma75_s}  {ma200_s}  {nh25_s}"
                  f"  ATR:{t.get('atr_pct', 0):.1f}%"
                  f"  収縮:{t.get('atr_compress', 1.0):.2f}"
                  f"  出来高比:{t.get('vol_ratio', 0):.1f}x"
                  f"  RS:{t.get('rs_diff', 0):+.3f}"
                  f"  RSI:{t.get('rsi', 0):.0f}"
                  f"  {nk}")
        print("  " + "─" * 82)
        if r["open_pos"]:
            print("  ★ = 現在保有中（最終日終値で仮決済）")
    print()


# ── ランキング表示 ──────────────────────────────────────────
def print_ranking(results: list[dict], backtest_days: int, top_n: int) -> None:
    since    = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today    = datetime.today().strftime("%Y-%m-%d")
    sorted_r = sorted(results, key=lambda x: x["ret_pct"], reverse=True)
    display  = sorted_r[:top_n]

    print()
    print("═" * 80)
    print(f"  VCP (Volatility Contraction Pattern) × 相対強度  "
          f"ランキング TOP{top_n}  [{since} ～ {today}]")
    print("═" * 80)
    print(f"  {'順位':<4} {'銘柄':<24} {'損益':>10} {'損益率':>8} {'勝率':>6} "
          f"{'PF':>5} {'取引':>4} {'平均保有':>7}  現在値")
    print("  " + "─" * 76)

    for rank, r in enumerate(display, 1):
        sign  = "+" if r["ret_pct"] >= 0 else ""
        trend = "↑" if r["last_close"] > r["last_ma25"] else "↓"
        rs_s   = f"{r['last_hist']:+.2f}"   # last_hist は rs_diff を格納
        pf_s   = "∞" if r["pf"] == float("inf") else f"{r['pf']:.1f}"
        bar    = ("▲" if r["ret_pct"] >= 0 else "▽") * min(int(abs(r["ret_pct"]) / 2), 12)

        label = f"{r['name']}({r['symbol']})"
        print(f"  {rank:<4} {label:<24} "
              f"{sign}{r['total']:>9,.0f}円 {sign}{r['ret_pct']:>6.1f}% "
              f"{r['win_rate']:>5.0f}% {pf_s:>5} {r['trades']:>3}回 "
              f"{r['avg_hold']:>5.1f}日  {r['last_close']:>7,.0f}{trend} "
              f"RS:{rs_s}  {bar}")

    print("  " + "─" * 76)
    total_tr   = sum(r["trades"] for r in results)
    profitable = sum(1 for r in results if r["ret_pct"] > 0)
    avg_ret    = sum(r["ret_pct"] for r in results) / len(results)
    no_sig_cnt = len(SYMBOLS) - len(results)

    # ── 全銘柄合算の詳細統計 ──
    all_trades = [t for r in results for t in r["trade_log"]]
    all_wins   = [t for t in all_trades if t["pnl"] > 0]
    all_losses = [t for t in all_trades if t["pnl"] <= 0]
    win_hold   = (sum(t["hold_days"] for t in all_wins)   / len(all_wins)   if all_wins   else 0)
    loss_hold  = (sum(t["hold_days"] for t in all_losses) / len(all_losses) if all_losses else 0)
    max_win    = max((t["pnl"] for t in all_wins), default=0)
    max_win_r  = max(
        ((t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100 for t in all_wins),
        default=0
    )
    nk_up_tr  = [t for t in all_trades if t.get("nikkei_up") is True]
    nk_dn_tr  = [t for t in all_trades if t.get("nikkei_up") is False]
    nk_up_win = sum(1 for t in nk_up_tr if t["pnl"] > 0)
    nk_dn_win = sum(1 for t in nk_dn_tr if t["pnl"] > 0)

    print(f"  スキャン: {len(SYMBOLS)}銘柄  取引あり: {len(results)}件  "
          f"取引なし: {no_sig_cnt}件  トレード計: {total_tr}回")
    print(f"  プラス銘柄: {profitable}/{len(results)}  平均損益率: {avg_ret:+.2f}%")
    print(f"  平均保有日数     勝ち: {win_hold:.1f}日  /  負け: {loss_hold:.1f}日")
    print(f"  最大勝ちトレード: {max_win:+,.0f}円  （{max_win_r:+.2f}%）")
    if nk_up_tr or nk_dn_tr:
        up_wr = f"{nk_up_win/len(nk_up_tr)*100:.0f}%" if nk_up_tr else "--"
        dn_wr = f"{nk_dn_win/len(nk_dn_tr)*100:.0f}%" if nk_dn_tr else "--"
        print(f"  エントリー時日経 ↑上昇: {len(nk_up_tr)}回 勝率{up_wr}  "
              f"↓下落: {len(nk_dn_tr)}回 勝率{dn_wr}")
    print()
    ma200_s = "MA200上のみ" if FILTER_MA200_ABOVE else "MA200不問"
    excl = "、".join(sorted(EXCLUDE_SECTORS)) if EXCLUDE_SECTORS else "なし"
    excl_sym = "、".join(sorted(EXCLUDE_SYMBOLS)) if EXCLUDE_SYMBOLS else "なし"
    print(f"  【戦略】 VCP (Volatility Contraction Pattern) × 相対強度")
    print(f"  【エントリー】 ①EMA21>MA50>MA200 ＋ 終値>EMA21（トレンド一致）")
    print(f"              ②直近5日TR% < 21日平均TR%×{ATR_COMPRESS}（ボラ収縮）")
    print(f"              ③終値 > 前{VCP_WINDOW}日間最高終値（ブレイクアウト）")
    print(f"              ④出来高 > {VOL_MA_PERIOD}日平均×{FILTER_VOL_SURGE}倍（機関投資家参入）")
    print(f"              ⑤RS(日経対比{RS_LOOKBACK}日) ≥ {RS_MIN:+.0%}  RSI {FILTER_RSI_MIN:.0f}〜{FILTER_RSI_MAX:.0f}  {ma200_s}")
    print(f"  【決済】 ①ATRトレイリング×{ATR_TRAIL_MULT}  ②EMA21下抜け  ③損切り-{HARD_STOP_PCT:.0f}%  ④+{HALF_PROFIT_PCT:.0f}%半分利確")
    print(f"  【資金】 {INITIAL_CASH:,}円（総枠）  1銘柄 {POSITION_SIZE:,}円固定")
    print(f"  【除外セクター】 {excl}  【除外銘柄】 {excl_sym}")
    print()

    # ── 全トレード詳細（ランキング上位銘柄） ────────────────
    print("─" * 80)
    print(f"  全トレード詳細  （上位 {top_n} 銘柄）")
    print("─" * 80)
    hdr1 = (f"  {'銘柄':<18} {'エントリー':>10} {'エグジット':>10} "
            f"{'買値':>8} {'売値':>8} {'損益':>9}{'損益率':>7}  決済理由")
    hdr2 = (f"  {'':18} {'業種':>5}{'株価帯':>4}"
            f"  MA乖離  MA75  MA200  25H"
            f"  ATR%  収縮  出来高比  RS%    RSI  日経")
    print(hdr1)
    print(hdr2)
    print("  " + "─" * 78)

    for r in display:
        for t in r["trade_log"]:
            pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            mark    = "★" if t["reason"] == "保有中（最終日終値）" else " "
            label   = f"{r['name']}({r['symbol']})"
            nk_s    = ("↑ON" if t.get("nikkei_up") is True
                       else "↓OFF" if t.get("nikkei_up") is False
                       else " -- ")
            ma75_s  = "↑" if t.get("above_ma75")  else "↓"
            ma200_s = "↑" if t.get("above_ma200") else "↓"
            nh25_s  = "↑" if t.get("new_high25")  else "-"
            print(f" {mark}{label:<18} "
                  f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
                  f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
                  f"{t['entry_price']:>8,.0f} {t['exit_price']:>8,.0f} "
                  f"{t['pnl']:>+9,.0f}{pnl_pct:>+6.1f}%  {t['reason']}")
            print(f"  {'':18} {t.get('sector','--'):>5s}{t.get('price_tier','--'):>4s}"
                  f"  {t.get('ma25_dev',0):>+5.1f}%"
                  f"  MA75{ma75_s}  MA200{ma200_s}  25H{nh25_s}"
                  f"  {t.get('atr_pct',0):>4.1f}%"
                  f"  {t.get('vol_ratio',0):>5.1f}x"
                  f"  {t.get('atr_compress',1.0):>4.2f}"
                  f"  {t.get('rs_diff',0)*100:>+5.1f}%"
                  f"  {t.get('rsi',0):>5.1f}"
                  f"  {nk_s}")
        print("  " + "·" * 60)
    print()


# ── HTML レポート生成 ───────────────────────────────────────
def generate_html(results: list[dict], backtest_days: int, label: str) -> Path:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")
    sorted_r = sorted(results, key=lambda x: x["ret_pct"], reverse=True)

    # ── チャート用データ（全銘柄） ──
    chart_labels = [f"{r['name']}" for r in sorted_r]
    chart_vals   = [round(r["ret_pct"], 2)  for r in sorted_r]
    chart_colors = [
        "rgba(34,197,94,0.8)"  if v >= 0 else "rgba(239,68,68,0.8)"
        for v in chart_vals
    ]

    # ── サマリー統計 ──
    total_tr   = sum(r["trades"]  for r in results)
    profitable = sum(1 for r in results if r["ret_pct"] > 0)
    avg_ret    = sum(r["ret_pct"] for r in results) / len(results)

    # ── 銘柄行HTML ──
    rows_html = ""
    for rank, r in enumerate(sorted_r, 1):
        pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        sign  = "+" if r["ret_pct"] >= 0 else ""
        cls   = "pos" if r["ret_pct"] >= 0 else "neg"
        trend = "↑" if r["last_close"] > r["last_ma25"] else "↓"

        # トレード詳細行
        trade_rows = ""
        for t in r["trade_log"]:
            pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            t_cls   = "pos" if t["pnl"] >= 0 else "neg"
            nk_s    = ("↑ON" if t.get("nikkei_up") is True
                       else "↓OFF" if t.get("nikkei_up") is False else "--")
            ma75_s  = "↑" if t.get("above_ma75")  else "↓"
            ma200_s = "↑" if t.get("above_ma200") else "↓"
            nh25_s  = "↑" if t.get("new_high25")  else "-"
            mark    = " ★" if t["reason"] == "保有中（最終日終値）" else ""
            trade_rows += f"""
            <tr class="trade-row">
              <td>{t['entry_dt'].strftime('%Y-%m-%d')}</td>
              <td>{t['exit_dt'].strftime('%Y-%m-%d')}</td>
              <td class="num">{t['entry_price']:,.0f}</td>
              <td class="num">{t['exit_price']:,.0f}</td>
              <td class="num {t_cls}">{t['pnl']:+,.0f}円<br><small>{pnl_pct:+.1f}%</small></td>
              <td class="num">{t['hold_days']}日</td>
              <td>{t['reason']}{mark}</td>
              <td>{t.get('sector','--')}</td>
              <td class="num">{t.get('ma25_dev',0):+.1f}%</td>
              <td>MA75{ma75_s} MA200{ma200_s} 25H{nh25_s}</td>
              <td class="num">{t.get('atr_pct',0):.1f}%</td>
              <td class="num">{t.get('vol_ratio',0):.1f}x</td>
              <td class="num">{t.get('rs_diff',0)*100:+.1f}%</td>
              <td class="num">{t.get('rsi',0):.0f}</td>
              <td>{nk_s}</td>
            </tr>"""

        rows_html += f"""
        <tr class="stock-row {cls}" onclick="toggleTrades('{r['symbol']}')">
          <td class="rank">{rank}</td>
          <td class="name">{r['name']}<br><small>{r['symbol']}</small></td>
          <td class="num {cls}">{sign}{r['total']:,.0f}円</td>
          <td class="num {cls}">{sign}{r['ret_pct']:.2f}%</td>
          <td class="num">{r['win_rate']:.0f}%</td>
          <td class="num">{pf_s}</td>
          <td class="num">{r['trades']}</td>
          <td class="num">{r['avg_hold']:.1f}日</td>
          <td class="num">{r['last_close']:,.0f} {trend}</td>
          <td class="expand-btn">▼</td>
        </tr>
        <tr class="trades-detail" id="detail-{r['symbol']}" style="display:none">
          <td colspan="10">
            <table class="inner-table">
              <thead>
                <tr>
                  <th>エントリー</th><th>エグジット</th><th>買値</th><th>売値</th>
                  <th>損益</th><th>保有</th><th>決済理由</th><th>業種</th>
                  <th>MA乖離</th><th>MA位置</th><th>ATR%</th><th>出来高比</th>
                  <th>RS%</th><th>RSI</th><th>日経</th>
                </tr>
              </thead>
              <tbody>{trade_rows}</tbody>
            </table>
          </td>
        </tr>"""

    # ── フィルター表示 ──
    excl_s = "、".join(sorted(EXCLUDE_SECTORS)) if EXCLUDE_SECTORS else "なし"
    excl_sym_s = "、".join(sorted(EXCLUDE_SYMBOLS)) if EXCLUDE_SYMBOLS else "なし"
    ma200_fs = "MA200上のみ" if FILTER_MA200_ABOVE else "MA200不問"
    filter_html = (
        f"VCP(ボラ収縮{ATR_COMPRESS}×＋ブレイクアウト{VCP_WINDOW}日)　"
        f"出来高 >{FILTER_VOL_SURGE}x　RSI {FILTER_RSI_MIN:.0f}〜{FILTER_RSI_MAX:.0f}　"
        f"RS(日経対比{RS_LOOKBACK}日) ≥{RS_MIN:+.0%}　{ma200_fs}　"
        f"除外セクター: {excl_s}　除外銘柄: {excl_sym_s}"
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VCPバックテスト {label} — {today}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Hiragino Kaku Gothic ProN', Meiryo, sans-serif;
          background: #0f172a; color: #e2e8f0; font-size: 13px; }}
  h1 {{ padding: 18px 24px 6px; font-size: 1.3rem; color: #f1f5f9; }}
  .subtitle {{ padding: 0 24px 16px; color: #94a3b8; font-size: 0.85rem; }}
  .stats {{ display: flex; gap: 16px; padding: 0 24px 20px; flex-wrap: wrap; }}
  .stat-card {{ background: #1e293b; border-radius: 8px; padding: 12px 20px;
                min-width: 130px; }}
  .stat-card .val {{ font-size: 1.5rem; font-weight: 700; color: #38bdf8; }}
  .stat-card .lbl {{ font-size: 0.75rem; color: #64748b; margin-top: 2px; }}
  .chart-wrap {{ padding: 0 24px 24px; }}
  .chart-wrap canvas {{ background: #1e293b; border-radius: 8px;
                        padding: 12px; max-height: 300px; }}
  .filter-bar {{ margin: 0 24px 16px; background: #1e293b; border-radius: 6px;
                 padding: 8px 14px; color: #94a3b8; font-size: 0.8rem; }}
  table.main-table {{ width: calc(100% - 48px); margin: 0 24px 32px;
                      border-collapse: collapse; }}
  table.main-table th {{ background: #1e293b; padding: 8px 10px; text-align: center;
                         color: #94a3b8; font-weight: 600; white-space: nowrap;
                         border-bottom: 1px solid #334155; }}
  table.main-table td {{ padding: 7px 10px; border-bottom: 1px solid #1e293b;
                         white-space: nowrap; }}
  .stock-row {{ background: #0f172a; cursor: pointer; transition: background .15s; }}
  .stock-row:hover {{ background: #1e293b; }}
  .rank {{ color: #64748b; text-align: center; width: 36px; }}
  .name {{ color: #e2e8f0; font-weight: 600; }}
  .name small {{ color: #64748b; font-weight: 400; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pos {{ color: #4ade80; }}
  .neg {{ color: #f87171; }}
  .expand-btn {{ text-align: center; color: #475569; }}
  .trades-detail td {{ padding: 0 !important; }}
  table.inner-table {{ width: 100%; border-collapse: collapse;
                       background: #0a1120; font-size: 0.78rem; }}
  table.inner-table th {{ background: #162032; padding: 5px 8px;
                          color: #64748b; white-space: nowrap; }}
  table.inner-table td {{ padding: 5px 8px; border-bottom: 1px solid #162032;
                          white-space: nowrap; }}
  .trade-row:nth-child(even) {{ background: #0d1826; }}
</style>
</head>
<body>
<h1>VCP (Volatility Contraction Pattern) × 相対強度　バックテスト</h1>
<p class="subtitle">期間: {since} ～ {today}（直近{label}）
  VCP {VCP_WINDOW}日収縮({ATR_COMPRESS})　ATR×{ATR_TRAIL_MULT}トレイリング　RS≥{RS_MIN:+.0%}</p>

<div class="stats">
  <div class="stat-card"><div class="val">{len(results)}</div><div class="lbl">シグナル銘柄数</div></div>
  <div class="stat-card"><div class="val">{total_tr}</div><div class="lbl">総トレード数</div></div>
  <div class="stat-card"><div class="val">{profitable}/{len(results)}</div><div class="lbl">プラス銘柄</div></div>
  <div class="stat-card"><div class="val {'pos' if avg_ret>=0 else 'neg'}"
    style="color:{'#4ade80' if avg_ret>=0 else '#f87171'}">{avg_ret:+.2f}%</div>
    <div class="lbl">平均損益率</div></div>
</div>

<div class="chart-wrap">
  <canvas id="barChart"></canvas>
</div>

<div class="filter-bar">フィルター: {filter_html}</div>

<table class="main-table">
  <thead>
    <tr>
      <th>#</th><th>銘柄</th><th>損益</th><th>損益率</th>
      <th>勝率</th><th>PF</th><th>取引数</th><th>平均保有</th><th>現在値</th><th></th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>

<script>
const ctx = document.getElementById('barChart').getContext('2d');
new Chart(ctx, {{
  type: 'bar',
  data: {{
    labels: {chart_labels},
    datasets: [{{
      label: '損益率 (%)',
      data: {chart_vals},
      backgroundColor: {chart_colors},
      borderRadius: 3,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          label: ctx => ctx.parsed.y.toFixed(2) + '%'
        }}
      }}
    }},
    scales: {{
      x: {{ ticks: {{ color: '#64748b', font: {{ size: 10 }} }},
             grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#64748b',
                      callback: v => v + '%' }},
             grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});

function toggleTrades(sym) {{
  const el = document.getElementById('detail-' + sym);
  el.style.display = el.style.display === 'none' ? '' : 'none';
}}
</script>
</body>
</html>"""

    path = Path(f"macd_report_{datetime.today().strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="VCP × 相対強度 バックテスト（日経225）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_macd_scan.py                    # 直近1ヶ月
  python backtest_macd_scan.py --months 3         # 直近3ヶ月
  python backtest_macd_scan.py --years 1          # 直近1年
  python backtest_macd_scan.py --years 5          # 直近5年
  python backtest_macd_scan.py --days 45          # 直近45日
  python backtest_macd_scan.py 7203.T --years 2   # トヨタ 2年 詳細
  python backtest_macd_scan.py --years 3 --top 20 # 3年 上位20件
""")
    parser.add_argument("symbol",   nargs="?", default=None,
                        help="特定銘柄コード（省略時は225銘柄スキャン）")
    parser.add_argument("--days",   type=int,  default=None,
                        help="バックテスト日数")
    parser.add_argument("--months", type=int,  default=None,
                        help="バックテスト月数（1〜60）")
    parser.add_argument("--years",  type=int,  default=None,
                        help="バックテスト年数（1〜5）")
    parser.add_argument("--top",    type=int,  default=30,
                        help="ランキング表示件数（デフォルト: 30）")
    args = parser.parse_args()

    # 期間を日数に変換
    if args.days is not None:
        days = args.days
        label = f"{days}日"
    elif args.months is not None:
        days  = max(1, min(args.months, 60)) * 30
        label = f"{args.months}ヶ月"
    elif args.years is not None:
        days  = max(1, min(args.years, 5)) * 365
        label = f"{args.years}年"
    else:
        days  = BACKTEST_DAYS
        label = "1ヶ月"

    since = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    print(f"\n  VCP (Volatility Contraction Pattern) × 相対強度  直近{label}バックテスト")
    print(f"  期間: {since} ～ {datetime.today().strftime('%Y-%m-%d')}\n")

    # 対象銘柄を絞り込み
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        target = [(s, n) for s, n in SYMBOLS if s == sym]
        if not target:
            target = [(sym, sym.replace(".T", ""))]
    else:
        target = SYMBOLS

    total      = len(target)
    stock_data = {}

    # 日経平均データ取得
    print(f"  日経平均データ取得中...")
    nikkei_df = fetch_nikkei(days)
    nk_s = "取得成功" if nikkei_df is not None else "取得失敗（地合い判定スキップ）"
    print(f"  日経225: {nk_s}\n")

    # Phase 1: 順次データ取得
    print(f"  [Phase 1] データ取得中 ({total}銘柄)...")
    for i, (sym, name) in enumerate(target, 1):
        print(f"  [{i:3d}/{total}] {name}({sym})", end=" ", flush=True)
        df = fetch_df(sym, backtest_days=days)
        if df is None:
            print("× スキップ")
        else:
            print("✓")
            stock_data[sym] = (name, df)

    # Phase 2: 並列バックテスト計算
    print(f"\n  [Phase 2] バックテスト計算中 ({len(stock_data)}銘柄, {WORKERS}並列)...")
    tasks   = [(s, n, d) for s, (n, d) in stock_data.items()]
    results = []

    def _bt(task):
        return run_backtest(task[0], task[1], task[2], days, nikkei_df=nikkei_df)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_bt, t): t[0] for t in tasks}
        done = 0
        for fut in as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            print(f"  計算中 {done}/{len(tasks)}", end="\r", flush=True)

    print()

    if not results:
        print("\n  シグナルが発生した銘柄がありませんでした。\n")
        return

    if args.symbol:
        for r in results:
            print_detail(r, days)
    else:
        print_ranking(results, days, args.top)

    # ── ポートフォリオシミュレーション（最大3銘柄同時保有）──────
    if not args.symbol:
        print(f"  [Phase 3] ポートフォリオシミュレーション中"
              f"（最大{MAX_POSITIONS}銘柄同時保有）...")
        pr = run_portfolio(stock_data, days, nikkei_df=nikkei_df)
        if pr:
            print_portfolio(pr, days, label)
        else:
            print("  シグナルなし（ポートフォリオ）\n")

    # ── HTML レポート ─────────────────────────────────────────
    html_path = generate_html(results, days, label)
    print(f"  HTMLレポート: {html_path.resolve()}")
    webbrowser.open(html_path.resolve().as_uri())
    print()

    # ── CSV出力 ───────────────────────────────────────────────
    rows = []
    for r in results:
        for t in r["trade_log"]:
            pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            rows.append({
                "symbol":      r["symbol"],
                "name":        r["name"],
                "entry_dt":    t["entry_dt"].strftime("%Y-%m-%d"),
                "exit_dt":     t["exit_dt"].strftime("%Y-%m-%d"),
                "entry_price": t["entry_price"],
                "exit_price":  t["exit_price"],
                "qty":         t["qty"],
                "pnl":         t["pnl"],
                "pnl_pct":     round(pnl_pct, 2),
                "hold_days":   t["hold_days"],
                "reason":      t["reason"],
                "sector":      t.get("sector", ""),
                "price_tier":  t.get("price_tier", ""),
                "ma25_dev":    round(t.get("ma25_dev", 0), 2),
                "above_ma75":  t.get("above_ma75", ""),
                "above_ma200": t.get("above_ma200", ""),
                "new_high25":  t.get("new_high25", ""),
                "atr_pct":     round(t.get("atr_pct", 0), 2),
                "vol_ratio":   round(t.get("vol_ratio", 0), 2),
                "rs_diff":     round(t.get("rs_diff", 0), 4),
                "atr_compress": round(t.get("atr_compress", 1.0), 3),
                "rsi":         round(t.get("rsi", 0), 1),
                "nikkei_up":   t.get("nikkei_up", ""),
            })
    if rows:
        csv_path = f"vcp_trades_{datetime.today().strftime('%Y%m%d')}_{label}.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"  CSVエクスポート: {csv_path}  ({len(rows)}件)\n")


if __name__ == "__main__":
    main()
