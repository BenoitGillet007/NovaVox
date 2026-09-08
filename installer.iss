; ============================================================
; Script Inno Setup - Installeur "NovaVox"
;
; A COMPILER SUR WINDOWS avec Inno Setup (gratuit) :
;   https://jrsoftware.org/isdl.php
;
; Prerequis : avoir deja lance build_exe.bat, pour obtenir le
; dossier dist\NovaVox\ a cote de ce fichier .iss.
;
; OPTIONNEL mais recommande - runtime WebView2 (necessaire pour
; que la fenetre de l'appli s'affiche, via pywebview) :
;   1. Telecharge le petit programme officiel Microsoft (~2 Mo) :
;      https://go.microsoft.com/fwlink/p/?LinkId=2124703
;   2. Renomme-le "MicrosoftEdgeWebView2Setup.exe"
;   3. Place-le a cote de ce fichier installer.iss
; S'il est present au moment de la compilation, l'installeur
; l'integrera et l'installera silencieusement chez l'utilisateur
; UNIQUEMENT si WebView2 n'est pas deja present (cas quasi
; systematique sur Windows 10/11 a jour). S'il est absent, le
; script fonctionne quand meme : il proposera juste un lien de
; telechargement a la fin de l'installation, si besoin.
;
; Utilisation : ouvre ce fichier avec l'appli Inno Setup Compiler
; (ou clic droit > "Compile"), le Setup.exe final est genere dans
; le dossier Output\ a cote de ce script.
; ============================================================

#if FileExists("MicrosoftEdgeWebView2Setup.exe")
  #define HasWebView2Bootstrapper
#endif

#define MyAppName "NovaVox"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "Toi"
#define MyAppExeName "NovaVox.exe"

[Setup]
; AppId volontairement inchangé (même identifiant que la version
; "Commandes Vocales") : Windows l'utilise pour reconnaître qu'il
; s'agit d'une mise à jour de la même application plutôt qu'un
; nouveau programme séparé installé en double. Change-le seulement
; si tu veux au contraire que les deux puissent coexister.
AppId={{B4E1C9A2-6F3D-4A21-9E7C-2C8F1D5A9B10}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Affiche explicitement la page "Sélectionner le dossier de destination"
; du wizard (comportement par défaut d'Inno Setup, mais rendu explicite
; ici plutôt que de compter dessus) : sans cette page, NovaVox
; s'installait toujours dans DefaultDirName sans que l'utilisateur
; puisse choisir un autre emplacement.
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=NovaVox_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Compatible 64 bits (recommande pour Windows 10/11 modernes)
ArchitecturesInstallIn64BitMode=x64compatible
; Necessite un clic droit "administrateur" a l'installation, utile
; car l'appli elle-meme doit parfois tourner en admin (voir README).
PrivilegesRequired=lowest
; NovaVox reste actif en arriere-plan (icone dans la barre des taches)
; quand on ferme sa fenetre au lieu de vraiment quitter -- comportement
; volontaire (voir "Icone dans la barre des taches" dans patch_maj.txt).
; Consequence lors d'une mise a jour : ses fichiers (NovaVox.exe, DLLs)
; restent verrouilles, et le comportement par defaut d'Inno Setup
; (CloseApplications=yes) se contente de DEMANDER a l'appli de se
; fermer via le Restart Manager de Windows -- ce que NovaVox, en tant
; qu'appli de fond avec fenetre masquee, ne fait visiblement pas
; toujours correctement, d'ou l'erreur "L'assistant d'installation n'a
; pas pu arreter toutes les applications automatiquement". "force"
; demande a Inno Setup de forcer sa fermeture (TerminateProcess) sans
; jamais bloquer sur ce prompt ; RestartApplications relance ensuite
; NovaVox automatiquement une fois l'installation terminee.
CloseApplications=force
RestartApplications=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"

[Files]
Source: "dist\NovaVox\*"; DestDir: "{app}"; Excludes: "commands.json,ai_config.json,audio_config.json"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\NovaVox\commands.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "dist\NovaVox\ai_config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "dist\NovaVox\audio_config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
#ifdef HasWebView2Bootstrapper
Source: "MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not IsWebView2Installed
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Désinstaller {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
#ifdef HasWebView2Bootstrapper
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installation du runtime WebView2 (Microsoft)..."; Check: not IsWebView2Installed; Flags: waituntilterminated
#else
Filename: "https://go.microsoft.com/fwlink/p/?LinkId=2124703"; Description: "Télécharger et installer le runtime WebView2 (nécessaire, Microsoft)"; Flags: postinstall shellexec skipifsilent unchecked; Check: not IsWebView2Installed
#endif
Filename: "{app}\{#MyAppExeName}"; Description: "Lancer {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function IsWebView2Installed(): Boolean;
var
  Version: String;
begin
  { Vérifie les emplacements possibles du runtime WebView2 Evergreen
    (installation machine 64 bits, machine 32 bits, ou par utilisateur). }
  Result :=
    RegQueryStringValue(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
end;

[Messages]
FinishedLabel=L'installation est terminée.%n%nIMPORTANT : le modèle de reconnaissance vocale Vosk (français) n'est PAS inclus dans cet installeur — télécharge-le depuis https://alphacephei.com/vosk/models et sélectionne son dossier dans l'appli au premier lancement (voir README.md).
