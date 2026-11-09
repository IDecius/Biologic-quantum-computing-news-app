param(
    [string]$ExecutablePath
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultExecutable = Join-Path $projectDirectory "dist\VeilleActualites.exe"
$legacyTaskNames = @(
    "Actualites informatique biologique quantique",
    ("Veille d'actualit" + [char]0x00C3 + [char]0x00A9 + "s scientifiques")
)
$taskName = "Veille actualites scientifiques"

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

foreach ($legacyTaskName in $legacyTaskNames) {
    if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
    }
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($dailyTrigger, $logonTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description "Ouvre la veille scientifique quantique et IA à 11 h, ou à la prochaine connexion si 11 h a été manqué." `
    -Force | Out-Null

Write-Host "Tâche Veille d'actualités planifiée à 11 h et à la connexion Windows."
