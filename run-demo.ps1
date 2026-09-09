$ErrorActionPreference = 'Stop'
$taskPython = 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
Push-Location $PSScriptRoot
try {
    & $taskPython -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Prototype tests failed.' }
    & $taskPython -X utf8 -m logistics_lab.demo
    if ($LASTEXITCODE -ne 0) { throw 'Demo generation failed.' }
} finally {
    Pop-Location
}
