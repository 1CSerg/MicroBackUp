$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "=== MicroBackUp Build Script ===" -ForegroundColor Green

function Find-Python {
    foreach ($cmd in @("python", "py")) {
        try {
            & $cmd --version *>$null
            if ($LASTEXITCODE -eq 0) {
                return $cmd
            }
        }
        catch {
            continue
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "Python не найден. Установите Python 3.8+ и добавьте его в PATH." -ForegroundColor Red
    Read-Host -Prompt "Press Enter to exit"
    exit 1
}

Write-Host "Installing build dependencies from requirements-build.txt (including PyInstaller)..."
& $python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to install dependencies." -ForegroundColor Red
    Read-Host -Prompt "Press Enter to exit"
    exit $LASTEXITCODE
}

& $python build.py
$buildExit = $LASTEXITCODE

Write-Host ""
Read-Host -Prompt "Press Enter to exit"
exit $buildExit
