param(
    [string]$ExecutablePath
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultExecutable = Join-Path $projectDirectory "dist\mistral-gemini-groq\ActualitesBiologieQuantique.exe"

if (-not $ExecutablePath) {
    $ExecutablePath = $defaultExecutable
}

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "Exécutable introuvable : $ExecutablePath. Construisez-le d’abord avec build_windows.ps1."
}

$action = New-ScheduledTaskAction `
    -Execute $ExecutablePath `
    -Argument "--scheduled" `
    -WorkingDirectory $projectDirectory
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "11:00AM"
$logonTrigger = New-ScheduledTaskTrigger `
    -AtLogOn `
    -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "Actualites informatique biologique quantique" `
    -Action $action `
    -Trigger @($dailyTrigger, $logonTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description "Ouvre le widget de veille à 11 h, ou à la prochaine connexion si 11 h a été manqué." `
    -Force | Out-Null

Write-Host "Tâche planifiée à 11 h et à la connexion Windows."
