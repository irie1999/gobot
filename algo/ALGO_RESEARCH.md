# 24時間市場 自動売買アルゴリズム — エッジ調査 (第1版)

**目的**: 走らせておくだけで利益を生むアルゴリズムを作る。
**対象**: 24時間動いている市場。銘柄は問わない。

> ⚠ 先に前提を1つ。**「走らせておくだけで利益が出る」を事前に保証することはできません。**
> できるのは「**実証的な証拠があるエッジ**を選び、**厳密に検証して**、
> ダメなら捨てる」ことです。このドキュメントはその第1段階です。

---

## 0. 市場の選択

| 市場 | 稼働 | 判定 |
|---|---|---|
| **暗号資産** | **24時間365日** | ✅ **唯一の完全な24時間市場** |
| FX | 24時間 **5日**（土日は閉場） | △ |
| 日本株 | 平日 9:00–15:30 | ✗ |

→ **暗号資産を対象とする。**

---

## 1. エッジ候補の一覧（証拠の強さ順）

### ★★★ A. ファンディングレート・アービトラージ（デルタニュートラル）

**仕組み**

```
現物を買う（ロング） ＋ 同額の無期限先物を売る（ショート）
   ↓
価格が上下しても、両者が相殺されて損益はほぼゼロ（デルタニュートラル）
   ↓
無期限先物のファンディングレート（資金調達料）だけを受け取り続ける
```

無期限先物には満期がないため、価格を現物に近づける仕組みとして
**8時間ごとにロング↔ショート間で資金をやり取りする**制度がある。
強気相場ではロングがショートに払う（＝ショート側が受け取る）ことが多い。

**証拠**

