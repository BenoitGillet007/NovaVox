@echo off
REM ============================================================
REM  Compile "NOVAVOX" en executable Windows autonome.
REM
REM  A executer SUR WINDOWS, avec Python 3.9/3.10/3.11 installe
REM  et dans le PATH, depuis le dossier du projet qui doit
REM  contenir :
REM
REM    app.py
REM    game_log_watcher.py
REM    requirements.txt
REM    commands.json
REM    patch_maj.txt
REM    gui\index.html
REM    gui\script.js
REM    gui\style.css
REM    gui\overlay.html
REM    icon.ico            (optionnel)
REM
REM  Resultat : dist\NOVAVOX\NOVAVOX.exe
REM  (plus tous les fichiers necessaires a cote, dans le meme
REM  dossier -- c'est normal, ne pas deplacer seulement le .exe).
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Interpreteur Python a utiliser pour tout le script. Ta machine a un
REM Python 3.14 par defaut (incompatible avec pythonnet==3.0.3, requis
REM par pywebview) : on pointe donc explicitement vers l'installation
REM 3.11 dediee, avec repli sur "python" du PATH si jamais ce chemin
REM n'existe pas (autre machine, autre emplacement d'installation).
set PY="C:\Users\benoi\AppData\Local\Programs\Python\Python311\python.exe"
if not exist %PY% set PY=python

echo [1/5] Verification de Python...
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo ERREUR : Python 3.11 introuvable a l'emplacement attendu, et
    echo "python" n'est pas non plus trouve dans le PATH.
    echo Installe Python 3.9-3.11 depuis https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "gui\index.html" (
    echo ERREUR : gui\index.html introuvable.
    echo Ce script doit etre lance depuis la racine du projet,
    echo avec index.html / script.js / style.css places dans un
    echo sous-dossier "gui".
    pause
    exit /b 1
)

if not exist "gui\overlay.html" (
    echo AVERTISSEMENT : gui\overlay.html introuvable.
    echo L'overlay en jeu ne fonctionnera pas dans l'executable compile
    echo tant que ce fichier n'est pas place a cote de index.html.
)

if not exist "game_log_watcher.py" (
    echo ERREUR : game_log_watcher.py introuvable a la racine du projet.
    echo app.py en depend directement ^(import game_log_watcher^).
    pause
    exit /b 1
)

echo [2/5] Installation des dependances du projet...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERREUR lors de l'installation de requirements.txt
    pause
    exit /b 1
)

echo [3/5] Installation de PyInstaller...
%PY% -m pip install pyinstaller
if errorlevel 1 (
    echo ERREUR lors de l'installation de PyInstaller
    pause
    exit /b 1
)

echo [4/5] Nettoyage des anciens builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist NOVAVOX.spec del /q NOVAVOX.spec

set ICON_ARG=
if exist icon.ico set ICON_ARG=--icon "icon.ico"

REM Detection explicite du dossier Tcl/Tk de cette installation Python.
REM Sur l'installeur officiel python.org, il se trouve dans <racine>\tcl\.
REM On force son inclusion plutot que de compter uniquement sur la
REM detection automatique de PyInstaller, qui peut echouer selon les
REM versions ("TclError: Can't find a usable init.tcl").
set TCL_ARG=
set TK_ARG=
set "TCL_HELPER=%TEMP%\novavox_tcl_base.py"
set "TCL_OUT=%TEMP%\novavox_tcl_base.txt"
echo import sys, os> "%TCL_HELPER%"
echo print(os.path.join(sys.base_prefix, "tcl"))>> "%TCL_HELPER%"
%PY% "%TCL_HELPER%" > "%TCL_OUT%" 2>nul
set "TCL_BASE="
for /f "usebackq delims=" %%i in ("%TCL_OUT%") do set "TCL_BASE=%%i"
del /q "%TCL_HELPER%" >nul 2>&1
del /q "%TCL_OUT%" >nul 2>&1
if exist "%TCL_BASE%\tcl8.6\init.tcl" (
    echo Tcl trouve : %TCL_BASE%\tcl8.6
    set TCL_ARG=--add-data "%TCL_BASE%\tcl8.6;tcl8.6"
) else (
    echo AVERTISSEMENT : dossier Tcl introuvable a l'emplacement attendu ^(%TCL_BASE%\tcl8.6^).
    echo La compilation continue quand meme, mais si l'appli plante avec
    echo "Can't find a usable init.tcl", ce sera la cause.
)
if exist "%TCL_BASE%\tk8.6" (
    echo Tk trouve : %TCL_BASE%\tk8.6
    set TK_ARG=--add-data "%TCL_BASE%\tk8.6;tk8.6"
)

