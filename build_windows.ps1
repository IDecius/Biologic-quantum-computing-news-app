param(
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$environmentPython = Join-Path $projectDirectory ".venv\Scripts\python.exe"
$executableDirectory = Join-Path $projectDirectory "dist"

if (-not (Test-Path -LiteralPath $environmentPython -PathType Leaf)) {
    if (-not $PythonExe) {
        $pythonCommand = Get-Command python.exe -ErrorAction Stop
        $PythonExe = $pythonCommand.Source
    }

    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "Interpréteur Python introuvable : $PythonExe"
    }

    & $PythonExe -m venv (Join-Path $projectDirectory ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Impossible de créer l’environnement virtuel du projet."
    }
}

Push-Location $projectDirectory
try {
    & $environmentPython -m pip install -r .\requirements-build.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Installation de PyInstaller impossible."
    }

    & $environmentPython -m PyInstaller `
        --clean `
        --noconfirm `
        --onefile `
        --windowed `
        --name ActualitesBiologieQuantique `
        --distpath $executableDirectory `
        --workpath .\build `
        --specpath .\build `
        .\app.py
    if ($LASTEXITCODE -ne 0) {
        throw "La création de l’exécutable a échoué."
    }

    Write-Host "Exécutable créé : $(Join-Path $executableDirectory 'ActualitesBiologieQuantique.exe')"
} finally {
    Pop-Location
}
