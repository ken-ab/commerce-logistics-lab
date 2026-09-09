$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot/.venv-tau/Scripts/python.exe" -X utf8 -m delivery_apparel_sources
