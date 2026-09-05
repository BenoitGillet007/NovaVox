# notify_discord.ps1
# Envoie une notification dans un salon Discord via un Webhook, avec le
# numero de version, les notes de version (extraites de patch_maj.txt
# par build_exe.bat) et un lien vers le salon #novavox.
#
# Appele automatiquement par build_exe.bat apres un deploiement reussi.
# Ne fait jamais planter le build : toute erreur ici est juste affichee,
# jamais fatale.

param(
    [string]$Version,
    [string]$NotesFile,
    [string]$WebhookFile = "discord_webhook.txt",
    [string]$FirstPostFile = "discord_first_post.txt",
    [string]$ChannelLink = "https://discord.com/channels/1545838159261204510/1545868137545601044"
)

if (-not (Test-Path $WebhookFile)) {
    Write-Host "  -> discord_webhook.txt introuvable, notification Discord ignoree."
    exit 0
}

$webhookUrl = (Get-Content $WebhookFile -Raw -Encoding UTF8).Trim()
if ([string]::IsNullOrWhiteSpace($webhookUrl)) {
    Write-Host "  -> discord_webhook.txt est vide, notification Discord ignoree."
    exit 0
}

$title = "NovaVox v$Version disponible"
$notes = "Voir le changelog complet dans l'application."

# Recap complet a usage UNIQUE : si discord_first_post.txt existe (premier
# envoi dans le salon), on l'utilise a la place des notes de version
# normales pour ce lancement, puis on le supprime aussitot -- tous les
# envois suivants repassent automatiquement en mode normal (juste les
# notes de la version courante).
if ($FirstPostFile -and (Test-Path $FirstPostFile)) {
    $recap = (Get-Content $FirstPostFile -Raw -Encoding UTF8).Trim()
    if ($recap) {
        $notes = $recap
        $title = "NovaVox - recap complet + v$Version"
    }
    Remove-Item -Path $FirstPostFile -Force -ErrorAction SilentlyContinue
} elseif ($NotesFile -and (Test-Path $NotesFile)) {
    $fileContent = (Get-Content $NotesFile -Raw -Encoding UTF8).Trim()
    if ($fileContent) {
        $notes = $fileContent
    }
}

if ($notes.Length -gt 3900) {
    $notes = $notes.Substring(0, 3900) + "..."
}

$embed = @{
    title       = $title
    description = $notes
    url         = $ChannelLink
    color       = 1752262
    fields      = @(
        @{ name = "Salon"; value = $ChannelLink }
    )
}

$payload = @{
    username = "NovaVox"
    embeds   = @($embed)
} | ConvertTo-Json -Depth 6

try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    Invoke-RestMethod -Uri $webhookUrl -Method Post -ContentType "application/json; charset=utf-8" -Body $bytes | Out-Null
    Write-Host "  -> Notification Discord envoyee."
} catch {
    Write-Host "  -> ERREUR envoi notification Discord : $_"
}