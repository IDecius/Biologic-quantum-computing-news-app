$ErrorActionPreference = "Stop"
$taskName = "Actualites informatique biologique quantique"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Tâche planifiée supprimée."
} else {
    Write-Host "Aucune tâche planifiée trouvée."
}
