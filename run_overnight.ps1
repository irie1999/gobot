# run_overnight.ps1 ― デイトレ戦略の夜間一括 WalkForward 検証
# 使い方:  .\run_overnight.ps1
#          .\run_overnight.ps1 -Workers 6 -MaxPrice 6000 -MinPrice 1000
#
# 全9戦略 × 全プライム銘柄を OOS 検証し、結果を overnight_<日時>.log に保存。
# 朝に log と walkforward_results\wf_strategies_*.csv を確認する。

param(
    [int]$Workers  = 8,
    [double]$MaxPrice = 6000,   # 1株6000円以下 (60万円で100株)
    [double]$MinPrice = 1000,   # 低位株除外
    [string]$Strategy = "ALL",  # "Donchian" など個別指定も可
    [int]$Limit = 0,            # 先頭N銘柄に制限 (0=全銘柄)。本番前の動作確認に使う
    [int[]]$Holdouts = @(30, 90, 180),  # ホールドアウト日数の一括検証 (スイング同様)。@(0)=除外なし
    [switch]$SkipAudit          # データ監査をスキップ
)

$ErrorActionPreference = "Continue"
# Python 出力を cp932 にし、エンコード不能文字(✓等)は ? に置換。
# これで UnicodeEncodeError クラッシュも文字化けも両方回避 (PowerShellがcp932のため)。
$env:PYTHONUTF8 = "0"
$env:PYTHONIOENCODING = "cp932:replace"
$ts  = Get-Date -Format "yyyyMMdd_HHmm"
$log = "overnight_$ts.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line
}

$mode = if ($Limit -gt 0) { "確認実行 (先頭${Limit}銘柄)" } else { "本番 (全プライム銘柄)" }
Log "==================================================================="
Log " デイトレ戦略 夜間一括 WalkForward 検証 開始  [$mode]"
Log "  Workers=$Workers  MaxPrice=$MaxPrice  MinPrice=$MinPrice  Strategy=$Strategy"
Log "==================================================================="

$limitArg = if ($Limit -gt 0) { @("--limit", $Limit) } else { @() }
$stratArg = if ($Strategy -eq "ALL") { @() } else { @("--strategy", $Strategy) }
Log "  Holdouts(除外日数の一括検証): $($Holdouts -join ', ')"
Log "==================================================================="

# ── データ品質の監査 (往復破損が増えていないか確認) ──
if (-not $SkipAudit) {
    Log "[監査] データ品質 (残存破損チェック)..."
    python daytrade_data.py --audit @limitArg 2>&1 | Tee-Object -FilePath $log -Append
} else {
    Log "[監査] スキップ"
}

# ── 各ホールドアウト設定で WalkForward 検証 (スイング同様の複数設定一括) ──
$i = 0
foreach ($ho in $Holdouts) {
    $i++
    $hoArg = if ($ho -gt 0) { @("--holdout-days", $ho) } else { @() }
    $label = if ($ho -gt 0) { "ホールドアウト${ho}日" } else { "除外なし" }
    Log "[WF $i/$($Holdouts.Count)] $label で検証中..."
    python scan_wf_strategies.py --workers $Workers --max-price $MaxPrice --min-price $MinPrice @stratArg @limitArg @hoArg 2>&1 |
        Tee-Object -FilePath $log -Append
}

# ── WATCHLIST 再構築 (新CSVから TEST窓のみで選定。直近HO日は使わない) ──
$today = Get-Date -Format 'yyyy-MM-dd'
Log "[WATCHLIST] build_watchlist_wf.py で再構築中 (TEST選定・OOS不使用)..."
python build_watchlist_wf.py --date $today 2>&1 | Tee-Object -FilePath $log -Append

# ── HTMLレポート生成 (スイング同様にブラウザで確認) ──
Log "[レポート] HTML生成中..."
python daytrade_report.py --date $today --no-browser 2>&1 | Tee-Object -FilePath $log -Append

Log "==================================================================="
Log " 完了。確認するもの:"
Log "   - HTMLレポート:    daytrade_report_$today.html  (★本命=HO90&HO180で黒字)"
Log "   - ログ全体:        $log"
Log "   - 各ホールドアウト別CSV: walkforward_results\wf_strategies_<戦略>_ho<N>_$today.csv"
Log "==================================================================="