echo [5/5] Compilation avec PyInstaller (mode dossier, plus fiable
echo       que --onefile pour les dependances audio/COM utilisees ici)...
%PY% -m PyInstaller ^
    --name "NOVAVOX" ^
    --windowed ^
    --onedir ^
    --noconfirm ^
    --add-data "gui;gui" ^
    --collect-all vosk ^
    --collect-all sounddevice ^
    --collect-all numpy ^
    --collect-all webview ^
    --collect-all pythonnet ^
    --collect-all clr_loader ^
    --collect-all keyboard ^
    --collect-all pygame ^
    --collect-all mouse ^
    --collect-all tzdata ^
    %TCL_ARG% ^
    %TK_ARG% ^
    %ICON_ARG% ^
    app.py

if errorlevel 1 (
    echo ERREUR pendant la compilation PyInstaller. Voir le detail ci-dessus.
    pause
    exit /b 1
)

echo.
echo Verification de Tcl/Tk dans le resultat compile...

REM Detecte dynamiquement ou PyInstaller a place les ressources (gui\ y
REM est forcement, on sait qu'il a fonctionne) plutot que de supposer que
REM le sous-dossier s'appelle "_internal" (peut varier selon la version).
set RES_DIR=dist\NOVAVOX\_internal
if not exist "%RES_DIR%\gui\index.html" set RES_DIR=dist\NOVAVOX

if not exist "%RES_DIR%\tcl8.6\init.tcl" (
    echo   -^> Tcl ABSENT malgre --add-data ^(cherche dans %RES_DIR%^) : copie manuelle de secours...
    if exist "%TCL_BASE%\tcl8.6" (
        xcopy /e /i /y /q "%TCL_BASE%\tcl8.6" "%RES_DIR%\tcl8.6\" >nul
        echo   -^> Tcl copie manuellement.
    ) else (
        echo   -^> ERREUR : impossible de copier, %TCL_BASE%\tcl8.6 introuvable sur cette machine.
    )
) else (
    echo   -^> Tcl present, OK.
)
if not exist "%RES_DIR%\tk8.6" (
    echo   -^> Tk absent malgre --add-data ^(cherche dans %RES_DIR%^) : copie manuelle de secours...
    if exist "%TCL_BASE%\tk8.6" (
        xcopy /e /i /y /q "%TCL_BASE%\tk8.6" "%RES_DIR%\tk8.6\" >nul
        echo   -^> Tk copie manuellement.
    ) else (
        echo   -^> ERREUR : impossible de copier, %TCL_BASE%\tk8.6 introuvable sur cette machine.
    )
) else (
    echo   -^> Tk present, OK.
)

if not exist "%RES_DIR%\gui\overlay.html" (
    echo   -^> AVERTISSEMENT : gui\overlay.html absent du resultat compile.
    echo      L'overlay en jeu ne fonctionnera pas dans cet executable.
) else (
    echo   -^> overlay.html present, OK.
)

echo.
echo Copie des fichiers de donnees utilisateur a cote de l'exe...

REM commands.json (et les configs facultatives) sont lus/ecrits par
REM app.py directement a cote de l'exe (BASE_DIR), PAS dans le dossier
REM de ressources PyInstaller (--add-data). Il faut donc les copier ici
REM "en dur" apres la compilation, sans quoi le programme compile ne
REM trouve rien au demarrage et retombe sur les commandes par defaut
REM codees dans app.py.
if exist "commands.json" (
    copy /y "commands.json" "dist\NOVAVOX\commands.json" >nul
    echo   -^> commands.json copie ^(tes commandes personnalisees seront presentes des le premier lancement^).
) else (
    echo   -^> commands.json absent du dossier source : l'exe demarrera avec les commandes par defaut.
)
if exist "ai_config.json" (
    copy /y "ai_config.json" "dist\NOVAVOX\ai_config.json" >nul
    REM SECURITE : ai_config.json contient ta cle API Gemini personnelle
    REM (gemini_api_key), enregistree localement quand tu testes l'appli.
    REM On la retire systematiquement de la copie livree dans dist, sinon
    REM chaque utilisateur qui installe le build demarre avec TA cle deja
    REM en place (et peut la retrouver en clair dans ses propres fichiers).
    REM Les autres reglages (langue, voix, contexte IA, etc.) sont conserves.
    %PY% -c "import json,sys; p='dist/NOVAVOX/ai_config.json'; d=json.load(open(p,encoding='utf-8-sig')); d['gemini_api_key']=''; json.dump(d,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)"
    if errorlevel 1 (
        echo ERREUR : impossible de retirer la cle API de la copie dist\NOVAVOX\ai_config.json.
        echo Par securite, le build s'arrete pour eviter de livrer ta cle personnelle.
        pause
        exit /b 1
    )
    echo   -^> ai_config.json copie ^(cle API Gemini retiree pour ne pas etre distribuee^).
)
if exist "audio_config.json" (
    copy /y "audio_config.json" "dist\NOVAVOX\audio_config.json" >nul
    echo   -^> audio_config.json copie.
)
REM icon.ico n'est passe a PyInstaller que via --icon, qui l'embarque
REM comme icone du FICHIER .exe (visible dans l'Explorateur/les
REM raccourcis) -- ca ne le copie PAS a cote de l'exe. Or _build_tray_icon_image
REM (app.py) cherche justement "icon.ico" a cote de l'exe (BASE_DIR) pour
REM l'icone de la barre des taches (systray) : sans cette copie, le build
REM compile retombe silencieusement sur un repli generique (hexagone
REM dessine a la volee) au lieu du vrai logo NovaVox, alors que le lancement
REM depuis les sources (python app.py, ou icon.ico est deja a la racine du
REM projet) affiche le bon logo -- d'ou un logo different entre les deux.
if exist "icon.ico" (
    copy /y "icon.ico" "dist\NOVAVOX\icon.ico" >nul
    echo   -^> icon.ico copie ^(icone de la barre des taches^).
) else (
    echo   -^> icon.ico absent du dossier source : l'icone de la barre des taches utilisera un repli generique.
)
if exist "patch_maj.txt" (
    copy /y "patch_maj.txt" "dist\NOVAVOX\patch_maj.txt" >nul
    echo   -^> patch_maj.txt copie ^(notes de mise a jour^).
) else (
    echo   -^> patch_maj.txt absent du dossier source : le bouton de version n'affichera rien.
)

echo.
echo Extraction de la version depuis patch_maj.txt...
set APPVER=0.0.0
for /f "tokens=1 delims= " %%v in ('findstr /r "^v[0-9]" patch_maj.txt') do (
    set APPVER=%%v
    goto :gotver
)
:gotver
set APPVER=%APPVER:v=%
echo   -^> Version detectee : %APPVER%

echo Mise a jour automatique de version.json...
echo { "version": "%APPVER%", "url": "https://novanox.1ercorpscolonial.fr/NovaVox_Setup.exe" }> version.json
echo   -^> version.json mis a jour avec la version %APPVER%.

REM Notes de version : extrait uniquement le bloc de la version courante depuis
REM patch_maj.txt, plutot que tout l'historique complet. Partage entre la
REM publication GitHub et la notification Discord ci-dessous. Delegue a
REM PowerShell (extract_release_notes.ps1) plutot qu'une boucle batch native
REM : le changelog utilise ">" comme separateur visuel de chemin de menu
REM (ex. "Reglages > NovaVox"), et une boucle "for /f" avec expansion
REM retardee (!LINE!) directement collee a une redirection re-interprete
REM CE ">" comme une VRAIE redirection a l'execution -- ca creait des
REM fichiers parasites (nommes d'apres le mot suivant le ">", parfois
REM deforme par le code page de la console) a la racine du projet a
REM chaque build (vu en usage reel : fichiers "NovaVox", "Alias"...).
set "NOTES_FILE=%TEMP%\novavox_release_notes.txt"
if exist "%NOTES_FILE%" del /q "%NOTES_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -File "extract_release_notes.ps1" -PatchFile "patch_maj.txt" -NotesFile "%NOTES_FILE%"
if not exist "%NOTES_FILE%" echo Voir patch_maj.txt pour le detail.> "%NOTES_FILE%"

echo.
echo Compilation de l'installateur (Inno Setup) -- variante site officiel...
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% goto :iscc1_missing

echo https://novanox.1ercorpscolonial.fr/version.json> "%RES_DIR%\gui\update_source.txt"
%ISCC% /DMyAppVersion=%APPVER% installer.iss
if not exist "Output\NovaVox_Setup.exe" goto :iscc1_failed
echo   -^> Installateur genere ^(site officiel^) : Output\NovaVox_Setup.exe
goto :iscc1_done

:iscc1_missing
echo   -^> Inno Setup introuvable, installateur non genere.
echo      Installe-le depuis https://jrsoftware.org/isdl.php
goto :iscc1_done

:iscc1_failed
echo   -^> ERREUR : la compilation de l'installateur a echoue.

:iscc1_done

REM Details de connexion SSH (IP, utilisateur, nom de la cle) lus depuis
REM un fichier local non versionne (voir .gitignore) plutot qu'ecrits en
REM clair ici, puisque ce script se retrouve maintenant sur un depot
REM GitHub public.
set SSH_HOST=
set SSH_USER=
set SSH_KEYNAME=
if not exist "deploy_config.txt" goto :deploy_skip

for /f "usebackq delims=" %%A in ("deploy_config.txt") do (
    if not defined SSH_HOST (
        set "SSH_HOST=%%A"
    ) else if not defined SSH_USER (
        set "SSH_USER=%%A"
    ) else if not defined SSH_KEYNAME (
        set "SSH_KEYNAME=%%A"
    )
)
if not defined SSH_HOST goto :deploy_skip

scp -i "%USERPROFILE%\.ssh\%SSH_KEYNAME%" "Output\NovaVox_Setup.exe" %SSH_USER%@%SSH_HOST%:
scp -i "%USERPROFILE%\.ssh\%SSH_KEYNAME%" "version.json" %SSH_USER%@%SSH_HOST%:
goto :deploy_done

:deploy_skip
echo   -^> deploy_config.txt introuvable ou incomplet, envoi vers le site officiel ignore.
echo      Cree ce fichier ^(3 lignes : IP, utilisateur SSH, nom de la cle^) pour l'activer.

:deploy_done

echo.
echo Compilation de l'installateur (Inno Setup) -- variante GitHub...
if exist "Output\NovaVox_Setup_GitHub.exe" del /q "Output\NovaVox_Setup_GitHub.exe"
if not exist %ISCC% goto :iscc2_missing

echo https://api.github.com/repos/ammoniak07/NovaVox/releases/latest> "%RES_DIR%\gui\update_source.txt"
%ISCC% /DMyAppVersion=%APPVER% installer.iss
if not exist "Output\NovaVox_Setup.exe" goto :iscc2_failed
ren "Output\NovaVox_Setup.exe" "NovaVox_Setup_GitHub.exe"
echo   -^> Installateur genere ^(GitHub^) : Output\NovaVox_Setup_GitHub.exe
goto :iscc2_done

:iscc2_missing
echo   -^> Inno Setup introuvable, installateur ^(variante GitHub^) non genere.
goto :iscc2_done

:iscc2_failed
echo   -^> ERREUR : la compilation de l'installateur ^(variante GitHub^) a echoue.

:iscc2_done

echo.
echo Notification Discord...
powershell -NoProfile -ExecutionPolicy Bypass -File "notify_discord.ps1" -Version "%APPVER%" -NotesFile "%NOTES_FILE%"

echo.
REM ============================================================
REM  Publication automatique sur GitHub Releases, en plus du
REM  site officiel ci-dessus. La variante "site officiel" de
REM  l'installeur ne verifie les mises a jour que sur ce site ;
REM  la variante "GitHub" (voir plus haut) verifie via l'API
REM  GitHub Releases -- chacune reste independante de l'autre.
REM
REM  Necessite GitHub CLI (gh), installe une seule fois
REM  manuellement (https://cli.github.com/) puis connecte via
REM  "gh auth login" une seule fois (session ensuite memorisee
REM  sur cette machine). Depot prive : ce backup GitHub n'est
REM  pas la source publique telechargee par les joueurs -- juste
REM  une copie versionnee/de secours.
REM ============================================================
echo Publication sur GitHub Releases (ammoniak07/NovaVox)...
where gh >nul 2>&1
if errorlevel 1 goto :gh_missing

gh auth status >nul 2>&1
if errorlevel 1 goto :gh_not_logged_in

if not exist "Output\NovaVox_Setup_GitHub.exe" goto :gh_no_exe

set GH_REPO=ammoniak07/NovaVox
set GH_TAG=v%APPVER%

gh release view %GH_TAG% --repo %GH_REPO% >nul 2>&1
if errorlevel 1 goto :gh_create
goto :gh_upload

:gh_create
gh release create %GH_TAG% "Output\NovaVox_Setup_GitHub.exe" --repo %GH_REPO% --title "NovaVox %APPVER%" --notes-file "%NOTES_FILE%"
if errorlevel 1 goto :gh_create_failed
echo   -^> Release %GH_TAG% creee sur GitHub, NovaVox_Setup_GitHub.exe joint.
goto :gh_done

:gh_create_failed
echo   -^> ERREUR lors de la creation de la release GitHub %GH_TAG%.
goto :gh_done

:gh_upload
gh release upload %GH_TAG% "Output\NovaVox_Setup_GitHub.exe" --repo %GH_REPO% --clobber
if errorlevel 1 goto :gh_upload_failed
echo   -^> Release %GH_TAG% existante mise a jour sur GitHub.
goto :gh_done

:gh_upload_failed
echo   -^> ERREUR lors de la mise a jour de la release GitHub %GH_TAG%.
goto :gh_done

:gh_missing
echo   -^> gh introuvable, publication GitHub ignoree.
echo      Installe-le depuis https://cli.github.com/ puis lance "gh auth login" une fois.
goto :gh_done

:gh_not_logged_in
echo   -^> gh n'est pas connecte a un compte GitHub, publication ignoree.
echo      Lance "gh auth login" une fois manuellement, puis relance ce script.
goto :gh_done

:gh_no_exe
echo   -^> Output\NovaVox_Setup_GitHub.exe introuvable, publication GitHub ignoree.

:gh_done
echo.
echo ============================================================
echo  Termine !
echo  Executable : dist\NOVAVOX\NOVAVOX.exe
echo  (le modele Vosk n'est PAS inclus : a placer/selectionner
echo   separement, comme indique dans README.md)
echo.
echo  Pour creer un vrai installeur (Setup.exe) a partir de ce
echo  resultat, utilise ensuite installer.iss avec Inno Setup :
echo  https://jrsoftware.org/isdl.php
echo ============================================================
pause