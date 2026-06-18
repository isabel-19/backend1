$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (Test-Path .venv) {
  $python = Join-Path $here '.venv\Scripts\python.exe'
} else {
  $python = 'python'
}

& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
