"""
Star Citizen - NOVAVOX
=================================
Interface "panneau de contrôle" (HTML/CSS/JS) affichée via pywebview.
Le moteur (Vosk, pydirectinput, gestion des commandes) reste en Python pur.

Au lancement, l'application vérifie que toutes les dépendances Python
sont installées et les installe automatiquement si besoin, avant
d'afficher le panneau de contrôle.
"""

import difflib
import importlib
import importlib.metadata
import array
import base64
import ctypes
import ctypes.wintypes
import datetime
import gc
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import wave
import zipfile
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from game_log_watcher import (
    GameLogWatcher,
    game_state_to_prompt_block,
    _resolve_destination_label,
    destination_alias_key,
    destination_is_unresolved,
    _obstruction_label_is_generic_guess,
)


def _clean_hud_notification_text(text):
    """Nettoie le texte d'une notification HUD capturée par
    GameLogWatcher avant de l'annoncer à voix haute : retire le résidu de
    fermeture de citation qui subsiste parfois pour les notifications
    multi-lignes (le fichier de log referme la citation par une ligne du
    style ": \" [18]", dont il ne reste plus, une fois le nom de
    l'événement retiré, qu'un ":" isolé en toute fin — voir
    RE_HUD_NOTIFICATION_CLOSE dans game_log_watcher.py), remplace les
    retours à la ligne internes par un espace pour une lecture fluide, et
    réduit les espaces multiples."""
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s*:\s*$", "", text)  # ":" isolé résiduel en fin de chaîne
    text = re.sub(r"\s*\n\s*", " ", text)  # retours à la ligne internes -> espace
    text = re.sub(r"\s+", " ", text).strip()
    return text

# audioop est nettement plus rapide (implémentation C) que la mise à
# l'échelle manuelle en Python pur pour ajuster le volume du micro, mais il
# est déprécié depuis Python 3.11 et retiré à partir de 3.13 : on l'utilise
# s'il est là, avec un repli en Python pur (via le module array) sinon.
try:
    import audioop
except ImportError:
    audioop = None

# En mode --windowed (PyInstaller), il n'y a pas de console : sys.stdout et
# sys.stderr valent None. N'importe quel print()/écriture interne (même
# venant d'une bibliothèque tierce) plante alors immédiatement avec
# "AttributeError: 'NoneType' object has no attribute 'write'", sans le
# moindre message visible. On les redirige vers un flux "poubelle" pour
# éviter ce plantage silencieux.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# Déclare le processus "DPI-aware" (par moniteur) le plus tôt possible,
# AVANT la création de la moindre fenêtre — que ce soit l'écran de
# démarrage Tkinter (_show_splash) ou la fenêtre principale pywebview
# (WinForms/EdgeChromium). Sans ça, chaque bibliothèque graphique décide
# indépendamment comment interpréter l'échelle d'affichage Windows (DPI)
# quand elle n'est pas à 100% : Tkinter et pywebview finissaient par
# raisonner dans deux systèmes de coordonnées différents, ce qui rendait
# impossible de faire correspondre exactement taille/position entre le
# splash et la fenêtre principale (voir load_window_config/
# save_window_config plus bas) — d'où les sauts de position observés au
# démarrage. Avec cet appel, les deux utilisent les mêmes pixels
# "physiques" réels de l'écran.
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # repli pour Windows plus anciens
        except Exception:
            pass

# Ces modules sont importés plus tard, une fois les dépendances vérifiées
# (voir ensure_dependencies() et le début de main()). Déclarés ici comme
# None pour que les méthodes de la classe Api puissent y faire référence
# sans erreur avant que l'import effectif n'ait lieu.
sd = None
vosk = None
pydirectinput = None
webview = None
keyboard = None
mouse_lib = None
pygame = None
pystray = None
PIL_Image = None
PIL_ImageDraw = None
# np/aec_* : voir _import_runtime_dependencies. Utilisés par l'annulation
# d'écho (AEC, voir aec.py) — numpy est requis pour ça mais pour rien
# d'autre dans l'appli, d'où son import différé comme les autres
# dépendances lourdes plutôt qu'un simple "import numpy" en tête de
# fichier (qui romprait le démarrage rapide si numpy n'est pas encore
# installé, avant même que l'installeur de dépendances ait pu proposer de
# le faire).
np = None
AecReferenceBuffer = None
NLMSEchoCanceller = None
resample_linear = None

# (nom du module à importer, spécificateur pip, libellé affiché sur le splash)
REQUIRED_PACKAGES = [
    ("vosk", "vosk", "Vosk (reconnaissance vocale)"),
    ("sounddevice", "sounddevice", "SoundDevice (audio)"),
    ("pydirectinput", "pydirectinput", "PyDirectInput (simulation clavier)"),
    ("webview", "pywebview<5.3", "PyWebView (interface)"),
    ("keyboard", "keyboard", "Keyboard (touches globales)"),
    ("mouse", "mouse", "Mouse (détection clic global)"),
    ("pygame", "pygame", "Pygame (support manette/joystick)"),
    ("pystray", "pystray", "Pystray (icône barre des tâches)"),
    ("PIL", "Pillow", "Pillow (image de l'icône barre des tâches)"),
    ("numpy", "numpy", "NumPy (annulation d'écho audio)"),
]

# --------------------------------------------------------------------------
# Assistant IA (Ollama, local et gratuit — https://ollama.com)
# --------------------------------------------------------------------------
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
# URL de l'installeur Windows officiel, pour pouvoir le télécharger et le
# lancer directement depuis l'application (voir ai_install_ollama), sans
# obliger l'utilisateur à ouvrir un navigateur.
OLLAMA_WINDOWS_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_AI_NAME = "Nova"
AI_QUESTION_TIMEOUT = 8.0  # secondes avant d'annuler l'attente d'une question
AI_MAX_HISTORY_MESSAGES = 20  # ~10 échanges question/réponse conservés

# Modèles proposés au choix à l'installation et dans les réglages. Liste
# volontairement courte et curatée (pas la liste complète d'Ollama), pour
# rester simple à choisir pour quelqu'un qui découvre l'assistant.
DEFAULT_OLLAMA_MODEL = "llama3.2"
AVAILABLE_MODELS = [
    {
        "id": "llama3.2",
        "label": "Llama 3.2 (3B) — léger",
        "description": "~2 Go. Rapide, tourne sur presque tous les PC, même sans carte graphique dédiée.",
    },
    {
        "id": "llama3.1:8b",
        "label": "Llama 3.1 (8B) — meilleure qualité",
        "description": "~4,7 Go. Réponses plus cohérentes, demande un PC gaming standard (~8 Go de RAM/VRAM libres).",
    },
]

# --------------------------------------------------------------------------
# Assistant IA Gemini (Google, en ligne — https://aistudio.google.com)
# --------------------------------------------------------------------------
# Assistant SÉPARÉ de Nova/Ollama ci-dessus (voir DEFAULT_AI_NAME) : sa
# propre clé API, son propre historique, son propre nom d'activation
# vocale. Contrairement à Ollama, aucune installation locale n'est
# nécessaire — juste une clé API gratuite (quota généreux pour un usage
# personnel) — mais les questions posées sont envoyées aux serveurs de
# Google, contrairement à Ollama qui reste 100% local.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_NAME = "Gemini"
# Format de requête/réponse vérifié via la documentation officielle
# (generateContent) : POST .../models/{model}:generateContent avec l'en-tête
# "x-goog-api-key", corps {"contents": [...], "systemInstruction": {...},
# "generationConfig": {...}}, réponse dans candidates[0].content.parts[*].text.
#
# Modèles VÉRIFIÉS en date du 08/09/2026 : gemini-2.0-flash a été arrêté le
# 01/06/2026, et gemini-2.5-flash a renvoyé une erreur 404 "no longer
# available to new users" en usage réel — Google fait tourner cette gamme
# vite. Si un futur 404 similaire apparaît, la solution est la même :
# mettre à jour cette liste avec l'ID que Google indique dans son propre
# message d'erreur (le plus fiable — voir gemini_test_connection), pas
# deviner depuis la doc ou une recherche web.
GEMINI_AVAILABLE_MODELS = [
    {
        "id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash — recommandé",
        "description": "Rapide, gratuit avec un quota généreux pour un usage personnel, bon compromis qualité/vitesse.",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite — encore plus rapide",
        "description": "Quota gratuit plus élevé et réponses plus rapides, un peu moins riches que Flash.",
    },
]

# Limites gratuites (RPD, requêtes par jour) du niveau gratuit de l'API
# Gemini pour les modèles proposés ci-dessus. Comme pour la liste des
# modèles ci-dessus, Google ajuste régulièrement ses quotas : à revérifier
# sur https://ai.google.dev/gemini-api/docs/rate-limits si l'API renvoie un
# 429 "quota exceeded" avant que la barre de progression de l'overlay
# n'indique la limite atteinte. GEMINI_DAILY_LIMIT_DEFAULT (la plus prudente
# des deux) sert de repli si un futur modèle ajouté à GEMINI_AVAILABLE_MODELS
# n'a pas encore été ajouté ici.
GEMINI_DAILY_LIMITS = {
    "gemini-3.6-flash": 500,
    "gemini-3.5-flash-lite": 1500,
}
GEMINI_DAILY_LIMIT_DEFAULT = 500

try:
    # Le quota RPD de Google se réinitialise à minuit heure du Pacifique
    # (Californie), pas sur une fenêtre glissante de 24h — voir
    # Api._gemini_quota_state.
    GEMINI_QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    # Base de données de fuseaux horaires introuvable (ex. "tzdata" pas
    # installé) : repli sur UTC plutôt que de planter toute l'application
    # pour cette fonctionnalité annexe — seule l'heure exacte de la remise à
    # zéro du compteur local peut dériver de quelques heures, le comptage
    # lui-même reste correct.
    GEMINI_QUOTA_TZ = datetime.timezone.utc

# Emplacements où chercher l'exécutable Ollama si absent du PATH (arrive
# quand Ollama vient d'être installé sans redémarrer l'application).
OLLAMA_FALLBACK_PATHS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Ollama\ollama.exe"),
]

# --------------------------------------------------------------------------
# Modèle de reconnaissance vocale Vosk (téléchargement automatique)
# --------------------------------------------------------------------------
# Liste volontairement courte (2 choix), affichée à l'utilisateur au premier
# lancement si aucun dossier "model" n'est détecté. URLs vérifiées sur
# https://alphacephei.com/vosk/models (section French).
VOSK_MODELS = {
    "small": {
        "id": "small",
        "label": "Modèle léger",
        "description": "~41 Mo, rapide à charger, précision correcte. Recommandé pour commencer.",
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip",
    },
    "large": {
        "id": "large",
        "label": "Modèle précis",
        "description": "~1,4 Go, plus long à télécharger et à charger, mais reconnaissance plus fine.",
        "url": "https://alphacephei.com/vosk/models/vosk-model-fr-0.22.zip",
    },
}


def _download_file_with_retry(url, dest_path, headers=None, timeout=30,
                               max_retries=2, chunk_size=1024 * 256, on_chunk=None):
    """Télécharge `url` vers `dest_path` en flux, avec :
    - des ré-essais automatiques (avec un court délai croissant) en cas
      d'erreur réseau transitoire (coupure Wi-Fi, timeout, DNS...) ;
    - une vérification que la taille reçue correspond bien à l'en-tête
      Content-Length annoncé par le serveur, pour détecter un
      téléchargement tronqué AVANT de tenter d'extraire/utiliser un
      fichier incomplet (ce qui produisait auparavant des erreurs
      d'extraction ou de chargement de modèle peu claires en aval, sans
      indiquer que la cause réelle était une coupure réseau pendant le
      téléchargement).
    on_chunk(downloaded, total), si fourni, est appelé après chaque bloc
    écrit ; `total` vaut None si le serveur n'annonce pas de
    Content-Length (certains CDN ne le font pas pour les réponses
    compressées à la volée)."""
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)

    last_error = None
    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as f:
                total = resp.getheader("Content-Length")
                total = int(total) if total else None
                downloaded = 0
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_chunk:
                        on_chunk(downloaded, total)

            if total is not None and downloaded != total:
                raise IOError(
                    f"Téléchargement incomplet : {downloaded} octet(s) reçu(s) "
                    f"sur {total} annoncé(s) par le serveur."
                )
            return  # succès
        except Exception as e:
            last_error = e
            try:
                if os.path.exists(dest_path):
                    os.remove(dest_path)
            except Exception:
                pass
            if attempt < attempts:
                time.sleep(1.5 * attempt)  # backoff simple avant le prochain essai
                continue
            raise last_error


def find_ollama_executable():
    """Cherche l'exécutable ollama : d'abord dans le PATH, puis dans les
    emplacements d'installation par défaut sur Windows."""
    on_path = shutil.which("ollama")
    if on_path:
        return on_path
    for path in OLLAMA_FALLBACK_PATHS:
        if os.path.isfile(path):
            return path
    return None


# Longueur de réponse souhaitée pour l'assistant IA — réglable dans le
# panneau IA. "normal" reste volontairement strict sur les questions de
# suivi : les modèles de langage ont tendance à enchaîner plusieurs
# questions à la suite ("Comment allez-vous ? Avez-vous déjà... ? Que
# puis-je faire... ?"), ce qui alourdit beaucoup une conversation vocale.
RESPONSE_LENGTH_INSTRUCTIONS = {
    "short": (
        "Réponds en une seule phrase très courte (15 mots maximum). Va droit au "
        "but : pas de salutation ni de formule de politesse si elle n'est pas "
        "indispensable, ne reformule pas la question, ne rajoute pas de contexte "
        "non demandé. Ne pose une question de suivi que si c'est absolument "
        "indispensable pour comprendre la demande — sinon n'en pose aucune."
    ),
    "normal": (
        "Réponds de façon concise : quelques phrases courtes suffisent pour la "
        "plupart des échanges. Ne pose jamais plus d'une question de suivi à la "
        "fois — évite d'enchaîner plusieurs questions dans la même réponse."
    ),
    "long": (
        "Tu peux développer tes réponses plus en détail quand le sujet le "
        "justifie, tout en restant clair et bien structuré. Limite-toi quand "
        "même à une question de suivi à la fois."
    ),
}
DEFAULT_RESPONSE_LENGTH = "normal"


def ai_system_prompt(name, custom_context=None, user_name=None, response_length=None):
    length_instruction = RESPONSE_LENGTH_INSTRUCTIONS.get(
        response_length, RESPONSE_LENGTH_INSTRUCTIONS[DEFAULT_RESPONSE_LENGTH]
    )
    base = (
        f"Tu es {name}, un copilote embarqué dans une application de "
        "NOVAVOX pour Star Citizen. Réponds en français, sur un ton "
        f"amical. {length_instruction} Tu connais bien les mécaniques et les "
        "commandes clavier de Star Citizen et tu peux aider l'utilisateur "
        "à comprendre le jeu, mais tu peux aussi discuter de sujets "
        "généraux."
    )
    user_name = (user_name or "").strip()
    if user_name:
        base += (
            f"\n\nL'utilisateur s'appelle {user_name}. Adresse-toi à lui/elle par ce "
            "prénom de temps en temps, de façon naturelle (pas à chaque phrase), pour "
            "rendre la conversation plus personnelle."
        )
    custom_context = (custom_context or "").strip()
    if custom_context:
        base += (
            "\n\nVoici des informations supplémentaires fournies par l'utilisateur "
            "(lore, règles maison, contexte de sa partie...), à prendre en compte "
            "en priorité dans tes réponses si elles sont pertinentes. Base-toi "
            "STRICTEMENT sur ces informations pour tout ce qui concerne des faits "
            "précis (distances, noms de lieux, de stations, de jump points, "
            "procédures...) : n'invente jamais un détail chiffré ou un nom qui n'y "
            "figure pas explicitement. Si l'information demandée n'est pas dans ce "
            "contexte, dis-le clairement plutôt que d'inventer une réponse "
            "plausible :\n" + custom_context
        )
    return base



if getattr(sys, "frozen", False):
    # Application compilée (PyInstaller) : __file__ n'est pas fiable pour
    # localiser les fichiers une fois "gelée". BASE_DIR = dossier contenant
    # le .exe (pour les fichiers modifiables : commands.json, ai_config.json,
    # model/). RESOURCE_DIR = dossier où PyInstaller a extrait les fichiers
    # ajoutés via --add-data (ex. gui/), qui peut différer de BASE_DIR.
    BASE_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

CONFIG_FILE = os.path.join(BASE_DIR, "commands.json")
AI_CONFIG_FILE = os.path.join(BASE_DIR, "ai_config.json")
AUDIO_CONFIG_FILE = os.path.join(BASE_DIR, "audio_config.json")
WINDOW_CONFIG_FILE = os.path.join(BASE_DIR, "window_config.json")
# Profils de commandes : commands.json (CONFIG_FILE) reste, comme avant,
# la copie "active" utilisée par tout le reste du code (add_command,
# save_commands...) — inchangé pour ne rien casser. PROFILES_DIR contient
# en plus un fichier par profil ({id}.json avec {"name":..., "commands":
# [...]}), tenu à jour en miroir à chaque écriture de commands.json (voir
# save_commands). PROFILES_META_FILE retient quel profil est actif entre
# deux lancements.
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
PROFILES_META_FILE = os.path.join(BASE_DIR, "profiles_config.json")
DEFAULT_PROFILE_NAME = "Défaut"
# Sauvegarde automatique périodique (voir _auto_backup_config plus bas) :
# filet de sécurité indépendant de l'export manuel (Api.export_config),
# au plus une fois par jour, pour ne jamais perdre sa config même sans y
# penser. BACKUP_KEEP_COUNT limite le dossier à un nombre raisonnable de
# sauvegardes plutôt que de le laisser grossir indéfiniment au fil des
# sessions.
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")
BACKUP_KEEP_COUNT = 10
# Notes de mise à jour affichées quand on clique sur le numéro de version
# dans le pied de page (voir Api.get_patch_notes et versionBtn côté
# script.js). Fichier texte libre, à la racine du projet (à côté de
# app.py), édité manuellement à chaque nouvelle version.
PATCH_NOTES_FILE = os.path.join(BASE_DIR, "patch_maj.txt")
# URL du manifest de mise à jour. Par défaut, celui hébergé sur le
# serveur d'Engooref. Peut être remplacée à la compilation par un
# fichier gui/update_source.txt (voir build_exe.bat, qui génère deux
# variantes d'installeur : une pointant ici, une pointant vers l'API
# GitHub Releases) — ce fichier n'existe pas en développement normal,
# d'où le repli automatique sur cette valeur par défaut dans ce cas.
# Format attendu du manifest : {"version": "0.1.2", "url": "https://.../NovaVox_Setup.exe"}
# (ou directement une réponse de l'API GitHub Releases, gérée séparément
# dans check_for_update ci-dessous).
def _load_update_manifest_url():
    override_file = os.path.join(RESOURCE_DIR, "gui", "update_source.txt")
    try:
        with open(override_file, "r", encoding="utf-8") as f:
            url = f.read().strip()
        if url:
            return url
    except Exception:
        pass
    return "https://novanox.1ercorpscolonial.fr/version.json"

UPDATE_MANIFEST_URL = _load_update_manifest_url()
# Repli utilisé uniquement si patch_maj.txt est absent ou ne contient
# aucune ligne "vX.Y.Z" reconnaissable (voir get_app_version ci-dessous).
APP_VERSION_FALLBACK = "0.2.9"
MODEL_DIR_DEFAULT = os.path.join(BASE_DIR, "model")
GUI_INDEX = os.path.join(RESOURCE_DIR, "gui", "index.html")
# Fenêtre séparée, superposée à Star Citizen — PAS une injection dans le
# processus du jeu (voir plus bas pour le détail de l'approche et
# pourquoi une injection DirectX/hook de rendu est volontairement exclue).
OVERLAY_INDEX = os.path.join(RESOURCE_DIR, "gui", "overlay.html")
OVERLAY_CONFIG_FILE = os.path.join(BASE_DIR, "overlay_config.json")
OVERLAY_WINDOW_TITLE = "NovaVoxOverlay"
OVERLAY_DEFAULT_WIDTH = 260
# Utilisée en mode "déplacer" (voir _create_overlay_window et le
# commentaire d'overlayRecalcHeight dans overlay.html : la fenêtre garde
# alors une taille fixe pour que toutes les lignes + leurs cases à cocher
# restent visibles) — donc suffisamment grande pour les 8 lignes actuelles
# (dont la barre de quota Gemini). Le mode verrouillé, lui, se redimensionne
# toujours automatiquement au contenu réellement affiché.
OVERLAY_DEFAULT_HEIGHT = 250
# Identifiants des lignes affichables dans l'overlay (voir overlay.html) :
# chacune peut être masquée individuellement par l'utilisateur via une
# case à cocher visible uniquement en mode "déplacer" (déverrouillé) —
# voir Api.overlay_set_row_visible. Toutes visibles par défaut.
OVERLAY_ROW_KEYS = ("time", "listening", "mic", "ai", "phrase", "zone", "lastCmd", "geminiQuota")

def _version_tuple(v):
    """Convertit '0.1.2' en (0, 1, 2) pour une comparaison fiable
    (une comparaison de chaînes échouerait sur '0.9' vs '0.10')."""
    parts = []
    for x in re.findall(r"\d+", v or ""):
        parts.append(int(x))
    return tuple(parts) or (0,)

def get_app_version():
    """Déduit le numéro de version actuel directement de patch_maj.txt,
    plutôt que de le maintenir en double dans le code : on y prend la
    toute première ligne au format "vX.Y[.Z]" rencontrée dans le fichier
    (par convention, la version la plus récente est toujours en haut,
    voir patch_maj.txt). Ainsi, mettre à jour patch_maj.txt à chaque
    nouvelle version suffit — pas besoin d'un numéro séparé à changer
    ailleurs. Repli sur APP_VERSION_FALLBACK si le fichier est absent ou
    ne contient aucune ligne reconnaissable de ce format."""
    try:
        with open(PATCH_NOTES_FILE, "r", encoding="utf-8-sig") as f:
            content = f.read()
        match = re.search(r"(?m)^v(\d+\.\d+(?:\.\d+)?)", content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return APP_VERSION_FALLBACK

def get_vosk_version():
    """Version du paquet vosk réellement installé (utile pour diagnostiquer
    un souci de reconnaissance/chargement de modèle propre à une version
    précise). Lue via les métadonnées du paquet plutôt qu'un attribut
    __version__ (le module vosk n'en expose pas) ; "inconnue" en repli si
    ces métadonnées sont absentes (ex. non embarquées par PyInstaller)."""
    try:
        return importlib.metadata.version("vosk")
    except Exception:
        return "inconnue"

# --------------------------------------------------------------------------
# Moteur vocal : Piper (synthèse neuronale locale)
# --------------------------------------------------------------------------
# Piper (https://github.com/rhasspy/piper) est un moteur de synthèse
# neuronal, léger et rapide (tourne bien en CPU, pas besoin de carte
# graphique), entièrement hors-ligne une fois téléchargé, avec des voix
# nettement plus naturelles que l'ancien moteur Windows (System.Speech/
# SAPI), qui n'est plus utilisé par cette application.
PIPER_DIR = os.path.join(BASE_DIR, "piper")
PIPER_EXE = os.path.join(PIPER_DIR, "piper.exe")
PIPER_VOICES_DIR = os.path.join(PIPER_DIR, "voices")
PIPER_GITHUB_LATEST_API = "https://api.github.com/repos/rhasspy/piper/releases/latest"

# Quelques voix françaises curatées parmi celles publiées sur le dépôt
# Hugging Face officiel de Piper (rhasspy/piper-voices) — un éventail de
# styles/genres différents, pas un genre imposé par défaut : le choix reste
# entièrement à l'utilisateur, avec un bouton de test avant de valider.
PIPER_VOICES = {
    "fr_FR-siwis-medium": {
        "label": "Siwis — voix féminine, très naturelle (qualité medium)",
        "url_base": "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium",
    },
    "fr_FR-siwis-low": {
        "label": "Siwis — même voix féminine, version légère/rapide",
        "url_base": "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/low/fr_FR-siwis-low",
    },
    "fr_FR-tom-medium": {
        "label": "Tom — voix masculine, ton neutre",
        "url_base": "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/tom/medium/fr_FR-tom-medium",
    },
    "fr_FR-gilles-low": {
        "label": "Gilles — voix masculine",
        "url_base": "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/gilles/low/fr_FR-gilles-low",
    },
    "fr_FR-upmc-medium": {
        "label": "UPMC — voix mixte (jessica/pierre)",
        "url_base": "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/upmc/medium/fr_FR-upmc-medium",
    },
    "fr_FR-mls_1840-low": {
        "label": "MLS 1840 — voix alternative",
        "url_base": "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/mls_1840/low/fr_FR-mls_1840-low",
    },
}
# Réglages fins de la voix Piper (passés directement à piper.exe) : plus
# "length_scale" est bas, plus le débit est rapide ; plus "noise_scale" est
# bas, plus la voix est monotone/mécanique (baisser les deux à la fois
# donne un rendu plus "ordinateur de bord" que la voix naturelle par défaut).
DEFAULT_PIPER_LENGTH_SCALE = 1.0
DEFAULT_PIPER_NOISE_SCALE = 0.667
DEFAULT_RADIO_EFFECT = False

# --------------------------------------------------------------------------
# Phrases annoncées pour les événements du Game.log (voir game_log_watcher.py
# et _on_game_event). Modifiables par l'utilisateur depuis les réglages —
# voir ai_get_state()/set_game_log_phrase()/_format_game_log_phrase(). Les
# champs entre accolades ({dest}, {zone}, {text}) sont remplacés au moment
# de l'annonce ; un gabarit personnalisé qui casse le formatage (accolade
# mal fermée, nom de champ inconnu...) retombe silencieusement sur le
# gabarit par défaut plutôt que de faire planter l'annonce.
DEFAULT_GAME_LOG_PHRASES = {
    "route_set": "Route tracée vers {dest}",
    "route_set_no_dest": "Route tracée",
    "jump_start": "Départ vers {dest}",
    "jump_start_no_dest": "Départ en saut quantique",
    "zone_change": "Arrivée à destination : {zone}",
    "zone_change_no_zone": "Arrivée à destination",
    "hud_notification": "{text}",
}

# Emoji affiché devant chaque phrase dans le journal (fixe, pas éditable :
# seul le texte de la phrase est personnalisable).
GAME_LOG_PHRASE_EMOJI = {
    "route_set": "🧭",
    "route_set_no_dest": "🧭",
    "jump_start": "🚀",
    "jump_start_no_dest": "🚀",
    "zone_change": "🧭",
    "zone_change_no_zone": "🧭",
    "hud_notification": "📢",
}

# Libellé + placeholder(s) disponibles pour chaque phrase, utilisé pour
# construire les champs de réglages côté interface (voir ai_get_state()).
GAME_LOG_PHRASE_META = {
    "route_set": {"label": "Route tracée (destination connue)", "placeholders": ["dest"]},
    "route_set_no_dest": {"label": "Route tracée (destination inconnue)", "placeholders": []},
    "jump_start": {"label": "Départ en saut quantique (destination connue)", "placeholders": ["dest"]},
    "jump_start_no_dest": {"label": "Départ en saut quantique (destination inconnue)", "placeholders": []},
    "zone_change": {"label": "Arrivée à destination (connue)", "placeholders": ["zone"]},
    "zone_change_no_zone": {"label": "Arrivée à destination (inconnue)", "placeholders": []},
    "hud_notification": {"label": "Notification affichée à l'écran (HUD)", "placeholders": ["text"]},
}


def apply_radio_effect(wav_path):
    """Applique un effet léger de "communication radio/vaisseau" (filtre
    passe-bande façon petit haut-parleur + écho court + saturation douce)
    directement sur un fichier WAV mono 16 bits, en pur Python (module
    standard `wave` + `array`, même approche que le traitement du volume
    micro ailleurs dans ce fichier) — aucune dépendance supplémentaire."""
    try:
        with wave.open(wav_path, "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return  # Fichier illisible : on laisse la voix telle quelle plutôt que planter

    if params.sampwidth != 2 or params.nchannels != 1:
        return  # Ne traite que le cas courant ici (PCM 16 bits mono)

    samples = array.array("h")
    samples.frombytes(raw)
    n = len(samples)
    if n == 0:
        return

    # 1) Passe-haut à un pôle : enlève le grave, effet "petit haut-parleur"
    #    plutôt que voix pleine et grave.
    alpha_hp = 0.90
    prev_in = prev_out = 0.0
    band = [0.0] * n
    for i in range(n):
        x = samples[i]
        y = alpha_hp * (prev_out + x - prev_in)
        band[i] = y
        prev_in, prev_out = x, y

    # 2) Passe-bas à un pôle : adoucit l'aigu laissé trop strident par le
    #    passe-haut ci-dessus.
    alpha_lp = 0.55
    prev = 0.0
    for i in range(n):
        prev = prev + alpha_lp * (band[i] - prev)
        band[i] = prev

    # 3) Écho court façon "canal de communication", puis légère saturation
    #    pour un grain radio, avec saturation aux bornes int16.
    delay_samples = max(1, int(params.framerate * 0.018))  # ~18 ms
    feedback = 0.16
    out = array.array("h", [0]) * n
    for i in range(n):
        v = band[i]
        if i >= delay_samples:
            v += feedback * band[i - delay_samples]
        if v > 9000:
            v = 9000 + (v - 9000) * 0.3
        elif v < -9000:
            v = -9000 + (v + 9000) * 0.3
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out[i] = int(v)

    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(out.tobytes())
    except Exception:
        pass

# Fichier de log dédié aux erreurs applicatives "gérées" (celles qui
# s'affichent dans le panneau via _log(..., "error")) : micro absent,
# téléchargement échoué, Ollama injoignable, etc. À ne pas confondre avec
# crash_log.txt (voir _install_crash_handler), qui capture lui les
# exceptions non gérées qui font planter l'application entièrement.
ERROR_LOG_FILE = os.path.join(BASE_DIR, "erreurs.log")
SAMPLE_RATE = 16000


def _setup_error_logger():
    """Configure un logger qui écrit dans erreurs.log toutes les erreurs
    signalées via Api._log(..., "error") (micro, réseau, Ollama, Vosk...).
    Utilise un RotatingFileHandler pour que le fichier ne grossisse pas
    indéfiniment (1 Mo max, 2 fichiers de sauvegarde conservés). Reste
    silencieux en cas d'échec de création du fichier (ex. dossier en
    lecture seule) : l'absence de log ne doit jamais empêcher l'appli de
    fonctionner. Doit être appelée après que BASE_DIR/ERROR_LOG_FILE sont
    connus (donc ici, pas plus haut dans le fichier)."""
    logger = logging.getLogger("commandes_vocales")
    logger.setLevel(logging.ERROR)
    if not logger.handlers:
        try:
            handler = RotatingFileHandler(
                ERROR_LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            ))
            logger.addHandler(handler)
        except Exception:
            logger.addHandler(logging.NullHandler())
    return logger


error_logger = _setup_error_logger()
# Délai minimum avant de pouvoir redéclencher la MÊME commande. Volontairement
# plus long qu'un simple anti-rebond : sert aussi de filet de sécurité contre
# des reconnaissances rapprochées mais non simultanées (ex. écho résiduel,
# bruit de fond ressemblant à la phrase) qui, sans ça, retoggleraient une
# commande comme le train d'atterrissage plusieurs fois de suite. Réglable
# dans l'appli (Assistant IA > ...), stocké dans ai_config.json.
DEFAULT_TRIGGER_COOLDOWN = 3.0
# Seuil de ressemblance (0-1) utilisé uniquement quand une phrase est dite
# juste après le nom de l'IA (ex. "Nova, train d'atterrissage") : permet de
# reconnaître une commande même si elle n'est pas dite mot pour mot, sans
# quoi elle serait toujours envoyée en question à l'IA. Plus la valeur est
# proche de 1, plus la phrase doit ressembler exactement à la commande.
AI_COMMAND_MATCH_THRESHOLD = 0.6
# Tournures interrogatives typiques ("Nova, c'est quoi la touche pour sortir
# le train d'atterrissage ?") : la question peut contenir mot pour mot la
# phrase d'une commande existante ("sortir le train d'atterrissage"), ce qui
# la ferait à tort reconnaître et exécuter comme une commande au lieu de
# partir en question vers l'IA — vécu en usage réel (le train sortait sans
# que Nova ne réponde). On ne touche pas à la commande elle-même : dès
# qu'une de ces tournures apparaît, la phrase est considérée comme une
# vraie question et n'est plus du tout comparée aux commandes (voir
# _try_execute_command_from_ai_text).
AI_QUESTION_MARKER_RE = re.compile(
    r"c[\s']?est quoi|c[\s']?est o[uù]\b|"
    r"qu[\s']?est[\s-]?ce que|qu[\s']?est[\s-]?ce qui|"
    r"quel(?:le)?s? (?:touche|est|sont|bouton)|"
    r"\bcomment\b|\bpourquoi\b|\bcombien\b|[aà] quoi sert|\bo[uù] est\b"
)

# Volume/sensibilité du micro appliqué par l'appli elle-même (indépendant du
# volume micro réglé dans Windows). Un facteur < 1.0 atténue tout ce que le
# micro capte : utile quand le micro-casque entend l'audio de tes propres
# écouteurs (fuite acoustique) — en baissant ce curseur, le bruit de fond
# capté plus faiblement (l'audio qui fuit) tombe sous le plancher de bruit
# numérique tandis que ta voix, bien plus forte car plus proche du micro,
# reste exploitable. Ce n'est pas une vraie suppression d'écho, juste une
# atténuation globale, donc ce n'est jamais parfait.
DEFAULT_MIC_GAIN = 1.0

# Seuil de coupure (noise gate) : contrairement au volume ci-dessus, ceci
# coupe complètement le son en dessous d'un certain niveau au lieu de tout
# atténuer proportionnellement. C'est le vrai outil pour ignorer un bruit
# de fond récurrent (fuite audio d'un casque, ventilateur...) tout en
# laissant passer la voix, plus forte — un simple ajustement de volume ne
# fonctionne pas ici car Vosk/Kaldi normalise le niveau du signal en
# interne avant analyse (normalisation cepstrale), donc un gain uniforme
# ne change presque rien à ce qui est reconnu comme de la parole.
DEFAULT_MIC_GATE = 0  # 0 = désactivé
MIC_GATE_MAX_RAW = 4000  # borne haute du curseur côté interface (valeur RMS int16 brute)

# Volume appliqué au signal de synthèse vocale (Piper) avant lecture,
# indépendamment du volume système. Sert notamment à limiter le
# crosstalk électrique entrée micro / sortie casque sur certains
# codecs audio (Realtek courant) : Piper génère son WAV à une
# amplitude fixe, sans normalisation, qui peut ressortir plus fort que
# le reste de l'audio habituel de l'utilisateur (jeu, vidéo au volume
# déjà réglé par lui) — d'où une fuite qui n'apparaît qu'avec la
# synthèse vocale. 1.0 = amplitude brute de Piper, inchangée.
DEFAULT_TTS_VOLUME = 0.6

# --------------------------------------------------------------------------
# Annulation d'écho acoustique (AEC) — voir aec.py pour le détail de
# l'algorithme (filtre adaptatif NLMS). Soustrait en temps réel, du son
# capté par le micro, le son actuellement joué par le PC (jeu, vidéo,
# réponse vocale de l'IA...) — même principe que Discord/Zoom/Teams.
# Désactivé par défaut : c'est un traitement supplémentaire (coût CPU,
# résultat jamais garanti à 100%), l'utilisateur doit l'activer lui-même
# s'il en a besoin plutôt que de le subir sans le savoir.
# --------------------------------------------------------------------------
DEFAULT_AEC_ENABLED = False
AEC_FILTER_MS = 200  # durée couverte par le filtre adaptatif (voir NLMSEchoCanceller) — doit
                      # dépasser le délai réel entre la sortie audio et sa capture par le
                      # micro (traitement Windows + trajet acoustique/électrique) ; 200 ms
                      # laisse une bonne marge tout en restant largement dans le budget CPU
                      # disponible (~54 ms de calcul mesurés pour 500 ms de son avec ce
                      # réglage, sur un CPU de bureau ordinaire).
AEC_MU = 0.4  # pas d'adaptation du NLMS (voir aec.py)
AEC_REFERENCE_HISTORY_SECONDS = 3.0  # profondeur du tampon de référence (voir AecReferenceBuffer)

# Mode d'activation de l'écoute vocale :
#  - "always"       : bouton manuel classique, l'écoute reste active tant
#                      qu'on ne la coupe pas soi-même (comportement historique).
#  - "toggle_key"    : l'écoute doit être engagée (bouton), puis une touche
#                      globale (fonctionne même si le jeu a le focus) coupe/
#                      réactive juste le micro à chaque appui — sans jamais
#                      redémarrer le moteur (modèle Vosk, flux audio), qui
#                      reste chargé en permanence pour une bascule instantanée.
#  - "push_to_talk"  : l'écoute doit être engagée (bouton) mais le flux
#                      micro n'est réellement transmis à la reconnaissance
#                      que pendant que la touche est maintenue enfoncée.
LISTEN_MODES = ("always", "toggle_key", "push_to_talk")
DEFAULT_LISTEN_MODE = "always"
DEFAULT_LISTEN_HOTKEY = None  # ex. "f9" ; None = pas encore configurée

# Disposition clavier physique réelle de l'utilisateur (indépendante de
# celle envoyée au jeu, qui utilise toujours des positions physiques via
# pydirectinput/DirectInput). Sert uniquement à traduire une touche vers
# le nom attendu par le module `keyboard` — voir AZERTY_TO_KEYBOARD_LIB
# ci-dessous pour le pourquoi.
DEFAULT_KB_LAYOUT = "azerty_fr"

# Le module `keyboard` (utilisé pour la touche bascule / push-to-talk)
# identifie les touches par le CARACTÈRE qu'elles tapent selon la
# disposition Windows active — contrairement à pydirectinput qui vise
# toujours la même touche PHYSIQUE quelle que soit la disposition. Sur un
# clavier AZERTY, la touche physique portant l'étiquette "A" tape en
# réalité le caractère 'q' (elle est à l'emplacement du Q d'un clavier
# QWERTY), et inversement pour la touche étiquetée "Q". Sans cette table,
# `keyboard.is_pressed("q")` réagirait donc à la mauvaise touche physique
# pour un utilisateur AZERTY (celle étiquetée "Q", pas "A").
# Cette table ne couvre que les lettres réellement permutées entre
# QWERTY et AZERTY (France/Belgique) ; les touches non alphabétiques
# (F1-F12, flèches, Ctrl/Alt/Shift, Inser...) sont identiques dans les
# deux mondes et ne nécessitent aucune conversion.
AZERTY_TO_KEYBOARD_LIB = {
    "q": "a", "w": "z", "a": "q", "z": "w",
    ";": "m", "m": ",", ",": ";", ".": ":", "/": "!",
}

# --------------------------------------------------------------------------
# Envoi direct de code de balayage (scan code) via l'API Windows SendInput
# --------------------------------------------------------------------------
# pydirectinput ne reconnaît que les touches présentes dans son propre
# dictionnaire interne (KEYBOARD_MAPPING), basé sur un clavier US — il n'a
# donc pas nativement de nom pour la touche ISO supplémentaire des claviers
# européens ("< > \", voir iso102 côté interface). Plutôt que de dépendre
# d'un correctif fragile sur son dictionnaire interne (dont on ne maîtrise
# pas complètement le comportement de validation), on envoie directement
# cette touche nous-mêmes via SendInput en mode "code de balayage brut"
# (KEYEVENTF_SCANCODE) — exactement la même technique bas niveau que celle
# qu'utilise pydirectinput en interne pour toutes les autres touches, donc
# tout aussi compatible avec les jeux basés sur DirectInput comme Star
# Citizen.
INPUT_KEYBOARD = 1
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002
ISO102_SCAN_CODE = 0x56  # DIK_OEM_102 : touche "< > \" à gauche de Z/W en AZERTY

# Touches numériques du pavé numérique (num0-num9) : leur code de balayage
# est bien défini dans pydirectinput.KEYBOARD_MAPPING... mais en commentaire
# (jamais activé), contrairement à numlock/divide/multiply/subtract/add/
# decimal qui, eux, fonctionnent. Résultat : pydirectinput.keyDown("num1")
# ne lève aucune exception (le nom de touche est juste absent du
# dictionnaire, donc keyDown() retourne silencieusement sans rien envoyer)
# — la commande apparaît "déclenchée" dans le journal alors qu'aucune
# touche n'atteint le jeu. Mêmes codes que le dictionnaire de pydirectinput,
# envoyés nous-mêmes via SendInput comme pour ISO102_SCAN_CODE ci-dessus.
NUMPAD_DIGIT_SCAN_CODES = {
    "num0": 0x52, "num1": 0x4F, "num2": 0x50, "num3": 0x51, "num4": 0x4B,
    "num5": 0x4C, "num6": 0x4D, "num7": 0x47, "num8": 0x48, "num9": 0x49,
}

PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _InputUnion)]


def _send_raw_scan_code(scan_code, key_up=False):
    """Envoie une touche par son code de balayage brut via SendInput,
    sans passer par pydirectinput. Utilisé uniquement pour la touche
    ISO 102e ("< > \\"), absente du dictionnaire de pydirectinput."""
    extra = ctypes.c_ulong(0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
    ii_ = _InputUnion()
    ii_.ki = _KeyBdInput(0, scan_code, flags, 0, ctypes.pointer(extra))
    packet = _Input(INPUT_KEYBOARD, ii_)
    ctypes.windll.user32.SendInput(1, ctypes.pointer(packet), ctypes.sizeof(packet))


# Taille de fenêtre partagée entre le splash et la fenêtre principale,
# pour qu'elles correspondent toujours même si l'une des deux change.
# Servent aussi de valeurs de repli si aucune taille n'a encore été
# enregistrée (voir load_window_config / WINDOW_CONFIG_FILE plus bas).
MAIN_WINDOW_WIDTH = 1000
MAIN_WINDOW_HEIGHT = 930  # +150px (~4 cm à 96 DPI) par rapport à l'original (780)
MAIN_WINDOW_MIN_SIZE = (860, 640)
# Borne haute volontairement large mais finie, pour ignorer une valeur
# aberrante (fichier corrompu, taille venant d'un tout autre écran bien
# plus grand débranché depuis) plutôt que de tenter d'ouvrir une fenêtre
# de taille délirante.
MAIN_WINDOW_MAX_SIZE = (6000, 6000)

DEFAULT_COMMANDS = [
    {
        "phrase": "train d'atterrissage",
        "keys": "n",
        "synonyms": ["train d atterrissage", "train d'atterissage", "train d aterissage"]
    },
    {
        "phrase": "scanner",
        "keys": "v",
        "synonyms": []
    },
    {
        "phrase": "poste combustion",
        "keys": "shift",
        "synonyms": ["postcombustion", "post combustion", "boss combustion", "poste-combustion", "passe combustion"]
    },
    {
        "phrase": "mode quantique",
        "keys": "b",
        "synonyms": []
    },
    {
        "phrase": "bouclier",
        "keys": "insert",
        "synonyms": ["boucliers", "goupillier"]
    }
]


def apply_mic_gain(data, gain):
    """Multiplie l'amplitude d'un bloc audio PCM 16 bits mono par `gain`
    (1.0 = inchangé), avec saturation aux bornes int16 pour éviter tout
    dépassement/retournement de signe. Utilise audioop si disponible
    (implémentation C, rapide) sinon un repli en Python pur via le module
    array (plus lent mais toujours disponible, y compris sur les versions
    de Python où audioop a été retiré)."""
    if gain == 1.0 or not data:
        return data

    if audioop is not None:
        try:
            return audioop.mul(data, 2, gain)
        except Exception:
            pass

    try:
        samples = array.array("h")
        samples.frombytes(data)
        for i in range(len(samples)):
            v = int(samples[i] * gain)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            samples[i] = v
        return samples.tobytes()
    except Exception:
        return data


def compute_rms(data):
    """Calcule le niveau RMS (énergie) d'un bloc audio PCM 16 bits mono.
    Utilisé à la fois pour le mètre de niveau affiché dans le panneau et
    pour décider si un bloc doit être coupé par le seuil de sensibilité."""
    if not data:
        return 0
    if audioop is not None:
        try:
            return audioop.rms(data, 2)
        except Exception:
            pass
    try:
        samples = array.array("h")
        samples.frombytes(data)
        if not samples:
            return 0
        sum_squares = sum(s * s for s in samples)
        return int((sum_squares / len(samples)) ** 0.5)
    except Exception:
        return 0


def model_folder_is_valid(path):
    """Vérifie sommairement qu'un dossier ressemble à un modèle Vosk valide
    (présence des sous-dossiers attendus), plutôt que de se contenter de
    vérifier qu'un dossier "model" existe mais serait vide/incomplet."""
    if not path or not os.path.isdir(path):
        return False
    return os.path.isdir(os.path.join(path, "am")) and os.path.isdir(os.path.join(path, "conf"))


def get_model_folder_info(path):
    """Nom et taille sur le disque du dossier de modèle Vosk sélectionné —
    affichés à côté de la version de Vosk (voir get_vosk_version) pour
    qu'on distingue d'un coup d'œil LE MOTEUR utilisé (identique partout)
    DU MODÈLE chargé (peut varier énormément en taille/temps de
    chargement d'une installation à l'autre : "petit" ~41 Mo contre
    "précis" ~1,4 Go, voir VOSK_MODELS). None si le dossier est absent ou
    invalide."""
    if not model_folder_is_valid(path):
        return None
    total_bytes = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    size_mo = total_bytes / (1024 * 1024)
    size_label = f"{size_mo / 1024:.2f} Go" if size_mo >= 1024 else f"{size_mo:.1f} Mo"
    return {"name": os.path.basename(os.path.normpath(path)), "sizeLabel": size_label}


# Nombre maximum d'actions SUPPLÉMENTAIRES qu'une commande peut enchaîner
# après son action principale (ex. "approche finale" -> touche du train
# d'atterrissage, PUIS touche d'appel du hangar) — voir _execute_command et
# _run_command_sequence. Plafonné à 4 : avec l'action principale, ça fait 5
# actions au total par commande.
MAX_COMMAND_EXTRA_STEPS = 4
DEFAULT_EXTRA_STEP_DELAY = 0.3


def _normalize_extra_steps(raw_steps):
    """Valide/nettoie la liste des actions supplémentaires d'une commande
    (voir MAX_COMMAND_EXTRA_STEPS) : chaque étape est {"keys": str,
    "delay": float}, "delay" étant le délai (secondes) attendu AVANT
    d'envoyer cette touche, pour laisser le jeu enregistrer distinctement
    chaque appui plutôt que d'enchaîner les touches instantanément. Les
    entrées invalides (pas un dict, "keys" vide) sont ignorées plutôt que
    de faire planter le chargement."""
    if not isinstance(raw_steps, list):
        return []
    steps = []
    for raw in raw_steps[:MAX_COMMAND_EXTRA_STEPS]:
        if not isinstance(raw, dict):
            continue
        keys = str(raw.get("keys", "")).strip()
        if not keys:
            continue
        try:
            delay = max(0.0, min(10.0, float(raw.get("delay", DEFAULT_EXTRA_STEP_DELAY))))
        except (TypeError, ValueError):
            delay = DEFAULT_EXTRA_STEP_DELAY
        steps.append({"keys": keys, "delay": delay})
    return steps


def _normalize_commands(items):
    """Assure la compatibilité ascendante et la cohérence de la liste des
    éléments (commandes ET titres de groupe, mélangés dans une seule liste
    ordonnée — l'ordre de la liste EST l'ordre d'affichage/de déplacement).
    Les anciennes commandes (sans champ "type", format d'avant l'ajout des
    titres) sont traitées comme des commandes classiques. Les entrées
    invalides sont ignorées plutôt que de faire planter le chargement."""
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "title":
            normalized.append({"type": "title", "text": str(item.get("text", "")).strip()})
        else:
            item = dict(item)
            item["type"] = "command"
            item.setdefault("synonyms", [])
            item["hold"] = bool(item.get("hold", False))
            try:
                item["repeat_count"] = max(1, min(50, int(item.get("repeat_count", 1))))
            except (TypeError, ValueError):
                item["repeat_count"] = 1
            try:
                item["repeat_delay"] = max(0.0, min(10.0, float(item.get("repeat_delay", 0.1))))
            except (TypeError, ValueError):
                item["repeat_delay"] = 0.1
            item["extra_steps"] = _normalize_extra_steps(item.get("extra_steps"))
            if "phrase" not in item or "keys" not in item:
                continue
            normalized.append(item)
    return normalized


def load_commands():
    if os.path.exists(CONFIG_FILE):
        try:
            # encoding="utf-8-sig" gère aussi bien les fichiers avec BOM
            # (ex : sauvegardés depuis le Bloc-notes Windows) que sans.
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                return _normalize_commands(data)
        except Exception as e:
            print(f"[Erreur] Impossible de lire commands.json ({e}). "
                  f"Retour aux commandes par défaut.", file=sys.stderr)
    return _normalize_commands(DEFAULT_COMMANDS)


def save_commands(commands, mirror_to_profile=True):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(commands, f, ensure_ascii=False, indent=2)
    # Recopie aussi vers le fichier du profil actif (voir _write_profile
    # plus bas) : commands.json reste la copie "active" utilisée par le
    # reste du code (add_command, edit_command... tous inchangés), les
    # fichiers de profiles/ n'en sont qu'un miroir nommé, permettant de
    # garder plusieurs jeux de commandes et de basculer entre eux (voir
    # Api.profiles_switch). mirror_to_profile=False évite ce miroir
    # quand on vient justement de charger commands.json À PARTIR du
    # profil actif (au démarrage, ou juste après un switch) : le fichier
    # de profil est alors déjà identique, pas besoin de le réécrire.
    if mirror_to_profile and _active_profile_id:
        profile_path = _profile_path(_active_profile_id)
        if not os.path.isfile(profile_path):
            # Le profil actif vient d'être supprimé (voir
            # Api.profiles_delete, qui appelle profiles_switch AVANT de
            # mettre à jour _active_profile_id — cette fonction voit donc
            # encore l'ANCIEN id, dont le fichier n'existe plus). Il n'y
            # a alors plus rien de valide où écrire : NE PAS retomber sur
            # DEFAULT_PROFILE_NAME et recréer un fichier à cet
            # emplacement, sous peine de faire réapparaître un profil
            # "Défaut" fantôme à chaque suppression du profil actif —
            # exactement le bug corrigé ici.
            return
        try:
            name, _ = _read_profile(_active_profile_id)
            _write_profile(_active_profile_id, name, commands)
        except Exception:
            pass


# Identifiant (nom de fichier sans extension) du profil actuellement
# actif — mis à jour par Api.__init__ au démarrage et par
# Api.profiles_switch/profiles_create/profiles_delete en cours de
# session. Global de module (plutôt qu'un attribut d'instance) car
# save_commands() ci-dessus, une fonction libre appelée depuis une
# dizaine d'endroits différents de la classe Api, en a besoin pour savoir
# où répercuter chaque modification de commande.
_active_profile_id = None


def _profiles_dir():
    os.makedirs(PROFILES_DIR, exist_ok=True)
    return PROFILES_DIR


def _profile_path(profile_id):
    return os.path.join(_profiles_dir(), f"{profile_id}.json")


def _new_profile_id():
    """Génère un identifiant de profil basé sur l'horodatage (millisecondes),
    vérifié inoccupé — pas besoin d'UUID pour un fichier local créé par un
    clic utilisateur, deux créations ne peuvent pas tomber sur la même
    milliseconde en usage normal, et la boucle ci-dessous couvre le cas
    contraire par sécurité."""
    _profiles_dir()
    while True:
        candidate = f"p{int(time.time() * 1000)}"
        if not os.path.isfile(_profile_path(candidate)):
            return candidate
        time.sleep(0.001)


def _write_profile(profile_id, display_name, commands):
    path = _profile_path(profile_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"name": display_name, "commands": commands}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # écriture atomique : jamais de fichier à moitié écrit en cas de coupure


def _read_profile(profile_id):
    path = _profile_path(profile_id)
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict):
        name = str(data.get("name") or profile_id).strip() or profile_id
        commands = _normalize_commands(data.get("commands", []))
    else:
        # Tolère un fichier qui ne serait qu'une liste brute (jamais
        # produit par _write_profile, mais accepté en lecture par
        # cohérence avec load_commands()/commands.json).
        name = profile_id
        commands = _normalize_commands(data)
    return name, commands


def list_profiles():
    """Retourne la liste des profils disponibles, triée par nom affiché
    (pas par identifiant de fichier, qui n'a aucun sens pour
    l'utilisateur). Inclut le nombre de commandes de chaque profil
    ("count") : utile en soi, et surtout pour distinguer plusieurs
    profils qui porteraient accidentellement le même nom (ex. après une
    ancienne duplication) — impossibles à différencier autrement dans un
    menu déroulant qui n'affiche que des noms."""
    _profiles_dir()
    result = []
    for fname in os.listdir(PROFILES_DIR):
        if not fname.endswith(".json") or fname.endswith(".tmp"):
            continue
        pid = fname[:-5]
        try:
            name, commands = _read_profile(pid)
        except Exception:
            continue
        count = sum(1 for c in commands if c.get("type") != "title")
        result.append({"id": pid, "name": name, "count": count})
    result.sort(key=lambda p: p["name"].lower())
    return result


def load_active_profile_id():
    try:
        with open(PROFILES_META_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        pid = data.get("active")
        if pid:
            return pid
    except Exception:
        pass
    return None


def save_active_profile_id(profile_id):
    try:
        with open(PROFILES_META_FILE, "w", encoding="utf-8") as f:
            json.dump({"active": profile_id}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # confort seulement : au pire on retombe sur le 1er profil au prochain lancement


def _ensure_profiles_migrated():
    """Première utilisation de cette fonctionnalité (dossier profiles/
    absent ou vide) : crée un profil "Défaut" reprenant telles quelles
    les commandes actuelles de commands.json (ou les commandes par défaut
    si le fichier n'existe pas encore), pour ne rien perdre de la
    configuration existante au moment où les profils apparaissent.
    Retourne l'id du profil créé, ou None si des profils existaient déjà
    (rien à migrer).

    IMPORTANT : se base sur la simple PRÉSENCE de fichiers *.json dans
    profiles/ (os.listdir), PAS sur list_profiles() (qui ignore
    silencieusement tout fichier qu'elle échoue à lire/parser). Un
    dossier synchronisé par un service cloud (OneDrive, Dropbox...)
    verrouille parfois brièvement un fichier fraîchement écrit pendant
    la synchronisation ; si cette fonction se fiait à list_profiles(),
    un tel verrou transitoire ferait croire à tort qu'aucun profil
    n'existe, et un nouveau profil "Défaut" seraît recréé À CHAQUE
    lancement concerné par le verrou — d'où la duplication observée
    (plusieurs "Défaut" au fil des redémarrages) avec l'ancienne
    version de cette fonction."""
    _profiles_dir()
    existing_files = [
        f for f in os.listdir(PROFILES_DIR)
        if f.endswith(".json") and not f.endswith(".tmp")
    ]
    if existing_files:
        return None
    commands = load_commands()
    pid = _new_profile_id()
    _write_profile(pid, DEFAULT_PROFILE_NAME, commands)
    save_active_profile_id(pid)
    return pid


def _auto_backup_config():
    """Sauvegarde automatique et silencieuse de la configuration complète
    (commandes de TOUS les profils + réglages IA/audio/overlay) dans
    backups/, au plus une fois par jour — filet de sécurité indépendant
    de l'export manuel (voir Api.export_config), pour ne jamais perdre sa
    config même sans y penser. Pensée pour être lancée sur un thread à
    part (voir l'appel dans Api.__init__) : ne fait que de l'I/O disque,
    mais autant ne jamais retarder le démarrage pour ça. N'importe quelle
    erreur reste silencieuse (les logs applicatifs, appendLog côté JS, ne
    sont pas encore accessibles si cette fonction est appelée trop tôt) —
    ce n'est qu'un filet de sécurité, pas une fonctionnalité que
    l'utilisateur observe directement."""
    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
        today = time.strftime("%Y%m%d")
        marker = os.path.join(BACKUPS_DIR, f".last_{today}")
        if os.path.isfile(marker):
            return  # déjà sauvegardé aujourd'hui, rien à refaire

        # Marque IMMÉDIATEMENT (avant même d'avoir fini d'écrire la
        # sauvegarde) pour qu'un second lancement rapproché le même jour
        # ne déclenche pas une seconde sauvegarde en parallèle. Nettoie
        # au passage les marqueurs des jours précédents (un seul suffit).
        for fname in os.listdir(BACKUPS_DIR):
            if fname.startswith(".last_") and fname != f".last_{today}":
                try:
                    os.remove(os.path.join(BACKUPS_DIR, fname))
                except Exception:
                    pass
        open(marker, "w").close()

        dest_path = os.path.join(BACKUPS_DIR, f"auto_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip")
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, path in Api._EXPORTABLE_CONFIG_FILES.items():
                if os.path.isfile(path):
                    zf.write(path, arcname=arcname)
            _profiles_dir()
            for fname in os.listdir(PROFILES_DIR):
                if fname.endswith(".json") and not fname.endswith(".tmp"):
                    zf.write(os.path.join(PROFILES_DIR, fname), arcname=f"profiles/{fname}")
            if os.path.isfile(PROFILES_META_FILE):
                zf.write(PROFILES_META_FILE, arcname="profiles_config.json")

        # Ne conserve que les BACKUP_KEEP_COUNT sauvegardes les plus
        # récentes (tri par nom = tri chronologique, grâce à l'horodatage
        # AAAAMMJJ_HHMMSS dans le nom de fichier).
        backups = sorted(
            f for f in os.listdir(BACKUPS_DIR)
            if f.startswith("auto_backup_") and f.endswith(".zip")
        )
        for old in backups[:-BACKUP_KEEP_COUNT]:
            try:
                os.remove(os.path.join(BACKUPS_DIR, old))
            except Exception:
                pass
    except Exception:
        pass


def load_ai_config():
    config = {
        "name": DEFAULT_AI_NAME,
        "voice": None,
        "confirm_commands": False,
        "ai_enabled": True,
        "model": DEFAULT_OLLAMA_MODEL,
        "trigger_cooldown": DEFAULT_TRIGGER_COOLDOWN,
        "custom_context": "",
        "user_name": "",
        "response_length": DEFAULT_RESPONSE_LENGTH,
        "piper_voice": None,
        "piper_length_scale": DEFAULT_PIPER_LENGTH_SCALE,
        "piper_noise_scale": DEFAULT_PIPER_NOISE_SCALE,
        "radio_effect": DEFAULT_RADIO_EFFECT,
        "game_log_enabled": False,
        "game_log_announce_events": True,
        "game_log_player_handle": "",
        "game_log_phrases": {},
        "game_log_hud_overrides": {},
        "game_log_destination_aliases": {},
        "gemini_enabled": True,
        "gemini_api_key": "",
        "gemini_model": DEFAULT_GEMINI_MODEL,
        "gemini_name": DEFAULT_GEMINI_NAME,
        "gemini_response_length": DEFAULT_RESPONSE_LENGTH,
        "gemini_custom_context": "",
        "gemini_request_count": 0,
        "gemini_request_day": "",
    }
    if os.path.exists(AI_CONFIG_FILE):
        try:
            with open(AI_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            for key in config:
                if key in data:
                    config[key] = data[key]
            if config["response_length"] not in RESPONSE_LENGTH_INSTRUCTIONS:
                config["response_length"] = DEFAULT_RESPONSE_LENGTH
            if config["piper_voice"] not in PIPER_VOICES:
                config["piper_voice"] = None
            try:
                config["piper_length_scale"] = max(0.5, min(2.0, float(config["piper_length_scale"])))
            except (TypeError, ValueError):
                config["piper_length_scale"] = DEFAULT_PIPER_LENGTH_SCALE
            try:
                config["piper_noise_scale"] = max(0.0, min(1.5, float(config["piper_noise_scale"])))
            except (TypeError, ValueError):
                config["piper_noise_scale"] = DEFAULT_PIPER_NOISE_SCALE
            config["radio_effect"] = bool(config["radio_effect"])
            config["ai_enabled"] = bool(config.get("ai_enabled", True))
            config["gemini_enabled"] = bool(config.get("gemini_enabled", True))
            config["game_log_enabled"] = bool(config["game_log_enabled"])
            config["game_log_announce_events"] = bool(config["game_log_announce_events"])
            config["game_log_player_handle"] = (config.get("game_log_player_handle") or "").strip()
            if not isinstance(config.get("game_log_phrases"), dict):
                config["game_log_phrases"] = {}
            if not isinstance(config.get("game_log_hud_overrides"), dict):
                config["game_log_hud_overrides"] = {}
            if not isinstance(config.get("game_log_destination_aliases"), dict):
                config["game_log_destination_aliases"] = {}
            config["gemini_api_key"] = (config.get("gemini_api_key") or "").strip()
            config["gemini_model"] = (config.get("gemini_model") or "").strip() or DEFAULT_GEMINI_MODEL
            if config["gemini_model"] not in {m["id"] for m in GEMINI_AVAILABLE_MODELS}:
                # Retombe sur le modèle par défaut si celui enregistré n'est
                # plus dans la liste connue (ex. Google a retiré/renommé un
                # modèle depuis — vérifié en usage réel : gemini-2.5-flash a
                # fini par renvoyer une 404 "no longer available to new
                # users") : sans ça, une config existante resterait bloquée
                # sur un modèle mort tant que l'utilisateur n'irait pas le
                # changer lui-même dans les réglages.
                config["gemini_model"] = DEFAULT_GEMINI_MODEL
            config["gemini_name"] = (config.get("gemini_name") or "").strip() or DEFAULT_GEMINI_NAME
            if config.get("gemini_response_length") not in RESPONSE_LENGTH_INSTRUCTIONS:
                config["gemini_response_length"] = DEFAULT_RESPONSE_LENGTH
            config["gemini_custom_context"] = config.get("gemini_custom_context") or ""
            try:
                config["gemini_request_count"] = max(0, int(config.get("gemini_request_count") or 0))
            except (TypeError, ValueError):
                config["gemini_request_count"] = 0
            config["gemini_request_day"] = (config.get("gemini_request_day") or "").strip()
        except Exception:
            pass
    return config


def save_ai_config(config):
    with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_audio_config():
    """Charge la config audio (micro sélectionné manuellement, volume
    appliqué). Stocké par nom de périphérique plutôt que par index :
    l'index sounddevice peut changer d'un lancement à l'autre selon les
    périphériques branchés, alors que le nom reste stable."""
    config = {
        "input_device": None,
        "mic_gain": DEFAULT_MIC_GAIN,
        "mic_gate": DEFAULT_MIC_GATE,
        "listen_mode": DEFAULT_LISTEN_MODE,
        "listen_hotkey": DEFAULT_LISTEN_HOTKEY,
        "kb_layout": DEFAULT_KB_LAYOUT,
        "profile_cycle_hotkey": None,
        "aec_enabled": DEFAULT_AEC_ENABLED,
        "output_device": None,
        "tts_volume": DEFAULT_TTS_VOLUME,
    }
    if os.path.exists(AUDIO_CONFIG_FILE):
        try:
            with open(AUDIO_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if "input_device" in data:
                config["input_device"] = data["input_device"]
            if "mic_gain" in data:
                try:
                    config["mic_gain"] = max(0.1, min(3.0, float(data["mic_gain"])))
                except (TypeError, ValueError):
                    pass
            if "mic_gate" in data:
                try:
                    config["mic_gate"] = max(0, min(MIC_GATE_MAX_RAW, int(data["mic_gate"])))
                except (TypeError, ValueError):
                    pass
            if data.get("listen_mode") in LISTEN_MODES:
                config["listen_mode"] = data["listen_mode"]
            if "listen_hotkey" in data:
                hotkey = (data["listen_hotkey"] or "").strip()
                # Le .lower() ne doit s'appliquer qu'aux combinaisons
                # clavier ("ctrl+f9") : pour un bouton de joystick (préfixe
                # "joy:" suivi d'un JSON {name, guid, button}), le mettre en
                # minuscules casse la comparaison du nom/GUID exact du
                # périphérique lors du prochain lancement (voir
                # _match_joystick), et le bouton n'est alors plus jamais
                # reconnu comme actif.
                if not hotkey.startswith("joy:"):
                    hotkey = hotkey.lower()
                config["listen_hotkey"] = hotkey or None
            if data.get("kb_layout") in ("qwerty", "azerty_fr", "azerty_be"):
                config["kb_layout"] = data["kb_layout"]
            if "profile_cycle_hotkey" in data:
                cycle_hotkey = (data["profile_cycle_hotkey"] or "").strip().lower()
                config["profile_cycle_hotkey"] = cycle_hotkey or None
            if "aec_enabled" in data:
                config["aec_enabled"] = bool(data["aec_enabled"])
            if "output_device" in data:
                config["output_device"] = data["output_device"]
            if "tts_volume" in data:
                try:
                    config["tts_volume"] = max(0.1, min(1.5, float(data["tts_volume"])))
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
    return config


def save_audio_config(config):
    with open(AUDIO_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _enumerate_monitors():
    """Liste tous les écrans actuellement connectés (limites dans l'espace
    du bureau virtuel + échelle DPI de chacun), directement via l'API
    Windows (EnumDisplayMonitors) — pour diagnostiquer automatiquement la
    configuration d'écrans réelle dans le journal système, plutôt que de
    devoir la faire relever manuellement par l'utilisateur : elle peut
    changer à tout moment (écran débranché/rebranché, échelle modifiée),
    donc autant l'interroger à chaque lancement. Renvoie une liste de
    dicts {x, y, width, height, scale} (un par écran), classée par x
    croissant ; liste vide en cas d'échec (ex. hors Windows)."""
    monitors = []
    try:
        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double,
        )
        MDT_EFFECTIVE_DPI = 0

        def _callback(hmonitor, _hdc, rect_ptr, _data):
            rect = rect_ptr.contents
            scale = 1.0
            try:
                dpi_x = ctypes.c_uint()
                dpi_y = ctypes.c_uint()
                hresult = ctypes.windll.shcore.GetDpiForMonitor(
                    hmonitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
                )
                if hresult == 0 and dpi_x.value:  # S_OK
                    scale = dpi_x.value / 96.0
            except Exception:
                pass
            monitors.append({
                "x": rect.left, "y": rect.top,
                "width": rect.right - rect.left, "height": rect.bottom - rect.top,
                "scale": round(scale, 3),
            })
            return 1  # continue l'énumération

        callback = MonitorEnumProc(_callback)
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    except Exception:
        pass
    monitors.sort(key=lambda m: m["x"])
    return monitors


def _scale_for_position(x, y):
    """Facteur d'échelle d'affichage (1.0 = 100%, 1.25 = 125%...) de
    l'écran contenant le point (x, y), déterminé à partir de la liste
    réelle des écrans connectés (voir _enumerate_monitors). Repli utilisé
    par _primary_monitor_scale ci-dessous si aucun écran ne débute
    exactement à (0, 0)."""
    try:
        for m in _enumerate_monitors():
            if m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]:
                return m["scale"] or 1.0
    except Exception:
        pass
    return 1.0


def _primary_monitor_scale():
    """Facteur d'échelle d'affichage (1.0 = 100%, 1.25 = 125%...) de
    l'écran PRINCIPAL — celui dont le coin supérieur gauche est à (0, 0)
    dans l'espace du bureau virtuel Windows, par convention toujours
    l'écran principal.

    Utilisé pour compenser à l'avance un bug de window.move() (voir
    _apply_size_position) : contrairement à l'intuition, ce bug ne dépend
    NI de l'écran de destination, NI de la position courante de la
    fenêtre au moment de l'appel (vérifié empiriquement : une fenêtre
    déjà positionnée sur l'écran secondaire, à 100%, avec une destination
    également sur cet écran, se voyait quand même déplacée en appliquant
    l'échelle de 125% de l'écran PRINCIPAL) — il applique systématiquement
    l'échelle de l'écran principal, quelle que soit la cible. Cohérent
    avec un comportement "System DPI Aware" (une seule échelle pour tout
    le processus, calée sur l'écran principal) côté hébergement WinForms
    interne de pywebview, malgré la déclaration "Per-Monitor DPI Aware"
    faite par ailleurs au niveau du processus Windows (voir
    SetProcessDpiAwareness en tout début de fichier) — les deux couches
    (Win32 natif vs .NET/WinForms) semblent avoir chacune leur propre
    notion de DPI ici, sans être parfaitement synchronisées.

    Repli sur _scale_for_position(0, 0) si aucun écran ne débute
    exactement à (0, 0) (cas normalement impossible sous Windows, mais
    on reste prudent)."""
    try:
        for m in _enumerate_monitors():
            if m["x"] == 0 and m["y"] == 0:
                return m["scale"] or 1.0
    except Exception:
        pass
    return _scale_for_position(0, 0)


def _virtual_screen_bounds():
    """Renvoie (x, y, largeur, hauteur) de l'écran virtuel Windows (l'union
    de tous les écrans connectés), via l'API Windows directement — pour
    valider une position de fenêtre enregistrée sans dépendre de Tkinter
    ou pywebview, qui ne sont pas forcément prêts à ce stade du démarrage.
    Renvoie None en cas d'échec (ex. hors Windows)."""
    try:
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        gm = ctypes.windll.user32.GetSystemMetrics
        return gm(SM_XVIRTUALSCREEN), gm(SM_YVIRTUALSCREEN), gm(SM_CXVIRTUALSCREEN), gm(SM_CYVIRTUALSCREEN)
    except Exception:
        return None


def load_window_config():
    """Charge la taille et la position de la fenêtre principale
    enregistrées à la dernière fermeture/déplacement (voir
    save_window_config). Valeurs de repli (taille par défaut, position
    centrée = x/y à None) si le fichier est absent, illisible, ou contient
    des valeurs aberrantes (fichier corrompu, écran débranché depuis...)."""
    width, height = MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT
    x, y = None, None
    if os.path.exists(WINDOW_CONFIG_FILE):
        try:
            with open(WINDOW_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            width = int(data.get("width", width))
            height = int(data.get("height", height))
            if data.get("x") is not None and data.get("y") is not None:
                x, y = int(data["x"]), int(data["y"])
        except Exception:
            width, height, x, y = MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT, None, None
    width = max(MAIN_WINDOW_MIN_SIZE[0], min(MAIN_WINDOW_MAX_SIZE[0], width))
    height = max(MAIN_WINDOW_MIN_SIZE[1], min(MAIN_WINDOW_MAX_SIZE[1], height))

    if x is not None and y is not None:
        # Ignore une position qui laisserait la fenêtre quasiment
        # entièrement hors de tout écran actuellement connecté (ex. un
        # second écran débranché depuis la dernière fermeture) — mieux
        # vaut revenir au centrage automatique que d'ouvrir une fenêtre
        # introuvable à l'écran.
        bounds = _virtual_screen_bounds()
        if bounds:
            vx, vy, vw, vh = bounds
            margin = 80  # au moins cette marge doit rester visible
            if x + width < vx + margin or x > vx + vw - margin or \
               y + height < vy + margin or y > vy + vh - margin:
                x, y = None, None

    return {"width": width, "height": height, "x": x, "y": y}


def save_window_config(width, height, x=None, y=None):
    try:
        width = max(MAIN_WINDOW_MIN_SIZE[0], min(MAIN_WINDOW_MAX_SIZE[0], int(width)))
        height = max(MAIN_WINDOW_MIN_SIZE[1], min(MAIN_WINDOW_MAX_SIZE[1], int(height)))
    except (TypeError, ValueError):
        return
    data = {"width": width, "height": height}
    if x is not None and y is not None:
        try:
            data["x"] = int(x)
            data["y"] = int(y)
        except (TypeError, ValueError):
            pass
    try:
        with open(WINDOW_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # La taille/position de fenêtre n'est qu'un confort : jamais bloquant.


# --------------------------------------------------------------------------
# Overlay en jeu (fenêtre séparée superposée à Star Citizen)
# --------------------------------------------------------------------------
# APPROCHE VOLONTAIREMENT CHOISIE — À NE JAMAIS CHANGER SANS Y REPENSER :
# ceci crée une fenêtre Windows ORDINAIRE (via pywebview), réglée en
# superposition ("toujours au premier plan") avec un fond transparent, et
# rendue cliquable-au-travers via l'API Windows standard (SetWindowLong /
# WS_EX_TRANSPARENT). Ce n'est PAS une injection dans le processus de
# Star Citizen : aucune DLL n'est chargée dans le jeu, aucun hook n'est
# posé sur son moteur de rendu (DirectX/Vulkan) — techniques qui, elles,
# sont indiscernables de celles utilisées par les logiciels de triche et
# exposeraient le compte du joueur à un risque de bannissement. Cette
# fenêtre est un simple "calque" indépendant que Windows affiche
# par-dessus, exactement comme n'importe quelle fenêtre ordinaire réglée
# en "toujours visible" — le jeu n'a absolument aucune conscience de son
# existence.

def load_overlay_config():
    config = {
        "enabled": False, "x": None, "y": None,
        "visible_rows": {k: True for k in OVERLAY_ROW_KEYS},
    }
    if os.path.exists(OVERLAY_CONFIG_FILE):
        try:
            with open(OVERLAY_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            config["enabled"] = bool(data.get("enabled", False))
            if data.get("x") is not None and data.get("y") is not None:
                config["x"] = int(data["x"])
                config["y"] = int(data["y"])
            saved_rows = data.get("visible_rows")
            if isinstance(saved_rows, dict):
                # Ne prend en compte que les clés connues (OVERLAY_ROW_KEYS) :
                # une clé absente du fichier (ex. nouvelle ligne ajoutée
                # depuis l'enregistrement précédent) garde sa valeur par
                # défaut (visible), plutôt que de faire planter le chargement
                # ou d'ignorer tout le dict à cause d'une clé inconnue.
                for k in OVERLAY_ROW_KEYS:
                    if k in saved_rows:
                        config["visible_rows"][k] = bool(saved_rows[k])
        except Exception:
            pass
    return config


def save_overlay_config(enabled, x=None, y=None, visible_rows=None):
    data = {"enabled": bool(enabled)}
    if x is not None and y is not None:
        try:
            data["x"] = int(x)
            data["y"] = int(y)
        except (TypeError, ValueError):
            pass
    if visible_rows is not None:
        # Toujours réécrit avec l'ensemble complet des clés connues
        # (OVERLAY_ROW_KEYS), même si `visible_rows` en contenait moins —
        # garantit un fichier toujours cohérent avec load_overlay_config,
        # qui s'attend à retrouver chaque clé.
        data["visible_rows"] = {k: bool(visible_rows.get(k, True)) for k in OVERLAY_ROW_KEYS}
    try:
        with open(OVERLAY_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # Simple confort, jamais bloquant — même principe que save_window_config.


# Constantes de l'API Windows utilisées pour rendre l'overlay cliquable-
# au-travers (mode "verrouillé") ou non (mode "déplacer"). GWL_EXSTYLE
# est l'index du "style étendu" d'une fenêtre ; WS_EX_LAYERED est requis
# pour qu'une fenêtre transparente fonctionne correctement, WS_EX_
# TRANSPARENT fait que tous les clics/la souris traversent la fenêtre
# jusqu'à ce qu'il y a en dessous (donc jusqu'au jeu).
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020


def _user32_get_window_long(hwnd):
    """SetWindowLongPtr/GetWindowLongPtr sont les variantes 64 bits
    correctes de ces appels — utilisées en priorité si disponibles
    (toujours le cas sur un Windows 64 bits moderne), avec repli sur les
    variantes 32 bits historiques sinon."""
    func = getattr(ctypes.windll.user32, "GetWindowLongPtrW", None) or ctypes.windll.user32.GetWindowLongW
    return func(hwnd, _GWL_EXSTYLE)


def _user32_set_window_long(hwnd, value):
    func = getattr(ctypes.windll.user32, "SetWindowLongPtrW", None) or ctypes.windll.user32.SetWindowLongW
    return func(hwnd, _GWL_EXSTYLE, value)


def _find_hwnd_by_title(title, timeout=5.0):
    """Attend jusqu'à `timeout` secondes que la fenêtre portant ce titre
    exact existe réellement côté Windows (la création via pywebview est
    asynchrone : le handle natif n'existe pas encore à l'instant où
    create_window() retourne)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
        except Exception:
            hwnd = None
        if hwnd:
            return hwnd
        time.sleep(0.1)
    return None


def _set_window_clickthrough(hwnd, clickthrough):
    """Active/désactive le mode "clic-traversant" d'une fenêtre déjà
    créée, en ne touchant QUE le bit WS_EX_TRANSPARENT. Reste
    délibérément sans effet sur WS_EX_LAYERED : pywebview configure déjà
    la transparence de la fenêtre lui-même (paramètre transparent=True à
    la création, voir _create_overlay_window) via son propre mécanisme
    de composition — le réappliquer nous-mêmes ici cassait le rendu et
    rendait la fenêtre entièrement invisible (bug identifié : fenêtre
    créée sans erreur, mais alpha à zéro partout, alors que WS_EX_
    TRANSPARENT seul suffit pour le clic-traversant, sans dépendre de
    WS_EX_LAYERED).

    Renvoie (style_avant, style_après, transparent_avant, transparent_
    après) pour permettre un diagnostic précis côté appelant — None en
    cas d'échec (ex. hors Windows, handle invalide)."""
    if not hwnd:
        return None
    try:
        style_before = _user32_get_window_long(hwnd)
        style_after = style_before
        if clickthrough:
            style_after |= _WS_EX_TRANSPARENT
        else:
            style_after &= ~_WS_EX_TRANSPARENT
        _user32_set_window_long(hwnd, style_after)
        # Relit immédiatement le style pour confirmer que Windows a bien
        # appliqué le changement (et pas juste accepté silencieusement
        # sans effet réel).
        style_reread = _user32_get_window_long(hwnd)
        return (
            style_before, style_reread,
            bool(style_before & _WS_EX_TRANSPARENT),
            bool(style_reread & _WS_EX_TRANSPARENT),
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# Vérification / installation automatique des dépendances
# --------------------------------------------------------------------------

def _webview_available():
    """Vérifie très rapidement (sans rien installer) si pywebview est déjà
    installé, dans la version compatible attendue (voir requirements.txt :
    pywebview<5.3). Sert uniquement à décider, dans main(), si l'on peut
    ouvrir directement la fenêtre principale (voir _main_fast_path) :
    pywebview est indispensable pour la moindre fenêtre — y compris pour
    afficher un message disant qu'une AUTRE dépendance manque — d'où ce
    contrôle minimal et séparé, avant toute chose. Les autres paquets
    requis (vosk, sounddevice, pydirectinput...) sont eux vérifiés et
    installés si besoin APRÈS coup, une fois la fenêtre déjà ouverte (voir
    ensure_dependencies, appelée par _main_fast_path) — ils n'ont pas
    besoin d'être confirmés avant de pouvoir afficher quoi que ce soit.
    Seul un pywebview manquant ou incompatible force le repli sur l'écran
    de démarrage Tkinter (_main_legacy_path), cas rare en pratique (tout
    premier lancement, ou après suppression manuelle du paquet)."""
    try:
        installed = importlib.metadata.version("pywebview")
        major, minor = (int(x) for x in installed.split(".")[:2])
        return (major, minor) < (5, 3)
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return False


def ensure_dependencies(status_state, on_status=None):
    """Vérifie que chaque paquet requis est importable ; installe via pip
    ceux qui manquent. Met à jour status_state["text"] à chaque étape pour
    que le splash Tkinter (chemin de démarrage de secours, voir
    _main_legacy_path) affiche une progression lisible, et conserve un
    historique dans status_state["history"] pour le journal système.
    on_status(text, kind), si fourni, est appelé en plus à chaque étape en
    temps réel — utilisé par le chemin de démarrage rapide (voir
    _main_fast_path) pour pousser la progression directement dans le
    journal système et l'écran de chargement HTML de la fenêtre déjà
    ouverte, plutôt que d'attendre la fin comme le fait status_state."""
    def set_status(text, kind="info"):
        status_state["text"] = text
        status_state.setdefault("history", []).append((text, kind))
        if on_status:
            on_status(text, kind)

    missing = []

    set_status("Vérification des dépendances...")
    time.sleep(0.3)  # laisse le temps de lire le message sur le splash

    for import_name, pip_spec, label in REQUIRED_PACKAGES:
        set_status(f"Vérification : {label}")

        if import_name == "webview":
            # pywebview a besoin d'une contrainte de version précise
            # (<5.3, voir requirements.txt) : un simple import réussi ne
            # suffit pas, il faut vérifier le numéro installé.
            try:
                installed = importlib.metadata.version("pywebview")
                major, minor = (int(x) for x in installed.split(".")[:2])
                if (major, minor) >= (5, 3):
                    missing.append((import_name, pip_spec, f"{label} (version {installed} incompatible)"))
            except (importlib.metadata.PackageNotFoundError, ValueError):
                missing.append((import_name, pip_spec, label))
        else:
            try:
                importlib.import_module(import_name)
            except ImportError:
                missing.append((import_name, pip_spec, label))
        time.sleep(0.15)

    if not missing:
        set_status("Dépendances déjà installées.", "success")
        time.sleep(0.3)
        return True

    set_status(f"{len(missing)} dépendance(s) manquante(s), installation...", "info")
    time.sleep(0.5)

    failures = []
    for import_name, pip_spec, label in missing:
        set_status(f"Installation : {label}...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", pip_spec]
            )
            set_status(f"Installé : {label}", "success")
        except subprocess.CalledProcessError as e:
            failures.append(label)
            set_status(f"Échec de l'installation : {label}", "error")
            print(f"[Erreur] Échec de l'installation de {pip_spec} : {e}", file=sys.stderr)

    if failures:
        set_status("Échec d'installation : " + ", ".join(failures), "error")
        time.sleep(2.5)
        return False

    set_status("Dépendances installées avec succès.", "success")
    time.sleep(0.4)
    return True


def _import_runtime_dependencies():
    """Importe réellement les modules dans les globales du module, une fois
    ensure_dependencies() passé. Les méthodes de Api y font référence par
    leur nom de module global (vosk, sd, pydirectinput, webview)."""
    global sd, vosk, pydirectinput, webview, keyboard, mouse_lib, pygame, pystray, PIL_Image, PIL_ImageDraw
    global np, AecReferenceBuffer, NLMSEchoCanceller, resample_linear

    import sounddevice as _sd
    import vosk as _vosk
    sd = _sd
    vosk = _vosk

    import numpy as _np
    np = _np
    # Importé seulement maintenant (pas en tête de fichier) car aec.py
    # importe lui-même numpy : le faire plus tôt court-circuiterait la
    # vérification/installation automatique de numpy ci-dessus.
    from aec import AecReferenceBuffer as _AecReferenceBuffer
    from aec import NLMSEchoCanceller as _NLMSEchoCanceller
    from aec import resample_linear as _resample_linear
    AecReferenceBuffer = _AecReferenceBuffer
    NLMSEchoCanceller = _NLMSEchoCanceller
    resample_linear = _resample_linear

    try:
        import pydirectinput as _pdi
        _pdi.PAUSE = 0.02
        # Désactive le fail-safe hérité de PyAutoGUI (abandon de l'action
        # si le curseur souris se trouve dans un coin de l'écran) : pensé
        # pour interrompre un script d'automatisation de bureau qui
        # dérape, il n'a pas de sens ici et casse au contraire les
        # commandes légitimes qui impliquent un clic (ex. "mouseleft") —
        # le curseur peut tout à fait se retrouver dans un coin en jeu
        # sans rapport avec NOVAVOX (TrackIR, jeu qui déplace lui-même le
        # curseur, plusieurs écrans...), ce qui déclenchait alors
        # FailSafeException et empêchait la commande de s'exécuter.
        _pdi.FAILSAFE = False
        pydirectinput = _pdi
    except ImportError:
        pydirectinput = None

    try:
        import keyboard as _keyboard
        keyboard = _keyboard
    except ImportError:
        keyboard = None

    try:
        import mouse as _mouse
        mouse_lib = _mouse
    except ImportError:
        mouse_lib = None

    try:
        import pygame as _pygame
        # Empêche pygame d'ouvrir sa propre fenêtre/contexte vidéo : on ne
        # se sert que du sous-module joystick, pas d'affichage.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        pygame = _pygame
    except ImportError:
        pygame = None

    import webview as _webview
    webview = _webview

    try:
        import pystray as _pystray
        from PIL import Image as _PIL_Image, ImageDraw as _PIL_ImageDraw
        pystray = _pystray
        PIL_Image = _PIL_Image
        PIL_ImageDraw = _PIL_ImageDraw
    except ImportError:
        # Facultatif : sans pystray/Pillow, l'appli fonctionne normalement,
        # simplement sans icône dans la barre des tâches (repli sur
        # l'ancien comportement : fermer la fenêtre quitte directement).
        pystray = None
        PIL_Image = None
        PIL_ImageDraw = None


class Api:
    """Pont entre l'interface web (JS) et la logique Python."""

    def __init__(self):
        global _active_profile_id
        migrated_pid = _ensure_profiles_migrated()
        _active_profile_id = migrated_pid or load_active_profile_id()
        profiles = list_profiles()
        if not _active_profile_id or not any(p["id"] == _active_profile_id for p in profiles):
            # Profil actif introuvable (fichier supprimé manuellement,
            # profiles_config.json obsolète...) : on retombe sur le
            # premier profil disponible plutôt que de charger
            # silencieusement une liste de commandes vide.
            _active_profile_id = profiles[0]["id"] if profiles else None
            if _active_profile_id:
                save_active_profile_id(_active_profile_id)
        if _active_profile_id:
            _, self.commands = _read_profile(_active_profile_id)
            # Resynchronise commands.json sur le profil actif (utile si
            # profiles_config.json a été changé à la main, ou après une
            # réinitialisation partielle) — mirror_to_profile=False car
            # le fichier de profil est déjà la source de cette écriture,
            # pas la peine de le réécrire avec son propre contenu.
            save_commands(self.commands, mirror_to_profile=False)
        else:
            self.commands = load_commands()
        self.model_path = MODEL_DIR_DEFAULT
        # Cache du modèle Vosk déjà chargé (voir _listen_loop) : recharger
        # vosk.Model(model_path) à chaque démarrage d'écoute est coûteux
        # (jusqu'à ~20s observés dans le build compilé, contre ~0.2s en
        # exécution Python directe — vraisemblablement un antivirus qui
        # scrute chaque lecture disque de l'exe compilé) alors que rien ne
        # change entre deux activations tant que le dossier du modèle
        # reste le même. On ne recharge donc que si le chemin a changé
        # (nouveau modèle sélectionné) plutôt qu'à chaque clic sur
        # "Écouter".
        self._vosk_model = None
        self._vosk_model_path = None
        self.listening = False
        self.stop_event = threading.Event()
        self.last_trigger = {}
        self._window = None
        self.ai_history = []
        self.ai_voice_output = True
        ai_config = load_ai_config()
        self.ai_name = ai_config["name"]
        self.ai_enabled = bool(ai_config.get("ai_enabled", True))
        self.ai_model = ai_config["model"] or DEFAULT_OLLAMA_MODEL
        self.confirm_commands_voice = bool(ai_config["confirm_commands"])
        self.ai_custom_context = ai_config.get("custom_context", "") or ""
        self.user_name = ai_config.get("user_name", "") or ""
        self.ai_response_length = ai_config.get("response_length", DEFAULT_RESPONSE_LENGTH)
        if self.ai_response_length not in RESPONSE_LENGTH_INSTRUCTIONS:
            self.ai_response_length = DEFAULT_RESPONSE_LENGTH
        self.piper_voice = ai_config.get("piper_voice") or None
        if self.piper_voice not in PIPER_VOICES:
            self.piper_voice = None
        self.piper_length_scale = ai_config.get("piper_length_scale", DEFAULT_PIPER_LENGTH_SCALE)
        self.piper_noise_scale = ai_config.get("piper_noise_scale", DEFAULT_PIPER_NOISE_SCALE)
        self.radio_effect = bool(ai_config.get("radio_effect", DEFAULT_RADIO_EFFECT))
        # Surveillance en temps réel du Game.log de Star Citizen (kills,
        # morts, destructions de vaisseau, changements de zone...) — voir
        # game_log_watcher.py. Désactivée par défaut : c'est une fonction
        # optionnelle, l'utilisateur l'active depuis les réglages IA.
        self.game_log_enabled = bool(ai_config.get("game_log_enabled", False))
        self.game_log_announce = bool(ai_config.get("game_log_announce_events", True))
        # Ton pseudo RSI exact (Player > Profile sur robertsspaceindustries.com,
        # ou visible en jeu). Sert à distinguer "mon vaisseau détruit" /
        # "je viens de détruire quelqu'un" / "sans rapport avec moi" dans
        # les événements du Game.log (voir _on_game_event). PAS ton
        # prénom d'affichage (self.user_name) : les deux sont volontairement
        # séparés, l'un est un identifiant de jeu, l'autre un prénom pour
        # la conversation.
        self.game_log_player_handle = (ai_config.get("game_log_player_handle") or "").strip()
        # Phrases annoncées pour chaque événement du Game.log, éditables
        # depuis les réglages (voir DEFAULT_GAME_LOG_PHRASES,
        # set_game_log_phrase() et _format_game_log_phrase() plus bas).
        # Fusionnées avec les gabarits par défaut plutôt que remplacées en
        # bloc, pour que toute nouvelle clé ajoutée dans une future version
        # de l'appli récupère automatiquement un défaut même si l'utilisateur
        # a un ai_config.json plus ancien.
        self.game_log_phrases = dict(DEFAULT_GAME_LOG_PHRASES)
        saved_phrases = ai_config.get("game_log_phrases") or {}
        if isinstance(saved_phrases, dict):
            for key, value in saved_phrases.items():
                if key in DEFAULT_GAME_LOG_PHRASES and isinstance(value, str) and value.strip():
                    self.game_log_phrases[key] = value
        # Corrections de lecture pour les notifications HUD précises (voir
        # RE_HUD_NOTIFICATION_START dans game_log_watcher.py) : le texte
        # brut détecté dans le jeu (ex. "VOUS QUITTEZ LA ZONE D'ARMISTICE -
        # PRUDENCE EST MÈRE DE SÛRETÉ") sert de clé exacte, et la valeur
        # associée est ce qui est lu à voix haute à la place. Contrairement
        # à game_log_phrases (gabarits par type d'événement), ceci cible un
        # texte de notification précis parmi tous ceux que le jeu peut
        # afficher — voir set_game_log_hud_override()/_on_game_event().
        raw_overrides = ai_config.get("game_log_hud_overrides") or {}
        self.game_log_hud_overrides = {
            str(k).strip(): str(v)
            for k, v in raw_overrides.items()
            if isinstance(raw_overrides, dict) and str(k).strip() and str(v).strip()
        } if isinstance(raw_overrides, dict) else {}
        # Alias PERSONNALISÉS pour les identifiants de destination bruts
        # (ex. 'rs_entry_nyx_pyro_jp1') qui ne sont pas déjà résolus
        # proprement par KNOWN_LOCATION_ALIASES/RE_OOC_LOCATION dans
        # game_log_watcher.py (voir destination_alias_key) : la clé est
        # l'identifiant normalisé (espaces/tirets/underscores uniformisés),
        # la valeur est le nom à annoncer à la place ; une valeur vide
        # signifie "pas encore personnalisé" (repli sur le nettoyage
        # générique par défaut), l'entrée reste néanmoins dans la liste —
        # voir set_game_log_destination_alias()/_maybe_register_destination_
        # alias() plus bas.
        raw_dest_aliases = ai_config.get("game_log_destination_aliases") or {}
        self.game_log_destination_aliases = {
            str(k).strip(): str(v)
            for k, v in raw_dest_aliases.items()
            if isinstance(raw_dest_aliases, dict) and str(k).strip()
        } if isinstance(raw_dest_aliases, dict) else {}
        self._game_log_watcher = None
        if self.game_log_enabled:
            self._start_game_log_watcher()
        try:
            self.trigger_cooldown = max(0.5, float(ai_config.get("trigger_cooldown", DEFAULT_TRIGGER_COOLDOWN)))
        except (TypeError, ValueError):
            self.trigger_cooldown = DEFAULT_TRIGGER_COOLDOWN
        self._ai_awaiting_question = False
        self._ai_awaiting_since = 0

        # Assistant Gemini — voir la section "Assistant IA Gemini" en tête
        # de fichier. Complètement séparé de Nova/Ollama ci-dessus : sa
        # propre clé API, son propre historique de discussion, son propre
        # nom d'activation vocale (par défaut "Gemini", pour ne jamais
        # entrer en conflit avec "Nova").
        self.gemini_history = []
        self.gemini_voice_output = True
        self.gemini_enabled = bool(ai_config.get("gemini_enabled", True))
        self.gemini_api_key = ai_config.get("gemini_api_key", "") or ""
        self.gemini_model = ai_config.get("gemini_model") or DEFAULT_GEMINI_MODEL
        self.gemini_name = ai_config.get("gemini_name") or DEFAULT_GEMINI_NAME
        self.gemini_response_length = ai_config.get("gemini_response_length", DEFAULT_RESPONSE_LENGTH)
        if self.gemini_response_length not in RESPONSE_LENGTH_INSTRUCTIONS:
            self.gemini_response_length = DEFAULT_RESPONSE_LENGTH
        self.gemini_custom_context = ai_config.get("gemini_custom_context", "") or ""
        # Compteur de requêtes Gemini du jour (RPD, quota gratuit), affiché
        # sous forme de barre de progression dans l'overlay — voir
        # _gemini_quota_state/_gemini_record_request et GEMINI_DAILY_LIMITS.
        self.gemini_request_count = ai_config.get("gemini_request_count", 0) or 0
        self.gemini_request_day = ai_config.get("gemini_request_day", "") or ""
        self._gemini_awaiting_question = False
        self._gemini_awaiting_since = 0

        # Périphérique d'entrée (micro) sélectionné manuellement par
        # l'utilisateur. None = laisser sounddevice utiliser le périphérique
        # par défaut du système. Utile notamment quand le périphérique par
        # défaut de Windows est mal réglé (ex. "Mixage stéréo" au lieu du
        # vrai micro), ce qui ferait entendre à l'appli tout le son joué
        # par le PC (vidéos, musique...) au lieu de seulement la voix.
        audio_config = load_audio_config()
        self.input_device_name = audio_config.get("input_device")
        self.mic_gain = audio_config.get("mic_gain", DEFAULT_MIC_GAIN)
        self.mic_gate = audio_config.get("mic_gate", DEFAULT_MIC_GATE)
        self._last_level_push_time = 0.0

        # Annulation d'écho (AEC, voir aec.py) — préférence persistée,
        # indépendante de sa disponibilité effective (numpy/aec.py pas
        # encore importés à ce stade, voir _import_runtime_dependencies).
        # _echo_canceller/_aec_reference ne sont créés qu'au moment où la
        # capture démarre réellement (voir _ensure_aec_running), pas ici,
        # puisqu'ils dépendent de numpy.
        self.aec_enabled = bool(audio_config.get("aec_enabled", DEFAULT_AEC_ENABLED))
        self._echo_canceller = None
        self._aec_reference = None
        self._aec_loopback_stream = None
        self._aec_loopback_refcount = 0  # combien de consommateurs actifs (écoute + surveillance) ont besoin du flux loopback
        self._aec_lock = threading.Lock()

        # Sortie audio de la synthèse vocale (TTS/Piper) — périphérique de
        # sortie dédié (comme le micro d'entrée) et volume appliqué avant
        # lecture (voir DEFAULT_TTS_VOLUME). Restauré ici après avoir
        # constaté sa disparition (voir _play_wav_file plus bas) : la
        # lecture était repassée par PowerShell/Media.SoundPlayer, qui ne
        # permet ni l'un ni l'autre.
        self.output_device_name = audio_config.get("output_device")
        self.tts_volume = audio_config.get("tts_volume", DEFAULT_TTS_VOLUME)

        # Mode d'activation vocale (bouton seul / touche bascule / push-to-
        # talk) — voir LISTEN_MODES.
        self.listen_mode = audio_config.get("listen_mode", DEFAULT_LISTEN_MODE)
        self.listen_hotkey = audio_config.get("listen_hotkey", DEFAULT_LISTEN_HOTKEY)
        self.kb_layout = audio_config.get("kb_layout", DEFAULT_KB_LAYOUT)
        # Raccourci clavier global de changement de profil de commandes
        # (voir set_profile_cycle_hotkey/_register_profile_cycle_hotkey) —
        # indépendant du système bouton bascule/push-to-talk ci-dessus
        # (listen_hotkey), volontairement plus simple : basé directement
        # sur `keyboard.add_hotkey`, sans support joystick ni logique de
        # maintien, puisqu'il ne fait qu'un cyclage ponctuel.
        self.profile_cycle_hotkey = audio_config.get("profile_cycle_hotkey")
        self._profile_cycle_hotkey_registered = None

        # Surveillance de la touche d'activation (bascule ou push-to-talk)
        # isolée sur son propre thread léger (voir _sync_hotkey_poll). Le
        # callback audio de sounddevice, temps réel, ne fait ensuite que
        # LIRE self._mic_gate_open — il n'appelle jamais lui-même le
        # module `keyboard`, dont les appels (hooks Windows bas niveau)
        # n'ont rien à faire dans un callback audio et peuvent y provoquer
        # un plantage natif.
        self._mic_gate_open = True
        self._hotkey_poll_thread = None
        self._hotkey_poll_stop = threading.Event()
        self._hotkey_poll_prev_held = False

        # État pour la prise en charge d'un bouton de joystick/manette (ex.
        # VirPil) comme touche bascule/push-to-talk : liste des manettes
        # détectées (mise en cache, réénumérée périodiquement — voir
        # _get_joysticks), et signal d'arrêt pour la détection en cours
        # (voir capture_joystick_button).
        self._pygame_joystick_ready = False
        self._joysticks = None
        self._joysticks_refreshed_at = 0.0
        self._joystick_capture_stop = threading.Event()

        # Détection physique d'un clic souris (module "mouse", hook global
        # bas niveau — fonctionne même si le clic n'a pas lieu dans la
        # fenêtre de l'appli), pour assigner un bouton à une commande en
        # cliquant vraiment dessus plutôt qu'en le sélectionnant dans une
        # liste. Voir capture_mouse_button.
        self._mouse_capture_stop = threading.Event()

        # Fil dédié à la simple "surveillance" du niveau micro (sans
        # reconnaissance vocale), utilisé pour animer le mètre de niveau
        # dans les Réglages sans avoir à engager l'écoute complète.
        self._mic_monitor_stop_event = threading.Event()
        self._mic_monitor_thread = None

        # Un thread dédié traite les demandes de synthèse vocale une par
        # une (via une file d'attente), pour lire les réponses dans
        # l'ordre sans les faire se chevaucher. Chaque lecture proprement
        # dite est déléguée à un processus externe et isolé (piper.exe,
        # voir _speak_via_piper) : aucune bibliothèque tierce embarquée
        # dans notre propre processus, donc aucun risque qu'un souci de
        # synthèse vocale ne fasse planter l'application.
        self._tts_queue = queue.Queue()
        self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self._tts_thread.start()

        # Référence vers la lecture sounddevice en cours (le cas échéant),
        # sous forme d'un threading.Event à positionner pour l'interrompre
        # immédiatement si l'utilisateur dit "<nom de l'IA>, stop" pendant
        # que ça parle. Protégé par un verrou car lu/écrit depuis deux
        # threads différents (le thread vocal qui le crée, le thread
        # d'écoute qui peut vouloir l'interrompre) — voir _play_wav_file /
        # _interrupt_speech.
        self._tts_lock = threading.Lock()
        self._tts_stop_event = None

        # Anti-écho : pendant que l'appli parle (confirmation de commande,
        # réponse de l'IA, test de voix), le micro capte cette voix sortie
        # par les haut-parleurs et peut la retranscrire, redéclenchant la
        # même commande en boucle. On ignore donc tout ce qui est reconnu
        # pendant la synthèse vocale, plus une courte marge après (le temps
        # que l'écho résiduel se dissipe) — à l'exception du mot d'arrêt
        # (voir _is_stop_phrase), toujours pris en compte pour permettre
        # d'interrompre une lecture trop longue.
        self._is_speaking = False
        self._speech_mute_until = 0.0
        self._speech_mute_grace = 0.6  # secondes

        # Overlay en jeu (fenêtre séparée superposée à Star Citizen — voir
        # le bloc de commentaires au-dessus de load_overlay_config()). Pas
        # recréée automatiquement ici : la fenêtre principale doit être
        # affichée en premier (voir _on_loaded_extra dans _main_fast_path,
        # qui restaure l'overlay si l'utilisateur l'avait activé lors de
        # la dernière session).
        #
        # IMPORTANT : ce bloc doit rester AVANT l'appel à
        # _sync_hotkey_poll() juste en dessous — celui-ci peut appeler
        # _set_mic_gate(), qui répercute l'état sur l'overlay via
        # _overlay_set_mic(), laquelle accède à self._overlay_last_state.
        # Le définir après aurait provoqué un AttributeError au tout
        # premier lancement (attribut pas encore créé au moment où
        # _sync_hotkey_poll() en a besoin).
        overlay_config = load_overlay_config()
        self._overlay_window = None
        self.overlay_enabled = False  # état RÉEL de la fenêtre, pas juste la préférence sauvegardée
        self.overlay_edit_mode = False
        self._overlay_hwnd = None
        # True pendant une recréation intentionnelle (changement verrouillé
        # <-> déplacer, voir overlay_set_edit_mode) : empêche l'événement
        # "closing" de la fenêtre en cours de destruction d'être traité
        # comme une vraie désactivation par l'utilisateur.
        self._overlay_recreating = False
        # True dès que la fenêtre principale commence à se fermer (voir
        # _wire_main_window_events) : empêche l'événement "closing" de
        # l'overlay, déclenché juste après par la fermeture en cascade,
        # d'être traité comme une désactivation manuelle par
        # l'utilisateur — sans ce garde-fou, quitter l'appli overlay
        # activé effaçait la préférence et l'overlay ne revenait plus au
        # lancement suivant.
        self._app_closing = False
        self._overlay_saved_enabled = overlay_config.get("enabled", False)
        self._overlay_saved_pos = (overlay_config.get("x"), overlay_config.get("y"))
        # Lignes de l'overlay cochées/décochées par l'utilisateur en mode
        # "déplacer" (voir overlay_set_row_visible) : une ligne décochée
        # reste masquée une fois l'overlay verrouillé. Toutes visibles par
        # défaut si rien n'a encore été enregistré.
        self.overlay_visible_rows = dict(
            overlay_config.get("visible_rows") or {k: True for k in OVERLAY_ROW_KEYS}
        )
        # Dernier état connu de chaque info affichée, pour pouvoir tout
        # renvoyer d'un coup dès que l'overlay (ré)ouvre — sinon il
        # resterait vide jusqu'au prochain changement de chaque info.
        self._overlay_last_state = {}

        # Applique dès le démarrage la surveillance de touche si un mode
        # bascule/push-to-talk était sélectionné lors de la dernière
        # session.
        self._sync_hotkey_poll()
        # Idem pour le raccourci de changement de profil (voir
        # set_profile_cycle_hotkey) : sans effet ici si `keyboard` n'est
        # pas encore importé (chemin de démarrage rapide — voir le
        # ré-appel explicite après _import_runtime_dependencies() dans
        # _main_fast_path).
        self._register_profile_cycle_hotkey()

        # Sauvegarde automatique silencieuse (voir _auto_backup_config) :
        # sur un thread à part pour ne jamais retarder le démarrage, même
        # légèrement.
        threading.Thread(target=_auto_backup_config, daemon=True).start()

    def set_window(self, window):
        self._window = window

    # ------------------------------------------------- Appels JS -> ici

    def get_state(self):
        return {
            "commands": self.commands,
            "modelPath": self.model_path,
            "listening": self.listening,
            "modelReady": model_folder_is_valid(self.model_path),
            "availableVoskModels": list(VOSK_MODELS.values()),
            "listenMode": self.listen_mode,
            "listenHotkey": self.listen_hotkey,
            "listenHotkeyAvailable": self._hotkey_module_available(),
            "kbLayout": self.kb_layout,
            "appVersion": get_app_version(),
            "voskVersion": get_vosk_version(),
            "modelInfo": get_model_folder_info(self.model_path),
            "profiles": list_profiles(),
            "activeProfile": _active_profile_id,
            "profileCycleHotkey": self.profile_cycle_hotkey,
        }

    def get_patch_notes(self):
        """Renvoie le contenu de patch_maj.txt (notes de mise à jour,
        éditées manuellement à la racine du projet), affiché quand on
        clique sur le numéro de version dans le pied de page. Reste
        tolérant si le fichier est absent (ex. dossier utilisateur pas
        encore mis à jour) plutôt que de faire planter l'appel."""
        try:
            with open(PATCH_NOTES_FILE, "r", encoding="utf-8-sig") as f:
                return f.read().strip()
        except FileNotFoundError:
            return "Aucune note de mise à jour disponible pour le moment."
        except Exception as e:
            self._log(f"[Erreur] Lecture de patch_maj.txt : {e}", "error")
            return "Impossible de lire les notes de mise à jour."
    def check_for_update(self):
        """Compare la version locale à celle du manifest distant. Ne bloque
        jamais l'appli en cas d'échec (pas de réseau, serveur down, etc.) —
        retourne juste "rien de nouveau" plutôt que de lever une exception
        côté JS.

        Gère deux formats de manifest selon UPDATE_MANIFEST_URL :
        - Format simple (serveur Engooref) : {"version": "...", "url": "..."}
        - Réponse native de l'API GitHub Releases (contient "tag_name" et
          "assets") : la version est déduite du tag (ex. "v0.2.0" → "0.2.0"),
          et l'URL de téléchargement est celle de l'asset nommé
          NovaVox_Setup.exe dans la release."""
        try:
            req = urllib.request.Request(
                UPDATE_MANIFEST_URL, headers={"User-Agent": "NOVAVOX-updater"}
            )
            with urllib.request.urlopen(req, timeout=4) as r:
                raw = r.read().decode("utf-8")
            data = json.loads(raw)
            if "tag_name" in data:
                remote_version = str(data.get("tag_name", "")).strip().lstrip("vV")
                download_url = ""
                for asset in data.get("assets", []) or []:
                    asset_name = str(asset.get("name", "")).strip().lower()
                    if asset_name.endswith(".exe"):
                        download_url = str(asset.get("browser_download_url", "")).strip()
                        break
            else:
                remote_version = str(data.get("version", "")).strip()
                download_url = str(data.get("url", "")).strip()
            local_version = get_app_version()
            self._log(
                f"[Info] Check MAJ : manifest lu ({UPDATE_MANIFEST_URL}) → "
                f"version distante={remote_version!r}, version locale={local_version!r}.",
                "info",
            )
            if remote_version and _version_tuple(remote_version) > _version_tuple(local_version):
                self._log(f"[Info] Nouvelle version disponible : v{remote_version}.", "success")
                return {
                    "available": True,
                    "version": remote_version,
                    "url": download_url,
                }
            else:
                self._log("[Info] Déjà à jour, aucune mise à jour disponible.", "info")
        except Exception as e:
            self._log(f"[Info] Vérification de mise à jour impossible : {e}", "info")
        return {"available": False}

    def open_update_url(self, url):
        """Ouvre le lien de téléchargement dans le navigateur par défaut,
        plutôt que de tenter un remplacement auto de l'exe en cours
        d'exécution (fragile sous Windows, fichier verrouillé pendant que
        l'appli tourne)."""
        url = (url or "").strip()
        if url.startswith("https://") or url.startswith("http://"):
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception as e:
                self._log(f"[Erreur] Impossible d'ouvrir le lien de mise à jour : {e}", "error")
    # --------------------------------------------- Profils de commandes --
    # Voir la section "Profils de commandes" plus haut dans le fichier
    # (_ensure_profiles_migrated, _read_profile, _write_profile...) pour
    # le format sur disque. Ici, uniquement les points d'entrée appelés
    # depuis l'interface (voir la barre de profils dans index.html et
    # onProfile* dans script.js).

    def profiles_list(self):
        return {"profiles": list_profiles(), "active": _active_profile_id}

    def profiles_switch(self, profile_id):
        global _active_profile_id
        profiles = {p["id"]: p for p in list_profiles()}
        if profile_id not in profiles:
            return {"ok": False, "error": "Profil introuvable."}
        if profile_id == _active_profile_id:
            return {"ok": True, "name": profiles[profile_id]["name"], "commands": self.commands}

        # Sauvegarde d'abord les modifications en cours dans le profil
        # qu'on s'apprête à quitter, pour ne rien perdre (édition faite
        # juste avant de basculer, jamais explicitement "enregistrée"
        # puisque chaque action de la liste des commandes s'auto-persiste
        # déjà normalement — sécurité supplémentaire au cas où).
        save_commands(self.commands)

        try:
            name, commands = _read_profile(profile_id)
        except Exception as e:
            return {"ok": False, "error": f"Impossible de charger ce profil : {e}"}

        _active_profile_id = profile_id
        save_active_profile_id(profile_id)
        self.commands = commands
        save_commands(self.commands, mirror_to_profile=False)
        self._log(f"Profil de commandes actif : « {name} ».", "info")
        return {"ok": True, "name": name, "commands": self.commands}

    def profiles_create(self, name, duplicate_current=False):
        global _active_profile_id
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Le nom du profil ne peut pas être vide."}

        pid = _new_profile_id()
        commands = list(self.commands) if duplicate_current else []
        _write_profile(pid, name, commands)

        _active_profile_id = pid
        save_active_profile_id(pid)
        self.commands = commands
        save_commands(self.commands, mirror_to_profile=False)
        self._log(f"Nouveau profil créé : « {name} »" + (" (copie du profil actuel)" if duplicate_current else " (vide)") + ".", "success")
        return {"ok": True, "id": pid, "name": name, "profiles": list_profiles(), "commands": self.commands}

    def profiles_rename(self, profile_id, new_name):
        new_name = (new_name or "").strip()
        if not new_name:
            return {"ok": False, "error": "Le nom du profil ne peut pas être vide."}
        try:
            _, commands = _read_profile(profile_id)
        except Exception:
            return {"ok": False, "error": "Profil introuvable."}
        try:
            _write_profile(profile_id, new_name, commands)
        except Exception as e:
            # Erreur d'écriture (ex. fichier verrouillé momentanément par
            # un service de synchronisation type OneDrive/Dropbox sur le
            # dossier du projet) : on la remonte proprement plutôt que de
            # laisser une exception non gérée traverser le pont JS — et
            # SURTOUT, on ne touche à rien d'autre : contrairement à
            # l'ancien bug de migration, un échec ici ne crée jamais de
            # nouveau profil, il ne fait qu'échouer.
            return {"ok": False, "error": f"Écriture impossible : {e}"}
        self._log(f"Profil renommé : « {new_name} ».", "info")
        return {"ok": True, "profiles": list_profiles()}

    def profiles_delete(self, profile_id):
        global _active_profile_id
        profiles = list_profiles()
        if len(profiles) <= 1:
            return {"ok": False, "error": "Impossible de supprimer le dernier profil restant."}
        if not any(p["id"] == profile_id for p in profiles):
            return {"ok": False, "error": "Profil introuvable."}

        deleted_name = next((p["name"] for p in profiles if p["id"] == profile_id), profile_id)
        try:
            path = _profile_path(profile_id)
            if os.path.isfile(path):
                os.remove(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        self._log(f"Profil supprimé : « {deleted_name} ».", "info")
        remaining = list_profiles()

        if profile_id == _active_profile_id:
            # On vient de supprimer le profil actif : bascule
            # automatiquement sur un autre profil existant plutôt que de
            # laisser l'application sans profil actif valide.
            #
            # IMPORTANT : on met _active_profile_id à None AVANT
            # d'appeler profiles_switch ci-dessous. profiles_switch
            # commence par appeler save_commands(self.commands) pour
            # sauvegarder les modifications en cours dans le profil
            # qu'on quitte — mais ce profil vient justement d'être
            # supprimé : son fichier n'existe plus. Avec
            # _active_profile_id encore sur l'ANCIEN id à ce moment-là,
            # save_commands() écrirait dans un fichier inexistant, qui
            # (via son ancien repli sur DEFAULT_PROFILE_NAME) recréait
            # par erreur un profil "Défaut" fantôme au même identifiant
            # que celui qu'on venait de supprimer — c'est le bug observé
            # ("je supprime Défaut et Défaut revient"). save_commands()
            # est désormais lui-même protégé contre ce cas (voir sa
            # docstring), mais mettre None ici en plus évite même la
            # tentative, par sécurité.
            _active_profile_id = None
            fallback = remaining[0]["id"] if remaining else None
            if fallback:
                switch_result = self.profiles_switch(fallback)
                return {
                    "ok": True, "profiles": remaining, "active": _active_profile_id,
                    "name": switch_result.get("name"), "commands": switch_result.get("commands"),
                }

        return {"ok": True, "profiles": remaining, "active": _active_profile_id}

    def _cycle_profile(self):
        """Bascule vers le profil suivant dans la liste (ordre
        alphabétique, voir list_profiles), en boucle — c'est l'action
        déclenchée par le raccourci clavier global (voir
        _register_profile_cycle_hotkey). Tourne sur le thread interne de
        la bibliothèque `keyboard`, jamais sur le thread UI pywebview :
        self._push()/self._log() restent sûrs à appeler depuis là (déjà
        le cas ailleurs dans le fichier, ex. le thread d'écoute vocale)."""
        profiles = list_profiles()
        if len(profiles) <= 1:
            return
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(_active_profile_id)
        except ValueError:
            idx = -1
        next_id = ids[(idx + 1) % len(ids)]
        result = self.profiles_switch(next_id)
        if result.get("ok"):
            self._push(
                f"profileSwitchedExternally({json.dumps(result.get('name'))}, "
                f"{json.dumps(next_id)}, {json.dumps(self.commands)})"
            )

    def _register_profile_cycle_hotkey(self):
        """(Ré)enregistre le raccourci clavier global de cyclage de
        profil auprès de la bibliothèque `keyboard`. Appelé une première
        fois au démarrage (une fois `keyboard` importé — voir
        _run_startup_checks) puis à chaque changement de raccourci (voir
        set_profile_cycle_hotkey). Sans effet si `keyboard` n'est pas
        disponible (import optionnel échoué) ou si aucun raccourci n'est
        configuré."""
        if keyboard is not None and self._profile_cycle_hotkey_registered:
            try:
                keyboard.remove_hotkey(self._profile_cycle_hotkey_registered)
            except Exception:
                pass
            self._profile_cycle_hotkey_registered = None

        if not self.profile_cycle_hotkey or keyboard is None:
            return
        try:
            keyboard.add_hotkey(self.profile_cycle_hotkey, self._cycle_profile)
            self._profile_cycle_hotkey_registered = self.profile_cycle_hotkey
        except Exception as e:
            self._log(f"[Erreur] Raccourci de changement de profil invalide : {e}", "error")

    def set_profile_cycle_hotkey(self, key):
        key = (key or "").strip().lower() or None
        self.profile_cycle_hotkey = key
        self._persist_audio_config()
        self._register_profile_cycle_hotkey()
        if key:
            self._log(f"Raccourci de changement de profil défini : {key}.", "success")
        else:
            self._log("Raccourci de changement de profil désactivé.", "info")
        return {"ok": True, "hotkey": self.profile_cycle_hotkey}

    def add_command(self, phrase, keys, hold=False, repeat_count=1, repeat_delay=0.1, extra_steps=None):
        phrase = (phrase or "").strip()
        keys = (keys or "").strip().lower()
        if phrase and keys:
            try:
                repeat_count = max(1, min(50, int(repeat_count)))
            except (TypeError, ValueError):
                repeat_count = 1
            try:
                repeat_delay = max(0.0, min(10.0, float(repeat_delay)))
            except (TypeError, ValueError):
                repeat_delay = 0.1
            self.commands.append({
                "type": "command", "phrase": phrase, "keys": keys, "synonyms": [],
                "hold": bool(hold), "repeat_count": repeat_count, "repeat_delay": repeat_delay,
                "extra_steps": _normalize_extra_steps(extra_steps),
            })
            save_commands(self.commands)
        return self.commands

    def add_title(self, text):
        """Ajoute un titre de groupe (ex. « Bouclier ») dans la liste,
        à la suite des commandes existantes. Comme les commandes, un
        titre est un élément de la même liste ordonnée : il peut être
        déplacé (move_item) et supprimé (delete_command) comme n'importe
        quel autre élément."""
        text = (text or "").strip()
        if text:
            self.commands.append({"type": "title", "text": text})
            save_commands(self.commands)
        return self.commands

    def edit_title(self, index, text):
        text = (text or "").strip()
        try:
            item = self.commands[int(index)]
        except (IndexError, ValueError, TypeError):
            return self.commands
        if text and item.get("type") == "title":
            item["text"] = text
            save_commands(self.commands)
        return self.commands

    def move_item(self, index, direction):
        """Déplace un élément (titre OU commande) d'une position vers le
        haut ou le bas dans la liste, en l'échangeant avec son voisin.
        Comme titres et commandes partagent une seule liste ordonnée,
        cette unique méthode gère le déplacement des deux à la fois — pas
        besoin de logique séparée pour "groupe" et "commande"."""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return self.commands
        if index < 0 or index >= len(self.commands):
            return self.commands
        if direction == "up":
            target = index - 1
        elif direction == "down":
            target = index + 1
        else:
            return self.commands
        if target < 0 or target >= len(self.commands):
            return self.commands
        self.commands[index], self.commands[target] = self.commands[target], self.commands[index]
        save_commands(self.commands)
        return self.commands

    def move_item_to(self, from_index, to_index):
        """Déplace un élément (titre OU commande) directement à une
        position arbitraire de la liste, en une seule opération — utilisé
        par le glisser-déposer à la souris (voir dragstart/drop dans
        script.js), qui peut franchir plusieurs positions d'un coup,
        contrairement à move_item ci-dessus qui n'échange qu'avec le
        voisin immédiat.

        `to_index` s'interprète comme "insérer avant cette position DANS
        LA LISTE D'ORIGINE" (donc avant le retrait de l'élément déplacé) —
        c'est ce que calcule naturellement le JS à partir de la carte
        survolée (index de la carte, +1 si on dépose sur sa moitié
        basse). Après le retrait de l'élément, un `to_index` situé après
        `from_index` doit être décalé d'un cran vers la gauche pour
        continuer à viser le même emplacement dans la liste désormais
        amputée d'un élément."""
        try:
            from_index = int(from_index)
            to_index = int(to_index)
        except (TypeError, ValueError):
            return self.commands
        n = len(self.commands)
        if from_index < 0 or from_index >= n:
            return self.commands

        item = self.commands.pop(from_index)
        if to_index > from_index:
            to_index -= 1
        to_index = max(0, min(len(self.commands), to_index))
        self.commands.insert(to_index, item)
        save_commands(self.commands)
        return self.commands

    def add_synonym(self, index, synonym):
        synonym = (synonym or "").strip().lower()
        try:
            cmd = self.commands[int(index)]
        except (IndexError, ValueError, TypeError):
            return self.commands
        if cmd.get("type") == "title":
            return self.commands
        if synonym and synonym not in cmd["synonyms"]:
            cmd["synonyms"].append(synonym)
            save_commands(self.commands)
        return self.commands

    def edit_command(self, index, phrase, keys, hold=None, repeat_count=None, repeat_delay=None, extra_steps=None):
        phrase = (phrase or "").strip()
        keys = (keys or "").strip().lower()
        try:
            cmd = self.commands[int(index)]
        except (IndexError, ValueError, TypeError):
            return self.commands
        if cmd.get("type") == "title":
            return self.commands
        if phrase and keys:
            cmd["phrase"] = phrase
            cmd["keys"] = keys
            if hold is not None:
                cmd["hold"] = bool(hold)
            if repeat_count is not None:
                try:
                    cmd["repeat_count"] = max(1, min(50, int(repeat_count)))
                except (TypeError, ValueError):
                    pass
            if repeat_delay is not None:
                try:
                    cmd["repeat_delay"] = max(0.0, min(10.0, float(repeat_delay)))
                except (TypeError, ValueError):
                    pass
            if extra_steps is not None:
                cmd["extra_steps"] = _normalize_extra_steps(extra_steps)
            save_commands(self.commands)
        return self.commands

    def edit_synonym(self, cmd_index, syn_index, new_text):
        new_text = (new_text or "").strip().lower()
        try:
            cmd = self.commands[int(cmd_index)]
            syn_index = int(syn_index)
            cmd["synonyms"][syn_index]
        except (IndexError, ValueError, TypeError):
            return self.commands
        if new_text:
            cmd["synonyms"][syn_index] = new_text
            save_commands(self.commands)
        return self.commands

    def delete_command(self, index):
        try:
            del self.commands[int(index)]
            save_commands(self.commands)
        except (IndexError, ValueError, TypeError):
            pass
        return self.commands

    def delete_synonym(self, cmd_index, syn_index):
        try:
            del self.commands[int(cmd_index)]["synonyms"][int(syn_index)]
            save_commands(self.commands)
        except (IndexError, ValueError, TypeError):
            pass
        return self.commands

    def speak_command_phrase(self, index):
        """Prononce à voix haute la phrase d'une commande (bouton ▶ du
        panneau), via Piper, pour permettre de vérifier comment elle sera
        entendue une fois reconnue."""
        try:
            cmd = self.commands[int(index)]
        except (IndexError, ValueError, TypeError):
            return {"ok": False, "error": "Commande introuvable."}
        if cmd.get("type") == "title":
            return {"ok": False, "error": "Pas de voix pour un titre."}
        if not self.piper_voice:
            return {"ok": False, "error": "Aucune voix Piper sélectionnée (voir Réglages > Moteur vocal)."}
        self._speak(cmd["phrase"])
        return {"ok": True}

    # ------------------------------------------- Export/Import config --

    # Fichiers de configuration inclus dans export_config/import_config
    # (voir plus bas) : tout ce qui est modifiable par l'utilisateur
    # depuis le panneau, à l'exclusion de window_config.json (géométrie
    # de fenêtre, sans intérêt à exporter/partager).
    _EXPORTABLE_CONFIG_FILES = {
        "commands.json": CONFIG_FILE,
        "ai_config.json": AI_CONFIG_FILE,
        "audio_config.json": AUDIO_CONFIG_FILE,
        "overlay_config.json": OVERLAY_CONFIG_FILE,
    }

    def export_config(self, all_profiles=False):
        """Regroupe les fichiers de configuration modifiables par
        l'utilisateur (commandes, réglages IA/audio/overlay — PAS
        window_config.json, qui n'est qu'une géométrie de fenêtre sans
        intérêt à partager) dans une archive .zip unique, via une boîte
        de dialogue "Enregistrer sous" standard. all_profiles=True inclut
        EN PLUS tous les profils de commandes (voir profiles_list) sous
        profiles/, et profiles_config.json (quel profil est actif) —
        sinon seul le profil actif (déjà présent dans commands.json) est
        exporté, comme avant l'introduction des profils multiples."""
        if not self._window:
            return {"ok": False, "error": "Fenêtre non initialisée."}
        suffix = "_profils" if all_profiles else ""
        default_name = f"novavox_config{suffix}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        try:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=("Archives ZIP (*.zip)",),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not result:
            return {"ok": False, "error": None}  # dialogue annulé par l'utilisateur
        dest_path = result[0] if isinstance(result, (list, tuple)) else result
        if not dest_path:
            return {"ok": False, "error": None}
        if not dest_path.lower().endswith(".zip"):
            dest_path += ".zip"

        try:
            with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, path in Api._EXPORTABLE_CONFIG_FILES.items():
                    if os.path.isfile(path):
                        zf.write(path, arcname=arcname)
                if all_profiles:
                    _profiles_dir()
                    for fname in os.listdir(PROFILES_DIR):
                        if fname.endswith(".json") and not fname.endswith(".tmp"):
                            zf.write(os.path.join(PROFILES_DIR, fname), arcname=f"profiles/{fname}")
                    if os.path.isfile(PROFILES_META_FILE):
                        zf.write(PROFILES_META_FILE, arcname="profiles_config.json")
            self._log(f"Configuration exportée vers {dest_path}.", "success")
            return {"ok": True, "path": dest_path}
        except Exception as e:
            self._log(f"[Erreur] Export de la configuration : {e}", "error")
            return {"ok": False, "error": str(e)}

    def import_config(self):
        """Extrait une archive produite par export_config() ci-dessus et
        écrase les fichiers de configuration correspondants (et les
        profils sous profiles/, si l'archive en contient — voir
        all_profiles ci-dessus). N'applique PAS les changements à chaud
        (recharger commandes + réglages IA + audio + overlay en mémoire
        de façon cohérente reproduirait pratiquement toute la logique
        d'__init__) : l'utilisateur doit fermer puis rouvrir
        l'application, comme après une réinitialisation (voir
        reset_application) — choix délibéré pour rester simple et
        fiable plutôt que de risquer un état à moitié rechargé."""
        if not self._window:
            return {"ok": False, "error": "Fenêtre non initialisée."}
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("Archives ZIP (*.zip)",)
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not result:
            return {"ok": False, "error": None}
        src_path = result[0] if isinstance(result, (list, tuple)) else result
        if not src_path or not os.path.isfile(src_path):
            return {"ok": False, "error": None}

        imported = []
        imported_profiles = []
        try:
            with zipfile.ZipFile(src_path, "r") as zf:
                names = set(zf.namelist())
                # Sécurité : n'extrait QUE les noms de fichiers attendus,
                # jamais un membre d'archive arbitraire — protège contre
                # le "zip slip" (un nom contenant ../../ pour écrire hors
                # du dossier de l'application) et contre l'écrasement
                # accidentel d'un fichier non lié à la config.
                for arcname, dest_path in Api._EXPORTABLE_CONFIG_FILES.items():
                    if arcname not in names:
                        continue
                    data = zf.read(arcname)
                    # Valide que c'est du JSON avant d'écraser la config
                    # actuelle : une archive corrompue ou trafiquée ne
                    # doit jamais pouvoir laisser un fichier de config
                    # à moitié écrit ou invalide sur le disque.
                    json.loads(data.decode("utf-8"))
                    with open(dest_path, "wb") as f:
                        f.write(data)
                    imported.append(arcname)

                # Profils multiples (voir export_config all_profiles=True) :
                # seuls les membres "profiles/<nom>.json" SANS sous-dossier
                # supplémentaire sont acceptés — même logique de défense
                # contre le zip slip que ci-dessus, appliquée ici membre
                # par membre puisque leurs noms exacts ne sont pas connus
                # à l'avance (contrairement à _EXPORTABLE_CONFIG_FILES).
                _profiles_dir()
                for arcname in sorted(names):
                    if not arcname.startswith("profiles/"):
                        continue
                    base = arcname[len("profiles/"):]
                    if not base or "/" in base or "\\" in base or not base.endswith(".json") or base.endswith(".tmp"):
                        continue
                    data = zf.read(arcname)
                    try:
                        json.loads(data.decode("utf-8"))
                    except Exception:
                        continue  # membre corrompu : ignoré plutôt que de faire échouer tout l'import
                    with open(os.path.join(PROFILES_DIR, base), "wb") as f:
                        f.write(data)
                    imported_profiles.append(base)

                if imported_profiles and "profiles_config.json" in names:
                    data = zf.read("profiles_config.json")
                    try:
                        json.loads(data.decode("utf-8"))
                        with open(PROFILES_META_FILE, "wb") as f:
                            f.write(data)
                    except Exception:
                        pass
        except Exception as e:
            self._log(f"[Erreur] Import de la configuration : {e}", "error")
            return {"ok": False, "error": str(e)}

        if not imported and not imported_profiles:
            return {"ok": False, "error": "Aucun fichier de configuration reconnu dans cette archive."}

        if "commands.json" in imported and _active_profile_id and not imported_profiles:
            # commands.json vient d'être écrasé directement par
            # l'archive, en contournant le mécanisme normal de
            # save_commands() : sans cette resynchronisation, le profil
            # actif garderait ses ANCIENNES commandes sur disque, et
            # l'import serait silencieusement perdu au prochain
            # lancement (qui recharge les commandes depuis le profil
            # actif, pas depuis commands.json — voir Api.__init__). Pas
            # nécessaire si l'archive contenait déjà des profils complets
            # (imported_profiles) : le profil actif y est alors inclus
            # tel quel.
            try:
                imported_commands = load_commands()
                name, _ = _read_profile(_active_profile_id)
                _write_profile(_active_profile_id, name, imported_commands)
            except Exception as e:
                self._log(f"[Erreur] Synchronisation du profil actif après import : {e}", "error")

        detail_parts = list(imported)
        if imported_profiles:
            detail_parts.append(f"{len(imported_profiles)} profil(s)")
        detail = ", ".join(detail_parts)
        self._log(f"Configuration importée depuis {src_path} ({detail}).", "success")
        return {"ok": True, "imported": imported, "importedProfiles": len(imported_profiles)}

    def close_application(self):
        """Ferme complètement l'application — utilisé après un import de
        configuration (voir import_config), sur le même principe que la
        fermeture en fin de réinitialisation (reset_application) :
        l'utilisateur relance lui-même l'exécutable ensuite."""
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"ok": True}

    def browse_model(self):
        if not self._window:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        # Selon la version/le backend de pywebview, create_file_dialog peut
        # renvoyer soit un tuple/liste de chemins, soit directement une
        # chaîne (un seul dossier sélectionné). En traitant toujours le
        # résultat comme une liste (result[0]), une chaîne se retrouvait
        # indexée caractère par caractère : on ne récupérait alors que "C"
        # (le premier caractère de "C:\...") au lieu du chemin complet. On
        # gère donc explicitement les deux cas.
        if isinstance(result, (list, tuple)):
            path = result[0] if result else None
        else:
            path = result
        if not path:
            return None
        self.model_path = path
        return self.model_path

    def get_model_info(self, path=None):
        """Nom/taille du dossier de modèle Vosk à afficher à côté de la
        version de Vosk (voir get_model_folder_info) — appelé par le JS
        après un changement de dossier (Parcourir / téléchargement d'un
        nouveau modèle) pour rafraîchir l'infobulle sans redemander tout
        l'état de l'appli. Sans argument, utilise le dossier actuellement
        retenu (self.model_path)."""
        return get_model_folder_info(path or self.model_path)

    # ------------------------------------------------- Réinitialisation --

    def reset_application(self, remove_ollama=False):
        """Point d'entrée appelé par le bouton "Réinitialiser l'application"
        des Réglages. Remet l'application dans son état de départ (juste
        après compilation, avant tout premier lancement) : supprime le
        modèle vocal téléchargé, les journaux et le raccourci Bureau,
        réinitialise commandes et réglages aux valeurs par défaut, et
        désinstalle Ollama + ses modèles si demandé. Ne supprime PAS le
        programme lui-même (l'exécutable et ses fichiers restent en place,
        réutilisables) mais ferme l'application à la fin — exactement
        l'état d'un dossier tout juste compilé, jamais encore lancé.
        Tourne sur un thread dédié pour ne jamais bloquer l'interface
        pendant l'opération."""
        threading.Thread(target=self._reset_application_thread, args=(bool(remove_ollama),), daemon=True).start()
        return {"ok": True}

    def _reset_application_thread(self, remove_ollama):
        self._log("Réinitialisation en cours...", "info")

        # 1) Couper l'écoute, la surveillance micro et toute synthèse
        #    vocale en cours.
        try:
            self.stop_event.set()
            self.listening = False
            self.stop_mic_monitor()
            self._interrupt_speech()
        except Exception:
            pass

        # 2) Retirer le raccourci Bureau créé automatiquement au premier
        #    lancement : au tout premier démarrage compilé, il n'existait
        #    pas encore. Il sera recréé de lui-même au prochain lancement
        #    de l'exécutable (voir ensure_desktop_shortcut).
        self._remove_desktop_shortcut()

        # 3) Désinstaller Ollama et ses modèles téléchargés, si demandé.
        if remove_ollama:
            self._uninstall_ollama()

        # 4) Supprimer le modèle vocal téléchargé -- uniquement le dossier
        #    "model" par défaut créé par l'appli elle-même (téléchargé via
        #    download_vosk_model) ; si l'utilisateur avait choisi un
        #    dossier externe via "Parcourir", on n'y touche pas, il ne
        #    fait pas partie de ce que l'appli a installé.
        try:
            if os.path.isdir(MODEL_DIR_DEFAULT):
                shutil.rmtree(MODEL_DIR_DEFAULT, ignore_errors=True)
                self._log("Modèle vocal téléchargé supprimé.", "success")
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression du modèle : {e}", "error")

        # 4bis) Supprimer Piper (moteur vocal optionnel) et ses voix
        #       téléchargées, dans le même esprit que le modèle Vosk.
        try:
            if os.path.isdir(PIPER_DIR):
                shutil.rmtree(PIPER_DIR, ignore_errors=True)
                self._log("Moteur vocal Piper et ses voix supprimés.", "success")
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression de Piper : {e}", "error")

        # 5) Vider erreurs.log (fermer le handler d'abord : le fichier
        #    reste ouvert en continu par le logger, donc verrouillé sous
        #    Windows tant qu'on ne le referme pas), puis crash_log.txt.
        self._reset_error_log_file()
        try:
            crash_log = os.path.join(BASE_DIR, "crash_log.txt")
            if os.path.isfile(crash_log):
                os.remove(crash_log)
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression de crash_log.txt : {e}", "error")

        # 6) Réinitialiser commandes et réglages (fichiers + mémoire) aux
        #    valeurs par défaut, comme au tout premier lancement.
        self._reset_config_files_and_state()

        # 7) Fermer l'application : c'est ce qu'on attend d'une remise à
        #    zéro complète (l'utilisateur relance lui-même l'exécutable
        #    ensuite, qui recréera le raccourci Bureau au passage).
        self._log("Réinitialisation terminée. Fermeture de l'application...", "success")
        time.sleep(1.5)  # laisse le temps au message d'atteindre le panneau avant la fermeture
        os._exit(0)

    def _remove_desktop_shortcut(self):
        """Supprime le raccourci Bureau créé par ensure_desktop_shortcut()
        au premier lancement. Reste silencieux si absent (déjà supprimé
        manuellement) ou hors Windows."""
        if sys.platform != "win32":
            return
        try:
            script = (
                "$desktop = [Environment]::GetFolderPath('Desktop'); "
                "$path = Join-Path $desktop 'NOVAVOX.lnk'; "
                "if (Test-Path $path) { Remove-Item -Path $path -Force }"
            )
            encoded_cmd = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags, timeout=10,
            )
            self._log("Raccourci Bureau supprimé.", "success")
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression du raccourci : {e}", "error")

    def _reset_error_log_file(self):
        """Ferme proprement le handler de erreurs.log (le fichier reste
        verrouillé sous Windows tant qu'il est ouvert), le supprime, puis
        recrée un handler tout neuf pour que la journalisation continue de
        fonctionner normalement après la réinitialisation."""
        for h in list(error_logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            error_logger.removeHandler(h)
        try:
            if os.path.isfile(ERROR_LOG_FILE):
                os.remove(ERROR_LOG_FILE)
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression de erreurs.log : {e}", "error")
        try:
            handler = RotatingFileHandler(
                ERROR_LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            ))
            error_logger.addHandler(handler)
        except Exception:
            error_logger.addHandler(logging.NullHandler())

    def _reset_config_files_and_state(self):
        """Réécrit commands.json / ai_config.json / audio_config.json avec
        les valeurs par défaut, et met à jour en conséquence l'état en
        mémoire de l'API (utile même si l'interface va être rechargée
        juste après, pour que tout reste cohérent immédiatement)."""
        self.commands = _normalize_commands(DEFAULT_COMMANDS)
        save_commands(self.commands)
        self.last_trigger = {}

        self.ai_name = DEFAULT_AI_NAME
        self.ai_enabled = True
        self.confirm_commands_voice = False
        self.ai_model = DEFAULT_OLLAMA_MODEL
        self.trigger_cooldown = DEFAULT_TRIGGER_COOLDOWN
        self.ai_custom_context = ""
        self.user_name = ""
        self.ai_response_length = DEFAULT_RESPONSE_LENGTH
        self.piper_voice = None
        self.piper_length_scale = DEFAULT_PIPER_LENGTH_SCALE
        self.piper_noise_scale = DEFAULT_PIPER_NOISE_SCALE
        self.radio_effect = DEFAULT_RADIO_EFFECT
        self.game_log_enabled = False
        self.game_log_announce = True
        self.game_log_player_handle = ""
        self.game_log_phrases = dict(DEFAULT_GAME_LOG_PHRASES)
        self.game_log_hud_overrides = {}
        self.game_log_destination_aliases = {}
        if self._game_log_watcher:
            self._game_log_watcher.stop()
            self._game_log_watcher = None
        self.ai_history = []
        self.gemini_history = []
        self.gemini_enabled = True
        self.gemini_api_key = ""
        self.gemini_model = DEFAULT_GEMINI_MODEL
        self.gemini_name = DEFAULT_GEMINI_NAME
        self.gemini_response_length = DEFAULT_RESPONSE_LENGTH
        self.gemini_custom_context = ""
        self.gemini_request_count = 0
        self.gemini_request_day = ""
        save_ai_config({
            "name": self.ai_name,
            "voice": self.ai_voice,
            "confirm_commands": self.confirm_commands_voice,
            "ai_enabled": self.ai_enabled,
            "model": self.ai_model,
            "trigger_cooldown": self.trigger_cooldown,
            "custom_context": self.ai_custom_context,
            "user_name": self.user_name,
            "response_length": self.ai_response_length,
            "piper_voice": self.piper_voice,
            "piper_length_scale": self.piper_length_scale,
            "piper_noise_scale": self.piper_noise_scale,
            "radio_effect": self.radio_effect,
            "game_log_enabled": self.game_log_enabled,
            "game_log_announce_events": self.game_log_announce,
            "game_log_player_handle": self.game_log_player_handle,
            "game_log_phrases": self.game_log_phrases,
            "game_log_hud_overrides": self.game_log_hud_overrides,
            "game_log_destination_aliases": self.game_log_destination_aliases,
            "gemini_enabled": self.gemini_enabled,
            "gemini_api_key": self.gemini_api_key,
            "gemini_model": self.gemini_model,
            "gemini_name": self.gemini_name,
            "gemini_response_length": self.gemini_response_length,
            "gemini_custom_context": self.gemini_custom_context,
            "gemini_request_count": self.gemini_request_count,
            "gemini_request_day": self.gemini_request_day,
        })

        self.input_device_name = None
        self.mic_gain = DEFAULT_MIC_GAIN
        self.mic_gate = DEFAULT_MIC_GATE
        self.listen_mode = DEFAULT_LISTEN_MODE
        self.listen_hotkey = DEFAULT_LISTEN_HOTKEY
        self.kb_layout = DEFAULT_KB_LAYOUT
        self.profile_cycle_hotkey = None
        self.aec_enabled = DEFAULT_AEC_ENABLED
        self.output_device_name = None
        self.tts_volume = DEFAULT_TTS_VOLUME
        self._sync_aec_state()
        self._register_profile_cycle_hotkey()
        self._sync_hotkey_poll()
        save_audio_config({
            "input_device": self.input_device_name,
            "mic_gain": self.mic_gain,
            "mic_gate": self.mic_gate,
            "listen_mode": self.listen_mode,
            "listen_hotkey": self.listen_hotkey,
            "kb_layout": self.kb_layout,
            "aec_enabled": self.aec_enabled,
            "output_device": self.output_device_name,
            "tts_volume": self.tts_volume,
        })

        self.model_path = MODEL_DIR_DEFAULT

        # Profils de commandes : supprimés eux aussi pour repartir sur un
        # unique profil "Défaut" (recréé automatiquement au prochain
        # lancement par _ensure_profiles_migrated), cohérent avec
        # l'esprit "état de départ" de cette réinitialisation.
        global _active_profile_id
        try:
            if os.path.isdir(PROFILES_DIR):
                shutil.rmtree(PROFILES_DIR, ignore_errors=True)
            if os.path.isfile(PROFILES_META_FILE):
                os.remove(PROFILES_META_FILE)
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression des profils : {e}", "error")
        _active_profile_id = None

        # Taille de fenêtre mémorisée : supprimée elle aussi pour repartir
        # sur la taille par défaut, cohérence avec le reste des réglages
        # remis à zéro ci-dessus.
        try:
            if os.path.isfile(WINDOW_CONFIG_FILE):
                os.remove(WINDOW_CONFIG_FILE)
        except Exception as e:
            self._log(f"[Erreur réinitialisation] Suppression de window_config.json : {e}", "error")

    def _uninstall_ollama(self):
        """Désinstalle Ollama et ses modèles téléchargés, si Ollama est
        présent sur cette machine. Cherche son désinstalleur officiel via
        le registre Windows (emplacement standard où tout logiciel
        installé avec un installeur classique déclare comment se retirer)
        plutôt que de deviner un chemin ; le lance ensuite tel quel — sans
        supposer d'option silencieuse spécifique, qui varie selon la
        version d'Ollama — donc une dernière fenêtre de confirmation peut
        apparaître côté désinstalleur. Termine en supprimant le dossier de
        données restant (~/.ollama), pas toujours nettoyé par le
        désinstalleur."""
        if sys.platform != "win32":
            self._log("[Info] Désinstallation d'Ollama non automatisée hors Windows.", "info")
            return

        exe_path = find_ollama_executable()
        if exe_path:
            try:
                self._log("Arrêt du service Ollama...", "info")
                creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                for proc_name in ("ollama.exe", "ollama app.exe"):
                    subprocess.run(
                        ["taskkill", "/IM", proc_name, "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=creationflags, timeout=10,
                    )
            except Exception:
                pass

        uninstall_cmd = self._find_ollama_uninstall_command()
        if uninstall_cmd:
            try:
                self._log("Lancement du désinstalleur Ollama...", "info")
                # Sous Windows, passer une chaîne de commande complète à
                # Popen (sans shell=True) fonctionne directement — le
                # backend Windows (CreateProcess) sait analyser les
                # guillemets et arguments d'une UninstallString telle
                # quelle. shell=True n'était donc pas nécessaire ici et
                # exposait à une injection de commande si jamais la
                # valeur du registre contenait des métacaractères shell
                # (peu probable pour Ollama, mais évitable sans coût).
                subprocess.Popen(uninstall_cmd)
                time.sleep(3)  # laisse le temps au désinstalleur de démarrer
            except Exception as e:
                self._log(f"[Erreur désinstallation] Désinstalleur Ollama : {e}", "error")
        else:
            self._log(
                "Désinstalleur Ollama introuvable automatiquement. Désinstalle-le "
                "manuellement via Paramètres Windows > Applications si besoin.",
                "error",
            )

        ollama_data_dir = os.path.expanduser("~/.ollama")
        try:
            if os.path.isdir(ollama_data_dir):
                shutil.rmtree(ollama_data_dir, ignore_errors=True)
                self._log("Dossier de données Ollama supprimé (modèles inclus).", "success")
        except Exception as e:
            self._log(f"[Erreur désinstallation] Suppression des données Ollama : {e}", "error")

    def _find_ollama_uninstall_command(self):
        """Cherche la commande de désinstallation officielle d'Ollama dans
        le registre Windows (HKLM/HKCU > ... > Uninstall), l'emplacement
        standard où tout programme installé via un installeur classique
        déclare comment se désinstaller. Retourne None si introuvable
        (Ollama absent, ou installé d'une façon non standard)."""
        try:
            script = (
                "$paths = @("
                "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*', "
                "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*', "
                "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'"
                "); "
                "Get-ItemProperty $paths -ErrorAction SilentlyContinue | "
                "Where-Object { $_.DisplayName -like '*Ollama*' } | "
                "Select-Object -First 1 -ExpandProperty UninstallString"
            )
            encoded_cmd = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_cmd],
                capture_output=True, text=True, creationflags=creationflags, timeout=15,
            )
            cmd = (result.stdout or "").strip()
            return cmd or None
        except Exception:
            return None

    # ------------------------------------------------- Microphone / audio

    def _resolve_input_device(self):
        """Retrouve l'index sounddevice correspondant au micro choisi par
        l'utilisateur (stocké par nom). Retourne None (= périphérique par
        défaut du système) si aucun micro n'est sélectionné, ou si celui
        sélectionné n'est plus disponible (débranché, pilote changé...)."""
        if not self.input_device_name:
            return None
        try:
            devices = sd.query_devices()
            for idx, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0 and d.get("name") == self.input_device_name:
                    return idx
        except Exception:
            pass
        self._log(
            f"[Avertissement] Micro sélectionné introuvable ({self.input_device_name}), "
            f"retour au périphérique par défaut.",
            "error",
        )
        return None

    # ------------------------------------------------- Annulation d'écho --
    # Voir aec.py pour l'algorithme (NLMS) et le tampon de référence. Les
    # méthodes ci-dessous ne font "que" la plomberie : démarrer/arrêter la
    # capture "loopback" du son actuellement joué par le PC (le signal de
    # référence), et appliquer le filtre à chaque bloc micro. La capture
    # loopback est partagée entre l'écoute complète et la simple
    # surveillance du niveau (voir _aec_should_be_active) : pas besoin de
    # deux flux loopback distincts, l'un ou l'autre suffit à justifier sa
    # présence.

    def _aec_should_be_active(self):
        monitor_alive = bool(self._mic_monitor_thread and self._mic_monitor_thread.is_alive())
        return bool(self.aec_enabled and np is not None and (self.listening or monitor_alive))

    def _sync_aec_state(self):
        """Démarre ou arrête la capture loopback de référence selon l'état
        courant (activé par l'utilisateur + écoute/surveillance en cours).
        Idempotent — peut être appelée à tout moment sans effet de bord si
        l'état voulu est déjà celui en place."""
        with self._aec_lock:
            should_run = self._aec_should_be_active()
            running = self._aec_loopback_stream is not None
            if should_run and not running:
                self._start_aec_loopback_locked()
            elif not should_run and running:
                self._stop_aec_loopback_locked()

    def _find_stereo_mix_device_index(self):
        """Retrouve l'index du périphérique d'entrée "Mixage stéréo" (ou
        équivalent selon la langue/le pilote : "Stereo Mix", "What U
        Hear", "Wave Out Mix"...). C'est un vrai périphérique d'ENTRÉE
        aux yeux de sounddevice/PortAudio — pas besoin d'astuce API
        particulière contrairement à ce qu'on pensait au départ (voir
        commentaire plus bas) — mais il doit être activé manuellement
        côté Windows, où il est très souvent présent mais désactivé par
        défaut."""
        keywords = (
            "stereo mix", "mixage stéréo", "mixage stereo",
            "what u hear", "wave out mix", "loopback",
        )
        devices = sd.query_devices()
        for idx, d in enumerate(devices):
            name = str(d.get("name", "")).lower()
            if d.get("max_input_channels", 0) > 0 and any(k in name for k in keywords):
                return idx
        raise RuntimeError(
            "Aucun périphérique \"Mixage stéréo\" trouvé/activé. Active-le : "
            "clic droit sur l'icône son -> Paramètres de son -> Plus de "
            "paramètres de son -> onglet Enregistrement -> clic droit dans "
            "la liste -> \"Afficher les périphériques désactivés\" -> "
            "active \"Mixage stéréo\"."
        )

    def _start_aec_loopback_locked(self):
        """Démarre la capture du signal de référence (voir
        _find_stereo_mix_device_index). À appeler avec self._aec_lock
        déjà tenu (voir _sync_aec_state).

        Note historique : la première version de cette fonction tentait
        d'utiliser une vraie capture loopback WASAPI via
        sd.WasapiSettings(loopback=True) — plus simple pour l'utilisateur
        (rien à activer côté Windows) mais qui s'est révélée ne pas
        exister dans sounddevice/PortAudio (le paramètre "loopback" n'a
        jamais fait partie de son API). D'où le choix de "Mixage stéréo"
        à la place : moins pratique (nécessite une activation manuelle),
        mais qui repose sur un vrai périphérique d'entrée standard,
        capturé exactement comme le micro lui-même."""
        try:
            device_index = self._find_stereo_mix_device_index()
            device_info = sd.query_devices(device_index, "input")
            in_samplerate = int(device_info.get("default_samplerate") or 44100)
            in_channels = max(1, min(2, int(device_info.get("max_input_channels") or 2)))

            self._aec_loopback_samplerate = in_samplerate
            self._aec_reference = AecReferenceBuffer(SAMPLE_RATE, AEC_REFERENCE_HISTORY_SECONDS)
            self._echo_canceller = NLMSEchoCanceller(
                filter_length=max(64, int(SAMPLE_RATE * AEC_FILTER_MS / 1000)),
                mu=AEC_MU,
            )

            stream = sd.InputStream(
                samplerate=in_samplerate,
                blocksize=0,
                device=device_index,
                channels=in_channels,
                dtype="float32",
                callback=self._aec_loopback_callback,
            )
            stream.start()
            self._aec_loopback_stream = stream
            self._log(
                f"Annulation d'écho : capture de référence démarrée ({device_info.get('name')}).",
                "info",
            )
        except Exception as e:
            self._log(f"[AEC] Impossible de démarrer la capture de référence : {e}", "error")
            self._aec_loopback_stream = None
            self._echo_canceller = None
            self._aec_reference = None

    def _stop_aec_loopback_locked(self):
        """Arrête et libère le flux loopback. À appeler avec
        self._aec_lock déjà tenu (voir _sync_aec_state)."""
        stream = self._aec_loopback_stream
        self._aec_loopback_stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._echo_canceller = None
        self._aec_reference = None

    def _aec_loopback_callback(self, indata, frames, time_info, status):
        """Callback temps réel du flux loopback : réduit à un canal,
        ré-échantillonne à SAMPLE_RATE et alimente le tampon de référence
        partagé (voir aec.AecReferenceBuffer). Ne doit jamais lever
        d'exception (callback audio PortAudio)."""
        try:
            if indata.ndim == 2 and indata.shape[1] > 1:
                mono = indata.mean(axis=1)
            else:
                mono = indata.reshape(-1)
            # dtype='float32' -> échelle [-1, 1] ; remise à l'échelle
            # int16 pour rester cohérent avec le flux micro (voir
            # _apply_echo_cancellation, qui travaille en int16).
            mono = mono.astype(np.float64) * 32768.0
            resampled = resample_linear(mono, self._aec_loopback_samplerate, SAMPLE_RATE)
            if self._aec_reference is not None:
                self._aec_reference.push(resampled)
        except Exception:
            pass

    def _apply_echo_cancellation(self, raw_bytes, frames):
        """Applique l'annulation d'écho au bloc micro brut (bytes int16
        mono) si elle est active et que suffisamment de signal de
        référence est déjà disponible ; sinon, retourne le bloc tel quel
        sans y toucher (dégradation silencieuse plutôt que blocage)."""
        if self._echo_canceller is None or self._aec_reference is None:
            return raw_bytes
        ref = self._aec_reference.pull_latest(frames)
        if ref is None:
            return raw_bytes
        try:
            mic_arr = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float64)
            cleaned = self._echo_canceller.process_block(mic_arr, ref)
            np.clip(cleaned, -32768, 32767, out=cleaned)
            return cleaned.astype(np.int16).tobytes()
        except Exception as e:
            self._log(f"[AEC] {e}", "error")
            return raw_bytes

    def set_aec_enabled(self, enabled):
        """Active/désactive l'annulation d'écho. Persisté immédiatement ;
        démarre/arrête la capture loopback tout de suite si une écoute ou
        une surveillance du niveau tourne déjà (voir _sync_aec_state)."""
        self.aec_enabled = bool(enabled)
        self._persist_audio_config()
        if self.aec_enabled and np is None:
            self._log(
                "[AEC] NumPy n'a pas pu être chargé — l'annulation d'écho reste inactive malgré l'activation.",
                "error",
            )
        self._sync_aec_state()
        self._log(f"Annulation d'écho {'activée' if self.aec_enabled else 'désactivée'}.", "info")
        return {"ok": True, "enabled": self.aec_enabled}

    # ------------------------------------------------------------ Micro --

    def start_mic_monitor(self):
        """Démarre une simple surveillance du niveau micro (sans charger
        Vosk ni faire de reconnaissance), pour animer le mètre de niveau
        des Réglages sans avoir à engager l'écoute complète. Sans effet
        si l'écoute complète tourne déjà (elle pousse déjà le niveau) ou
        si une surveillance est déjà en cours."""
        if self.listening:
            return {"ok": True}
        if self._mic_monitor_thread and self._mic_monitor_thread.is_alive():
            return {"ok": True}
        self._mic_monitor_stop_event.clear()
        self._mic_monitor_thread = threading.Thread(target=self._mic_monitor_loop, daemon=True)
        self._mic_monitor_thread.start()
        self._sync_aec_state()
        return {"ok": True}

    def stop_mic_monitor(self):
        """Arrête la surveillance du niveau micro démarrée par
        start_mic_monitor. Sans effet si elle n'était pas active. L'arrêt
        réel de la capture loopback AEC (le cas échéant) est fait par
        _mic_monitor_loop lui-même à sa sortie effective (voir son bloc
        finally) plutôt qu'ici, pour éviter une coupure prématurée pendant
        que le thread termine encore son dernier tour de boucle."""
        self._mic_monitor_stop_event.set()
        return {"ok": True}

    def _mic_monitor_loop(self):
        input_device = self._resolve_input_device()

        def callback(indata, frames, time_info, status):
            raw = bytes(indata)
            if self.aec_enabled:
                raw = self._apply_echo_cancellation(raw, frames)
            rms = compute_rms(raw)
            now = time.time()
            if now - self._last_level_push_time > 0.12:
                self._last_level_push_time = now
                self._push(f"micLevelUpdate({rms})")

        try:
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=2048,
                device=input_device,
                dtype="int16",
                channels=1,
                callback=callback,
            ):
                while not self._mic_monitor_stop_event.is_set():
                    time.sleep(0.05)
        except Exception as e:
            self._log(f"[Erreur micro] Impossible de surveiller le niveau du micro : {e}", "error")
        finally:
            self._sync_aec_state()

    def get_audio_devices(self):
        """Liste les périphériques d'entrée disponibles, pour le menu
        déroulant du panneau. Indique lequel est le défaut système et
        lequel est actuellement sélectionné dans l'appli, ainsi que le
        volume et le seuil de sensibilité micro actuellement appliqués."""
        if sd is None:
            # Sur le chemin de démarrage rapide (voir _main_fast_path), les
            # dépendances (dont sounddevice) sont vérifiées/importées en
            # arrière-plan une fois la fenêtre déjà affichée : cet état est
            # transitoire et normal les toutes premières secondes, pas une
            # vraie erreur — pas la peine d'inquiéter l'utilisateur avec un
            # message [Erreur] pour ça.
            return {
                "devices": [], "selected": self.input_device_name,
                "gain": self.mic_gain, "gate": self.mic_gate, "gateMax": MIC_GATE_MAX_RAW,
                "aecEnabled": self.aec_enabled, "aecAvailable": np is not None,
            }
        try:
            devices = sd.query_devices()
        except Exception as e:
            self._log(f"[Erreur micro] Impossible de lister les périphériques audio : {e}", "error")
            return {
                "devices": [], "selected": self.input_device_name,
                "gain": self.mic_gain, "gate": self.mic_gate, "gateMax": MIC_GATE_MAX_RAW,
                "aecEnabled": self.aec_enabled, "aecAvailable": np is not None,
            }

        try:
            default_name = sd.query_devices(None, "input")["name"]
        except Exception:
            default_name = None

        result = []
        for idx, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                result.append({
                    "index": idx,
                    "name": d.get("name", f"Périphérique {idx}"),
                    "isSystemDefault": bool(default_name) and d.get("name") == default_name,
                })

        return {
            "devices": result, "selected": self.input_device_name,
            "gain": self.mic_gain, "gate": self.mic_gate, "gateMax": MIC_GATE_MAX_RAW,
            "aecEnabled": self.aec_enabled, "aecAvailable": np is not None,
        }

    def _persist_audio_config(self):
        save_audio_config({
            "input_device": self.input_device_name,
            "mic_gain": self.mic_gain,
            "mic_gate": self.mic_gate,
            "listen_mode": self.listen_mode,
            "listen_hotkey": self.listen_hotkey,
            "kb_layout": self.kb_layout,
            "profile_cycle_hotkey": self.profile_cycle_hotkey,
            "aec_enabled": self.aec_enabled,
            "output_device": self.output_device_name,
            "tts_volume": self.tts_volume,
        })

    def set_input_device(self, device_name):
        """Change le micro utilisé pour l'écoute. device_name vide/None =
        revenir au périphérique par défaut du système."""
        device_name = (device_name or "").strip() or None
        self.input_device_name = device_name
        self._persist_audio_config()
        if device_name:
            self._log(f"Micro sélectionné : {device_name}", "info")
        else:
            self._log("Micro : retour au périphérique par défaut du système.", "info")
        return {"ok": True, "selected": self.input_device_name}

    def set_mic_gain(self, value):
        """Change le volume/amplification appliqué en interne au flux
        micro (1.0 = inchangé). Prend effet immédiatement, même en cours
        d'écoute, puisque le gain est relu à chaque bloc audio."""
        try:
            gain = float(value)
        except (TypeError, ValueError):
            return self.mic_gain
        gain = max(0.1, min(3.0, gain))
        self.mic_gain = gain
        self._persist_audio_config()
        return self.mic_gain

    def set_mic_gate(self, value):
        """Change le seuil de coupure (noise gate) : tout bloc audio dont
        le niveau RMS est en dessous de cette valeur est remplacé par du
        silence avant d'être envoyé au moteur de reconnaissance. 0 =
        désactivé (rien n'est coupé)."""
        try:
            gate = int(float(value))
        except (TypeError, ValueError):
            return self.mic_gate
        gate = max(0, min(MIC_GATE_MAX_RAW, gate))
        self.mic_gate = gate
        self._persist_audio_config()
        return self.mic_gate

    # ------------------------------------------------- Sortie audio (TTS)

    @staticmethod
    def _is_wdmks_device(d):
        """WDM-KS (Windows Driver Model - Kernel Streaming) est une API
        audio bas niveau que PortAudio/sounddevice expose parfois pour un
        même périphérique physique, EN PLUS de ses variantes WASAPI/MME —
        Windows énumère alors plusieurs entrées quasi identiques dans la
        liste (même nom affiché), sans que rien ne les distingue pour
        l'utilisateur. Or WDM-KS ne supporte pas le mode "bloquant" utilisé
        par RawOutputStream/RawInputStream ici (voir _play_wav_file), ce
        qui produit une erreur PortAudio -9999 ("Blocking API not
        supported yet") si on tombe dessus par hasard. On l'exclut donc
        entièrement des listes proposées, pour ne jamais laisser
        sélectionner une variante qui ne peut pas fonctionner."""
        try:
            hostapi_name = sd.query_hostapis(d.get("hostapi"))["name"]
        except Exception:
            return False
        return "wdm-ks" in hostapi_name.lower()

    def _resolve_output_device(self):
        """Équivalent de _resolve_input_device, côté sortie : retrouve
        l'index sounddevice correspondant au périphérique choisi pour la
        lecture de la synthèse vocale (stocké par nom). None = périphérique
        de sortie par défaut du système."""
        if not self.output_device_name:
            return None
        try:
            devices = sd.query_devices()
            for idx, d in enumerate(devices):
                if (
                    d.get("max_output_channels", 0) > 0
                    and d.get("name") == self.output_device_name
                    and not self._is_wdmks_device(d)
                ):
                    return idx
        except Exception:
            pass
        self._log(
            f"[Avertissement] Périphérique de sortie sélectionné introuvable "
            f"({self.output_device_name}), retour au périphérique par défaut.",
            "error",
        )
        return None

    def get_output_devices(self):
        """Liste les périphériques de sortie disponibles, pour le menu
        déroulant du panneau Réglages > Moteur vocal."""
        if sd is None:
            return {"devices": [], "selected": self.output_device_name, "volume": self.tts_volume}
        try:
            devices = sd.query_devices()
        except Exception as e:
            self._log(f"[Erreur audio] Impossible de lister les périphériques de sortie : {e}", "error")
            return {"devices": [], "selected": self.output_device_name, "volume": self.tts_volume}

        try:
            default_name = sd.query_devices(None, "output")["name"]
        except Exception:
            default_name = None

        result = []
        for idx, d in enumerate(devices):
            if d.get("max_output_channels", 0) > 0 and not self._is_wdmks_device(d):
                result.append({
                    "index": idx,
                    "name": d.get("name", f"Périphérique {idx}"),
                    "isSystemDefault": bool(default_name) and d.get("name") == default_name,
                })

        return {"devices": result, "selected": self.output_device_name, "volume": self.tts_volume}

    def set_output_device(self, device_name):
        """Change le périphérique de sortie utilisé pour lire la synthèse
        vocale. device_name vide/None = revenir au périphérique par défaut
        du système."""
        device_name = (device_name or "").strip() or None
        self.output_device_name = device_name
        self._persist_audio_config()
        if device_name:
            self._log(f"Sortie voix sélectionnée : {device_name}", "info")
        else:
            self._log("Sortie voix : retour au périphérique par défaut du système.", "info")
        return {"ok": True, "selected": self.output_device_name}

    def set_tts_volume(self, value):
        """Change le volume appliqué au signal de synthèse vocale avant
        lecture (1.0 = amplitude brute de Piper, inchangée). Un facteur
        plus bas réduit le risque de fuite dans le micro par crosstalk
        électrique (voir DEFAULT_TTS_VOLUME)."""
        try:
            volume = float(value)
        except (TypeError, ValueError):
            return self.tts_volume
        volume = max(0.1, min(1.5, volume))
        self.tts_volume = volume
        self._persist_audio_config()
        return self.tts_volume

    # -------------------------------------------- Mode d'activation vocale

    def set_listen_mode(self, mode):
        """Change le mode d'activation : "always" (bouton classique),
        "toggle_key" (touche bascule ON/OFF) ou "push_to_talk" (touche
        maintenue = micro transmis). Resynchronise la surveillance de la
        touche en conséquence."""
        mode = (mode or "").strip()
        if mode not in LISTEN_MODES:
            return self._listen_settings_payload()
        self.listen_mode = mode
        self._persist_audio_config()
        self._sync_hotkey_poll()
        return self._listen_settings_payload()

    def set_listen_hotkey(self, key):
        """Change la touche (ou combinaison, ex. "ctrl+f9") ou le bouton de
        joystick (encodé "joy:{...}", voir capture_joystick_button) utilisé
        pour le mode touche-bascule ou push-to-talk. Vide = désactive."""
        key = (key or "").strip()
        if key.startswith("joy:"):
            # On préserve la casse/le contenu exact : c'est un objet JSON
            # (nom de manette, GUID), pas une combinaison de touches à
            # normaliser.
            self.listen_hotkey = key or None
        else:
            self.listen_hotkey = key.lower() or None
        self._persist_audio_config()
        self._sync_hotkey_poll()
        return self._listen_settings_payload()

    def set_kb_layout(self, layout):
        """Reçoit la disposition clavier physique choisie côté interface
        (sélecteur AZERTY France/Belgique/QWERTY de la fenêtre de
        sélection de touche), pour traduire correctement les touches
        transmises au module 'keyboard' (voir AZERTY_TO_KEYBOARD_LIB).
        Sans effet sur les touches envoyées au jeu (pydirectinput), qui
        raisonnent toujours en position physique, indépendamment de la
        disposition."""
        layout = (layout or "").strip()
        if layout not in ("qwerty", "azerty_fr", "azerty_be"):
            return self.kb_layout
        if layout == self.kb_layout:
            return self.kb_layout
        self.kb_layout = layout
        self._persist_audio_config()
        self._sync_hotkey_poll()
        return self.kb_layout

    def _listen_settings_payload(self):
        return {
            "listenMode": self.listen_mode,
            "listenHotkey": self.listen_hotkey,
            "listenHotkeyAvailable": self._hotkey_module_available(),
            "kbLayout": self.kb_layout,
        }

    def _hotkey_check_parts(self, value):
        """Découpe une combinaison ("ctrl+iso102") en éléments directement
        vérifiables un par un via keyboard.is_pressed() : soit un nom de
        touche (str, traduit AZERTY si besoin), soit un code de balayage
        brut (int) pour "iso102" — cette touche n'a pas de nom reconnu par
        la bibliothèque `keyboard`, mais celle-ci accepte un entier
        directement en repli (contrairement à un texte numérique dans une
        combinaison, qui échoue)."""
        if not value:
            return []
        parts = []
        for p in value.split("+"):
            if p == "iso102":
                parts.append(ISO102_SCAN_CODE)
            elif self.kb_layout in ("azerty_fr", "azerty_be"):
                parts.append(AZERTY_TO_KEYBOARD_LIB.get(p, p))
            else:
                parts.append(p)
        return parts

    # ------------------------------------------------- Souris (clic physique)

    def capture_mouse_button(self):
        """Démarre en arrière-plan la détection du prochain clic physique
        (gauche/droit/molette), où qu'il ait lieu — pas seulement dans la
        fenêtre de l'appli, puisque le module `mouse` pose un hook global
        bas niveau, comme `keyboard` pour les touches. Permet d'assigner
        un bouton en cliquant vraiment dessus, plutôt qu'en sélectionnant
        une des 3 options dans la liste. Résultat poussé vers l'interface
        via mouseCaptureResult(...), l'annulation se fait côté JS via la
        touche Échap (pas un bouton cliquable : un clic sur "Annuler"
        serait lui-même intercepté comme le clic à assigner)."""
        if mouse_lib is None:
            self._push(f"mouseCaptureResult({json.dumps({'ok': False, 'reason': 'no_mouse_lib'})})")
            return {"ok": True}
        self._mouse_capture_stop.clear()
        threading.Thread(target=self._mouse_capture_thread, daemon=True).start()
        return {"ok": True}

    def cancel_mouse_capture(self):
        self._mouse_capture_stop.set()
        return {"ok": True}

    def _mouse_capture_thread(self):
        result_holder = {"button": None}
        done_event = threading.Event()
        value_map = {"left": "mouseleft", "right": "mouseright", "middle": "mousemiddle"}

        def on_event(event):
            if done_event.is_set():
                return
            button = getattr(event, "button", None)
            event_type = getattr(event, "event_type", None)
            if event_type != "down" or button not in value_map:
                return
            result_holder["button"] = value_map[button]
            done_event.set()

        try:
            mouse_lib.hook(on_event)
        except Exception as e:
            self._log(f"[Erreur souris] Détection impossible : {e}", "error")
            self._push(f"mouseCaptureResult({json.dumps({'ok': False, 'reason': 'error'})})")
            return

        try:
            timeout_at = time.time() + 15.0
            while time.time() < timeout_at and not done_event.is_set():
                if self._mouse_capture_stop.is_set():
                    self._push(f"mouseCaptureResult({json.dumps({'ok': False, 'reason': 'cancelled'})})")
                    return
                time.sleep(0.05)
            if not done_event.is_set():
                self._push(f"mouseCaptureResult({json.dumps({'ok': False, 'reason': 'timeout'})})")
                return
            self._push(f"mouseCaptureResult({json.dumps({'ok': True, 'button': result_holder['button']})})")
        finally:
            try:
                mouse_lib.unhook(on_event)
            except Exception:
                pass

    # ------------------------------------------------- Joystick / manette

    def _ensure_pygame_joystick_ready(self):
        """Initialise le sous-module joystick de pygame une seule fois
        (idempotent). Renvoie False si pygame est absent ou si
        l'initialisation échoue (ex. pilote SDL manquant)."""
        if pygame is None:
            return False
        if self._pygame_joystick_ready:
            return True
        try:
            pygame.init()
            pygame.joystick.init()
            self._pygame_joystick_ready = True
            return True
        except Exception as e:
            self._log(f"[Erreur joystick] Initialisation impossible : {e}", "error")
            return False

    def _get_joysticks(self, force_refresh=False):
        """Renvoie la liste des manettes/joysticks actuellement connectés
        (objets pygame.joystick.Joystick, déjà initialisés). Mise en cache
        avec réénumération périodique plutôt qu'à chaque appel, pour ne
        pas interroger le pilote 30 fois par seconde pendant la
        surveillance de la touche bascule/push-to-talk."""
        if not self._ensure_pygame_joystick_ready():
            return []
        now = time.time()
        if not force_refresh and self._joysticks is not None and (now - self._joysticks_refreshed_at) < 2.0:
            return self._joysticks
        try:
            joysticks = []
            for i in range(pygame.joystick.get_count()):
                j = pygame.joystick.Joystick(i)
                j.init()
                joysticks.append(j)
            self._joysticks = joysticks
            self._joysticks_refreshed_at = now
            return joysticks
        except Exception as e:
            self._log(f"[Erreur joystick] {e}", "error")
            return self._joysticks or []

    def list_joysticks(self):
        """Liste les manettes/joysticks détectés (nom + nombre de boutons),
        pour affichage informatif côté réglages (ex. vérifier que le
        VirPil est bien reconnu avant d'assigner un bouton)."""
        if pygame is None:
            return {"ok": False, "error": "Le module pygame est manquant.", "devices": []}
        joysticks = self._get_joysticks(force_refresh=True)
        return {
            "ok": True,
            "devices": [
                {"name": j.get_name(), "num_buttons": j.get_numbuttons()}
                for j in joysticks
            ],
        }

    def capture_joystick_button(self):
        """Démarre en arrière-plan la détection du prochain bouton de
        joystick pressé, sur n'importe quelle manette connectée, pour
        permettre d'assigner un bouton physique sans avoir à connaître son
        index à l'avance. Le résultat (ou l'échec/l'annulation) est poussé
        vers l'interface via joystickCaptureResult(...) plutôt que
        renvoyé directement, puisque l'appui peut prendre plusieurs
        secondes."""
        self._joystick_capture_stop.clear()
        threading.Thread(target=self._joystick_capture_thread, daemon=True).start()
        return {"ok": True}

    def cancel_joystick_capture(self):
        self._joystick_capture_stop.set()
        return {"ok": True}

    def _joystick_capture_thread(self):
        if not self._ensure_pygame_joystick_ready():
            self._push(f"joystickCaptureResult({json.dumps({'ok': False, 'reason': 'no_pygame'})})")
            return

        joysticks = self._get_joysticks(force_refresh=True)
        if not joysticks:
            self._push(f"joystickCaptureResult({json.dumps({'ok': False, 'reason': 'no_device'})})")
            return

        # État de référence établi APRÈS l'ouverture des manettes, plutôt
        # que de supposer que tous les boutons partent relâchés : certains
        # périphériques (ex. bases DirectInput comme le WarBRD-D) renvoient
        # un état initial non fiable juste après l'initialisation — sans
        # cette étape, le bouton 0 pouvait apparaître comme "pressé" dès le
        # premier passage et déclencher une détection instantanée, sans
        # aucun appui réel de l'utilisateur.
        for _ in range(3):
            pygame.event.pump()
            time.sleep(0.05)
        prev_states = [[bool(j.get_button(b)) for b in range(j.get_numbuttons())] for j in joysticks]

        timeout_at = time.time() + 15.0
        try:
            while time.time() < timeout_at:
                if self._joystick_capture_stop.is_set():
                    self._push(f"joystickCaptureResult({json.dumps({'ok': False, 'reason': 'cancelled'})})")
                    return
                pygame.event.pump()
                for ji, j in enumerate(joysticks):
                    for b in range(j.get_numbuttons()):
                        held = bool(j.get_button(b))
                        if held and not prev_states[ji][b]:
                            result = {
                                "ok": True,
                                "name": j.get_name(),
                                "guid": j.get_guid() if hasattr(j, "get_guid") else None,
                                "button": b,
                            }
                            self._push(f"joystickCaptureResult({json.dumps(result)})")
                            return
                        prev_states[ji][b] = held
                time.sleep(0.02)
            self._push(f"joystickCaptureResult({json.dumps({'ok': False, 'reason': 'timeout'})})")
        except Exception as e:
            self._log(f"[Erreur joystick] {e}", "error")
            self._push(f"joystickCaptureResult({json.dumps({'ok': False, 'reason': 'error'})})")

    @staticmethod
    def _joystick_hotkey_decode(value):
        """Reconnaît un listen_hotkey encodant un bouton de joystick
        (préfixe "joy:" suivi d'un objet JSON {name, guid, button}), par
        opposition à une combinaison clavier classique. Renvoie None si ce
        n'en est pas un — la combinaison clavier existante reste ainsi
        traitée exactement comme avant (rétrocompatibilité totale)."""
        if not value or not str(value).startswith("joy:"):
            return None
        try:
            return json.loads(str(value)[len("joy:"):])
        except Exception:
            return None

    @staticmethod
    def _match_joystick(joysticks, joy_info):
        """Retrouve, parmi les manettes actuellement connectées, celle
        visée par un listen_hotkey enregistré — par GUID matériel en
        priorité (identité stable d'un appareil précis, même si son index
        change au gré des branchements), et par nom en repli (GUID
        indisponible sur d'anciennes versions de pygame).

        Comparaison volontairement insensible à la casse/aux espaces : un
        ancien bug (déjà corrigé, voir load_audio_config) a pu, par le
        passé, enregistrer le nom/GUID en minuscules dans
        audio_config.json. Un fichier déjà corrompu par ce bug ne se
        répare pas tout seul juste en corrigeant le chargement — d'où
        cette tolérance ici, pour qu'un ancien réglage déjà cassé se
        remette à fonctionner sans que l'utilisateur ait à réassigner le
        bouton."""
        guid = (joy_info.get("guid") or "").strip().lower()
        name = (joy_info.get("name") or "").strip().lower()
        if guid:
            for j in joysticks:
                try:
                    if hasattr(j, "get_guid") and (j.get_guid() or "").strip().lower() == guid:
                        return j
                except Exception:
                    pass
        if name:
            for j in joysticks:
                try:
                    if (j.get_name() or "").strip().lower() == name:
                        return j
                except Exception:
                    pass
        return None

    def _joystick_button_is_held(self, joy_info):
        """True/False si l'état du bouton peut être déterminé, None sinon
        (pygame absent, manette débranchée, bouton hors limites...)."""
        if pygame is None:
            return None
        try:
            pygame.event.pump()
        except Exception:
            return None
        target = self._match_joystick(self._get_joysticks(), joy_info)
        if target is None:
            return None
        button = joy_info.get("button")
        try:
            if button is None or button >= target.get_numbuttons():
                return None
            return bool(target.get_button(button))
        except Exception:
            return None

    def _hotkey_module_available(self):
        """Le module requis dépend du type de touche configurée : pygame
        pour un bouton de joystick, keyboard pour une combinaison clavier
        classique."""
        if self._joystick_hotkey_decode(self.listen_hotkey) is not None:
            return pygame is not None
        return keyboard is not None

    def _hotkey_display_label(self):
        """Libellé lisible de la touche configurée, pour les messages du
        journal (indépendant de ce que le module `keyboard` accepte)."""
        if not self.listen_hotkey:
            return ""
        joy_info = self._joystick_hotkey_decode(self.listen_hotkey)
        if joy_info is not None:
            button = joy_info.get("button")
            return f"🕹 Bouton {button}"
        labels = []
        for p in self.listen_hotkey.split("+"):
            if p == "iso102":
                labels.append("< > \\")
            elif self.kb_layout in ("azerty_fr", "azerty_be"):
                labels.append(AZERTY_TO_KEYBOARD_LIB.get(p, p))
            else:
                labels.append(p)
        return " + ".join(labels)

    def _hotkey_is_held(self):
        """True/False si on peut déterminer l'état de la touche/bouton
        configuré, None si indéterminable (pas de touche, module requis
        absent, manette débranchée, ou erreur)."""
        if not self.listen_hotkey:
            return None
        joy_info = self._joystick_hotkey_decode(self.listen_hotkey)
        if joy_info is not None:
            return self._joystick_button_is_held(joy_info)
        if keyboard is None:
            return None
        parts = self._hotkey_check_parts(self.listen_hotkey)
        if not parts:
            return None
        try:
            return all(keyboard.is_pressed(p) for p in parts)
        except Exception:
            return None

    def _sync_hotkey_poll(self):
        """Démarre ou arrête la surveillance de la touche d'activation
        vocale selon le mode et la touche actuellement configurés. Appelé
        à chaque changement pertinent (mode, touche, disposition) plutôt
        que de gérer ça au coup par coup un peu partout."""
        needs_poll = (
            self.listen_mode in ("push_to_talk", "toggle_key")
            and bool(self.listen_hotkey)
            and self._hotkey_module_available()
        )
        if needs_poll:
            if not (self._hotkey_poll_thread and self._hotkey_poll_thread.is_alive()):
                self._hotkey_poll_stop.clear()
                self._hotkey_poll_prev_held = False
                if self.listen_mode == "push_to_talk":
                    self._set_mic_gate(False)
                self._hotkey_poll_thread = threading.Thread(
                    target=self._hotkey_poll_loop, daemon=True
                )
                self._hotkey_poll_thread.start()
        else:
            self._hotkey_poll_stop.set()
            self._hotkey_poll_thread = None
            self._set_mic_gate(True)
            if self.listen_mode in ("push_to_talk", "toggle_key") and self.listen_hotkey and not self._hotkey_module_available():
                module_name = "pygame" if self._joystick_hotkey_decode(self.listen_hotkey) is not None else "keyboard"
                self._log(
                    f"[Erreur] Le module '{module_name}' est manquant : la touche/le "
                    "bouton d'activation vocale ne peut pas être surveillé.",
                    "error",
                )

    def _hotkey_poll_loop(self):
        """Thread dédié qui surveille l'état (appuyée/relâchée) de la
        touche configurée à un rythme raisonnable (~30 fois par seconde).
        Volontairement séparé du callback audio temps réel de sounddevice
        (voir _listen_loop) — appeler `keyboard` directement depuis ce
        dernier a déjà causé un plantage natif par le passé."""
        joy_info = self._joystick_hotkey_decode(self.listen_hotkey)
        if joy_info is not None:
            # Échauffement avant de commencer à surveiller : force une
            # réénumération des manettes (plutôt que de se fier à un
            # éventuel cache obsolète d'un appel précédent) et laisse à
            # pygame/DirectInput le temps de se stabiliser — même
            # précaution que dans _joystick_capture_thread. Sans ça, la
            # toute première lecture juste après le lancement de l'appli
            # pouvait échouer à retrouver la manette (ou lire un état de
            # bouton faussé), et ce faux départ était auparavant
            # interprété comme un appui du bouton (voir plus bas), ce qui
            # bloquait la détection de tout vrai appui pour le reste de
            # la session tant que l'utilisateur ne réassignait pas le
            # bouton.
            self._get_joysticks(force_refresh=True)
            for _ in range(3):
                try:
                    pygame.event.pump()
                except Exception:
                    pass
                time.sleep(0.05)

        warned_unmatched = False
        while not self._hotkey_poll_stop.is_set():
            held = self._hotkey_is_held()

            if held is None and joy_info is not None:
                if not warned_unmatched:
                    warned_unmatched = True
                    devices = self._get_joysticks(force_refresh=True)
                    noms = ", ".join(j.get_name() for j in devices) or "aucune manette détectée"
                    self._log(
                        "[Avertissement] Le bouton joystick configuré ("
                        f"{self._hotkey_display_label()}) est introuvable parmi les "
                        f"manettes actuellement connectées ({noms}). Vérifie qu'elle "
                        "est bien branchée, ou réassigne le bouton.",
                        "error",
                    )
            elif held is not None:
                warned_unmatched = False

            if self.listen_mode == "push_to_talk":
                # Indéterminé : on suppose le micro ouvert plutôt que de
                # risquer de le couper par erreur (voir docstring plus
                # haut sur le choix de ce comportement par défaut).
                effective_held = True if held is None else held
                if effective_held != self._mic_gate_open:
                    self._set_mic_gate(effective_held)
            elif self.listen_mode == "toggle_key":
                # Contrairement au push-to-talk, on ignore ici une
                # lecture indéterminée plutôt que de la traiter comme un
                # appui : un état indéterminé passager (typiquement juste
                # après le démarrage, le temps que la manette soit
                # retrouvée) serait sinon confondu avec un "front
                # montant" (relâché → pressé), ce qui déclenchait une
                # bascule fantôme au lancement ET empêchait ensuite toute
                # détection d'un vrai appui pour le reste de la session
                # (le front montant ne se déclenche qu'au passage de
                # relâché à pressé, jamais tant que l'état reste "pressé").
                if held is not None:
                    if held and not self._hotkey_poll_prev_held:
                        # Front montant (touche qui vient d'être pressée).
                        display = self._hotkey_display_label()
                        if self.listening:
                            self._set_mic_gate(not self._mic_gate_open)
                            etat = "activé 🎙" if self._mic_gate_open else "coupé 🔇"
                            self._log(f"Micro {etat} (touche bascule « {display} »).", "info")
                        else:
                            self._log(
                                f"Touche bascule (« {display} ») pressée, mais l'écoute "
                                "n'est pas engagée. Clique d'abord sur « Engager l'écoute ».",
                                "info",
                            )
                    self._hotkey_poll_prev_held = held

            time.sleep(0.03)

    def _set_mic_gate(self, open_):
        """Change l'état ouvert/coupé du micro (utilisé par le push-to-talk
        et la touche bascule) et prévient l'interface pour qu'elle affiche
        un indicateur visuel à jour, plutôt que de laisser ce changement
        invisible à l'utilisateur."""
        self._mic_gate_open = bool(open_)
        self._push(f"micGateUpdate({'true' if self._mic_gate_open else 'false'})")
        self._overlay_set_mic(self._mic_gate_open and self.listening)


    def download_vosk_model(self, model_key):
        """Lance en arrière-plan le téléchargement + extraction du modèle
        Vosk choisi par l'utilisateur. Utilisé au premier lancement (aucun
        modèle détecté) comme depuis Réglages > NovaVox ensuite (bouton
        "Changer de modèle" à côté du dossier du modèle actuel), pour
        remplacer le modèle déjà installé par un autre. Progression et
        résultat poussés vers le panneau via voskDownloadProgress /
        voskDownloadDone."""
        model = VOSK_MODELS.get(model_key)
        if not model:
            return {"ok": False, "error": "Modèle inconnu."}
        threading.Thread(target=self._download_vosk_model_thread, args=(model,), daemon=True).start()
        return {"ok": True}

    def _push_vosk_progress(self, percent, message):
        self._push(f"voskDownloadProgress({json.dumps(percent)}, {json.dumps(message)})")

    def _download_vosk_model_thread(self, model):
        target_dir = MODEL_DIR_DEFAULT
        tmp_zip = None
        tmp_extract_dir = None
        try:
            self._push_vosk_progress(0, f"Téléchargement de « {model['label']} »...")

            fd, tmp_zip = tempfile.mkstemp(suffix=".zip", dir=BASE_DIR)
            os.close(fd)

            last_reported = {"percent": -1}

            def _on_chunk(downloaded, total):
                if total:
                    percent = int(downloaded / total * 90)  # 0-90% = téléchargement
                    if percent != last_reported["percent"]:
                        last_reported["percent"] = percent
                        mo = downloaded / (1024 * 1024)
                        total_mo = total / (1024 * 1024)
                        self._push_vosk_progress(
                            percent, f"Téléchargement... {mo:.0f} / {total_mo:.0f} Mo"
                        )
                else:
                    mo = downloaded / (1024 * 1024)
                    self._push_vosk_progress(min(90, 5), f"Téléchargement... {mo:.0f} Mo")

            # Ré-essaie automatiquement en cas de coupure réseau, et
            # vérifie que le fichier reçu est bien complet (voir
            # _download_file_with_retry) avant de tenter l'extraction.
            _download_file_with_retry(model["url"], tmp_zip, on_chunk=_on_chunk)

            self._push_vosk_progress(92, "Extraction de l'archive...")
            tmp_extract_dir = tempfile.mkdtemp(dir=BASE_DIR)
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(tmp_extract_dir)

            # L'archive contient un unique dossier racine (ex.
            # "vosk-model-small-fr-0.22") : on le retrouve pour le
            # renommer/déplacer en "model", quel que soit son nom exact.
            entries = [e for e in os.listdir(tmp_extract_dir) if not e.startswith(".")]
            if len(entries) == 1 and os.path.isdir(os.path.join(tmp_extract_dir, entries[0])):
                extracted_model_dir = os.path.join(tmp_extract_dir, entries[0])
            else:
                extracted_model_dir = tmp_extract_dir

            if not model_folder_is_valid(extracted_model_dir):
                raise RuntimeError("L'archive téléchargée ne ressemble pas à un modèle Vosk valide.")

            self._push_vosk_progress(96, "Installation du modèle...")
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir)
            shutil.move(extracted_model_dir, target_dir)

            self.model_path = target_dir
            # Invalide le modèle Vosk déjà chargé en mémoire (voir
            # _listen_loop) : téléchargement/large et téléchargement/small
            # s'installent tous les deux dans le même dossier target_dir, le
            # chemin ne change donc pas d'un modèle à l'autre — sans ça, une
            # écoute déjà lancée dans cette session avant le changement
            # continuerait à réutiliser l'ancien modèle en mémoire au lieu du
            # nouveau tout juste installé sur le disque.
            self._vosk_model = None
            self._vosk_model_path = None
            self._push_vosk_progress(100, "Modèle installé avec succès.")
            self._log(f"Modèle « {model['label']} » installé dans {target_dir}.", "success")
            if self.listening:
                # L'écoute en cours utilise encore l'ancien modèle chargé en
                # mémoire ; on l'arrête pour forcer un rechargement propre du
                # nouveau modèle à la prochaine activation plutôt que de la
                # laisser tourner silencieusement sur l'ancien.
                self.stop_listening()
                self._status("idle", "Arrêté", "Nouveau modèle installé — relance l'écoute pour l'utiliser")
                self._log("Écoute arrêtée : relance-la pour utiliser le nouveau modèle vocal.", "info")
            self._push(f"voskDownloadDone(true, {json.dumps(target_dir)})")
        except Exception as e:
            self._log(f"[Erreur] Téléchargement du modèle Vosk : {e}", "error")
            self._push(f"voskDownloadDone(false, {json.dumps(str(e))})")
        finally:
            for path in (tmp_zip,):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
            try:
                if tmp_extract_dir and os.path.isdir(tmp_extract_dir):
                    shutil.rmtree(tmp_extract_dir, ignore_errors=True)
            except Exception:
                pass

    def start_listening(self, model_path):
        if self.listening:
            return {"ok": False, "error": "L'écoute est déjà active."}
        model_path = (model_path or "").strip()
        if not os.path.isdir(model_path):
            return {"ok": False, "error": f"Dossier du modèle introuvable : {model_path}"}
        if pydirectinput is None:
            return {"ok": False, "error": "Le module pydirectinput est manquant."}

        # Évite tout conflit d'accès au périphérique audio si le mètre de
        # niveau des Réglages tournait encore (surveillance légère sans
        # reconnaissance) au moment où l'écoute complète démarre.
        self.stop_mic_monitor()

        # La surveillance de la touche (bascule/push-to-talk) tourne déjà
        # en continu indépendamment de l'écoute (voir _sync_hotkey_poll) ;
        # en mode "always", le micro reste simplement toujours ouvert.
        if self.listen_mode == "always":
            self._set_mic_gate(True)

        self.model_path = model_path
        self.stop_event.clear()
        self.listening = True
        self._overlay_set_mic(True)
        self._overlay_set_listening(True)
        threading.Thread(target=self._listen_loop, args=(model_path,), daemon=True).start()
        return {"ok": True}

    def stop_listening(self):
        self.stop_event.set()
        self.listening = False
        self._overlay_set_mic(False)
        self._overlay_set_listening(False)
        return {"ok": True}

    # ------------------------------------------------- Assistant IA (Ollama)

    def ai_get_state(self):
        return {
            "history": self.ai_history,
            "voiceOutput": self.ai_voice_output,
            "name": self.ai_name,
            "enabled": self.ai_enabled,
            "confirmCommands": self.confirm_commands_voice,
            "model": self.ai_model,
            "availableModels": AVAILABLE_MODELS,
            "triggerCooldown": self.trigger_cooldown,
            "customContext": self.ai_custom_context,
            "userName": self.user_name,
            "responseLength": self.ai_response_length,
            "piperVoice": self.piper_voice,
            "piperLengthScale": (1.0 / self.piper_length_scale) if self.piper_length_scale else 1.0,
            "piperNoiseScale": self.piper_noise_scale,
            "radioEffect": self.radio_effect,
            "gameLogEnabled": self.game_log_enabled,
            "gameLogAnnounce": self.game_log_announce,
            "gameLogPlayerHandle": self.game_log_player_handle,
            "gameLogPhrases": self.game_log_phrases,
            "gameLogPhraseDefaults": DEFAULT_GAME_LOG_PHRASES,
            "gameLogPhraseMeta": GAME_LOG_PHRASE_META,
            "gameLogHudOverrides": self.game_log_hud_overrides,
            "gameLogDestinationAliases": self.game_log_destination_aliases,
        }

    # ------------------------------------------------ Assistant IA Gemini

    def gemini_get_state(self):
        return {
            "history": self.gemini_history,
            "voiceOutput": self.gemini_voice_output,
            "name": self.gemini_name,
            "enabled": self.gemini_enabled,
            "apiKey": self.gemini_api_key,
            "model": self.gemini_model,
            "availableModels": GEMINI_AVAILABLE_MODELS,
            "customContext": self.gemini_custom_context,
            "userName": self.user_name,
            "responseLength": self.gemini_response_length,
        }

    def gemini_toggle_voice_output(self, enabled):
        self.gemini_voice_output = bool(enabled)
        return self.gemini_voice_output

    def gemini_toggle_enabled(self, enabled):
        """Active/désactive complètement l'assistant Gemini : plus aucune
        détection du mot d'activation dans _handle_text tant que c'est
        désactivé (voir gemini_wake_word là-bas). Utile pour ne garder
        qu'un seul des deux assistants (Nova ou Gemini) actif à la fois
        si les deux en même temps font double emploi."""
        self.gemini_enabled = bool(enabled)
        if not self.gemini_enabled:
            self._gemini_awaiting_question = False
        self._persist_ai_config()
        return self.gemini_enabled

    def gemini_set_api_key(self, key):
        """Enregistre la clé API Gemini (obtenue gratuitement sur
        aistudio.google.com). Vide = efface la clé enregistrée."""
        self.gemini_api_key = (key or "").strip()
        self._persist_ai_config()
        return bool(self.gemini_api_key)

    def gemini_set_model(self, model_id):
        model_id = (model_id or "").strip()
        if not model_id:
            return self.gemini_model
        self.gemini_model = model_id
        self._persist_ai_config()
        return self.gemini_model

    def gemini_set_name(self, name):
        name = (name or "").strip()
        if not name:
            return self.gemini_name
        self.gemini_name = name
        self._persist_ai_config()
        return self.gemini_name

    def gemini_set_response_length(self, value):
        value = (value or "").strip()
        if value not in RESPONSE_LENGTH_INSTRUCTIONS:
            return self.gemini_response_length
        self.gemini_response_length = value
        self._persist_ai_config()
        return self.gemini_response_length

    def gemini_set_custom_context(self, text):
        self.gemini_custom_context = (text or "").strip()
        self._persist_ai_config()
        return self.gemini_custom_context

    def gemini_clear_history(self):
        self.gemini_history = []
        return {"ok": True}

    def gemini_check_status(self):
        """Vérifie juste si une clé API est configurée — pas d'appel
        réseau ici (contrairement à Ollama, pas de service local dont il
        faut vérifier qu'il tourne), donc instantané et gratuit. Voir
        gemini_test_connection pour un vrai test d'appel à l'API."""
        return {"hasKey": bool((self.gemini_api_key or "").strip()), "model": self.gemini_model}

    def gemini_test_connection(self):
        """Envoie une requête minimale à Gemini pour vérifier que la clé
        API et le modèle choisis fonctionnent réellement (bouton "Tester
        la connexion" des réglages), sans passer par tout le flux de
        conversation."""
        api_key = (self.gemini_api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "Aucune clé API renseignée."}
        payload = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": "Réponds juste \"ok\"."}]}],
            # thinkingLevel minimal : avec un maxOutputTokens aussi petit (10),
            # le niveau de réflexion par défaut de l'API ("medium") peut à lui
            # seul consommer tout le budget de tokens de sortie et renvoyer une
            # réponse vide, alors que la clé/le modèle fonctionnent très bien.
            "generationConfig": {
                "maxOutputTokens": 10,
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        }).encode("utf-8")
        url = f"{GEMINI_API_BASE_URL}/models/{self.gemini_model}:generateContent"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                json.loads(resp.read().decode("utf-8"))
            return {"ok": True}
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                msg = err_body.get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            return {"ok": False, "error": f"Erreur {e.code} : {msg}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _gemini_quota_limit(self):
        return GEMINI_DAILY_LIMITS.get(self.gemini_model, GEMINI_DAILY_LIMIT_DEFAULT)

    def _gemini_quota_state(self):
        """Renvoie (requêtes utilisées, limite) pour le modèle Gemini
        actuel, en remettant d'abord le compteur à zéro si on a changé de
        jour côté Google (minuit heure du Pacifique, voir GEMINI_QUOTA_TZ)
        depuis la dernière requête envoyée."""
        today = datetime.datetime.now(GEMINI_QUOTA_TZ).date().isoformat()
        if self.gemini_request_day != today:
            self.gemini_request_day = today
            self.gemini_request_count = 0
            self._persist_ai_config()
        return self.gemini_request_count, self._gemini_quota_limit()

    def _gemini_record_request(self):
        """Incrémente le compteur de requêtes Gemini du jour (voir
        _gemini_quota_state) et met à jour la barre de progression de
        l'overlay en conséquence. Appelé juste avant chaque appel réel à
        l'API — la remise à zéro quotidienne est gérée au passage."""
        used, limit = self._gemini_quota_state()
        used += 1
        self.gemini_request_count = used
        self._persist_ai_config()
        self._overlay_set_gemini_quota(used, limit)
        return used, limit

    def _gemini_ask(self, question):
        """Envoie une question à Gemini (déclenchée par son mot
        d'activation vocal, voir _handle_text) et pousse la conversation
        vers son panneau dédié. Miroir de _ai_ask, mais pour un historique
        et un panneau complètement séparés de Nova/Ollama."""
        self.gemini_history.append({"role": "user", "content": question})
        self._push(f"geminiUserMessage({json.dumps(question)})")
        threading.Thread(target=self._gemini_reply_thread, daemon=True).start()

    def _gemini_reply_thread(self):
        api_key = (self.gemini_api_key or "").strip()
        if not api_key:
            reply = (
                "[Erreur] Aucune clé API Gemini configurée. Ouvre Réglages > 🌟 IA Gemini "
                "et renseigne ta clé (gratuite sur aistudio.google.com)."
            )
            self.gemini_history.append({"role": "assistant", "content": reply})
            self._push(f"geminiReceiveMessage({json.dumps(reply)})")
            return

        used, limit = self._gemini_quota_state()
        if used >= limit:
            model_label = next(
                (m["label"] for m in GEMINI_AVAILABLE_MODELS if m["id"] == self.gemini_model),
                self.gemini_model,
            )
            switch_hint = (
                " ou passe sur Gemini Flash-Lite dans les réglages (limite quotidienne plus haute)"
                if self.gemini_model != "gemini-3.5-flash-lite" else ""
            )
            reply = (
                f"[Limite atteinte] Tu as utilisé les {limit} requêtes gratuites du jour pour "
                f"{model_label}. Le quota se réinitialise à minuit, heure du Pacifique (Californie) "
                f"— utilise Nova (Ollama, local, illimité) en attendant{switch_hint}."
            )
            self.gemini_history.append({"role": "assistant", "content": reply})
            self._push(f"geminiReceiveMessage({json.dumps(reply)})")
            return

        game_state_block = ""
        if self._game_log_watcher:
            game_state_block = game_state_to_prompt_block(self._game_log_watcher.get_state())

        system_text = ai_system_prompt(
            self.gemini_name, self.gemini_custom_context, self.user_name, self.gemini_response_length
        ) + game_state_block

        # Gemini attend tout l'historique à chaque appel (comme Ollama),
        # mais son rôle assistant s'appelle "model", pas "assistant".
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in self.gemini_history
        ]
        max_output_tokens = self.AI_NUM_PREDICT_BY_LENGTH.get(
            self.gemini_response_length, self.AI_NUM_PREDICT_BY_LENGTH["normal"]
        )
        thinking_level = self.GEMINI_THINKING_LEVEL_BY_LENGTH.get(
            self.gemini_response_length, self.GEMINI_THINKING_LEVEL_BY_LENGTH["normal"]
        )
        payload = json.dumps({
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_text}]},
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": 0.7,
                "thinkingConfig": {"thinkingLevel": thinking_level},
            },
        }).encode("utf-8")

        url = f"{GEMINI_API_BASE_URL}/models/{self.gemini_model}:generateContent"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        # Comptée juste avant l'appel réel (pas avant, voir le contrôle de
        # quota plus haut) : un compteur local ne peut de toute façon pas
        # refléter le quota serveur au tick près, mais c'est le point le
        # plus proche de la consommation réelle du quota Google.
        self._gemini_record_request()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates") or []
            reply = ""
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                reply = "".join(p.get("text", "") for p in parts).strip()
            if not reply:
                reply = "(réponse vide du modèle)"
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                msg = err_body.get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            reply = f"[Erreur] Gemini a renvoyé une erreur ({e.code}) : {msg}"
        except TimeoutError:
            reply = "[Erreur] Gemini n'a pas répondu en moins de 30 secondes. Réessaie."
        except (urllib.error.URLError, OSError) as e:
            reply = f"[Erreur] Impossible de contacter Gemini ({e}). Vérifie ta connexion internet."
        except Exception as e:
            reply = f"[Erreur] {e}"

        self.gemini_history.append({"role": "assistant", "content": reply})
        if len(self.gemini_history) > AI_MAX_HISTORY_MESSAGES:
            self.gemini_history = self.gemini_history[-AI_MAX_HISTORY_MESSAGES:]
        self._push(f"geminiReceiveMessage({json.dumps(reply)})")

        if self.gemini_voice_output and "[Erreur]" not in reply:
            self._speak(reply)

    def _persist_ai_config(self):
        save_ai_config({
            "name": self.ai_name,
            "confirm_commands": self.confirm_commands_voice,
            "ai_enabled": self.ai_enabled,
            "model": self.ai_model,
            "trigger_cooldown": self.trigger_cooldown,
            "custom_context": self.ai_custom_context,
            "user_name": self.user_name,
            "response_length": self.ai_response_length,
            "piper_voice": self.piper_voice,
            "piper_length_scale": self.piper_length_scale,
            "piper_noise_scale": self.piper_noise_scale,
            "radio_effect": self.radio_effect,
            "game_log_enabled": self.game_log_enabled,
            "game_log_announce_events": self.game_log_announce,
            "game_log_player_handle": self.game_log_player_handle,
            "game_log_phrases": self.game_log_phrases,
            "game_log_hud_overrides": self.game_log_hud_overrides,
            "game_log_destination_aliases": self.game_log_destination_aliases,
            "gemini_enabled": self.gemini_enabled,
            "gemini_api_key": self.gemini_api_key,
            "gemini_model": self.gemini_model,
            "gemini_name": self.gemini_name,
            "gemini_response_length": self.gemini_response_length,
            "gemini_custom_context": self.gemini_custom_context,
            "gemini_request_count": self.gemini_request_count,
            "gemini_request_day": self.gemini_request_day,
        })

    # -------------------------------------------- Surveillance du Game.log

    def _start_game_log_watcher(self):
        """Démarre le fil de surveillance du Game.log de Star Citizen
        (voir game_log_watcher.py). Ne fait rien s'il tourne déjà."""
        if self._game_log_watcher is not None:
            return
        self._game_log_watcher = GameLogWatcher(
            on_event=self._on_game_event,
            player_name=self.game_log_player_handle or None,
        )
        self._game_log_watcher.start()
        self._log("Surveillance du Game.log démarrée.", "info")

    def stop_game_log_watcher(self):
        """Arrête le fil de surveillance du Game.log, s'il tourne."""
        if self._game_log_watcher:
            self._game_log_watcher.stop()
            self._game_log_watcher = None
            self._log("Surveillance du Game.log arrêtée.", "info")

    def set_game_log_enabled(self, enabled):
        """Appelé depuis l'interface (case à cocher des réglages avancés
        IA) pour activer/désactiver la surveillance du Game.log."""
        self.game_log_enabled = bool(enabled)
        if self.game_log_enabled:
            self._start_game_log_watcher()
        else:
            self.stop_game_log_watcher()
        self._persist_ai_config()
        return {"ok": True, "enabled": self.game_log_enabled}

    def set_game_log_announce(self, enabled):
        """Active/désactive l'annonce vocale des kills/morts/destructions
        détectés (indépendant de l'activation du watcher lui-même : on
        peut vouloir garder juste le journal texte sans les interruptions
        vocales)."""
        self.game_log_announce = bool(enabled)
        self._persist_ai_config()
        return self.game_log_announce

    def set_game_log_player_handle(self, handle):
        """Change le pseudo RSI utilisé pour filtrer/qualifier les
        événements du Game.log (voir game_log_watcher.GameLogWatcher).
        Redémarre le watcher s'il est actif, pour que le nouveau handle
        soit pris en compte immédiatement plutôt qu'au prochain lancement
        de l'appli."""
        self.game_log_player_handle = (handle or "").strip()
        self._persist_ai_config()
        if self._game_log_watcher:
            self.stop_game_log_watcher()
            self._start_game_log_watcher()
        return self.game_log_player_handle

    def set_game_log_phrase(self, key, text):
        """Change la phrase annoncée pour un type d'événement du Game.log
        (voir DEFAULT_GAME_LOG_PHRASES pour les clés valides et
        GAME_LOG_PHRASE_META pour les champs {…} disponibles par clé).
        Une valeur vide restaure le gabarit par défaut plutôt que de
        laisser une annonce vide."""
        if key not in DEFAULT_GAME_LOG_PHRASES:
            return {"ok": False, "error": "clé de phrase inconnue"}
        text = (text or "").strip()
        self.game_log_phrases[key] = text if text else DEFAULT_GAME_LOG_PHRASES[key]
        self._persist_ai_config()
        return {"ok": True, "key": key, "text": self.game_log_phrases[key]}

    def reset_game_log_phrase(self, key):
        """Restaure le gabarit par défaut d'une phrase (ou de toutes les
        phrases si key est vide/None)."""
        if key:
            if key not in DEFAULT_GAME_LOG_PHRASES:
                return {"ok": False, "error": "clé de phrase inconnue"}
            self.game_log_phrases[key] = DEFAULT_GAME_LOG_PHRASES[key]
        else:
            self.game_log_phrases = dict(DEFAULT_GAME_LOG_PHRASES)
        self._persist_ai_config()
        return {"ok": True, "phrases": self.game_log_phrases}

    def _format_game_log_phrase(self, key, **kwargs):
        """Construit le texte annoncé pour un événement, à partir du
        gabarit personnalisé de l'utilisateur (ou du défaut si absent).
        Si le gabarit personnalisé est cassé (accolade mal fermée, nom de
        champ qui ne correspond à aucun argument fourni...), on retombe
        silencieusement sur le gabarit par défaut : une faute de frappe
        dans les réglages ne doit jamais faire planter une annonce."""
        template = self.game_log_phrases.get(key) or DEFAULT_GAME_LOG_PHRASES.get(key, "")
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            try:
                return DEFAULT_GAME_LOG_PHRASES.get(key, "").format(**kwargs)
            except Exception:
                return ""

    def set_game_log_hud_override(self, original_text, custom_text):
        """Enregistre (ou remplace) une correction de lecture pour une
        notification HUD précise du jeu (voir RE_HUD_NOTIFICATION_START
        dans game_log_watcher.py) : quand le texte détecté correspond
        exactement à original_text, custom_text est annoncé à la place.
        Un custom_text vide revient à lire le texte brut tel quel (mais
        garde l'entrée dans la liste — voir _register_hud_text_seen —
        plutôt que de la faire disparaître, puisqu'elle a été détectée
        automatiquement dans le jeu)."""
        original_text = (original_text or "").strip()
        custom_text = (custom_text or "").strip()
        if not original_text:
            return {"ok": False, "error": "texte détecté manquant"}
        self.game_log_hud_overrides[original_text] = custom_text or original_text
        self._persist_ai_config()
        return {"ok": True, "overrides": self.game_log_hud_overrides}

    def _register_hud_text_seen(self, raw_text):
        """Enregistre automatiquement une notification HUD nouvellement
        rencontrée dans le Game.log (jamais vue auparavant) dans
        game_log_hud_overrides, avec le texte brut comme valeur de départ
        (= lu tel quel, comportement inchangé) — pour qu'elle apparaisse
        directement dans la liste des réglages, prête à être personnalisée,
        sans que l'utilisateur ait à la copier-coller manuellement depuis
        le journal. Ne fait rien si ce texte exact est déjà connu (que ce
        soit encore identique ou déjà personnalisé). Renvoie True si
        l'entrée vient d'être ajoutée (nouveauté), False sinon."""
        key = (raw_text or "").strip()
        if not key or key in self.game_log_hud_overrides:
            return False
        self.game_log_hud_overrides[key] = raw_text
        self._persist_ai_config()
        return True

    def delete_game_log_hud_override(self, original_text):
        """Supprime une correction de lecture existante."""
        self.game_log_hud_overrides.pop((original_text or "").strip(), None)
        self._persist_ai_config()
        return {"ok": True, "overrides": self.game_log_hud_overrides}

    def set_game_log_destination_alias(self, raw_key, custom_name):
        """Enregistre (ou remplace) le nom personnalisé à annoncer pour un
        identifiant de destination brut précis (voir destination_alias_key
        dans game_log_watcher.py, ex. 'rs entry nyx pyro jp1' ->
        'Pyro Gateway'). Un custom_name vide revient au nettoyage
        générique par défaut (mais garde l'entrée dans la liste, comme
        pour set_game_log_hud_override)."""
        raw_key = (raw_key or "").strip().lower()
        custom_name = (custom_name or "").strip()
        if not raw_key:
            return {"ok": False, "error": "identifiant manquant"}
        self.game_log_destination_aliases[raw_key] = custom_name
        self._persist_ai_config()
        return {"ok": True, "key": raw_key, "aliases": self.game_log_destination_aliases}

    def delete_game_log_destination_alias(self, raw_key):
        """Supprime un alias de destination personnalisé existant."""
        self.game_log_destination_aliases.pop((raw_key or "").strip().lower(), None)
        self._persist_ai_config()
        return {"ok": True, "aliases": self.game_log_destination_aliases}

    def _maybe_register_destination_alias(self, raw_id, current_name, obstruction_label):
        """Ajoute automatiquement l'identifiant brut de CHAQUE destination
        rencontrée (raw_id) à game_log_destination_aliases dès sa première
        détection — reconnue par un mécanisme automatique (alias codé en
        dur, point de saut, planète OOC_..., station via obstruction_label)
        OU non — pour que l'utilisateur puisse choisir/personnaliser le nom
        annoncé pour absolument toutes ses destinations depuis les
        réglages, pas seulement celles qui ne sont pas déjà reconnues.

        current_name (déjà calculé par _resolve_destination_label, quelle
        que soit sa source) sert de valeur de départ : aucun changement de
        comportement tant que l'entrée n'est pas éditée, puisque c'est
        déjà exactement le nom en train d'être annoncé.

        N'ENREGISTRE RIEN si obstruction_label est déjà un nom de lieu
        SPÉCIFIQUE (voir _obstruction_label_is_generic_guess) : dans ce
        cas, _resolve_destination_label ignore volontairement tout alias
        rattaché à raw_id (qui peut être un identifiant générique PARTAGÉ
        par plusieurs lieux réels différents — ex. 'ObjectContainer_
        RestStop' pour Baijini Point/Everus Harbor/Port Tressler à la
        fois) — proposer un alias ici serait donc trompeur : l'éditer
        n'aurait aucun effet sur les futurs trajets vers un AUTRE lieu
        partageant le même identifiant brut.

        Ne touche jamais une entrée déjà présente (voir
        destination_alias_key) : ni pour l'écraser si l'utilisateur l'a
        déjà personnalisée, ni pour la re-préremplir sinon — une seule
        fois à la première rencontre suffit.

        Signale aussi dans le journal système (voir
        destination_is_unresolved) les identifiants VRAIMENT non reconnus
        par aucun mécanisme automatique — ex. si Star Citizen change ou
        ajoute un identifiant depuis la dernière mise à jour de NovaVox —
        pour que ça se voie sans avoir à ouvrir les réglages soi-même.
        Silencieux pour les destinations déjà bien résolues (planètes,
        points de saut connus...), pas besoin d'alerter dans ce cas."""
        if obstruction_label and not _obstruction_label_is_generic_guess(obstruction_label):
            return
        key = destination_alias_key(raw_id, self.game_log_destination_aliases)
        if not key or key in self.game_log_destination_aliases:
            return
        if destination_is_unresolved(raw_id, self.game_log_destination_aliases):
            self._log(
                f"🛰 Nouvelle destination non reconnue dans le Game.log ({key}) — "
                "ajoutée à Réglages > Game.log > Alias de destinations, prête à être renommée.",
                "info",
            )
        value = (current_name or "").strip()
        self.game_log_destination_aliases[key] = value
        self._persist_ai_config()
        self._push(f"gameLogDestinationAliasAdded({json.dumps(key)}, {json.dumps(value)})")

    def _on_game_event(self, evt):
        """Callback appelé depuis le thread du GameLogWatcher à chaque
        événement détecté dans le Game.log. Reste volontairement léger et
        thread-safe : uniquement _log/_push/_speak, comme le reste de
        l'application (voir _hotkey_poll_loop pour le même principe côté
        surveillance clavier).

        Kills/morts/destructions de vaisseau retirés : vérifiés comme
        n'étant plus tracés dans le Game.log de cette version du jeu (voir
        l'avertissement en tête de game_log_watcher.py). "route_set" et
        "zone_change" sont fiables (vérifiés) ; "jump_start" reste un
        candidat non vérifié, désactivé par défaut côté watcher.

        La résolution du nom (_resolve_destination_label) priorise le
        texte lisible que le moteur du jeu révèle lui-même quand le
        trajet croise un obstacle (ex. "ArcCorp" -> "Baijini Point") —
        bien plus fiable qu'un nettoyage à l'aveugle de l'identifiant
        technique brut, qui reste le seul repli disponible pour les
        trajets locaux directs sans obstacle sur le chemin (voir
        game_log_watcher.py pour le détail)."""
        etype = evt.get("type")
        hud_raw_text = None

        if etype == "route_set":
            raw_dest = evt.get("destination")
            obstruction_label = evt.get("obstruction_label")
            start_location = evt.get("start_location")
            dest = _resolve_destination_label(
                raw_dest, obstruction_label, self.game_log_destination_aliases, start_location
            )
            key = "route_set" if dest else "route_set_no_dest"
            text = self._format_game_log_phrase(key, dest=dest)
            self._maybe_register_destination_alias(raw_dest, dest, obstruction_label)
        elif etype == "jump_start":
            raw_dest = evt.get("destination")
            obstruction_label = evt.get("obstruction_label")
            start_location = evt.get("start_location")
            dest = _resolve_destination_label(
                raw_dest, obstruction_label, self.game_log_destination_aliases, start_location
            )
            key = "jump_start" if dest else "jump_start_no_dest"
            text = self._format_game_log_phrase(key, dest=dest)
            self._maybe_register_destination_alias(raw_dest, dest, obstruction_label)
        elif etype == "zone_change":
            raw_zone = evt.get("zone")
            obstruction_label = evt.get("obstruction_label")
            start_location = evt.get("start_location")
            zone = _resolve_destination_label(
                raw_zone, obstruction_label, self.game_log_destination_aliases, start_location
            )
            key = "zone_change" if zone else "zone_change_no_zone"
            text = self._format_game_log_phrase(key, zone=zone)
            if zone:
                self._overlay_set_zone(zone)
            self._maybe_register_destination_alias(raw_zone, zone, obstruction_label)
        elif etype == "hud_notification":
            raw_text = _clean_hud_notification_text(evt.get("text", ""))
            if not raw_text:
                return
            key = "hud_notification"
            # Une correction de lecture exacte (voir
            # set_game_log_hud_override) prend le pas sur le texte brut
            # détecté dans le jeu — le journal affiche toujours le texte
            # brut original juste en dessous, pour garder une trace fidèle
            # de ce que le jeu a réellement affiché.
            is_new = self._register_hud_text_seen(raw_text)
            spoken_text = self.game_log_hud_overrides.get(raw_text.strip(), raw_text)
            if spoken_text != raw_text:
                hud_raw_text = raw_text
            text = self._format_game_log_phrase(key, text=spoken_text)
            if is_new:
                # Fait apparaître la nouvelle notification dans la liste
                # des corrections côté réglages, sans attendre une
                # réouverture du panneau (voir gameLogHudOverrideAdded
                # côté script.js).
                self._push(
                    f"gameLogHudOverrideAdded({json.dumps(raw_text.strip())}, {json.dumps(raw_text)})"
                )
        elif etype == "nickname_detected":
            # Pré-remplit le handle RSI une seule fois, silencieusement,
            # si l'utilisateur ne l'avait pas encore renseigné.
            if not self.game_log_player_handle:
                self.game_log_player_handle = evt.get("nickname", "")
                self._persist_ai_config()
                self._push(f"gameLogHandleAutoDetected({json.dumps(self.game_log_player_handle)})")
            return
        elif etype in ("watcher_started", "watcher_error"):
            self._log(evt.get("message", ""), "error" if etype == "watcher_error" else "info")
            return
        else:
            return

        emoji = GAME_LOG_PHRASE_EMOJI.get(key, "")
        msg = f"{emoji} {text}".strip()
        self._log(msg, "info")
        if hud_raw_text:
            self._log(f"   (texte détecté dans le jeu : « {hud_raw_text} »)", "info")
        if self.game_log_announce:
            self._speak(text)

    def ai_set_piper_length_scale(self, value):
        """Reçoit un facteur de VITESSE intuitif envoyé par le curseur
        (1.0 = normal, plus grand = plus rapide, plus petit = plus lent),
        et le convertit en length_scale pour Piper — qui raisonne en
        DURÉE, donc à l'inverse d'une vitesse (length_scale bas = parole
        plus rapide, ce qui est contre-intuitif pour un curseur)."""
        try:
            speed = max(0.5, min(2.0, float(value)))
        except (TypeError, ValueError):
            speed = 1.0 / self.piper_length_scale if self.piper_length_scale else 1.0
            return speed
        self.piper_length_scale = 1.0 / speed
        self._persist_ai_config()
        return speed

    def ai_set_piper_noise_scale(self, value):
        """Expressivité de la voix Piper (noise_scale) : plus bas = plus
        monotone/mécanique, plus haut = plus naturel et varié."""
        try:
            value = max(0.0, min(1.5, float(value)))
        except (TypeError, ValueError):
            return self.piper_noise_scale
        self.piper_noise_scale = value
        self._persist_ai_config()
        return self.piper_noise_scale

    def ai_set_radio_effect(self, enabled):
        """Active/désactive l'effet "communication vaisseau" (filtre radio
        léger appliqué à la voix, quel que soit le moteur utilisé)."""
        self.radio_effect = bool(enabled)
        self._persist_ai_config()
        return self.radio_effect

    def ai_set_user_name(self, name):
        """Enregistre le prénom de l'utilisateur (profil), pour que
        l'assistant puisse s'adresser à lui/elle nommément plutôt que de
        façon générique. Vide = efface le prénom enregistré."""
        self.user_name = (name or "").strip()
        self._persist_ai_config()
        return self.user_name

    def ai_set_response_length(self, value):
        """Change la longueur de réponse souhaitée ("short", "normal" ou
        "long" — voir RESPONSE_LENGTH_INSTRUCTIONS)."""
        value = (value or "").strip()
        if value not in RESPONSE_LENGTH_INSTRUCTIONS:
            return self.ai_response_length
        self.ai_response_length = value
        self._persist_ai_config()
        return self.ai_response_length

    def ai_set_custom_context(self, text):
        """Enregistre un texte libre (lore, règles maison, contexte de
        partie...) que l'assistant IA prendra en compte dans toutes ses
        réponses, en plus de son prompt système habituel."""
        self.ai_custom_context = (text or "").strip()
        self._persist_ai_config()
        return self.ai_custom_context

    def ai_set_trigger_cooldown(self, seconds):
        """Change le délai minimum avant de pouvoir redéclencher la même
        commande (protection contre les redéclenchements rapprochés dus à
        un écho, du bruit de fond ressemblant à la phrase, etc.)."""
        try:
            value = max(0.5, min(10.0, float(seconds)))
        except (TypeError, ValueError):
            return self.trigger_cooldown
        self.trigger_cooldown = value
        self._persist_ai_config()
        return self.trigger_cooldown

    def ai_set_model(self, model_id):
        """Change le modèle utilisé pour les prochaines conversations. Ne
        le télécharge pas automatiquement : le panneau vérifie ensuite
        s'il est déjà installé (ai_check_status) et propose de le
        télécharger sinon (ai_pull_model)."""
        model_id = (model_id or "").strip()
        if not model_id:
            return self.ai_model
        self.ai_model = model_id
        self._persist_ai_config()
        return self.ai_model

    def ai_toggle_confirm_commands(self, enabled):
        self.confirm_commands_voice = bool(enabled)
        self._persist_ai_config()
        return self.confirm_commands_voice

    def ai_set_name(self, name):
        name = (name or "").strip()
        if not name:
            return self.ai_name
        self.ai_name = name
        self._persist_ai_config()
        return self.ai_name

    # ------------------------------------------------- Moteur vocal Piper

    def ai_set_piper_voice(self, voice_id):
        """Change la voix Piper active (doit déjà être téléchargée)."""
        voice_id = (voice_id or "").strip() or None
        if voice_id is not None and voice_id not in PIPER_VOICES:
            return self.piper_voice
        self.piper_voice = voice_id
        self._persist_ai_config()
        return self.piper_voice

    def piper_get_status(self):
        """État actuel de Piper pour le panneau : moteur installé ou non,
        et pour chaque voix curatée si elle est déjà téléchargée."""
        return {
            "engineInstalled": os.path.isfile(PIPER_EXE),
            "voices": [
                {
                    "id": vid,
                    "label": info["label"],
                    "installed": os.path.isfile(os.path.join(PIPER_VOICES_DIR, f"{vid}.onnx")),
                }
                for vid, info in PIPER_VOICES.items()
            ],
        }

    def piper_install(self):
        """Télécharge et installe le moteur Piper (piper.exe + fichiers
        associés) depuis la dernière publication GitHub officielle.
        Progression poussée vers le panneau via piperInstallProgress /
        piperInstallDone."""
        threading.Thread(target=self._piper_install_thread, daemon=True).start()
        return {"ok": True}

    def _piper_install_thread(self):
        tmp_zip = None
        tmp_extract = None
        try:
            self._push(f"piperInstallProgress({json.dumps('Recherche de la dernière version de Piper...')})")
            req = urllib.request.Request(PIPER_GITHUB_LATEST_API, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                release = json.loads(resp.read().decode("utf-8"))

            asset_url = None
            for asset in release.get("assets", []):
                name = (asset.get("name") or "").lower()
                if "windows" in name and name.endswith(".zip") and (
                    "amd64" in name or "x64" in name or "x86_64" in name
                ):
                    asset_url = asset.get("browser_download_url")
                    break
            if not asset_url:
                raise RuntimeError(
                    "Version Windows de Piper introuvable dans la dernière publication "
                    "GitHub (sa structure a peut-être changé)."
                )

            self._push(f"piperInstallProgress({json.dumps('Téléchargement de Piper...')})")
            fd, tmp_zip = tempfile.mkstemp(suffix=".zip", dir=BASE_DIR)
            os.close(fd)
            def _on_chunk(downloaded, total):
                if total:
                    mo = downloaded / (1024 * 1024)
                    total_mo = total / (1024 * 1024)
                    self._push(f"piperInstallProgress({json.dumps(f'Téléchargement... {mo:.0f} / {total_mo:.0f} Mo')})")

            _download_file_with_retry(asset_url, tmp_zip, on_chunk=_on_chunk)

            self._push(f"piperInstallProgress({json.dumps('Extraction...')})")
            tmp_extract = tempfile.mkdtemp(dir=BASE_DIR)
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(tmp_extract)

            # L'archive dépose piper.exe soit à la racine, soit dans un
            # sous-dossier selon la version : on le cherche partout plutôt
            # que de supposer une structure fixe.
            found_dir = None
            for root, _dirs, files in os.walk(tmp_extract):
                if "piper.exe" in files:
                    found_dir = root
                    break
            if not found_dir:
                raise RuntimeError("piper.exe introuvable dans l'archive téléchargée.")

            if os.path.isdir(PIPER_DIR):
                shutil.rmtree(PIPER_DIR, ignore_errors=True)
            shutil.move(found_dir, PIPER_DIR)
            os.makedirs(PIPER_VOICES_DIR, exist_ok=True)

            self._push(f"piperInstallProgress({json.dumps('Piper installé avec succès.')})")
            self._push("piperInstallDone(true)")
            self._log("Piper (moteur vocal neuronal) installé.", "success")
        except Exception as e:
            self._log(f"[Erreur Piper] Installation : {e}", "error")
            self._push("piperInstallDone(false)")
        finally:
            try:
                if tmp_zip and os.path.exists(tmp_zip):
                    os.remove(tmp_zip)
            except Exception:
                pass
            try:
                if tmp_extract and os.path.isdir(tmp_extract):
                    shutil.rmtree(tmp_extract, ignore_errors=True)
            except Exception:
                pass

    def piper_download_voice(self, voice_id):
        """Télécharge une voix Piper curatée (modèle .onnx + config
        .onnx.json). Nécessite que le moteur Piper soit déjà installé."""
        voice_id = (voice_id or "").strip()
        if voice_id not in PIPER_VOICES:
            return {"ok": False, "error": "Voix Piper inconnue."}
        if not os.path.isfile(PIPER_EXE):
            return {"ok": False, "error": "Installe d'abord le moteur Piper."}
        threading.Thread(target=self._piper_download_voice_thread, args=(voice_id,), daemon=True).start()
        return {"ok": True}

    def _piper_download_voice_thread(self, voice_id):
        info = PIPER_VOICES[voice_id]
        os.makedirs(PIPER_VOICES_DIR, exist_ok=True)
        model_path = os.path.join(PIPER_VOICES_DIR, f"{voice_id}.onnx")
        config_path = os.path.join(PIPER_VOICES_DIR, f"{voice_id}.onnx.json")
        try:
            for suffix, dest in ((".onnx", model_path), (".onnx.json", config_path)):
                url = info["url_base"] + suffix
                self._push(
                    f"piperVoiceProgress({json.dumps(voice_id)}, "
                    f"{json.dumps(f'Téléchargement de {os.path.basename(dest)}...')})"
                )
                tmp_dest = dest + ".part"

                def _on_chunk(downloaded, total, suffix=suffix):
                    if total and suffix == ".onnx":
                        mo = downloaded / (1024 * 1024)
                        total_mo = total / (1024 * 1024)
                        self._push(
                            f"piperVoiceProgress({json.dumps(voice_id)}, "
                            f"{json.dumps(f'Téléchargement... {mo:.0f} / {total_mo:.0f} Mo')})"
                        )

                _download_file_with_retry(url, tmp_dest, on_chunk=_on_chunk)
                os.replace(tmp_dest, dest)

            self._log(f"Voix Piper « {info['label']} » téléchargée.", "success")
            self._push(f"piperVoiceDone({json.dumps(voice_id)}, true)")
        except Exception as e:
            self._log(f"[Erreur Piper] Téléchargement de la voix « {voice_id} » : {e}", "error")
            self._push(f"piperVoiceDone({json.dumps(voice_id)}, false)")
            # Retire tout fichier partiel/incohérent plutôt que de laisser
            # une voix à moitié téléchargée passer pour "installée".
            for p in (model_path, config_path, model_path + ".part", config_path + ".part"):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    def piper_delete_voice(self, voice_id):
        """Supprime une voix Piper téléchargée, pour libérer de l'espace
        disque."""
        voice_id = (voice_id or "").strip()
        if voice_id not in PIPER_VOICES:
            return {"ok": False}
        model_path = os.path.join(PIPER_VOICES_DIR, f"{voice_id}.onnx")
        config_path = os.path.join(PIPER_VOICES_DIR, f"{voice_id}.onnx.json")
        try:
            for p in (model_path, config_path):
                if os.path.exists(p):
                    os.remove(p)
        except Exception as e:
            self._log(f"[Erreur Piper] Suppression de la voix : {e}", "error")
            return {"ok": False}
        if self.piper_voice == voice_id:
            self.piper_voice = None
            self._persist_ai_config()
        self._log(f"Voix Piper « {PIPER_VOICES[voice_id]['label']} » supprimée.", "success")
        return {"ok": True}

    def ai_test_piper_voice(self, voice_id=None):
        """Prononce une phrase de test avec la voix Piper indiquée (ou
        celle actuellement configurée)."""
        voice_id = voice_id or self.piper_voice
        if not voice_id:
            return {"ok": False, "error": "Aucune voix Piper sélectionnée."}
        sample = f"Bonjour, je suis {self.ai_name}, prêt à vous accompagner dans le vaisseau."
        self._speak(sample, piper_voice=voice_id)
        return {"ok": True}

    def ai_check_status(self, allow_autostart=False):
        """Vérifie si Ollama tourne, et si le modèle actuellement
        sélectionné (self.ai_model) est déjà téléchargé. Répercute aussi
        le résultat vers l'overlay en jeu (voir _overlay_set_ai), pour
        que son statut IA reste à jour à chaque vérification, quelle que
        soit son origine (ouverture du panneau, bouton Revérifier...)."""
        status = self._ai_check_status_impl(allow_autostart)
        if status.get("running") and status.get("modelReady"):
            label = f"prêt · {status.get('model')}" if status.get("model") else "prêt"
        elif status.get("running"):
            label = "modèle manquant"
        else:
            label = "hors ligne"
        self._overlay_set_ai(label)
        return status

    def _ai_check_status_impl(self, allow_autostart=False):
        """Vérifie si Ollama tourne, et si le modèle actuellement
        sélectionné (self.ai_model) est déjà téléchargé.

        allow_autostart=False (par défaut) : vérification PASSIVE, ne
        touche à rien — utilisée à l'ouverture du panneau IA, pour ne
        jamais relancer Ollama dans le dos de l'utilisateur juste parce
        qu'il a regardé le statut (ça rendait notamment impossible de
        tester volontairement "Ollama coupé").

        allow_autostart=True : si l'exécutable est trouvé sur le disque
        mais que le service ne répond pas, tente de le démarrer avant de
        réessayer une fois (cas fréquent : Ollama vient d'être installé
        mais son service en arrière-plan n'a pas encore été lancé) — et
        le signale clairement dans le journal système, pour que ça ne
        soit jamais un redémarrage silencieux. Réservé aux actions
        explicites de l'utilisateur (bouton « Revérifier », installation
        d'Ollama)."""
        status = self._ai_ping_ollama()
        if status["running"]:
            return status

        exe_path = find_ollama_executable()
        if not exe_path:
            return {"running": False, "modelReady": False, "model": self.ai_model,
                    "installedModels": [], "installed": False, "downloadUrl": OLLAMA_DOWNLOAD_URL}

        if not allow_autostart:
            return {"running": False, "modelReady": False, "model": self.ai_model,
                    "installedModels": [], "installed": True, "downloadUrl": OLLAMA_DOWNLOAD_URL}

        # L'exécutable existe mais ne répond pas : on tente de le démarrer.
        self._log("Tentative de démarrage automatique d'Ollama...", "info")
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            subprocess.Popen(
                [exe_path, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception:
            pass

        time.sleep(1.5)
        status = self._ai_ping_ollama()
        status["installed"] = True
        if status["running"]:
            self._log("Ollama démarré automatiquement avec succès.", "success")
        if not status["running"]:
            status["downloadUrl"] = OLLAMA_DOWNLOAD_URL
        return status

    def _ai_ping_ollama(self):
        try:
            with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            installed_models = [m.get("name", "") for m in data.get("models", [])]
            model_ready = any(
                m == self.ai_model or m.startswith(self.ai_model + ":")
                for m in installed_models
            )
            return {
                "running": True,
                "modelReady": model_ready,
                "model": self.ai_model,
                "installedModels": installed_models,
            }
        except (urllib.error.URLError, ConnectionRefusedError, OSError, TimeoutError):
            return {"running": False, "modelReady": False, "model": self.ai_model, "installedModels": []}

    def ai_install_ollama(self):
        """Télécharge l'installeur Windows officiel d'Ollama et le lance,
        pour que l'utilisateur puisse l'installer sans quitter
        l'application ni chercher le lien lui-même. L'installeur garde son
        interface normale une fois lancé (Ollama ne documente pas d'option
        silencieuse fiable selon les versions) : l'utilisateur doit encore
        suivre les quelques étapes affichées, puis revenir cliquer sur
        « Revérifier »."""
        threading.Thread(target=self._ai_install_ollama_thread, daemon=True).start()
        return {"ok": True}

    def _ai_install_ollama_thread(self):
        if sys.platform != "win32":
            self._push(f"aiInstallOllamaProgress({json.dumps('Installation automatique disponible uniquement sous Windows. Télécharge Ollama manuellement depuis ' + OLLAMA_DOWNLOAD_URL)})")
            self._push("aiInstallOllamaDone(false)")
            return

        tmp_installer = None
        try:
            self._push(f"aiInstallOllamaProgress({json.dumps('Téléchargement de l’installeur Ollama...')})")
            fd, tmp_installer = tempfile.mkstemp(suffix=".exe", dir=BASE_DIR)
            os.close(fd)

            last_reported = {"percent": -1}

            def _on_chunk(downloaded, total):
                if total:
                    percent = int(downloaded / total * 100)
                    if percent != last_reported["percent"]:
                        last_reported["percent"] = percent
                        mo = downloaded / (1024 * 1024)
                        total_mo = total / (1024 * 1024)
                        self._push(f"aiInstallOllamaProgress({json.dumps(f'Téléchargement... {mo:.0f} / {total_mo:.0f} Mo')})")
                else:
                    mo = downloaded / (1024 * 1024)
                    self._push(f"aiInstallOllamaProgress({json.dumps(f'Téléchargement... {mo:.0f} Mo')})")

            _download_file_with_retry(OLLAMA_WINDOWS_INSTALLER_URL, tmp_installer, on_chunk=_on_chunk)

            self._push(f"aiInstallOllamaProgress({json.dumps('Lancement de l’installeur... suis les étapes affichées à l’écran si besoin.')})")
            # Popen (pas run) : on ne bloque pas en attendant que
            # l'utilisateur termine l'installeur, potentiellement
            # interactif.
            subprocess.Popen([tmp_installer], cwd=BASE_DIR)
            self._push("aiInstallOllamaDone(true)")
        except Exception as e:
            self._push(f"aiInstallOllamaProgress({json.dumps('Erreur : ' + str(e))})")
            self._push(f"aiInstallOllamaProgress({json.dumps('Tu peux aussi installer Ollama manuellement depuis ' + OLLAMA_DOWNLOAD_URL)})")
            self._push("aiInstallOllamaDone(false)")
            # En cas d'échec, le fichier temporaire (partiel ou inutile)
            # peut être nettoyé tout de suite ; en cas de succès on le
            # laisse en place, l'installeur en a besoin pendant qu'il
            # tourne (et Windows le verrouille de toute façon).
            try:
                if tmp_installer and os.path.exists(tmp_installer):
                    os.remove(tmp_installer)
            except Exception:
                pass
            return

        # Une fois l'installeur lancé, on enchaîne automatiquement :
        # attendre que le service Ollama démarre (l'utilisateur peut
        # encore avoir une étape à valider dans l'installeur Windows),
        # puis télécharger directement le modèle IA actuellement choisi
        # (llama3.2, llama3.1:8b...) — sans obliger l'utilisateur à
        # revenir cliquer sur « Revérifier » puis « Télécharger le
        # modèle » séparément : "installer l'IA" doit vouloir dire les
        # deux étapes d'un coup.
        attente_msg = "En attente du démarrage d'Ollama (termine l'installation à l'écran si besoin)..."
        self._push(f"aiInstallOllamaProgress({json.dumps(attente_msg)})")
        if not self._wait_for_ollama_ready(timeout=300):
            self._push(f"aiInstallOllamaProgress({json.dumps('Ollama ne répond toujours pas après 5 minutes. Une fois l’installation terminée, clique sur « Revérifier ».')})")
            return

        self._push(f"aiInstallOllamaProgress({json.dumps(f'Ollama est démarré. Téléchargement du modèle « {self.ai_model} » choisi...')})")
        self._ai_pull_model_thread()

    def _wait_for_ollama_ready(self, timeout=300, interval=3):
        """Attend que le service Ollama réponde (ping HTTP local),
        typiquement après avoir lancé son installeur. Retente aussi de le
        démarrer lui-même une fois en cours de route, au cas où
        l'installation soit déjà terminée mais le service pas encore
        lancé (cas fréquent juste après une installation). Retourne False
        si le délai est dépassé sans succès."""
        deadline = time.time() + timeout
        tried_start = False
        while time.time() < deadline:
            if self._ai_ping_ollama()["running"]:
                return True
            if not tried_start:
                exe_path = find_ollama_executable()
                if exe_path:
                    tried_start = True
                    try:
                        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                        subprocess.Popen(
                            [exe_path, "serve"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=creationflags,
                        )
                    except Exception:
                        pass
            time.sleep(interval)
        return False

    def ai_pull_model(self):
        """Télécharge le modèle actuellement sélectionné (self.ai_model)
        en arrière-plan ; progression poussée dans le panneau de
        discussion via ai_pull_progress / ai_pull_done."""
        threading.Thread(target=self._ai_pull_model_thread, daemon=True).start()
        return {"ok": True}

    def _ai_pull_model_thread(self):
        exe_path = find_ollama_executable()
        if not exe_path:
            self._push(f"aiPullProgress({json.dumps('Ollama introuvable sur cette machine. Installe-le depuis ' + OLLAMA_DOWNLOAD_URL + ', puis relance cette application.')})")
            self._push("aiPullDone(false)")
            return

        try:
            process = subprocess.Popen(
                [exe_path, "pull", self.ai_model],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            for line in process.stdout:
                line = line.strip()
                if line:
                    self._push(f"aiPullProgress({json.dumps(line)})")
            process.wait()
            if process.returncode == 0:
                self._push("aiPullDone(true)")
            else:
                self._push(f"aiPullProgress({json.dumps(f'Le processus a échoué (code {process.returncode}). Vérifie ta connexion internet.')})")
                self._push("aiPullDone(false)")
        except FileNotFoundError:
            self._push(f"aiPullProgress({json.dumps('Ollama introuvable. Installe-le depuis ' + OLLAMA_DOWNLOAD_URL)})")
            self._push("aiPullDone(false)")
        except Exception as e:
            self._push(f"aiPullProgress({json.dumps('Erreur : ' + str(e))})")
            self._push("aiPullDone(false)")

    def ai_uninstall_model(self, model_id):
        """Supprime un modèle Ollama du disque pour libérer de l'espace.
        Fonctionne en arrière-plan ; le panneau se rafraîchit une fois
        terminé via ai_uninstall_done."""
        model_id = (model_id or "").strip()
        if not model_id:
            return {"ok": False, "error": "Aucun modèle indiqué."}
        threading.Thread(target=self._ai_uninstall_model_thread, args=(model_id,), daemon=True).start()
        return {"ok": True}

    def _ai_uninstall_model_thread(self, model_id):
        exe_path = find_ollama_executable()
        if not exe_path:
            self._log(f"[Erreur] Ollama introuvable, impossible de désinstaller « {model_id} ».", "error")
            self._push("aiUninstallDone(false)")
            return
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            result = subprocess.run(
                [exe_path, "rm", model_id],
                capture_output=True, text=True, creationflags=creationflags, timeout=30,
            )
            if result.returncode == 0:
                self._log(f"Modèle « {model_id} » désinstallé, espace disque libéré.", "success")
                self._push("aiUninstallDone(true)")
            else:
                self._log(f"[Erreur] Impossible de désinstaller « {model_id} » : {result.stderr.strip()}", "error")
                self._push("aiUninstallDone(false)")
        except Exception as e:
            self._log(f"[Erreur] Désinstallation de « {model_id} » : {e}", "error")
            self._push("aiUninstallDone(false)")

    def ai_toggle_voice_output(self, enabled):
        self.ai_voice_output = bool(enabled)
        return self.ai_voice_output

    def ai_toggle_enabled(self, enabled):
        """Active/désactive complètement l'assistant Nova/Ollama : plus
        aucune détection du mot d'activation dans _handle_text tant que
        c'est désactivé (voir wake_word là-bas). Utile pour ne garder
        qu'un seul des deux assistants (Nova ou Gemini) actif à la fois
        si les deux en même temps font double emploi."""
        self.ai_enabled = bool(enabled)
        if not self.ai_enabled:
            self._ai_awaiting_question = False
        self._persist_ai_config()
        return self.ai_enabled

    def ai_clear_history(self):
        self.ai_history = []
        return {"ok": True}

    def _ai_ask(self, question):
        """Envoie une question à l'IA (déclenchée par le mot d'activation
        "Nova" reconnu dans la boucle d'écoute) et pousse la conversation
        vers le panneau. La réponse est aussi lue à voix haute si activé."""
        self.ai_history.append({"role": "user", "content": question})
        self._push(f"aiUserMessage({json.dumps(question)})")
        threading.Thread(target=self._ai_reply_thread, daemon=True).start()

    def _enforce_short_reply(self, reply):
        """Filet de sécurité pour le mode "Courtes" : certains petits
        modèles locaux (ex. Llama 3.2 3B) ne respectent pas toujours de
        façon fiable la consigne de brièveté donnée dans le prompt
        système — ils continuent d'enchaîner salutations, digressions et
        questions de suivi. Cette fonction retaille la réponse a
        posteriori pour garantir une seule phrase courte quoi qu'il
        arrive, sans dépendre entièrement de l'obéissance du modèle. Elle
        s'applique aussi à la version stockée dans l'historique, ce qui
        évite en plus que le modèle n'imite ses propres réponses trop
        longues lors des tours suivants."""
        if self.ai_response_length != "short":
            return reply
        text = (reply or "").strip()
        if not text or text.startswith("[Erreur]") or text.startswith("("):
            return reply

        # Réponse structurée en liste/étapes (ex. "1. ... 2. ... 3. ...",
        # ou des tirets "- ...") : on ne découpe pas, une liste de
        # consignes incomplète (coupée après "1.") est pire qu'une liste
        # un peu longue mais complète et utilisable.
        is_list = bool(re.search(r'(?:^|\n|\s)(?:\d+[.\)]|-)\s+\S', text))
        if is_list:
            return text

        # Ne garde que la première "vraie" phrase : on avance de point en
        # point jusqu'à avoir au moins quelques mots, pour ne pas couper
        # après une simple interpellation ("Ammoniak !") qui n'est pas
        # une phrase complète en soi. Un point précédé d'un chiffre
        # ("1.", "3.5") n'est pas considéré comme une fin de phrase, pour
        # ne pas confondre une numérotation ou un nombre décimal avec une
        # vraie ponctuation.
        matches = list(re.finditer(r'(?<!\d)[.!?](?:\s|$)', text))
        if matches:
            min_words = 4
            end = matches[-1].end()
            for m in matches:
                candidate = text[:m.end()].strip()
                if len(candidate.split()) >= min_words:
                    end = m.end()
                    break
            return text[:end].strip()

        # Aucune ponctuation de fin de phrase trouvée : la réponse est
        # formulée comme une seule longue phrase sans point (fréquent
        # avec un petit modèle qui enchaîne les virgules). On ne la coupe
        # PAS au milieu — une réponse informative légitime mais un peu
        # longue vaut mieux qu'une réponse tronquée et incompréhensible
        # ("...il te faudra naviguer jusqu'à la frontière, en…"). On
        # n'intervient qu'en tout dernier recours, si le texte est
        # vraiment démesuré (signe probable d'un dérapage du modèle).
        words = text.split()
        max_words = 60
        if len(words) > max_words:
            text = " ".join(words[:max_words]).rstrip(".,;:!?") + "…"

        return text

    @staticmethod
    def _compute_num_ctx(messages):
        """Calcule une fenêtre de contexte (num_ctx) suffisante pour tout
        faire tenir (prompt système + connaissances personnalisées +
        historique de conversation), plutôt que de laisser Ollama sur sa
        valeur par défaut (2048 tokens sur la plupart des installations) :
        un lore un peu long dépasse vite cette limite, et le début du
        contexte (qui peut contenir l'information demandée) se retrouve
        tronqué — le modèle invente alors une réponse plausible mais
        fausse plutôt que d'admettre qu'il ne sait pas. Reste toutefois
        borné, pour ne pas alourdir excessivement la RAM/VRAM pendant que
        Star Citizen tourne en même temps."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        # Estimation volontairement pessimiste (~1 token pour 2 caractères)
        # pour ne jamais sous-évaluer le nombre de tokens réellement
        # nécessaire.
        estimated_tokens = total_chars // 2 + 512
        num_ctx = max(2048, min(16384, ((estimated_tokens // 1024) + 1) * 1024))
        return num_ctx

    # Plafond de tokens générés par réponse, selon le réglage de longueur
    # choisi par l'utilisateur. Sans cette limite, Ollama génère souvent
    # bien plus de texte que nécessaire (le modèle continue d'enchaîner
    # phrases et digressions) avant que _enforce_short_reply ne le
    # retaille après coup — un gaspillage direct de temps de génération,
    # qui est le principal facteur de latence perçue sur un PC sans GPU
    # dédié à l'IA (Star Citizen occupe déjà le GPU). Valeurs généreuses
    # pour ne jamais couper une réponse légitime au milieu.
    AI_NUM_PREDICT_BY_LENGTH = {"short": 80, "normal": 300, "long": 800}

    # Les modèles Gemini 3.x réfléchissent avant de répondre (thinkingLevel),
    # ce qui ajoute une latence significative avant même le premier mot de
    # la réponse — c'est cette étape de "réflexion" interne, invisible pour
    # l'utilisateur, qui est la principale cause de lenteur perçue sur
    # Gemini (contrairement à Ollama, où le goulot est le matériel local).
    # Pour un assistant vocal de commandes courtes, un niveau réduit
    # accélère nettement la réponse sans perte de qualité notable ; "medium"
    # (le défaut de l'API) n'est gardé que pour les réponses longues, où un
    # peu plus de réflexion reste utile.
    GEMINI_THINKING_LEVEL_BY_LENGTH = {"short": "minimal", "normal": "low", "long": "medium"}

    def _ai_reply_thread(self):
        # GARDE-FOU : vérifie d'abord rapidement (2s max) qu'Ollama répond
        # avant de tenter la requête de génération complète. Si Ollama
        # n'est pas lancé, ça évite d'attendre jusqu'à 60 secondes pour un
        # échec qui, dans certains cas (pare-feu, antivirus, résolution
        # localhost), peut mettre bien plus de temps que l'instantané
        # "connexion refusée" attendu à trainer avant de se manifester.
        if not self._ai_ping_ollama()["running"]:
            reply = (
                "[Erreur] Ollama n'est pas lancé. Ouvre le panneau Assistant IA "
                "et clique sur « Revérifier », ou démarre Ollama manuellement."
            )
            self.ai_history.append({"role": "assistant", "content": reply})
            self._push(f"aiReceiveMessage({json.dumps(reply)})")
            return

        game_state_block = ""
        if self._game_log_watcher:
            game_state_block = game_state_to_prompt_block(self._game_log_watcher.get_state())

        messages = [{
            "role": "system",
            "content": ai_system_prompt(self.ai_name, self.ai_custom_context, self.user_name, self.ai_response_length)
                       + game_state_block,
        }] + self.ai_history
        num_predict = self.AI_NUM_PREDICT_BY_LENGTH.get(
            self.ai_response_length, self.AI_NUM_PREDICT_BY_LENGTH["normal"]
        )
        payload = json.dumps({
            "model": self.ai_model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": self._compute_num_ctx(messages),
                "num_predict": num_predict,
            },
            # Garde le modèle chargé en mémoire 30 minutes après chaque
            # usage (au lieu des 5 minutes par défaut d'Ollama) : Nova est
            # utilisée par intermittence pendant une session de jeu, et un
            # modèle déchargé doit être entièrement rechargé depuis le
            # disque à la requête suivante — souvent bien plus lent que la
            # génération elle-même sur un PC sans GPU dédié à l'IA. C'est
            # la principale cause de lenteur perçue de l'assistant IA.
            "keep_alive": "30m",
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            reply = data.get("message", {}).get("content", "").strip()
            if not reply:
                reply = "(réponse vide du modèle)"
        except TimeoutError:
            # On sait déjà qu'Ollama tourne (voir le pré-check en tête de
            # méthode, juste avant) : un timeout ICI ne veut donc PAS dire
            # "Ollama n'est pas lancé", mais que la génération elle-même
            # (modèle lourd et/ou contexte personnalisé volumineux, sur un
            # PC sans GPU dédié à l'IA) prend plus de 45 secondes. Message
            # différent de celui du pré-check, pour ne pas induire en
            # erreur sur la vraie cause.
            reply = (
                "[Erreur] Ollama est bien lancé, mais la génération de la réponse a "
                f"pris plus de 45 secondes avec le modèle « {self.ai_model} ». Essaie "
                "un modèle plus léger (llama3.2) ou réduis la taille de tes "
                "connaissances personnalisées dans les réglages avancés."
            )
        except (urllib.error.URLError, ConnectionRefusedError, OSError) as e:
            reply = (f"[Erreur] Impossible de contacter Ollama ({e}). "
                     f"Vérifie qu'Ollama est lancé et que le modèle est installé.")
        except Exception as e:
            reply = f"[Erreur] {e}"

        reply = self._enforce_short_reply(reply)

        self.ai_history.append({"role": "assistant", "content": reply})
        if len(self.ai_history) > AI_MAX_HISTORY_MESSAGES:
            # Garde uniquement les échanges les plus récents : avec un
            # long contexte personnalisé (lore...), un historique qui
            # grossit sans limite finirait par saturer la fenêtre de
            # contexte au fil d'une longue session.
            self.ai_history = self.ai_history[-AI_MAX_HISTORY_MESSAGES:]
        self._push(f"aiReceiveMessage({json.dumps(reply)})")

        if self.ai_voice_output and "[Erreur]" not in reply:
            self._speak(reply)

    @staticmethod
    def _strip_markdown_for_speech(text):
        """Retire la mise en forme Markdown que le modèle ajoute parfois
        (**gras**, *italique*, listes à puces "- ", titres "#"...) avant
        d'envoyer le texte à la synthèse vocale. Piper lit les symboles
        littéralement au lieu de les interpréter comme de la mise en
        forme (le "*" est par exemple prononcé "astérisque"), ce qui rend
        la lecture incompréhensible — cette fonction ne touche pas au
        texte affiché dans le panneau de discussion, seulement à la
        version envoyée à la voix."""
        if not text:
            return text
        text = re.sub(r'`+', '', text)
        text = re.sub(r'(?m)^#{1,6}\s*', '', text)
        text = re.sub(r'(?m)^[ \t]*[-*+]\s+', '', text)
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)
        text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', text)
        text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', text)
        # Symboles restants isolés (rares, mais au cas où) : on les retire
        # plutôt que de risquer que Piper les épelle.
        text = re.sub(r'[*_#`]', '', text)
        return text

    def _speak(self, text, piper_voice=None):
        """Dépose un texte dans la file d'attente vocale (le rendu audio se
        fait dans _tts_worker, sur son propre thread, via Piper). Sans
        argument, utilise la voix Piper actuellement configurée ; l'argument
        permet d'en forcer une précise (ex. pour tester une voix sans
        changer le réglage actif)."""
        text = (text or "").strip()
        if not text:
            return
        text = self._strip_markdown_for_speech(text)
        self._tts_queue.put({
            "text": text,
            "piper_voice": piper_voice if piper_voice is not None else self.piper_voice,
        })

    def _tts_worker(self):
        """Boucle exécutée sur un unique thread dédié pendant toute la vie
        de l'application. Chaque texte à lire est délégué à un processus
        externe et isolé (piper.exe + lecteur WAV — voir _speak_via_piper) :
        aucune bibliothèque tierce embarquée dans notre propre processus,
        donc aucun risque qu'un souci de synthèse vocale ne fasse planter
        l'application."""
        while True:
            item = self._tts_queue.get()
            self._is_speaking = True
            try:
                self._speak_via_piper(item["text"], item.get("piper_voice"))
            except Exception as e:
                self._log(f"[Erreur voix] {e}", "error")
            finally:
                self._is_speaking = False
                self._speech_mute_until = time.time() + self._speech_mute_grace

    def _play_wav_file(self, wav_path):
        """Lit un fichier .wav déjà généré par Piper directement via
        sounddevice (RawOutputStream), au lieu de PowerShell/
        Media.SoundPlayer. Deux raisons à ce choix :

        1) Media.SoundPlayer joue toujours sur le périphérique de sortie
           *par défaut de Windows* au moment du lancement du process
           powershell.exe — un process neuf à chaque lecture, qui n'a
           donc aucun réglage de sortie "épinglé" comme peuvent l'avoir
           d'autres applis (navigateur...). En cas de périphérique par
           défaut ambigu/changeant, le son pouvait sortir ailleurs que
           prévu. Ici, self._resolve_output_device() permet de fixer
           explicitement un périphérique de sortie (self.output_device_name),
           de la même façon que le micro d'entrée.

        2) Ça permet d'appliquer un gain (self.tts_volume) directement au
           signal avant lecture, via apply_mic_gain — Piper ne normalise
           pas son amplitude de sortie, qui peut ressortir plus fort que
           le reste de l'audio habituel et causer une fuite (crosstalk
           électrique entrée micro / sortie casque sur certains codecs).

        Interruptible via self._tts_stop_event (voir _interrupt_speech) :
        l'écriture se fait par petits blocs, vérifiés entre chaque, pour
        une latence d'arrêt courte plutôt que d'attendre la fin du fichier."""
        if sd is None:
            # Chemin rapide de démarrage (voir _main_fast_path) : sounddevice
            # peut ne pas encore avoir fini de s'importer en tâche de fond
            # dans les toute premières secondes. On échoue proprement (log
            # "[Erreur voix]" côté _tts_worker) plutôt que de planter.
            raise RuntimeError("Moteur audio pas encore prêt (démarrage en cours), réessaie dans un instant.")

        with wave.open(wav_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        # Piper génère toujours du PCM 16 bits ; on n'applique le gain
        # que dans ce cas plutôt que de risquer de corrompre un format
        # inattendu.
        if sampwidth == 2 and self.tts_volume != 1.0:
            raw = apply_mic_gain(raw, self.tts_volume)

        output_device = self._resolve_output_device()

        stop_event = threading.Event()
        with self._tts_lock:
            self._tts_stop_event = stop_event

        bytes_per_frame = sampwidth * n_channels
        chunk_bytes = 2048 * bytes_per_frame  # ~90 ms à 22050 Hz : latence d'arrêt courte

        def _play_on(device):
            with sd.RawOutputStream(
                samplerate=framerate,
                channels=n_channels,
                dtype="int16",
                device=device,
            ) as stream:
                total = len(raw)
                offset = 0
                while offset < total and not stop_event.is_set():
                    chunk = raw[offset:offset + chunk_bytes]
                    stream.write(chunk)
                    offset += chunk_bytes

        try:
            try:
                _play_on(output_device)
            except sd.PortAudioError as e:
                # Filet de sécurité : un périphérique déjà enregistré dans
                # audio_config.json AVANT l'exclusion des entrées WDM-KS
                # (voir _is_wdmks_device) pourrait encore pointer dessus.
                # Plutôt que de planter, on retente une fois sur le
                # périphérique de sortie PAR DÉFAUT du système.
                if output_device is not None:
                    self._log(
                        f"[Avertissement] Lecture impossible sur le périphérique de sortie "
                        f"sélectionné ({e}), repli sur le périphérique par défaut.",
                        "error",
                    )
                    _play_on(None)
                else:
                    raise
        finally:
            with self._tts_lock:
                if self._tts_stop_event is stop_event:
                    self._tts_stop_event = None

    def _speak_via_piper(self, text, voice_id):
        """Génère puis lit un texte via Piper (synthèse neuronale locale,
        voix naturelles). Les curseurs de vitesse/
        expressivité (self.piper_length_scale / self.piper_noise_scale)
        sont passés directement à piper.exe."""
        if not voice_id:
            raise RuntimeError("Aucune voix Piper sélectionnée (voir Réglages > Moteur vocal).")
        if not os.path.isfile(PIPER_EXE):
            raise RuntimeError("Piper n'est pas installé (voir Réglages > Moteur vocal).")
        model_path = os.path.join(PIPER_VOICES_DIR, f"{voice_id}.onnx")
        if not os.path.isfile(model_path):
            raise RuntimeError(f"Voix Piper « {voice_id} » non téléchargée.")

        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        fd, wav_path = tempfile.mkstemp(suffix=".wav", dir=BASE_DIR)
        os.close(fd)
        try:
            gen = subprocess.run(
                [
                    PIPER_EXE, "--model", model_path, "--output_file", wav_path,
                    "--length_scale", str(self.piper_length_scale),
                    "--noise_scale", str(self.piper_noise_scale),
                ],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags, timeout=60,
            )
            if gen.returncode != 0 or not os.path.isfile(wav_path):
                raise RuntimeError("La génération audio par Piper a échoué.")

            if self.radio_effect:
                apply_radio_effect(wav_path)

            self._play_wav_file(wav_path)
        finally:
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except Exception:
                pass

    def _is_stop_phrase(self, text_norm):
        """Détecte une demande d'interruption vocale du type
        "<nom de l'IA> stop" (ou arrête, silence, tais-toi...), utilisée
        pour couper une réponse trop longue en train d'être lue. Exige le
        nom de l'IA en plus du mot d'arrêt pour éviter qu'un "stop" capté
        par hasard (vidéo, discussion) ne coupe la voix à tort."""
        wake_word = self.ai_name.lower().strip()
        if not wake_word or wake_word not in text_norm:
            return False
        stop_keywords = ("stop", "stoppe", "arrête", "arrete", "silence", "tais-toi", "tais toi", "chut")
        return any(k in text_norm for k in stop_keywords)

    def _interrupt_speech(self):
        """Coupe immédiatement la lecture vocale en cours (le cas échéant)
        et vide la file d'attente pour ne pas enchaîner sur un autre
        message en attente."""
        try:
            while True:
                self._tts_queue.get_nowait()
        except queue.Empty:
            pass

        with self._tts_lock:
            stop_event = self._tts_stop_event
        if stop_event is not None:
            stop_event.set()

    # -------------------------------------------------- Appels ici -> JS

    def _push(self, js):
        if self._window:
            try:
                self._window.evaluate_js(js)
            except Exception:
                pass

    def _log(self, msg, kind="info"):
        self._push(f"appendLog({json.dumps(msg)}, {json.dumps(kind)})")

    def log_error(self, msg):
        """Point d'entrée appelé depuis le JavaScript (voir appendLog dans
        script.js) pour CHAQUE message d'erreur affiché dans le panneau,
        qu'il vienne de Python (via _log) ou d'une validation faite
        directement côté interface (ex. "Dossier du modèle introuvable").
        Centralise ainsi toutes les erreurs réellement montrées à
        l'utilisateur dans erreurs.log, peu importe leur origine."""
        error_logger.error(msg)
        return {"ok": True}

    def _status(self, status, label, sub=None):
        self._push(f"setStatus({json.dumps(status)}, {json.dumps(label)}, {json.dumps(sub)})")

    def _flash(self, index):
        self._push(f"flashCommand({json.dumps(index)})")

    # -------------------------------------------------------- Overlay --
    # Voir le bloc de commentaires au-dessus de load_overlay_config() pour
    # le choix d'architecture (fenêtre séparée, pas d'injection dans le
    # jeu).

    def overlay_get_state(self):
        return {
            "enabled": self.overlay_enabled, "editMode": self.overlay_edit_mode,
            "visibleRows": self.overlay_visible_rows,
        }

    def overlay_set_row_visible(self, row_key, visible):
        """Appelé depuis la case à cocher d'une ligne de l'overlay (visible
        uniquement en mode "déplacer", voir overlay.html) : mémorise si
        cette ligne doit rester affichée une fois l'overlay verrouillé.
        Persisté immédiatement, indépendamment de la position/activation
        (voir save_overlay_config), pour ne rien perdre même si l'overlay
        est fermé sans repasser par une sauvegarde déclenchée par un
        déplacement."""
        row_key = (row_key or "").strip()
        if row_key not in OVERLAY_ROW_KEYS:
            return {"ok": False, "error": "Ligne inconnue."}
        self.overlay_visible_rows[row_key] = bool(visible)
        save_overlay_config(
            self.overlay_enabled, *self._overlay_saved_pos,
            visible_rows=self.overlay_visible_rows,
        )
        return {"ok": True, "visibleRows": self.overlay_visible_rows}

    def overlay_resize_to_content(self, height):
        """Redimensionne la HAUTEUR de la fenêtre overlay pour qu'elle
        corresponde exactement aux lignes actuellement affichées (voir
        overlayRecalcHeight côté overlay.html), plutôt que de garder un
        espace vide en bas une fois des lignes décochées. La largeur ne
        change jamais (aucune ligne n'en a besoin, contrairement à la
        hauteur qui dépend directement du nombre de lignes empilées).

        Ne s'applique QU'en mode verrouillé : en mode "déplacer", la
        fenêtre garde volontairement sa taille par défaut (voir
        _create_overlay_window) pour que toutes les lignes — même
        décochées — restent visibles et cochables ; côté JS,
        overlayRecalcHeight() ne déclenche d'ailleurs cet appel que dans
        ce cas précis, mais on revérifie ici aussi par sécurité (ex. appel
        déclenché juste avant/après une bascule de mode)."""
        if not self._overlay_window or self.overlay_edit_mode:
            return {"ok": False}
        try:
            height = max(40, min(2000, int(round(float(height)))))
        except (TypeError, ValueError):
            return {"ok": False}
        try:
            self._overlay_window.resize(OVERLAY_DEFAULT_WIDTH, height)
        except Exception as e:
            self._log(f"[Erreur overlay] Redimensionnement au contenu impossible : {e}", "error")
            return {"ok": False}
        return {"ok": True}

    def overlay_set_enabled(self, enabled):
        """Active/désactive l'overlay (case à cocher des Réglages)."""
        enabled = bool(enabled)
        if enabled == self.overlay_enabled:
            return self.overlay_get_state()
        if enabled:
            self._create_overlay_window()
            # Renvoie l'état de façon OPTIMISTE : self.overlay_enabled ne
            # sera réellement mis à True qu'une fois le chargement de la
            # fenêtre terminé (événement "loaded", asynchrone — voir
            # _create_overlay_window). Sans ça, l'appelant recevait
            # "enabled: False" juste après avoir coché la case, cachant
            # à tort le bouton "Déplacer l'overlay" jusqu'à la prochaine
            # ouverture des Réglages.
            return {"enabled": True, "editMode": self.overlay_edit_mode}
        else:
            self._destroy_overlay_window()
        return self.overlay_get_state()

    def get_autolaunch_with_sc_enabled(self):
        """État actuel de la case « lancer NovaVox au démarrage de Star
        Citizen » (Réglages > NovaVox)."""
        return is_star_citizen_autolaunch_enabled()

    def set_autolaunch_with_sc_enabled(self, enabled):
        """Active/désactive la veille de démarrage automatique."""
        return set_star_citizen_autolaunch_enabled(bool(enabled))

    def overlay_set_edit_mode(self, editable):
        """Bascule entre mode "déplacer" (fenêtre normale opaque,
        glissable à la souris) et mode "verrouillé" (fenêtre transparente,
        qui bloque intégralement les clics — voir _create_overlay_window
        pour l'explication de cette correspondance). Recrée la fenêtre à
        chaque bascule : le paramètre transparent de pywebview ne peut
        pas être changé sur une fenêtre déjà créée, seulement à la
        création. Sans effet si l'overlay n'est pas actuellement
        affiché."""
        editable = bool(editable)
        if editable == self.overlay_edit_mode and self._overlay_window is not None:
            return self.overlay_edit_mode  # déjà dans cet état, rien à faire

        if self._overlay_window is None:
            # Overlay pas encore affiché : mémorise juste la préférence,
            # appliquée à la prochaine création.
            self.overlay_edit_mode = editable
            return self.overlay_edit_mode

        self._overlay_recreating = True
        try:
            self._overlay_window.destroy()
        except Exception:
            pass
        self._overlay_window = None
        self._overlay_hwnd = None
        self._create_overlay_window(transparent=not editable, edit_mode=editable)
        # Délai avant de retirer la protection : l'événement "closing" de
        # l'ANCIENNE fenêtre peut se déclencher de façon différée par
        # rapport à l'appel destroy() ci-dessus (comportement asynchrone
        # de pywebview) — le retirer trop tôt risquerait qu'il soit traité
        # comme une vraie désactivation juste après avoir recréé la
        # nouvelle fenêtre, cassant tout l'état.
        threading.Timer(0.6, lambda: setattr(self, "_overlay_recreating", False)).start()
        return self.overlay_edit_mode

    # -------- Glisser natif Windows (voir overlay.html) --------
    # Après plusieurs échecs avec un suivi manuel de la souris côté JS
    # (calcul du déplacement + window.move() à chaque mousemove) — le
    # diagnostic a montré que overlay_move_to n'était même jamais
    # atteint, suggérant un souci plus profond avec cette approche dans
    # cet environnement — on utilise maintenant la technique standard
    # pour rendre glissable une fenêtre sans bordure : au clic, on
    # indique à Windows lui-même que l'utilisateur vient de cliquer sur
    # la "barre de titre" (même si elle n'existe pas visuellement, vu que
    # la fenêtre est frameless), via les messages Windows natifs
    # WM_NCLBUTTONDOWN + HTCAPTION. Windows prend alors en charge
    # l'INTÉGRALITÉ du glisser lui-même (position, échelle DPI, tout) —
    # exactement comme s'il s'agissait d'une vraie barre de titre. C'est
    # la même technique qu'utilisent la quasi-totalité des applications à
    # fenêtre sans bordure (Electron et autres).
    _WM_NCLBUTTONDOWN = 0x00A1
    _HTCAPTION = 2

    def overlay_start_native_drag(self):
        """Démarre un glisser natif Windows de la fenêtre overlay. Appelé
        une seule fois au clic (mousedown) — Windows gère ensuite tout le
        reste lui-même jusqu'au relâchement du bouton, sans plus aucune
        coordination nécessaire côté JS/Python."""
        if not self._overlay_hwnd or not self.overlay_edit_mode:
            return {"ok": False}
        try:
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(
                self._overlay_hwnd, self._WM_NCLBUTTONDOWN, self._HTCAPTION, 0
            )
        except Exception as e:
            self._log(f"[Erreur overlay] Glisser natif impossible : {e}", "error")
            return {"ok": False}
        return {"ok": True}

    def overlay_report_error(self, message):
        """Appelé depuis overlay.html quand quelque chose échoue côté JS
        pendant le glisser (voir les try/catch dans overlay.html) — sans
        ça, un échec de window.pywebview.api.xxx() se contentait de
        couper le glisser silencieusement, sans aucune trace nulle part.
        Remonté dans le journal système PRINCIPAL (pas celui de
        l'overlay, qui n'a pas de journal visible) pour être réellement
        vu par l'utilisateur."""
        self._log(f"[Erreur overlay JS] {message}", "error")
        return {"ok": True}

    def _create_overlay_window(self, transparent=None, edit_mode=None):
        """Crée la fenêtre overlay.

        transparent / edit_mode : si fournis explicitement, forcent le
        mode de la nouvelle fenêtre (utilisé lors d'un changement
        verrouillé/déverrouillé, voir overlay_set_edit_mode). Si omis
        (première création), le mode "déplacer" par défaut dépend de la
        présence d'une position déjà enregistrée — voir plus bas — et
        transparent en découle automatiquement (voir la logique de
        correspondance : verrouillé -> transparent=True, déplacer ->
        transparent=False).

        Pourquoi cette correspondance : transparent=True bloque TOUS les
        clics sur la fenêtre (bug confirmé de compatibilité pywebview/
        WebView2 sur cette configuration) — un problème pour glisser
        l'overlay, mais exactement le comportement qu'on veut une fois
        VERROUILLÉ (clics traversants jusqu'au jeu). En mode "déplacer",
        on a besoin des clics, donc transparent=False, avec un fond
        opaque de repli (background_color) plutôt qu'un vrai rendu
        transparent."""
        if self._overlay_window is not None:
            return
        if webview is None:
            self._log(
                "[Erreur overlay] Les dépendances graphiques ne sont pas encore prêtes, réessaie dans quelques secondes.",
                "error",
            )
            return

        x, y = self._overlay_saved_pos

        if edit_mode is None:
            # Démarre en mode "déplacer" (déverrouillé) UNIQUEMENT s'il
            # n'y a encore aucune position enregistrée (toute première
            # activation) : sans ça, impossible de positionner l'overlay
            # sans d'abord deviner qu'il fallait aller cliquer sur
            # « Déplacer l'overlay » dans les Réglages. Une fois une
            # position déjà connue, on redémarre directement verrouillé.
            edit_mode = x is None or y is None
        self.overlay_edit_mode = edit_mode

        if transparent is None:
            transparent = not edit_mode

        create_kwargs = dict(
            js_api=self,
            width=OVERLAY_DEFAULT_WIDTH,
            height=OVERLAY_DEFAULT_HEIGHT,
            frameless=True,
            on_top=True,
            resizable=False,
            transparent=transparent,
        )
        if not transparent:
            # background_color n'est utile (et n'est accepté par
            # pywebview) qu'en mode non-transparent — cohérent avec le
            # thème sombre du reste de l'appli plutôt qu'un blanc brut.
            create_kwargs["background_color"] = "#0a0e14"
        if x is not None and y is not None:
            create_kwargs["x"] = x
            create_kwargs["y"] = y

        try:
            self._overlay_window = webview.create_window(OVERLAY_WINDOW_TITLE, OVERLAY_INDEX, **create_kwargs)
        except Exception as e:
            self._log(f"[Erreur overlay] Impossible de créer la fenêtre : {e}", "error")
            self._overlay_window = None
            return

        # Référence stable capturée par les closures ci-dessous : permet
        # de vérifier qu'un événement concerne bien TOUJOURS la fenêtre
        # actuelle avant de toucher à self._overlay_window/_overlay_hwnd —
        # indispensable pendant une recréation (changement verrouillé/
        # déplacer), où l'événement "closing" de l'ANCIENNE fenêtre peut
        # se déclencher APRÈS que la NOUVELLE ait déjà été créée et
        # assignée ; sans cette vérification, il écraserait la référence
        # à la nouvelle fenêtre avec None.
        this_window = self._overlay_window

        def _on_overlay_loaded():
            self.overlay_enabled = True
            # L'ordre compte : overlaySetEditMode DOIT être poussé AVANT
            # _push_overlay_full_state, qui pousse notamment la visibilité
            # des lignes (voir overlaySetRowVisibility côté overlay.html) —
            # cette dernière déclenche un recalcul/redimensionnement de la
            # fenêtre uniquement si elle sait déjà que l'overlay N'EST PAS
            # en mode "déplacer" (classe "edit-mode" du panneau). Dans
            # l'ordre inverse, la toute première activation (qui démarre
            # en mode déplacer) déclencherait un rétrécissement prématuré
            # et incorrect avant même que le mode déplacer ne soit connu
            # côté JS.
            self._overlay_push(f"overlaySetEditMode({'true' if self.overlay_edit_mode else 'false'})")
            self._push_overlay_full_state()
            # La fenêtre native n'existe côté Windows qu'une fois créée
            # par le backend pywebview, en léger différé par rapport à
            # create_window() ci-dessus — on la recherche sur un thread à
            # part pour ne jamais bloquer l'événement "loaded" lui-même.
            threading.Thread(target=self._finalize_overlay_window, daemon=True).start()

        def _on_overlay_moved(x_, y_):
            self._overlay_saved_pos = (x_, y_)
            save_overlay_config(True, x_, y_, visible_rows=self.overlay_visible_rows)

        def _on_overlay_closing():
            if self._overlay_window is not this_window:
                # Cet événement concerne une fenêtre déjà remplacée par
                # une plus récente (recréation en cours) : on l'ignore
                # complètement plutôt que d'écraser l'état actuel.
                return
            self._overlay_window = None
            self._overlay_hwnd = None
            if self._overlay_recreating:
                # Fermeture technique en vue d'une recréation immédiate
                # (changement de mode transparent) : ne PAS traiter comme
                # une vraie désactivation par l'utilisateur — la nouvelle
                # fenêtre va être créée juste après par
                # overlay_set_edit_mode, qui remettra tout en ordre.
                return
            self.overlay_enabled = False
            if self._app_closing:
                # L'appli entière est en train de se fermer (voir
                # _wire_main_window_events) : l'overlay se ferme juste en
                # cascade, ce n'est pas un choix de l'utilisateur de le
                # désactiver. On garde la préférence "activé" telle
                # quelle pour qu'il revienne automatiquement au prochain
                # lancement.
                return
            save_overlay_config(False, *self._overlay_saved_pos, visible_rows=self.overlay_visible_rows)

        try:
            self._overlay_window.events.loaded += _on_overlay_loaded
            self._overlay_window.events.moved += _on_overlay_moved
            self._overlay_window.events.closing += _on_overlay_closing
        except Exception:
            pass

        save_overlay_config(True, x, y, visible_rows=self.overlay_visible_rows)

    def _finalize_overlay_window(self):
        hwnd = _find_hwnd_by_title(OVERLAY_WINDOW_TITLE, timeout=5.0)
        self._overlay_hwnd = hwnd
        if not hwnd:
            self._log(
                "[Avertissement overlay] Fenêtre introuvable pour activer le mode "
                "clic-traversant — l'overlay reste utilisable, mais peut bloquer les "
                "clics à cet endroit tant que ce n'est pas résolu (réessaie de "
                "désactiver/réactiver l'overlay).",
                "error",
            )

    def _destroy_overlay_window(self):
        if self._overlay_window is not None:
            try:
                self._overlay_window.destroy()
            except Exception:
                pass
        self._overlay_window = None
        self.overlay_enabled = False
        self._overlay_hwnd = None
        save_overlay_config(False, *self._overlay_saved_pos, visible_rows=self.overlay_visible_rows)

    def _overlay_push(self, js):
        if self._overlay_window and self.overlay_enabled:
            try:
                self._overlay_window.evaluate_js(js)
            except Exception:
                pass

    def _push_overlay_full_state(self):
        """Renvoie d'un coup tout ce qui est déjà connu vers l'overlay —
        utilisé à sa (ré)ouverture, pour qu'il n'apparaisse jamais vide
        en attendant le prochain changement de chaque info."""
        # Visibilité des lignes (cases à cocher, voir overlay.html) :
        # poussée en tout premier, avant les valeurs elles-mêmes, pour que
        # les lignes masquées le restent dès l'affichage initial plutôt
        # que d'apparaître brièvement avant d'être cachées.
        self._overlay_push(f"overlaySetRowVisibility({json.dumps(self.overlay_visible_rows)})")
        st = self._overlay_last_state
        if "mic" in st:
            self._overlay_push(f"overlaySetMic({json.dumps(st['mic'])})")
        # self.listening (pas _overlay_last_state) : source de vérité
        # toujours à jour, y compris si l'overlay vient d'être recréé
        # (verrouillage/déverrouillage) pendant une session d'écoute déjà
        # en cours.
        self._overlay_push(f"overlaySetListening({'true' if self.listening else 'false'})")
        if "phrase" in st:
            self._overlay_push(f"overlaySetPhrase({json.dumps(st['phrase'])})")
        if "ai" in st:
            self._overlay_push(f"overlaySetAi({json.dumps(st['ai'])})")
        if "zone" in st:
            self._overlay_push(f"overlaySetZone({json.dumps(st['zone'])})")
        if "lastCommand" in st:
            self._overlay_push(f"overlaySetLastCommand({json.dumps(st['lastCommand'])})")
        # Calculé à la volée (pas depuis _overlay_last_state) : gère au
        # passage la remise à zéro quotidienne du compteur si l'overlay est
        # rouvert un autre jour sans qu'aucune requête Gemini n'ait encore
        # été faite depuis (voir _gemini_quota_state).
        used, limit = self._gemini_quota_state()
        self._overlay_set_gemini_quota(used, limit)

    def _overlay_set_mic(self, active):
        self._overlay_last_state["mic"] = bool(active)
        self._overlay_push(f"overlaySetMic({'true' if active else 'false'})")

    def _overlay_set_listening(self, active):
        """Reflète si l'écoute est ENGAGÉE (bouton « Engager l'écoute »),
        distinct de l'état "Micro" (voir _overlay_set_mic) qui lui reflète
        si le micro est actuellement OUVERT à l'intérieur d'une session
        d'écoute active — utile notamment en mode push-to-talk/touche
        bascule, où l'écoute peut être engagée mais le micro
        momentanément coupé."""
        self._overlay_last_state["listening"] = bool(active)
        self._overlay_push(f"overlaySetListening({'true' if active else 'false'})")

    def _overlay_set_phrase(self, text):
        self._overlay_last_state["phrase"] = text
        self._overlay_push(f"overlaySetPhrase({json.dumps(text)})")

    def _overlay_set_ai(self, text):
        self._overlay_last_state["ai"] = text
        self._overlay_push(f"overlaySetAi({json.dumps(text)})")

    def _overlay_set_zone(self, text):
        self._overlay_last_state["zone"] = text
        self._overlay_push(f"overlaySetZone({json.dumps(text)})")

    def _overlay_set_last_command(self, text):
        """Remplace la commande affichée sur l'overlay par la plus
        récente (pas une liste qui s'accumule — juste la dernière,
        écrasée à chaque nouvelle exécution)."""
        self._overlay_last_state["lastCommand"] = text
        self._overlay_push(f"overlaySetLastCommand({json.dumps(text)})")

    def _overlay_set_gemini_quota(self, used, limit):
        """Barre de progression du quota gratuit Gemini du jour (RPD) —
        vide (aucune requête), pleine (limite atteinte, voir
        _gemini_quota_state/_gemini_record_request)."""
        self._overlay_last_state["geminiQuota"] = {"used": used, "limit": limit}
        self._overlay_push(f"overlaySetGeminiQuota({json.dumps(used)}, {json.dumps(limit)})")

    def _overlay_flash_command(self):
        """Fait clignoter le fond de l'overlay pendant 2 secondes — appelé
        à chaque exécution d'une commande (voir _execute_command). Pas de
        state à mémoriser dans _overlay_last_state : contrairement aux
        autres setters, cet effet est transitoire et ne doit pas être
        rejoué à la (ré)ouverture de l'overlay via _push_overlay_full_state."""
        self._overlay_push("overlayFlashCommand()")

    # -------------------------------------------------------- Écoute --

    def _listen_loop(self, model_path):
        try:
            vosk.SetLogLevel(-1)
            # Réutilise le modèle déjà chargé tant que le dossier
            # sélectionné n'a pas changé (voir self._vosk_model dans
            # __init__) : recharger vosk.Model(model_path) à chaque
            # activation de l'écoute est coûteux (jusqu'à ~20s observés
            # dans le build compilé, contre ~0.2s en exécution Python
            # directe pour EXACTEMENT le même modèle) alors que rien ne
            # change entre deux activations successives.
            if self._vosk_model is not None and self._vosk_model_path == model_path:
                model = self._vosk_model
                self._log(f"Modèle vocal (Vosk {get_vosk_version()}) réutilisé (déjà chargé).", "info")
            else:
                # Chronométré et loggé (voir aussi get_vosk_version) : sert
                # à diagnostiquer un chargement anormalement lent (ex.
                # build PyInstaller très supérieur à l'exécution en Python
                # direct), en distinguant le temps de chargement du modèle
                # lui-même du reste de l'initialisation.
                _model_load_start = time.time()
                model = vosk.Model(model_path)
                self._log(
                    f"Modèle vocal (Vosk {get_vosk_version()}) chargé en "
                    f"{time.time() - _model_load_start:.1f}s.",
                    "info",
                )
                self._vosk_model = model
                self._vosk_model_path = model_path
            recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
            # Demande à Vosk ses N meilleures hypothèses au lieu d'une
            # seule : un mot mal transcrit dans la meilleure hypothèse
            # ("train d'atterrissache") n'empêche plus une commande de se
            # déclencher si une hypothèse voisine, presque aussi probable
            # pour le décodeur, correspond exactement (voir _handle_text).
            # Coût quasi nul (le décodeur explore déjà ces chemins en
            # interne), et n'affecte en rien le mot d'activation "Nova" ni
            # les questions libres posées à l'IA — seule la correspondance
            # de commande directe en tient compte.
            recognizer.SetMaxAlternatives(3)
        except Exception as e:
            self._log(f"[Erreur] Impossible de charger le modèle : {e}", "error")
            self.listening = False
            self._status("error", "Erreur", str(e))
            return

        audio_q = queue.Queue()

        def callback(indata, frames, time_info, status):
            data = bytes(indata)
            if self.aec_enabled:
                data = self._apply_echo_cancellation(data, frames)
            rms = compute_rms(data)

            # Mètre de niveau en direct dans le panneau, pour que
            # l'utilisateur puisse régler le seuil de coupure en se basant
            # sur ce qu'il voit réellement plutôt qu'à l'aveugle.
            now = time.time()
            if now - self._last_level_push_time > 0.12:
                self._last_level_push_time = now
                self._push(f"micLevelUpdate({rms})")

            if self.listen_mode in ("push_to_talk", "toggle_key") and not self._mic_gate_open:
                # Micro coupé (touche PTT relâchée, ou bascule sur "off") :
                # rien n'est transmis à la reconnaissance (silence complet).
                # Le moteur (modèle chargé, flux audio ouvert) continue de
                # tourner normalement — seul ce qui lui est transmis change.
                # self._mic_gate_open est mis à jour ailleurs, par le
                # thread dédié _hotkey_poll_loop — jamais depuis ce
                # callback audio lui-même.
                data = b"\x00" * len(data)
            elif self.mic_gate > 0 and rms < self.mic_gate:
                # Sous le seuil : silence complet, rien n'est transmis au
                # moteur de reconnaissance pour ce bloc.
                data = b"\x00" * len(data)
            elif self.mic_gain != 1.0:
                data = apply_mic_gain(data, self.mic_gain)

            audio_q.put(data)

        input_device = self._resolve_input_device()
        try:
            device_label = sd.query_devices(input_device, "input")["name"]
        except Exception:
            device_label = "périphérique par défaut"
        self._log(f"Modèle chargé. Écoute en cours... (micro : {device_label})", "success")
        self._status("listening", "Écoute en cours", "Micro actif")
        self._sync_aec_state()

        try:
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=8000,
                device=input_device,
                dtype="int16",
                channels=1,
                callback=callback
            ):
                while not self.stop_event.is_set():
                    try:
                        data = audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    if recognizer.AcceptWaveform(data):
                        result = json.loads(recognizer.Result())
                        # Avec SetMaxAlternatives activé, Result() renvoie
                        # {"alternatives": [{"text": ..., "confidence": ...}, ...]}
                        # (déjà triées par confiance décroissante par Vosk) au
                        # lieu d'un simple {"text": ...}.
                        alt_list = result.get("alternatives")
                        if alt_list:
                            alt_texts = [a.get("text", "").strip() for a in alt_list]
                            alt_texts = [t for t in alt_texts if t]
                        else:
                            alt_texts = []
                        text = alt_texts[0] if alt_texts else result.get("text", "").strip()

                        if self._is_speaking or time.time() < self._speech_mute_until:
                            # Anti-écho : on jette ce qui vient d'être reconnu
                            # pendant/juste après que l'appli a parlé, sans le
                            # traiter comme une commande ou une question —
                            # SAUF le mot d'arrêt ("<nom>, stop"), qui reste
                            # toujours pris en compte pour pouvoir couper une
                            # réponse trop longue en cours de lecture.
                            if self._is_speaking and text and self._is_stop_phrase(text.lower()):
                                self._interrupt_speech()
                                self._log(f"⏹ Lecture vocale interrompue (« {text} »).", "info")
                            continue

                        if text:
                            self._log(f"Reconnu : « {text} »", "info")
                            self._handle_text(text, alt_texts=alt_texts[1:])
        except Exception as e:
            self._log(f"[Erreur audio] {e}", "error")
        finally:
            self.listening = False
            self._sync_aec_state()
            self._overlay_set_mic(False)
            self._overlay_set_listening(False)
            if not self.stop_event.is_set():
                self._status("error", "Arrêté", "Flux audio interrompu")
            else:
                self._status("idle", "Arrêté", "Système en veille")

    def _handle_text(self, text, alt_texts=None):
        text_norm = text.lower().strip()
        # wake_word/gemini_wake_word vides quand l'assistant correspondant
        # est désactivé (voir ai_toggle_enabled/gemini_toggle_enabled) :
        # son mot d'activation n'est alors jamais détecté ci-dessous, comme
        # s'il n'existait pas — utile pour ne garder qu'un seul des deux
        # assistants actif si les deux en même temps font double emploi.
        wake_word = self.ai_name.lower().strip() if self.ai_enabled else ""
        gemini_wake_word = self.gemini_name.lower().strip() if self.gemini_enabled else ""

        # Étape 1 : si on attend la question suite au nom prononcé seul,
        # la phrase reconnue est envoyée telle quelle à l'IA (pas de
        # correspondance de commande sur cette phrase-là). Nova d'abord,
        # puis Gemini (voir gemini_name — les deux assistants sont
        # complètement indépendants, un seul peut être "en attente" à la
        # fois puisque chaque mot d'activation reprend son propre état).
        if self._ai_awaiting_question:
            self._ai_awaiting_question = False
            if time.time() - self._ai_awaiting_since > AI_QUESTION_TIMEOUT:
                self._log(f"({self.ai_name} : délai dépassé, annulé)", "info")
            elif text_norm:
                if self._try_execute_command_from_ai_text(text.strip()):
                    return
                self._log(f"Question pour {self.ai_name} : « {text.strip()} »", "info")
                self._overlay_set_phrase(text.strip())
                self._ai_ask(text.strip())
            return

        if self._gemini_awaiting_question:
            self._gemini_awaiting_question = False
            if time.time() - self._gemini_awaiting_since > AI_QUESTION_TIMEOUT:
                self._log(f"({self.gemini_name} : délai dépassé, annulé)", "info")
            elif text_norm:
                if self._try_execute_command_from_ai_text(text.strip()):
                    return
                self._log(f"Question pour {self.gemini_name} : « {text.strip()} »", "info")
                self._overlay_set_phrase(text.strip())
                self._gemini_ask(text.strip())
            return

        # Étape 2 : détection du nom de l'IA n'importe où dans la phrase
        # (pas seulement au début) — "c'est quoi le bouclier, Nova ?" ou
        # "dis-moi Nova comment ça marche" fonctionnent tous les deux.
        # Le nom est retiré de la phrase pour ne garder que la question ;
        # s'il ne reste rien, on passe en attente de la question suivante.
        # Nova est vérifiée en premier : si jamais les deux noms sont
        # identiques (renommage manuel malheureux), c'est elle qui gagne.
        if wake_word:
            pattern = r"(?<!\w)" + re.escape(wake_word) + r"(?!\w)"
            match = re.search(pattern, text_norm)
            if match:
                question = (text_norm[:match.start()] + " " + text_norm[match.end():])
                question = re.sub(r"\s+", " ", question).strip()
                if question:
                    if self._try_execute_command_from_ai_text(question):
                        return
                    self._log(f"Question pour {self.ai_name} : « {question} »", "info")
                    self._overlay_set_phrase(question)
                    self._ai_ask(question)
                else:
                    self._ai_awaiting_question = True
                    self._ai_awaiting_since = time.time()
                    self._log(f"{self.ai_name} à l'écoute, pose ta question...", "info")
                return

        if gemini_wake_word:
            pattern = r"(?<!\w)" + re.escape(gemini_wake_word) + r"(?!\w)"
            match = re.search(pattern, text_norm)
            if match:
                question = (text_norm[:match.start()] + " " + text_norm[match.end():])
                question = re.sub(r"\s+", " ", question).strip()
                if question:
                    if self._try_execute_command_from_ai_text(question):
                        return
                    self._log(f"Question pour {self.gemini_name} : « {question} »", "info")
                    self._overlay_set_phrase(question)
                    self._gemini_ask(question)
                else:
                    self._gemini_awaiting_question = True
                    self._gemini_awaiting_since = time.time()
                    self._log(f"{self.gemini_name} à l'écoute, pose ta question...", "info")
                return

        # Étape 3 : correspondance normale des commandes vocales. Si la
        # meilleure hypothèse de Vosk ne correspond à aucune commande, on
        # retente avec les hypothèses suivantes (voir SetMaxAlternatives
        # dans _listen_loop) avant d'abandonner — le décodeur a souvent
        # une variante presque aussi probable qui, elle, correspond
        # exactement à la phrase configurée.
        idx = self._find_strict_command_match(text_norm)
        if idx is None:
            for alt in (alt_texts or []):
                alt_norm = alt.lower().strip()
                if not alt_norm:
                    continue
                idx = self._find_strict_command_match(alt_norm)
                if idx is not None:
                    break
        if idx is not None:
            self._execute_command(idx)

    @staticmethod
    def _command_trigger_list(cmd):
        """Construit la liste des variantes (phrase + synonymes, avec et
        sans apostrophes) sur lesquelles comparer le texte reconnu."""
        phrase_norm = cmd["phrase"].lower().strip()
        declencheurs = [
            phrase_norm,
            phrase_norm.replace("'", " "),
            phrase_norm.replace("'", ""),
        ]
        for s in cmd.get("synonyms", []):
            s_clean = s.lower().strip()
            declencheurs.append(s_clean)
            declencheurs.append(s_clean.replace("'", " "))
        return [d for d in declencheurs if d]

    @staticmethod
    def _is_significant_match(declencheur, text_norm):
        """Un déclencheur composé d'un seul mot (ex. "système") ne doit
        pas suffire à lui seul à déclencher une commande s'il n'est
        qu'un mot perdu au milieu d'une phrase plus longue et sans
        rapport (ex. "je veux quitter le système pyro" ne doit pas
        déclencher "alimentation vaisseau" juste parce que "système" y
        apparaît). On exige alors que le déclencheur représente une part
        substantielle du texte reconnu. Une expression de plusieurs mots
        est déjà assez spécifique en soi et n'a pas besoin de ce garde-
        fou."""
        if len(declencheur.split()) >= 2:
            return True
        return len(declencheur) >= 0.5 * len(text_norm)

    def _find_strict_command_match(self, text_norm, require_significant=False):
        text_sans_apostrophe = text_norm.replace("'", " ")
        best_idx = None
        best_len = -1
        for idx, cmd in enumerate(self.commands):
            if cmd.get("type") == "title":
                continue
            declencheurs = self._command_trigger_list(cmd)
            if not declencheurs:
                continue
            for d in declencheurs:
                for candidate in (text_norm, text_sans_apostrophe):
                    if d in candidate and (not require_significant or self._is_significant_match(d, candidate)):
                        if len(d) > best_len:
                            best_len = len(d)
                            best_idx = idx
                        break
        return best_idx

    def _find_fuzzy_command_match(self, text_norm, threshold=AI_COMMAND_MATCH_THRESHOLD):
        """Cherche la commande dont la phrase/un synonyme ressemble le plus
        au texte reconnu (correspondance stricte d'abord, puis ressemblance
        approximative via difflib). Utilisé quand la phrase suit le nom de
        l'IA : l'utilisateur vient de s'adresser explicitement à
        l'assistant, donc on tolère une reconnaissance vocale imparfaite
        ("train d'atterisage" mal transcrit, mot manquant, etc.) — mais on
        exige en échange (via require_significant) qu'un déclencheur d'un
        seul mot représente une part significative de la phrase, pour ne
        pas confondre une vraie question ("je veux quitter le système
        pyro") avec une commande à cause d'un mot générique isolé."""
        strict_idx = self._find_strict_command_match(text_norm, require_significant=True)
        if strict_idx is not None:
            return strict_idx, 1.0

        text_sans_apostrophe = text_norm.replace("'", " ")
        best_idx, best_ratio = None, 0.0
        for idx, cmd in enumerate(self.commands):
            if cmd.get("type") == "title":
                continue
            for d in self._command_trigger_list(cmd):
                ratio = max(
                    difflib.SequenceMatcher(None, d, text_norm).ratio(),
                    difflib.SequenceMatcher(None, d, text_sans_apostrophe).ratio(),
                )
                if ratio > best_ratio:
                    best_idx, best_ratio = idx, ratio

        if best_idx is not None and best_ratio >= threshold:
            return best_idx, best_ratio
        return None, 0.0

    def _try_execute_command_from_ai_text(self, text):
        """Après le nom de l'IA, vérifie si la phrase ressemble à une
        commande connue et l'exécute le cas échéant. Renvoie True si une
        commande a été déclenchée (ou reconnue mais bloquée par le
        cooldown) — dans ce cas, la phrase ne doit pas partir en question
        vers l'IA."""
        text_norm = (text or "").lower().strip()
        if not text_norm:
            return False
        if AI_QUESTION_MARKER_RE.search(text_norm):
            # Tournure clairement interrogative (voir AI_QUESTION_MARKER_RE) :
            # même si la phrase de la commande y apparaît mot pour mot, c'est
            # une question sur cette commande, pas un ordre de l'exécuter.
            return False
        idx, ratio = self._find_fuzzy_command_match(text_norm)
        if idx is None:
            return False
        self._execute_command(idx, via_ai=True, ratio=ratio)
        return True

    def _execute_command(self, idx, via_ai=False, ratio=1.0):
        cmd = self.commands[idx]
        phrase_norm = cmd["phrase"].lower().strip()
        now = time.time()
        last = self.last_trigger.get(phrase_norm, 0)
        if now - last < self.trigger_cooldown:
            return

        self.last_trigger[phrase_norm] = now
        if via_ai:
            self._log(
                f"  → Commande reconnue via {self.ai_name} (ressemblance {ratio:.0%}) : "
                f"touche(s) '{cmd['keys']}'",
                "success",
            )
        else:
            self._log(f"  → Action déclenchée : touche(s) '{cmd['keys']}'", "success")
        self._overlay_set_phrase(cmd["phrase"])
        self._overlay_set_last_command(f"{cmd['phrase']} ({cmd['keys']})")
        self._overlay_flash_command()
        self._flash(idx)
        repeat_count = max(1, int(cmd.get("repeat_count", 1) or 1))
        repeat_delay = max(0.0, float(cmd.get("repeat_delay", 0.1) or 0.1))
        extra_steps = cmd.get("extra_steps") or []
        if cmd.get("hold", False) or repeat_count > 1 or extra_steps:
            threading.Thread(
                target=self._run_command_sequence,
                args=(cmd["keys"], extra_steps),
                kwargs={"hold": cmd.get("hold", False), "repeat_count": repeat_count, "repeat_delay": repeat_delay},
                daemon=True,
            ).start()
        else:
            self._press_keys(cmd["keys"], hold=False)
        if self.confirm_commands_voice:
            self._speak(cmd["phrase"])

    def _run_command_sequence(self, primary_keys, extra_steps, hold=False, repeat_count=1, repeat_delay=0.1):
        """Exécute l'action principale d'une commande (avec maintien/
        répétition éventuels, voir _press_keys_repeated), PUIS chacune de
        ses actions supplémentaires dans l'ordre (voir extra_steps et
        MAX_COMMAND_EXTRA_STEPS — jusqu'à 5 actions au total en comptant
        l'action principale). Chaque action supplémentaire est un simple
        appui bref (pas de maintien/répétition individuels), précédé du
        délai configuré pour laisser le jeu enregistrer distinctement
        chaque touche plutôt que de les enchaîner instantanément. Tourne
        toujours sur son propre thread (voir _execute_command) pour ne
        jamais geler la reconnaissance vocale pendant les délais."""
        self._press_keys_repeated(primary_keys, hold=hold, repeat_count=repeat_count, repeat_delay=repeat_delay)
        for step in extra_steps:
            delay = max(0.0, float(step.get("delay", DEFAULT_EXTRA_STEP_DELAY) or DEFAULT_EXTRA_STEP_DELAY))
            if delay > 0:
                time.sleep(delay)
            self._press_keys(step.get("keys", ""), hold=False)

    # Boutons souris pouvant être attribués à une commande comme n'importe
    # quelle touche clavier (ex. "ctrl+mouseleft") — voir KB_MOUSE_LAYOUT
    # côté script.js pour la sélection dans le clavier virtuel.
    MOUSE_BUTTONS = {"mouseleft": "left", "mouseright": "right", "mousemiddle": "middle"}
    HOLD_DURATION_SECONDS = 2.0

    def _press_keys(self, keys_str, hold=False):
        keys = [k.strip() for k in keys_str.split("+") if k.strip()]
        if not keys:
            return
        mouse_button = None
        keyboard_keys = []
        for k in keys:
            if k in self.MOUSE_BUTTONS:
                mouse_button = self.MOUSE_BUTTONS[k]
            else:
                keyboard_keys.append(k)

        # La touche ISO 102e ("< > \") et les chiffres du pavé numérique
        # (num0-num9, voir NUMPAD_DIGIT_SCAN_CODES) sont envoyés nous-mêmes
        # via SendInput (voir _send_raw_scan_code), indépendamment de
        # pydirectinput — ils ne doivent donc pas être bloqués si
        # pydirectinput est absent.
        needs_pydirectinput = mouse_button is not None or any(
            k != "iso102" and k not in NUMPAD_DIGIT_SCAN_CODES for k in keyboard_keys
        )
        if pydirectinput is None and needs_pydirectinput:
            return
        # GARDE-FOU CRITIQUE : les touches déjà enfoncées (keyDown) DOIVENT
        # toujours être relâchées, même si une exception survient en cours
        # de route (ex. mouseDown qui échoue après un keyDown réussi) —
        # sans ça, une touche comme "shift" (poste de combustion) peut
        # rester bloquée enfoncée indéfiniment dans le jeu, ce qui se
        # traduit par un comportement erratique de tous les inputs suivants
        # (facilement perçu comme un "freeze" du jeu). Le try/finally
        # garantit l'exécution du relâchement quoi qu'il arrive.
        pressed_keys = []
        mouse_pressed = False
        try:
            for k in keyboard_keys:
                if k == "iso102":
                    _send_raw_scan_code(ISO102_SCAN_CODE, key_up=False)
                elif k in NUMPAD_DIGIT_SCAN_CODES:
                    _send_raw_scan_code(NUMPAD_DIGIT_SCAN_CODES[k], key_up=False)
                else:
                    pydirectinput.keyDown(k)
                pressed_keys.append(k)
            if mouse_button:
                pydirectinput.mouseDown(button=mouse_button)
                mouse_pressed = True
            time.sleep(self.HOLD_DURATION_SECONDS if hold else 0.05)
        except Exception as e:
            self._log(f"[Erreur touche] '{keys_str}' : {e}", "error")
        finally:
            # Relâchement inconditionnel de tout ce qui a été enfoncé,
            # même partiellement — chaque relâchement est isolé dans son
            # propre try pour qu'un échec sur l'un n'empêche pas les
            # autres d'être tentés.
            if mouse_pressed:
                try:
                    pydirectinput.mouseUp(button=mouse_button)
                except Exception as e:
                    self._log(f"[Erreur touche] Relâchement souris '{keys_str}' : {e}", "error")
            for k in reversed(pressed_keys):
                try:
                    if k == "iso102":
                        _send_raw_scan_code(ISO102_SCAN_CODE, key_up=True)
                    elif k in NUMPAD_DIGIT_SCAN_CODES:
                        _send_raw_scan_code(NUMPAD_DIGIT_SCAN_CODES[k], key_up=True)
                    else:
                        pydirectinput.keyUp(k)
                except Exception as e:
                    self._log(f"[Erreur touche] Relâchement de '{k}' : {e}", "error")

    def _press_keys_repeated(self, keys_str, hold=False, repeat_count=1, repeat_delay=0.1):
        """Enfonce (et relâche) la combinaison de touches `repeat_count`
        fois de suite, avec `repeat_delay` secondes entre chaque
        répétition. Tourne toujours sur son propre thread (voir
        _execute_command) pour ne jamais geler la reconnaissance vocale
        pendant les délais entre répétitions."""
        for i in range(max(1, repeat_count)):
            self._press_keys(keys_str, hold=hold)
            if i < repeat_count - 1 and repeat_delay > 0:
                time.sleep(repeat_delay)


def _show_splash(stop_event, status_state, window_width=MAIN_WINDOW_WIDTH, window_height=MAIN_WINDOW_HEIGHT,
                  window_x=None, window_y=None):
    """Fenêtre Tkinter affichée instantanément, à la même taille ET au
    même endroit que la fenêtre principale (si une position a déjà été
    mémorisée — voir load_window_config), le temps que les dépendances
    soient vérifiées et que la fenêtre principale (WebView2) finisse de
    s'initialiser. Tkinter ne dépend d'aucune des deux, donc elle apparaît
    sans délai."""
    root = tk.Tk()
    # Masqué immédiatement : évite qu'elle apparaisse un court instant à
    # sa position par défaut (coin ou centre approximatif de l'écran)
    # avant qu'on ait pu lui appliquer la géométrie voulue ci-dessous —
    # c'est ce "flash" initial qui causait le saut de position visible au
    # démarrage. root.deiconify() la fait réapparaître une fois la bonne
    # géométrie déjà en place.
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#0a0e14")

    w, h = window_width, window_height
    if window_x is not None and window_y is not None:
        x, y = window_x, window_y
    else:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.deiconify()

    # Carte centrée au milieu de la fenêtre (plutôt que d'étirer le contenu
    # sur toute la surface, qui deviendrait illisible à cette taille).
    card_w, card_h = 460, 320
    border = tk.Frame(root, bg="#2dd4ff", bd=0)
    border.place(relx=0.5, rely=0.5, anchor="center", width=card_w, height=card_h)
    inner = tk.Frame(border, bg="#121822")
    inner.place(x=2, y=2, width=card_w - 4, height=card_h - 4)

    # Logo de l'appli (hexagone) et icône représentant l'IA (réseau de
    # nœuds), côte à côte en haut de la carte de démarrage.
    header_row = tk.Frame(inner, bg="#121822")
    header_row.pack(pady=(30, 6))

    tk.Label(header_row, text="⬡", fg="#2dd4ff", bg="#121822",
              font=("Segoe UI", 38)).pack(side="left", padx=(0, 18))

    ai_icon_size = 60
    ai_canvas = tk.Canvas(header_row, width=ai_icon_size, height=ai_icon_size,
                           bg="#121822", highlightthickness=0)
    ai_canvas.pack(side="left")
    cx = cy = ai_icon_size / 2
    r_outer, r_node, r_core = 24, 3.6, 8
    node_points = []
    for i in range(6):
        angle = math.radians(60 * i - 90)
        nx = cx + r_outer * math.cos(angle)
        ny = cy + r_outer * math.sin(angle)
        node_points.append((nx, ny))
        ai_canvas.create_line(cx, cy, nx, ny, fill="#2dd4ff", width=1.3)
    for nx, ny in node_points:
        ai_canvas.create_oval(nx - r_node, ny - r_node, nx + r_node, ny + r_node,
                               outline="#2dd4ff", fill="#121822", width=1.3)
    ai_canvas.create_oval(cx - r_core, cy - r_core, cx + r_core, cy + r_core,
                           outline="#2dd4ff", fill="#0d3a45", width=1.6)

    tk.Label(inner, text="NOVAVOX", fg="#dbe4ee", bg="#121822",
              font=("Segoe UI", 14, "bold")).pack()
    status_lbl = tk.Label(inner, text=status_state["text"],
                            fg="#7c8a9c", bg="#121822", font=("Segoe UI", 10),
                            wraplength=380, justify="center")
    status_lbl.pack(pady=(8, 20))

    bar_w = 340
    bar_bg = tk.Frame(inner, bg="#1a2230", width=bar_w, height=6)
    bar_bg.pack()
    bar_bg.pack_propagate(False)
    bar_fg = tk.Frame(bar_bg, bg="#2dd4ff", width=0, height=6)
    bar_fg.place(x=0, y=0, relheight=1)

    # Remplissage progressif qui ralentit en approchant 92% (on ne sait
    # jamais combien de temps il reste réellement), puis complète à 100%
    # dès que la fenêtre principale signale qu'elle est prête.
    progress = {"value": 0.0}
    target_width = bar_w * 0.92

    def animate_bar():
        if stop_event.is_set():
            return
        progress["value"] += (target_width - progress["value"]) * 0.04 + 0.6
        if progress["value"] > target_width:
            progress["value"] = target_width
        bar_fg.place(x=0, y=0, width=progress["value"], height=6)
        root.after(30, animate_bar)

    animate_bar()

    # Le texte de statut est modifié par le thread principal (vérification
    # des dépendances, chargement de la fenêtre) via status_state["text"].
    # On le relit périodiquement ici plutôt que de toucher le widget
    # directement depuis l'autre thread (Tkinter n'aime pas ça).
    last_text = {"value": None}

    def poll_status():
        if status_state["text"] != last_text["value"]:
            last_text["value"] = status_state["text"]
            status_lbl.configure(text=status_state["text"])

        if stop_event.is_set():
            bar_fg.place(x=0, y=0, width=bar_w, height=6)
            status_lbl.configure(text="Prêt.")
            root.after(200, root.destroy)
        else:
            root.after(100, poll_status)

    root.after(100, poll_status)
    root.mainloop()

    # Les widgets Tk (callbacks .after, closures animate_bar/poll_status,
    # relations parent/enfant...) forment des références circulaires entre
    # eux. Sans ce nettoyage explicite ICI — sur ce même thread, juste
    # après la fin de mainloop() — le ramasse-miettes CYCLIQUE de Python
    # pourrait ne les libérer que plus tard, potentiellement déclenché
    # depuis un tout autre thread (n'importe quelle allocation mémoire
    # ailleurs dans l'appli peut le déclencher). Or l'interpréteur Tcl
    # sous-jacent doit impérativement être détruit sur le thread qui l'a
    # créé : sinon, plantage fatal et immédiat de tout le programme
    # ("Tcl_AsyncDelete: async handler deleted by the wrong thread"),
    # impossible à intercepter côté Python.
    del animate_bar, poll_status
    del border, inner, bar_bg, bar_fg, status_lbl, root
    gc.collect()


def _ps_escape(value):
    """Échappe une valeur pour l'insérer sans risque dans une chaîne
    PowerShell entre apostrophes (doublement de l'apostrophe, convention
    PowerShell)."""
    return (value or "").replace("'", "''")


def _star_citizen_autolaunch_shortcut_path():
    """Chemin du raccourci de veille placé dans le dossier Démarrage de
    Windows (lancé automatiquement à chaque connexion)."""
    startup_dir = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )
    return os.path.join(startup_dir, "NOVAVOX (veille Star Citizen).lnk")


def is_star_citizen_autolaunch_enabled():
    """Vrai si le raccourci de veille est présent dans le dossier
    Démarrage de Windows (case cochée dans Réglages > NovaVox)."""
    if sys.platform != "win32":
        return False
    return os.path.isfile(_star_citizen_autolaunch_shortcut_path())


def set_star_citizen_autolaunch_enabled(enabled):
    """Crée ou supprime le raccourci de veille dans le dossier Démarrage
    de Windows. Ce raccourci relance l'exécutable avec l'argument
    --wait-for-sc (voir _wait_for_star_citizen_if_requested), qui reste
    en veille silencieuse jusqu'à détecter StarCitizen.exe puis démarre
    l'appli normalement. Reste silencieux en cas d'échec, comme
    ensure_desktop_shortcut : ce réglage ne doit jamais empêcher l'appli
    de fonctionner normalement."""
    if sys.platform != "win32":
        return False
    shortcut_path = _star_citizen_autolaunch_shortcut_path()
    try:
        if not enabled:
            if os.path.isfile(shortcut_path):
                os.remove(shortcut_path)
            return True

        if not getattr(sys, "frozen", False):
            # En développement (python app.py), pas d'exécutable autonome
            # à relancer automatiquement : réglage disponible seulement
            # depuis la version compilée (.exe).
            return False

        target = _ps_escape(sys.executable)
        workdir = _ps_escape(BASE_DIR)
        path_ps = _ps_escape(shortcut_path)
        script = (
            "$shell = New-Object -ComObject WScript.Shell; "
            f"$sc = $shell.CreateShortcut('{path_ps}'); "
            f"$sc.TargetPath = '{target}'; "
            "$sc.Arguments = '--wait-for-sc'; "
            f"$sc.WorkingDirectory = '{workdir}'; "
            "$sc.IconLocation = $sc.TargetPath + ',0'; "
            "$sc.Save()"
        )
        encoded_cmd = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags, timeout=10,
        )
        return os.path.isfile(shortcut_path)
    except Exception:
        return False


def _wait_for_star_citizen_if_requested():
    """Si l'appli a été lancée avec --wait-for-sc (raccourci de veille
    créé par set_star_citizen_autolaunch_enabled), reste en veille
    silencieuse, sans aucune fenêtre, jusqu'à ce que StarCitizen.exe soit
    détecté, puis laisse main() démarrer normalement. Vérifie toutes les
    5 secondes ; empreinte CPU/mémoire quasi nulle en attendant."""
    if "--wait-for-sc" not in sys.argv:
        return
    if sys.platform != "win32":
        return
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    while True:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq StarCitizen.exe"],
                capture_output=True, text=True, timeout=10,
                creationflags=creationflags,
            )
            if "StarCitizen.exe" in result.stdout:
                return
        except Exception:
            pass
        time.sleep(5)


def ensure_desktop_shortcut():
    """Crée automatiquement un raccourci sur le Bureau au premier
    lancement de l'exécutable compilé, pour que l'utilisateur ait une
    icône pratique même s'il n'est pas passé par l'installeur Inno Setup.
    Ne fait rien si un raccourci existe déjà (ex. supprimé volontairement
    par l'utilisateur par la suite), et reste totalement silencieux en cas
    d'échec : un raccourci manquant ne doit jamais empêcher l'appli de
    démarrer normalement."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return  # Inutile en développement (python app.py) ou hors Windows

    try:
        target = _ps_escape(sys.executable)
        workdir = _ps_escape(BASE_DIR)
        script = (
            "$desktop = [Environment]::GetFolderPath('Desktop'); "
            # Nettoie l'ancien raccourci (nom du projet avant son
            # renommage en NOVAVOX), pour ne pas laisser deux raccourcis
            # différents traîner sur le Bureau après une mise à jour.
            "$oldPath = Join-Path $desktop 'Commandes Vocales.lnk'; "
            "if (Test-Path $oldPath) { Remove-Item -Path $oldPath -Force -ErrorAction SilentlyContinue }; "
            "$path = Join-Path $desktop 'NOVAVOX.lnk'; "
            "if (-not (Test-Path $path)) { "
            "$shell = New-Object -ComObject WScript.Shell; "
            "$sc = $shell.CreateShortcut($path); "
            f"$sc.TargetPath = '{target}'; "
            f"$sc.WorkingDirectory = '{workdir}'; "
            "$sc.IconLocation = $sc.TargetPath + ',0'; "
            "$sc.Save() "
            "}"
        )
        encoded_cmd = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags, timeout=10,
        )
    except Exception:
        pass


def _build_tray_icon_image():
    """Charge icon.ico (généré par make_icon.py, voir build_exe.bat) pour
    l'icône de la barre des tâches. Si le fichier est absent (build sans
    icône personnalisée, ou lancement depuis les sources sans l'avoir
    généré), dessine un petit repli à la volée avec le même style que
    make_icon.py (hexagone cyan sur fond transparent) plutôt que de se
    priver de tray faute d'un fichier optionnel."""
    ico_path = os.path.join(BASE_DIR, "icon.ico")
    if os.path.isfile(ico_path):
        try:
            return PIL_Image.open(ico_path)
        except Exception:
            pass

    size = 64
    img = PIL_Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = PIL_ImageDraw.Draw(img)
    cx = cy = size / 2
    radius = size * 0.42
    points = []
    for i in range(6):
        angle = math.radians(-90 + i * 60)
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    draw.polygon(points, outline=(45, 212, 255, 255), width=4)
    dot_r = size * 0.08
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=(45, 212, 255, 255))
    return img


def _start_system_tray(window, api, on_quit):
    """Démarre l'icône NOVAVOX dans la barre des tâches Windows, avec un
    menu Afficher/Profils/Quitter. Tourne dans son propre thread (boucle
    de messages séparée de celle de pywebview, ce qui est le mode de
    fonctionnement documenté de pystray) et vit tant que l'application
    n'est pas explicitement quittée depuis ce menu — c'est ce qui permet
    à la fenêtre principale de se "fermer" (bouton X) sans arrêter
    l'appli : voir _on_closing dans _wire_main_window_events, qui annule
    la fermeture par défaut et masque la fenêtre à la place quand le tray
    est actif.
    Retourne l'objet pystray.Icon (pour pouvoir l'arrêter proprement via
    icon.stop() au moment du "Quitter" réel), ou None si pystray/Pillow
    ne sont pas disponibles (repli sur l'ancien comportement : fermer la
    fenêtre quitte directement l'application)."""
    if pystray is None or PIL_Image is None:
        return None

    def _show(icon=None, item=None):
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def _quit(icon=None, item=None):
        icon.stop()
        on_quit()

    def _switch_profile(icon, item, profile_id):
        # Change de profil directement depuis le tray, sans avoir à
        # rouvrir la fenêtre — pratique en jeu (voir aussi le raccourci
        # clavier de cyclage, set_profile_cycle_hotkey, pour la même
        # idée au clavier). Répercute le changement sur l'interface si
        # elle est déjà ouverte (voir profileSwitchedExternally côté JS),
        # exactement comme le fait _cycle_profile pour le raccourci
        # clavier — sinon un changement fait depuis le tray pendant que
        # la fenêtre est affichée resterait invisible jusqu'au prochain
        # rechargement.
        result = api.profiles_switch(profile_id)
        if result.get("ok"):
            try:
                api._push(
                    f"profileSwitchedExternally({json.dumps(result.get('name'))}, "
                    f"{json.dumps(profile_id)}, {json.dumps(api.commands)})"
                )
            except Exception:
                pass

    def _make_switch_action(profile_id):
        # Fabrique fermée sur profile_id, plutôt qu'une lambda avec un
        # paramètre par défaut (lambda icon, item, pid=profile_id: ...) :
        # pystray inspecte __code__.co_argcount du callback pour décider
        # comment l'appeler, et co_argcount COMPTE les paramètres à
        # valeur par défaut — une lambda à 3 paramètres (dont un avec
        # défaut) est donc vue comme prenant 3 arguments obligatoires,
        # ce que pystray refuse (ValueError, crash au démarrage du tray).
        # Une fonction à exactement 2 paramètres, sans défaut, capturant
        # profile_id par fermeture, évite le problème.
        def _action(icon, item):
            _switch_profile(icon, item, profile_id)
        return _action

    def _make_checked(profile_id):
        def _checked(item):
            return profile_id == _active_profile_id
        return _checked

    def _profile_menu_items():
        # Menu régénéré à chaque ouverture (fonction passée à
        # pystray.Menu, pas un tuple figé) : reflète toujours la liste et
        # le profil actif à jour, y compris s'ils ont changé depuis
        # l'interface entre-temps.
        try:
            profiles = list_profiles()
        except Exception:
            profiles = []
        if not profiles:
            return (pystray.MenuItem("Aucun profil", None, enabled=False),)
        name_counts = {}
        for p in profiles:
            key = p["name"].lower()
            name_counts[key] = name_counts.get(key, 0) + 1
        return tuple(
            pystray.MenuItem(
                # Nombre de commandes affiché uniquement pour les noms en
                # double (voir list_profiles/count) : aide à distinguer
                # des doublons sans alourdir l'affichage sinon — même
                # logique que renderProfiles côté JS.
                f"{p['name']} ({p['count']} cmd)" if name_counts[p["name"].lower()] > 1 else p["name"],
                _make_switch_action(p["id"]),
                checked=_make_checked(p["id"]),
                radio=True,
            )
            for p in profiles
        )


    menu = pystray.Menu(
        pystray.MenuItem("Afficher NOVAVOX", _show, default=True),
        pystray.MenuItem("Profil de commandes", pystray.Menu(_profile_menu_items)),
        pystray.MenuItem("Quitter", _quit),
    )
    icon = pystray.Icon("novavox", _build_tray_icon_image(), "NOVAVOX", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def _wire_main_window_events(window, window_config, on_loaded_extra=None, api=None, tray_state=None):
    """Branche les événements communs aux deux chemins de démarrage
    (rapide et via Tkinter) : affichage/positionnement final de la
    fenêtre, et sauvegarde de la taille/position à chaque changement.
    on_loaded_extra, si fourni, est appelé avant l'affichage (ex. pour
    rejouer l'historique du journal côté chemin Tkinter). api, si fourni,
    permet de fermer automatiquement la fenêtre overlay (voir
    Api._create_overlay_window) quand la fenêtre principale se ferme,
    plutôt que de la laisser flotter seule à l'écran une fois NOVAVOX
    quitté. tray_state, si fourni (dict avec les clés "active"/
    "quitting"), permet de transformer le bouton de fermeture de la
    fenêtre en simple "masquer dans la barre des tâches" tant que l'icône
    NOVAVOX y est présente (voir _start_system_tray) — la fermeture
    réelle (overlay compris) ne se produit alors qu'en choisissant
    « Quitter » depuis le menu de l'icône."""

    # Devient False une fois la taille/position initiale vérifiée. Tant
    # qu'il vaut True, _on_resized/_on_moved n'enregistrent rien : sans ce
    # garde-fou, un éventuel appel correctif à resize()/move() ci-dessous
    # déclencherait lui-même les événements "resized"/"moved", qui
    # réécriraient aussitôt window_config.json avec une valeur transitoire
    # potentiellement incorrecte.
    restoring = {"value": True}
    # Empêche la correction de s'appliquer deux fois (une fois via
    # l'événement "maximized", une autre via le filet de sécurité
    # temporisé ci-dessous) si les deux se déclenchent l'un après l'autre.
    correction_done = {"value": False}

    def _run_correction():
        if correction_done["value"]:
            return
        correction_done["value"] = True

        # La fenêtre s'est rouverte MAXIMISÉE (état mémorisé par Windows/
        # le backend indépendamment de notre propre config) : on la
        # ramène d'abord à une taille "normale" avant de réappliquer
        # taille/position — resize()/move() seuls n'ont généralement
        # aucun effet tant que la fenêtre reste maximisée.
        try:
            window.restore()
        except Exception:
            pass

        def _apply_size_position():
            try:
                window.resize(window_config["width"], window_config["height"])
            except Exception:
                pass
            if window_config["x"] is not None and window_config["y"] is not None:
                # window.move() peut, sur un écran mis à l'échelle sous
                # Windows (125%, 150%...), multiplier les coordonnées
                # reçues par ce facteur au lieu de les appliquer telles
                # quelles — contrairement à create_window(x=, y=), qui lui
                # les applique correctement à la création. Vérifié
                # empiriquement : ce facteur ne dépend NI de l'écran de
                # destination NI de la position courante de la fenêtre —
                # il applique systématiquement l'échelle de l'écran
                # PRINCIPAL (voir _primary_monitor_scale), quelle que soit
                # la cible.
                scale = _primary_monitor_scale()
                try:
                    window.move(round(window_config["x"] / scale), round(window_config["y"] / scale))
                except Exception:
                    pass
            threading.Timer(0.2, _finish_correction).start()

        def _finish_correction():
            restoring["value"] = False

        # Court délai pour laisser l'animation de restauration Windows
        # (maximisé → normal) se terminer avant de redimensionner/
        # repositionner, sinon cette même animation peut repositionner la
        # fenêtre PAR-DESSUS notre propre appel juste après.
        threading.Timer(0.15, _apply_size_position).start()

    def _on_maximized():
        # Réagit dès que Windows maximise la fenêtre — le plus tôt
        # possible, pour réduire au minimum le temps pendant lequel
        # l'utilisateur voit la fenêtre agrandie avant la correction
        # (bien plus rapide que le filet de sécurité temporisé
        # ci-dessous, qui attend un délai fixe avant même de vérifier).
        _run_correction()

    def _on_loaded():
        if on_loaded_extra:
            on_loaded_extra()
        window.show()

        def _verify_after_show():
            # Filet de sécurité : si l'événement "maximized" ci-dessus ne
            # s'est pas déclenché (backend qui ne le supporte pas, ou un
            # désaccord dû à autre chose qu'une maximisation) mais qu'un
            # écart est quand même détecté après coup, on corrige ici.
            if correction_done["value"]:
                return
            try:
                actual_w, actual_h = window.width, window.height
                actual_x, actual_y = window.x, window.y
            except Exception:
                actual_w = actual_h = actual_x = actual_y = None

            mismatch = (
                actual_w != window_config["width"] or actual_h != window_config["height"]
                or (
                    window_config["x"] is not None
                    and (actual_x != window_config["x"] or actual_y != window_config["y"])
                )
            )

            if mismatch:
                _run_correction()
            else:
                correction_done["value"] = True
                restoring["value"] = False

        # Petit délai avant de vérifier : sur certains backends,
        # window.width/height/x/y ne reflètent pas encore la valeur
        # définitive juste après show().
        threading.Timer(0.3, _verify_after_show).start()

    def _on_closing():
        # Mémorise la taille et la position actuelles de la fenêtre pour
        # les retrouver au prochain lancement (voir load_window_config).
        # Reste silencieux en cas d'échec : ce n'est qu'un confort, jamais
        # une raison de bloquer la fermeture.
        try:
            save_window_config(window.width, window.height, window.x, window.y)
        except Exception:
            pass

        # Icône NOVAVOX présente dans la barre des tâches et fermeture PAS
        # encore demandée depuis son menu « Quitter » : le clic sur la
        # croix masque juste la fenêtre au lieu de vraiment fermer
        # l'application (comportement standard des overlays/utilitaires
        # qui tournent en fond pendant qu'on joue). Retourner False
        # annule la fermeture native — pris en charge par pywebview
        # depuis la correction de l'issue #744.
        if tray_state is not None and tray_state.get("active") and not tray_state.get("quitting"):
            try:
                window.hide()
            except Exception:
                pass
            return False

        # --- À partir d'ici, fermeture réelle de l'application ---

        # Ferme l'overlay en cascade avec la fenêtre principale : sans
        # ça, quitter NOVAVOX laissait l'overlay ouvert seul à l'écran
        # (fenêtre pywebview indépendante), obligeant l'utilisateur à le
        # fermer manuellement. _app_closing=True fait que la fermeture de
        # l'overlay ci-dessous (voir _on_overlay_closing) n'efface pas la
        # préférence "activé" : il reviendra tout seul au prochain
        # lancement.
        if api is not None:
            api._app_closing = True
            if api._overlay_window is not None:
                try:
                    api._overlay_window.destroy()
                except Exception:
                    pass

    def _on_resized(width, height):
        if restoring["value"]:
            return
        # Sauvegarde redondante à chaque redimensionnement plutôt que de
        # dépendre uniquement de l'événement "closing" ci-dessus : ce
        # dernier n'est pas garanti de se déclencher de façon fiable sur
        # tous les environnements/versions de pywebview. En enregistrant
        # dès le redimensionnement, la dernière taille/position connue
        # est déjà sur le disque même si la fermeture ne coopère pas.
        try:
            save_window_config(width, height, window.x, window.y)
        except Exception:
            pass

    def _on_moved(x, y):
        if restoring["value"]:
            return
        # Même logique que _on_resized ci-dessus, mais pour un simple
        # déplacement de la fenêtre sans changement de taille.
        try:
            save_window_config(window.width, window.height, x, y)
        except Exception:
            pass

    window.events.loaded += _on_loaded
    window.events.closing += _on_closing
    window.events.resized += _on_resized
    window.events.moved += _on_moved
    try:
        window.events.maximized += _on_maximized
    except Exception:
        # Événement absent sur cette version/ce backend de pywebview : le
        # filet de sécurité temporisé dans _on_loaded (_verify_after_show)
        # prend le relais, juste un peu plus lentement.
        pass


def _main_fast_path(window_config):
    """Chemin de démarrage normal (l'immense majorité des lancements) :
    pywebview est déjà confirmé disponible (voir _webview_available), donc
    on peut ouvrir directement — et uniquement — la fenêtre principale,
    sans jamais passer par l'ancien écran de démarrage Tkinter séparé.
    L'écran de chargement est désormais intégré à la page HTML elle-même
    (voir #splashOverlay dans index.html) : comme il n'y a jamais qu'UNE
    seule fenêtre du tout début à la toute fin, aucun décalage de taille
    ou de position n'est possible entre "l'écran de chargement" et
    "l'application".

    La vérification (et installation si besoin) des AUTRES dépendances
    (vosk, sounddevice, pydirectinput...) se fait ensuite, une fois la
    fenêtre déjà affichée, sur un thread dédié — avec sa progression
    poussée en temps réel à la fois dans le journal système et sur
    l'écran de chargement (voir ensure_dependencies/on_status
    ci-dessous), exactement comme avant, mais sans plus jamais avoir
    besoin d'une seconde fenêtre séparée pour l'afficher."""
    global webview
    # pywebview lui-même doit être importé ICI, avant de pouvoir créer la
    # fenêtre ci-dessous — le reste des dépendances (vosk, sounddevice,
    # pydirectinput...) est en revanche importé plus tard, une fois la
    # fenêtre affichée (voir _run_startup_checks). _webview_available() a
    # déjà confirmé juste avant que pywebview est bien installé dans la
    # bonne version, donc cet import est sûr.
    if webview is None:
        import webview as _webview
        webview = _webview

    api = Api()  # ne dépend pas de vosk/sounddevice/pydirectinput à la construction

    create_window_kwargs = dict(
        js_api=api,
        width=window_config["width"],
        height=window_config["height"],
        min_size=MAIN_WINDOW_MIN_SIZE,
        background_color="#0a0e14",
        hidden=True,
        text_select=True,
    )
    if window_config["x"] is not None and window_config["y"] is not None:
        create_window_kwargs["x"] = window_config["x"]
        create_window_kwargs["y"] = window_config["y"]
    window = webview.create_window("Star Citizen — NOVAVOX", GUI_INDEX, **create_window_kwargs)
    api.set_window(window)

    # État partagé avec _wire_main_window_events (voir _on_closing) : tant
    # que "active" est True et "quitting" reste False, fermer la fenêtre
    # la masque dans la barre des tâches au lieu de quitter l'appli.
    # L'icône elle-même n'est démarrée qu'un peu plus bas, une fois
    # pystray/Pillow importés par _import_runtime_dependencies (pas
    # encore disponibles à ce stade du chemin rapide).
    tray_state = {"active": False, "quitting": False}

    def _run_startup_checks():
        status_state = {"text": "Démarrage...", "history": []}

        def on_status(text, kind):
            api._log(text, kind)
            try:
                window.evaluate_js(f"splashSetStatus({json.dumps(text)})")
            except Exception:
                pass

        try:
            ok = ensure_dependencies(status_state, on_status=on_status)
            if ok:
                try:
                    _import_runtime_dependencies()
                    # pygame/keyboard valaient None au moment de la
                    # construction de l'Api (dépendances pas encore
                    # confirmées) : la surveillance d'un éventuel bouton/
                    # touche d'activation vocale configuré n'avait alors
                    # pas pu démarrer (voir _sync_hotkey_poll dans
                    # Api.__init__). On la relance maintenant que les
                    # modules sont réellement disponibles.
                    api._sync_hotkey_poll()
                    api._register_profile_cycle_hotkey()

                    def _do_real_quit():
                        tray_state["quitting"] = True
                        try:
                            window.destroy()
                        except Exception:
                            pass

                    tray_icon = _start_system_tray(window, api, on_quit=_do_real_quit)
                    tray_state["active"] = tray_icon is not None
                except Exception as e:
                    ok = False
                    on_status(f"[Erreur] Import des dépendances : {e}", "error")

            if not ok:
                on_status(
                    "Certaines dépendances n'ont pas pu être installées. Réessaie "
                    "de relancer l'application, ou installe-les manuellement "
                    "avec : pip install -r requirements.txt",
                    "error",
                )
        except Exception as e:
            on_status(f"[Erreur] Vérification des dépendances : {e}", "error")
        finally:
            # Quoi qu'il arrive (succès, échec, erreur inattendue), on ne
            # laisse jamais l'utilisateur bloqué derrière l'écran de
            # chargement indéfiniment.
            try:
                window.evaluate_js("hideSplashOverlay()")
            except Exception:
                pass

    def _on_loaded_extra():
        threading.Thread(target=_run_startup_checks, daemon=True).start()
        if api._overlay_saved_enabled:
            # Léger différé : laisse la fenêtre principale finir de
            # s'installer avant d'en ouvrir une seconde par-dessus.
            # Compromis assumé : ça rallonge un peu le temps de
            # démarrage (création d'une seconde fenêtre WebView2), mais
            # c'est ce que l'utilisateur souhaite — retrouver l'overlay
            # au même endroit sans avoir à recocher la case à chaque
            # session.
            threading.Timer(1.0, lambda: api.overlay_set_enabled(True)).start()

    _wire_main_window_events(window, window_config, on_loaded_extra=_on_loaded_extra, api=api, tray_state=tray_state)
    webview.start()


def _main_legacy_path(window_config):
    """Chemin de démarrage de secours, utilisé uniquement quand une
    dépendance manque encore (typiquement le tout premier lancement, ou
    après suppression d'un paquet) : on ne peut pas encore se fier à
    pywebview pour afficher quoi que ce soit tant qu'on n'a pas confirmé
    qu'il est bien installé, d'où l'écran de démarrage Tkinter (module
    standard, toujours disponible) le temps qu'ensure_dependencies()
    installe ce qui manque."""
    status_state = {"text": "Démarrage...", "history": []}
    splash_stop = threading.Event()
    splash_thread = threading.Thread(
        target=_show_splash,
        args=(splash_stop, status_state, window_config["width"], window_config["height"],
              window_config["x"], window_config["y"]),
        daemon=True,
    )
    splash_thread.start()

    ok = ensure_dependencies(status_state)
    if not ok:
        splash_stop.set()
        splash_thread.join(timeout=2)
        print("[Erreur] Certaines dépendances n'ont pas pu être installées. "
              "Essaie manuellement : pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    status_state["text"] = "Chargement de l'application..."
    status_state["history"].append(("Chargement de l'application...", "info"))
    _import_runtime_dependencies()

    api = Api()

    create_window_kwargs = dict(
        js_api=api,
        width=window_config["width"],
        height=window_config["height"],
        min_size=MAIN_WINDOW_MIN_SIZE,
        background_color="#0a0e14",
        hidden=True,
        text_select=True,
    )
    if window_config["x"] is not None and window_config["y"] is not None:
        create_window_kwargs["x"] = window_config["x"]
        create_window_kwargs["y"] = window_config["y"]
    window = webview.create_window("Star Citizen — NOVAVOX", GUI_INDEX, **create_window_kwargs)
    api.set_window(window)

    tray_state = {"active": False, "quitting": False}

    def _do_real_quit():
        tray_state["quitting"] = True
        try:
            window.destroy()
        except Exception:
            pass

    tray_icon = _start_system_tray(window, api, on_quit=_do_real_quit)
    tray_state["active"] = tray_icon is not None

    def _replay_startup_log():
        # Rejoue dans le journal système tout l'historique du démarrage
        # (vérification/installation des dépendances), pour qu'il reste
        # consultable même après la disparition du splash Tkinter.
        for msg, kind in status_state["history"]:
            api._log(msg, kind)
        splash_stop.set()

        # L'écran de chargement HTML (voir #splashOverlay dans
        # index.html) n'est plus masqué automatiquement côté JS — c'est
        # désormais Python qui en a la responsabilité dans les deux
        # chemins de démarrage (voir aussi _run_startup_checks dans
        # _main_fast_path), pour éviter toute course entre les deux. Ici,
        # tout est déjà prêt (le splash Tkinter vient justement de gérer
        # l'attente), donc on le masque dès que la fenêtre est chargée.
        try:
            window.evaluate_js("hideSplashOverlay()")
        except Exception:
            pass

    _wire_main_window_events(window, window_config, on_loaded_extra=_replay_startup_log, api=api, tray_state=tray_state)
    webview.start()


def main():
    _wait_for_star_citizen_if_requested()

    # En arrière-plan pour ne pas retarder le démarrage.
    threading.Thread(target=ensure_desktop_shortcut, daemon=True).start()

    # Taille ET position de fenêtre mémorisées à la dernière fermeture/
    # déplacement (voir save_window_config) ; repli sur la taille par
    # défaut centrée si rien n'a encore été enregistré.
    window_config = load_window_config()

    if _webview_available():
        _main_fast_path(window_config)
    else:
        _main_legacy_path(window_config)


def _install_crash_handler():
    """Filet de sécurité : en mode --windowed (PyInstaller), il n'y a pas
    de console pour voir une erreur qui plante l'appli — elle disparaît
    juste silencieusement. On intercepte donc toute exception non gérée
    pour : 1) l'écrire dans crash_log.txt à côté de l'exécutable, et
    2) afficher une fenêtre d'alerte Windows explicite, plutôt que de
    laisser l'utilisateur face à un programme qui se ferme sans un mot."""
    import traceback

    def _excepthook(exc_type, exc_value, exc_tb):
        log_path = os.path.join(BASE_DIR, "crash_log.txt")
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                f.write(details)
        except Exception:
            pass

        if sys.platform == "win32":
            try:
                import ctypes
                message = (
                    "L'application a rencontré une erreur et va se fermer.\n\n"
                    f"Détails enregistrés dans :\n{log_path}\n\n"
                    f"{exc_type.__name__} : {exc_value}"
                )
                ctypes.windll.user32.MessageBoxW(0, message, "NOVAVOX — Erreur", 0x10)
            except Exception:
                pass

    sys.excepthook = _excepthook
    # sys.excepthook ne couvre que le thread principal ; les erreurs dans
    # les threads secondaires (splash, écoute micro, TTS...) passent par
    # threading.excepthook (Python 3.8+).
    threading.excepthook = lambda args: _excepthook(args.exc_type, args.exc_value, args.exc_traceback)


if __name__ == "__main__":
    _install_crash_handler()
    main()
    # Filet de sécurité : sur certaines configurations, le runtime .NET
    # embarqué (pythonnet/WebView2, utilisé pour l'affichage) garde des
    # threads internes vivants même une fois toutes les fenêtres fermées,
    # ce qui empêchait le processus de se terminer tout seul — NOVAVOX.exe
    # restait visible dans le Gestionnaire des tâches, obligeant à le tuer
    # manuellement avant de pouvoir installer une mise à jour (le fichier
    # restait verrouillé). Toutes les étapes de fermeture propre
    # (sauvegarde de la fenêtre, fermeture de l'overlay, arrêt de l'icône
    # barre des tâches) ont déjà eu lieu de façon synchrone avant que
    # webview.start() ne rende la main dans main() ci-dessus (voir
    # _on_closing) : on peut donc forcer la sortie du processus ici sans
    # rien perdre.
    os._exit(0)