| 出典 | 内容 |
|---|---|
| [ScienceDirect 2025（CEX/DEXのリスク・リターン分析）](https://www.sciencedirect.com/science/article/pii/S2096720925000818) | BTC/ETH/XRP/BNB/SOL の**60シナリオ**を分析。**6ヶ月で最大 +115.9%**、**最大損失は 1.92% に限定** |
| 業界の実測レンジ | 年利 **10〜30%**、方向性リスクはほぼ無し |
| [MDPI 2026（Two-Tiered Structure of Funding Rate Markets）](https://www.mdpi.com/2227-7390/14/2/346) | **35.7百万件の1分足** × **26取引所**（CEX11 / DEX15）× 749銘柄の大規模検証 |

**⚠ 反証（同じMDPI論文）**

- 観測の **17%** で経済的に有意なスプレッド（20bps以上）が存在する
- しかし **上位機会のうち、取引コスト後にプラスになるのは 40% だけ**
- **95% の機会で強制退出（スプレッド反転）**が起きる

> **「常に美味しい」わけではない。選別と執行が成否を分ける。**
> 逆に言えば、**選別ロジックこそがこの戦略の中身**。

**なぜ第一候補か**

- **価格の方向を予測しなくてよい** ← 今回の一連の検証で「短期の方向は読めない」と結論した点と整合する
- **「走らせておくだけ」に最も近い**構造
- 実証研究の量と質が、他候補より明確に上

---

### ★★ B. 時間帯シーズナリティ（Intraday Seasonality）

| 発見 | 出典 |
|---|---|
| **22:00 と 23:00（UTC）のリターンが最も有意。3:00–4:00 が最悪** | [Quantpedia](https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin) |
| 日次で見える曜日効果は、時間別にすると消える。**唯一残るのは 日曜 23:00–00:00 UTC** | [mlquants](https://mlquants.substack.com/p/are-day-of-the-week-effects-in-cryptocurrencies) |
| **NYSE / LSE / 香港市場の稼働時間**が、暗号資産の日中周期を決めている | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1059056024006506) |
| **turn-of-the-candle 効果**: 15分足の変わり目（0/15/30/45分）に **+0.58bps/分** が集中 | [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10015199/) |

**評価**: 効果は実在するが**非常に薄い**。0.58bps = 0.0058%。
単独で使うには取引コストに埋もれる。
→ **A のフィルターとして併用する**のが現実的。

---

### ★★ C. 短期リバーサル（反転）

- [ScienceDirect（Cryptocurrency anomalies and economic constraints）](https://www.sciencedirect.com/science/article/abs/pii/S1057521924001509)
- 暗号資産のリバーサル・ポートフォリオは、**株式の同種戦略より高いリターン**
- **保守的な取引コストを入れてもロバスト**
- アウトオブサンプルのロング・ショート戦略で **Sharpe > 1**
- ⚠ 低流動性が取引コストを押し上げ、それがアノマリーを存続させている
  → **流動性の低い銘柄ほど効くが、そこはコストも高い**というジレンマ

---

### ★★ D. モメンタム

- 中期（**3〜12ヶ月**）が継続の sweet spot。超長期（3〜5年）では反転
- **ボラティリティ・スケーリングによるリスク管理が必須**。それでもテールリスクが極端
- [8,400の自動売買ボットの分析（2023–2025）](https://theledgermind.com/momentum-trading-bot-strategies/):
  上位7%はトレンド相場で **+247%**、**レンジ相場では -68%**
- **評価**: 保有期間が長く「デイトレ」から外れる。またレンジ相場での脆さが致命的

---

### ★ E. 取引所間アービトラージ

個人は**速度で機関投資家に勝てない**。除外。

---

## 2. 実現可能性のフィルター

| 論点 | 状態 |
|---|---|
| **日本居住者が使える取引所で、無期限先物とファンディングが取れるか** | ⬜ **要確認（最重要）** |
| 24時間サーバーを動かす環境 | ⬜ 要検討（VPS / 自宅PC） |
| 過去のファンディングレート・データの取得 | ⬜ 要確認（取引所API） |
| 必要資金 | ⬜ 要試算 |

⚠ **日本の規制上、海外取引所（Binance等）の無期限先物を日本居住者が使えるかは、
別途確認が必要**です。国内取引所（bitFlyer / GMOコイン等）の暗号資産FXは、
海外CEXの無期限先物とファンディングの仕組みが異なる可能性があります。
**ここが成立しないと A は選べません。** 最優先で確認します。

---

## 3. 推奨する方針

### 第一候補: A（ファンディングレート・アービトラージ）＋ 選別ロジック

理由:
1. **方向を当てる必要がない**
2. 証拠が最も強く、最大損失が限定的（学術研究で1.92%）
3. 「走らせておくだけ」に構造的に最も近い

**作るものの中身は「選別」です。**
論文が示す通り、機会の40%しかコスト後に残らないので、
**どのスプレッドを取り、どれを見送るか**がアルゴリズムの本体になります。

### 第二候補: C（短期リバーサル）

A が規制上できない場合の代替。方向を取るが、
**リバーサルは「方向がないこと」を利用する**ので、
モメンタムより今回の文脈に合う。

---

## 4. 進め方（この順で）

| 段階 | 内容 |
|---|---|
| **① 規制と取引所の確認** | 日本居住者がファンディングを取れる場所があるか。**ここが全ての前提** |
| **② データ取得基盤** | ファンディングレートと価格を取得し、蓄積する |
| **③ バックテスト** | 過去データで、選別ロジック込みの成績を測る |
| **④ Walk-forward 検証** | 期間をずらして、in-sample bias を除く |
| **⑤ ペーパートレード** | 実データ・実時間で、実弾なしで走らせる |
| **⑥ 実弾（最小額）** | ⑤ が想定通りなら |

**③ で勝てなければ、④以降には進みません。**
**④ で崩れたら、そこで捨てます。** これは前回の分析で確認した方法論と同じです。

---

## 5. 次の調査項目

- [ ] 日本居住者が利用できる、ファンディングレートのある取引所
- [ ] 各取引所のファンディング履歴API（過去データの遡及可能期間）
- [ ] 現物と無期限先物の手数料（メイカー/テイカー）
- [ ] 借入コスト・証拠金要件
- [ ] 「40%しか残らない」の選別条件を、論文から具体化できるか
- [ ] DEX（分散型取引所）は規制上の扱いが違うか

---

## 出典

- [Exploring risk and return profiles of funding rate arbitrage on CEX and DEX — ScienceDirect (2025)](https://www.sciencedirect.com/science/article/pii/S2096720925000818)
- [The Two-Tiered Structure of Cryptocurrency Funding Rate Markets — MDPI Mathematics (2026)](https://www.mdpi.com/2227-7390/14/2/346)
- [Cryptocurrency anomalies and economic constraints — ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1057521924001509)
- [Cryptocurrency momentum has (not) its moments — Springer](https://link.springer.com/article/10.1007/s11408-025-00474-9)
- [Overnight Seasonality in Bitcoin — Quantpedia](https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin)
- [Are Day-of-the-Week Effects in Cryptocurrencies Real? — mlquants](https://mlquants.substack.com/p/are-day-of-the-week-effects-in-cryptocurrencies)
- [Intraday and daily dynamics of cryptocurrency — ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1059056024006506)
- [Turn-of-the-candle effect in bitcoin returns — PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10015199/)
- [Momentum Trading Bot Strategies: 11 Data-Backed Methods — LedgerMind](https://theledgermind.com/momentum-trading-bot-strategies/)
- [Bitcoin Never Sleeps: Exploiting Seasonality, Momentum, and Mean Reversion — paperswithbacktest](https://paperswithbacktest.com/blog/bitcoin-never-sleeps-exploiting-seasonality)
