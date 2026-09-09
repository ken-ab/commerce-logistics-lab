$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot/.venv-tau/Scripts/python.exe" -X utf8 -u -m uvicorn delivery_apparel_reliability:create_app --factory --host 127.0.0.1 --port 5177
