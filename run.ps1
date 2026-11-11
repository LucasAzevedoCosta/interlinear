# Sobe o Interlinear no Windows. Cria o ambiente virtual e instala as
# dependências na primeira vez. Equivalente ao run.sh.
#
# Se o PowerShell recusar o script, rode uma vez:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# As mensagens ficam sem acento: o Windows PowerShell 5.1 lê scripts sem BOM
# como ANSI e embaralharia os caracteres.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# O projeto exige Python 3.11+. O launcher "py" vem com o instalador do
# python.org e escolhe a versão; sem ele, tenta o "python" do PATH.
function Find-Python {
    foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "python")) {
        $parts = $candidate -split " "
        $exe = $parts[0]
        $extra = @($parts | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        & $exe @extra -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

if (-not (Test-Path ".venv")) {
    $python = Find-Python
    if (-not $python) {
        Write-Error ("O Interlinear precisa do Python 3.11 ou mais recente. " +
            "Instale por https://www.python.org/downloads/ (marque 'Add python.exe to PATH') " +
            "ou com: winget install Python.Python.3.12")
    }
    $parts = $python -split " "
    $exe = $parts[0]
    $extra = @($parts | Select-Object -Skip 1)
    Write-Host "Criando ambiente virtual com $python..."
    & $exe @extra -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Error "Falha ao instalar as dependencias." }
}

$hostAddr = if ($env:INTERLINEAR_HOST) { $env:INTERLINEAR_HOST } else { "127.0.0.1" }
$port = if ($env:INTERLINEAR_PORT) { $env:INTERLINEAR_PORT } else { "8000" }

Set-Location backend
& ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host $hostAddr --port $port
