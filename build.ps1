$ErrorActionPreference = "Stop"
$AppName = "인트리홀딩스 출근 자동 체크"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "[$AppName] EXE 빌드를 시작합니다." -ForegroundColor Cyan
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python 3.11 이상이 필요합니다." }

py -m pip install -r requirements.txt
if (Test-Path "dist")  { Remove-Item "dist"  -Recurse -Force }
if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
py -m PyInstaller --noconfirm --clean "IntriHoldingsAttendance.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 빌드 실패" }

# 배포물은 EXE 파일 하나뿐이다. 더블클릭하면 그 자체가 설치 프로그램으로 동작한다.
$Out = Join-Path $Root "인트리홀딩스_출근자동체크_설치.exe"
Copy-Item (Join-Path $Root "dist\$AppName.exe") $Out -Force

Write-Host ""
Write-Host "완료: $Out" -ForegroundColor Green
Write-Host "이 파일 하나만 직원에게 전달하면 됩니다 (더블클릭 -> 설치)."
