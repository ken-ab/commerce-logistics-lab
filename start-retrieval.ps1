$ErrorActionPreference = 'Stop'
$labRoot = $PSScriptRoot
if (Get-Process -Name 'DeltaForceClient*' -ErrorAction SilentlyContinue) {
    Write-Output 'The game is running. Local GPU reranking remains stopped.'
    exit 0
}
$ranking = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like '*-m ranking.evaluate*' }
if ($ranking) {
    Write-Output 'The frozen ranking evaluation is running. Start the interactive ranker after it finishes.'
    exit 0
}
if (Get-NetTCPConnection -LocalPort 5175 -State Listen -ErrorAction SilentlyContinue) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:5175/health' -NoProxy -TimeoutSec 2
        if ($health.ready -and $health.model -eq 'Qwen/Qwen3-Reranker-0.6B') {
            Write-Output 'Local reranker is already running.'
            exit 0
        }
        throw 'Port 5175 is occupied by a different service.'
    } catch {
        throw 'Port 5175 already has a listener but its expected health check failed. Inspect that service before starting another.'
    }
}
$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$outFile = Join-Path $labRoot "evidence/background/$stamp-interactive-ranker.out.log"
$errFile = Join-Path $labRoot "evidence/background/$stamp-interactive-ranker.err.log"
$job = Start-Process -FilePath (Join-Path $labRoot '.venv-retrieval/Scripts/python.exe') -ArgumentList @('-u','-m','serving.reranker') -WorkingDirectory $labRoot -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru
@{pid=$job.Id;started=$job.StartTime;stdout=$outFile;stderr=$errFile} | ConvertTo-Json | Tee-Object -FilePath (Join-Path $labRoot "evidence/background/$stamp-interactive-ranker-process.json")
