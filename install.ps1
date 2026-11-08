# mes-compare 플러그인 설치 (Windows)
#   .\install.cmd                      # 이 PC 에 설치 (이미 있으면 업데이트)
#   .\install.cmd -Root D:\compare     # + 그 폴더를 비교 폴더로 표시 (그 아래 폴더 = 섹션)
#   .\install.cmd -Uninstall           # 제거
# 훅은 비교 폴더(.mes-compare 표시가 있는 폴더) 안에서만 동작한다. 그 밖의 프로젝트에는 기록도 차단도 없다.
param([string]$Root = "", [switch]$Uninstall)
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$plugin = "mes-compare@mes-compare"
function Fail($msg) { Write-Host "X $msg" -ForegroundColor Red; exit 1 }

$pyOk = $false
try { $v = & python --version 2>&1; $pyOk = "$v" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 8 } catch {}
if (-not $pyOk) { Fail "Python 3.8 이상이 PATH 에 필요합니다. 설치: winget install Python.Python.3.12  (설치 후 새 창에서 다시 실행)" }

$cl = (Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $cl) {
    $cands = @(Get-ChildItem "$env:LOCALAPPDATA\Claude-Data\*\claude-code\*\*\claude.exe" -ErrorAction SilentlyContinue) +
             @(Get-ChildItem "$env:USERPROFILE\.local\bin\claude.exe" -ErrorAction SilentlyContinue)
    $cl = ($cands | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $cl) {
    Write-Host "claude CLI 를 찾지 못했습니다. Claude Code 앱의 입력창에서 직접 실행하세요:" -ForegroundColor Yellow
    Write-Host "  /plugin marketplace add $repo"
    Write-Host "  /plugin install $plugin"
    exit 1
}
Write-Host "claude: $cl"

if ($Uninstall) {
    & $cl plugin uninstall $plugin
    & $cl plugin marketplace remove mes-compare
    Write-Host "제거 완료. 비교 폴더의 기록(mes-compare-data)은 그대로 남습니다."
    exit 0
}

$known = & $cl plugin marketplace list 2>&1 | Out-String
if ($known -match "(?m)^\s*❯?\s*mes-compare\s*$") { & $cl plugin marketplace update mes-compare }
else { & $cl plugin marketplace add $repo }
if ($LASTEXITCODE) { Fail "마켓플레이스 등록 실패" }
$have = & $cl plugin list 2>&1 | Out-String
if ($have -match [regex]::Escape($plugin)) { & $cl plugin update $plugin } else { & $cl plugin install $plugin }
if ($LASTEXITCODE) { Fail "플러그인 설치·업데이트 실패" }

if ($Root) {
    & python -X utf8 (Join-Path $repo "tools\mes-compare\mc.py") root $Root --yes
}
Write-Host ""
Write-Host "설치 완료. Claude Code 에서 섹션 폴더 하나를 열고 '비교 시작해줘' (그다음부터 각 섹션에서 작업을 시키면 자동 기록)" -ForegroundColor Green
