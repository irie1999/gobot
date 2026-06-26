"""
setup_task_close_stop.py — close_stop_guard をタスクスケジューラに自動登録

実行するだけで設定完了:
  python setup_task_close_stop.py

毎日 14:50 に close_stop_guard.py --kabu --execute --prod が自動実行されます。
"""
import subprocess
import sys
from pathlib import Path


TASK_NAME  = "close_stop_guard"
RUN_HOUR   = 14
RUN_MINUTE = 50


def main() -> int:
    python_exe = sys.executable
    script_dir = Path(__file__).parent.resolve()

    print("=" * 60)
    print("  close_stop_guard タスクスケジューラ登録")
    print("=" * 60)
    print(f"  Python    : {python_exe}")
    print(f"  作業Dir   : {script_dir}")
    print(f"  実行時刻  : 毎日 {RUN_HOUR:02d}:{RUN_MINUTE:02d}")
    print(f"  コマンド  : close_stop_guard.py --kabu --execute --prod")
    print("=" * 60)

    # XML でタスク定義（WorkingDirectory を正確に設定するために XML 方式を使う）
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>kabu station 保有建玉の損切り引け前チェック</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{RUN_HOUR:02d}:{RUN_MINUTE:02d}:00</StartBoundary>
      <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>close_stop_guard.py --kabu --execute --prod</Arguments>
      <WorkingDirectory>{script_dir}</WorkingDirectory>
    </Exec>
  </Actions>
  <Settings>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
</Task>"""

    xml_path = script_dir / "_task_tmp.xml"
    try:
        xml_path.write_text(xml, encoding="utf-16")

        # 既存タスクを削除（エラーは無視）
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True
        )

        # タスク登録
        result = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/xml", str(xml_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )

        if result.returncode == 0:
            print(f"\n✓ タスク '{TASK_NAME}' の登録が完了しました")
        else:
            print(f"\n✗ 登録失敗 (returncode={result.returncode})")
            print(f"  stdout: {result.stdout.strip()}")
            print(f"  stderr: {result.stderr.strip()}")
            print("\n  管理者権限で実行してみてください:")
            print("  → PowerShell を右クリック → 管理者として実行 → python setup_task_close_stop.py")
            return 1

        # タスク履歴を有効化
        hist = subprocess.run(
            ["wevtutil", "set-log",
             "Microsoft-Windows-TaskScheduler/Operational",
             "/enabled:true"],
            capture_output=True, text=True
        )
        if hist.returncode == 0:
            print("✓ タスク履歴を有効化しました")
        else:
            print("  (タスク履歴の有効化には管理者権限が必要です。スキップしました)")

        # 登録確認
        print()
        verify = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        print(verify.stdout)
        return 0

    finally:
        if xml_path.exists():
            xml_path.unlink()


if __name__ == "__main__":
    sys.exit(main())
