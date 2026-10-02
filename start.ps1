$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$gatewayPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $gatewayPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
    & $gatewayPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Run .venv\Scripts\python -m pip install -r requirements.lock.txt then retry.' }
}
& $gatewayPython -c 'import fastapi, uvicorn, httpx, dotenv'
if ($LASTEXITCODE -ne 0) {
    & $gatewayPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
& $gatewayPython setup_gateway.py
if ($LASTEXITCODE -ne 0) { throw 'Gateway setup failed.' }
& $gatewayPython run.py
