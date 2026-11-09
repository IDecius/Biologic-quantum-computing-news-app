$ErrorActionPreference = "Stop"
$taskNames = @(
    "Veille actualites scientifiques",
    ("Veille d'actualit" + [char]0x00C3 + [char]0x00A9 + "s scientifiques"),
    "Actualites informatique biologique quantique"
)

$removed = $false
foreach ($taskName in $taskNames) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        $removed = $true
    }
}

if ($removed) {
    Write-Host "Tâche planifiée supprimée."
} else {
    Write-Host "Aucune tâche planifiée trouvée."
}
