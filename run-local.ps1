$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Get-PythonVersionString {
    param([string]$PythonExe)

    return (& $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
}

function Get-PythonBootstrapCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            $pythonVersion = Get-PythonVersionString -PythonExe "python"
            if ([version]$pythonVersion -ge [version]"3.11") {
                return @("python")
            }
        } catch {
        }
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidateVersions = @("3.14", "3.13", "3.12", "3.11")
        foreach ($candidate in $candidateVersions) {
            try {
                & py -$candidate -c "import sys; print(sys.version)" *> $null
                return @("py", "-$candidate")
            } catch {
            }
        }

        try {
            & py -c "import sys; print(sys.version)" *> $null
            return @("py")
        } catch {
        }
    }

    throw "Python 3.11+ nao encontrado. Instale o Python e marque a opcao Add Python to PATH."
}

function Invoke-BootstrapPython {
    param(
        [string[]]$BootstrapCommand,
        [string[]]$Arguments
    )

    $exe = $BootstrapCommand[0]
    $prefix = @()
    if ($BootstrapCommand.Length -gt 1) {
        $prefix = $BootstrapCommand[1..($BootstrapCommand.Length - 1)]
    }

    & $exe @prefix @Arguments
}

function Invoke-PythonProcess {
    param(
        [string]$PythonExe,
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $process = Start-Process -FilePath $PythonExe -ArgumentList $Arguments -NoNewWindow -Wait -PassThru
    $exitCode = $process.ExitCode

    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "Comando falhou com exit code $exitCode."
    }

    return $exitCode
}

function Ensure-Venv {
    param([string[]]$BootstrapCommand)

    $venvPython = Join-Path $scriptDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }

    Write-Host "Criando ambiente virtual (.venv)..." -ForegroundColor Cyan
    Invoke-BootstrapPython -BootstrapCommand $BootstrapCommand -Arguments @("-m", "venv", ".venv")

    if (-not (Test-Path $venvPython)) {
        throw "Nao foi possivel criar o ambiente virtual."
    }

    return $venvPython
}

function Ensure-PythonDeps {
    param([string]$VenvPython)

    Write-Host "Verificando dependencias Python..." -ForegroundColor Cyan
    $probeExitCode = Invoke-PythonProcess -PythonExe $VenvPython -Arguments @("-c", "import flask, playwright, requests, bs4, gunicorn") -AllowFailure
    if ($probeExitCode -eq 0) {
        return
    }

    Write-Host "Instalando dependencias do projeto..." -ForegroundColor Yellow
    Invoke-PythonProcess -PythonExe $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-PythonProcess -PythonExe $VenvPython -Arguments @("-m", "pip", "install", "-r", "requirements.txt")
}

function Ensure-PlaywrightChromium {
    param([string]$VenvPython)

    $stampFile = Join-Path $scriptDir ".venv\.playwright-chromium-ready"
    if (Test-Path $stampFile) {
        return
    }

    Write-Host "Instalando Chromium do Playwright (primeira vez)..." -ForegroundColor Yellow
    Invoke-PythonProcess -PythonExe $VenvPython -Arguments @("-m", "playwright", "install", "chromium")

    New-Item -ItemType File -Path $stampFile -Force | Out-Null
}

function Import-LocalEnvFile {
    $envFile = Join-Path $scriptDir ".env.local"
    if (-not (Test-Path $envFile)) {
        return
    }

    Write-Host "Carregando variaveis de .env.local..." -ForegroundColor Cyan

    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $parts = $trimmed -split "=", 2
        if ($parts.Length -ne 2) {
            continue
        }

        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

try {
    $bootstrapCommand = Get-PythonBootstrapCommand
    $venvPython = Ensure-Venv -BootstrapCommand $bootstrapCommand
    Ensure-PythonDeps -VenvPython $venvPython
    Ensure-PlaywrightChromium -VenvPython $venvPython
    Import-LocalEnvFile

    if (-not $env:GITHUB_TARGET_OWNER) { $env:GITHUB_TARGET_OWNER = "eurodrigobispo" }
    if (-not $env:GITHUB_TARGET_REPO) { $env:GITHUB_TARGET_REPO = "referencias-html" }
    if (-not $env:GITHUB_TARGET_BRANCH) { $env:GITHUB_TARGET_BRANCH = "main" }
    if (-not $env:GITHUB_TARGET_ROOT) { $env:GITHUB_TARGET_ROOT = "sites" }

    Write-Host ""
    Write-Host "WebDrop local iniciando..." -ForegroundColor Green
    Write-Host "URL: http://localhost:5001" -ForegroundColor Green
    Write-Host "Para parar, feche esta janela ou pressione Ctrl+C." -ForegroundColor DarkGray
    Write-Host ""

    Invoke-PythonProcess -PythonExe $venvPython -Arguments @("app.py")
    exit 0
} catch {
    Write-Host ""
    Write-Host ("Erro ao iniciar o app: " + $_.Exception.Message) -ForegroundColor Red
    if ($_.ScriptStackTrace) {
        Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    }
    Write-Host "Feche esta janela ou corrija o erro e rode novamente." -ForegroundColor DarkGray
    exit 1
}
