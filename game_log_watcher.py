# -*- coding: utf-8 -*-
"""
game_log_watcher.py — Surveillance en temps réel du fichier Game.log de
Star Citizen, pour alimenter NovaVox en événements de jeu.

CONSTAT VÉRIFIÉ (15/08/2026, build 4.9 LIVE) — À LIRE :
Après analyse d'un Game.log réel couvrant une mission de combat complète
(acceptation → plusieurs PNJ détruits, VFX d'explosion visible → fin de
mission), AUCUNE ligne "Actor Death", "Vehicle Destruction" ou "Kill"
n'apparaît nulle part dans le fichier. Les outils communautaires qui
s'appuyaient sur ces lignes (StarLogs, AutoTrackR2, SC-Kill-Monitor...)
semblent avoir été conçus pour un format de log antérieur ; SC-Kill-Monitor
a d'ailleurs été archivé (lecture seule) par son auteur le 24/11/2025.

CONCLUSION : la détection de kills/morts/destructions de vaisseau via le
Game.log N'EST PLUS POSSIBLE dans cette version du jeu (ou en tout cas pas
avec un mot-clé identifiable sans accès à des lignes couvrant un vrai
PvP). Cette fonctionnalité est donc désactivée dans ce module — voir
plus bas pour ce qui reste réellement fonctionnel.

CE QUI FONCTIONNE (vérifié sur un vrai Game.log) :
- Changement de zone / arrivée après saut quantique : ligne
  "<Quantum Drive Arrived - Arrived at Final Destination>", fiable et
  observée à plusieurs reprises.
- Le pseudo RSI du joueur apparaît en clair dans de nombreuses lignes
  (`nickname="TonPseudo"`), utile pour pré-remplir le champ handle RSI.

CE QUI NE FONCTIONNE PAS (confirmé, pas juste "pas encore vérifié") :
- Kills, morts, destructions de vaisseau (PNJ ou joueur) : rien n'est
  écrit dans le Game.log pour ces événements.
- Les lignes "Channel Disconnected"/"Channel Destroyed" apparaissent
  aussi lors d'une simple transition menu → jeu (pas seulement une vraie
  déconnexion en cours de partie) : les utiliser comme alerte de
  déconnexion donnerait de faux positifs systématiques au chargement.
  Cette détection reste donc désactivée par prudence.

Ce module ne modifie ni n'envoie aucune donnée au jeu : il ne fait que
LIRE un fichier texte local en écriture par le jeu. Aucun risque
d'interaction avec l'anti-triche.
"""

import os
import re
import time
import threading
import glob


# --------------------------------------------------------------------------
# Localisation du Game.log
# --------------------------------------------------------------------------

# Emplacements standards possibles, à tester dans l'ordre. On regarde sur
# tous les lecteurs disponibles (C:, D:, E:...), pas seulement C:, car
# beaucoup de joueurs installent le jeu sur un second disque.
_INSTALL_SUBPATHS = [
    r"Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"Roberts Space Industries\StarCitizen\PTU\Game.log",
    r"Roberts Space Industries\StarCitizen\EPTU\Game.log",
    r"Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log",
]


def find_game_log_path(extra_paths=None):
    """Essaie de localiser le Game.log automatiquement en balayant les
    lettres de lecteur disponibles (comme le fait game_detector côté
    StarLogs). Retourne le premier chemin existant, ou None si rien
    n'est trouvé (l'utilisateur devra alors indiquer le chemin
    manuellement dans les réglages, comme pour le modèle Vosk)."""
    candidates = list(extra_paths or [])
    for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{drive_letter}:\\"
        if not os.path.isdir(drive):
            continue
        for sub in _INSTALL_SUBPATHS:
            candidates.append(os.path.join(drive, sub))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# --------------------------------------------------------------------------
# Patterns de reconnaissance des événements
# --------------------------------------------------------------------------

RE_TIMESTAMP = re.compile(r"^<([\d\-T:.Z]+)>")

# Format vérifié contre un vrai Game.log (build 4.9 LIVE, 15/08/2026) :
# "<2026-08-15T16:15:54.736Z> [Notice] <Quantum Drive Arrived - Arrived at
#  Final Destination> [ItemNavigation][CL][...] ... has arrived at final
#  destination [Team_CGP4][QuantumTravel]"
# Cette ligne ne contient PAS le nom de la destination elle-même (juste la
# confirmation d'arrivée) — pour connaître la zone/système, il faudrait la
# croiser avec les lignes "Calculate Route" / "Projected Start Location"
# précédentes, qui elles contiennent des noms de lieux (ex. "Magda").
RE_QUANTUM_ARRIVED = re.compile(r"<Quantum Drive Arrived")

# Capture la destination projetée d'un trajet en cours de calcul, AINSI
# QUE la zone de départ (voir LANDING_ZONE_TO_PLANET plus bas — sert de
# repli pour deviner la station visée par un identifiant AMBIGU comme
# "RestStop" quand aucune ligne d'obstruction n'apparaît, cas des trajets
# locaux trop courts pour en croiser une), ex. :
# "...CalculateRoute|Projected Start Location is Magda for route to
#  destination ab_mine_stanton1_med_008 [Team_CGP4][QuantumTravel]"
RE_ROUTE_PROJECTED = re.compile(
    r"CalculateRoute\|Projected Start Location is (?P<start_location>.+?) for route to "
    r"destination (?P<destination>[A-Za-z0-9_\-]+)"
)

# Confirmation FINALE du calcul de route (une fois tout obstacle
# contourné) — c'est le bon moment pour annoncer "route tracée", après
# avoir eu la chance de capturer un éventuel nom lisible via une ligne
# d'obstruction (voir RE_ROUTE_OBSTRUCTION ci-dessous), qui n'apparaît
# qu'ENTRE la ligne "Projected Start Location" et celle-ci. Ex. :
# "...CalculateRoute|Successfully calculated route to
#  ObjectContainer_RestStop fuel estimate 365208.531250"
RE_ROUTE_CALCULATED = re.compile(
    r"Successfully calculated route to (?P<destination>[A-Za-z0-9_\-]+) fuel estimate"
)

# PÉPITE vérifiée dans un vrai Game.log : quand le trajet croise un
# obstacle (typiquement une autre planète/lune sur le chemin), le moteur
# écrit le nom LISIBLE de la vraie destination en clair, y compris déjà
# traduit en français le cas échéant. Ex. :
# "...ProcessNextNodeForRouteRecursive|Found obsruction while routing
#  from Magda to ArcCorp Obstructing Entity OOC_Stanton_1c_Magda"
# "...found obsruction while routing from Magda to Base minière
#  #ODD-E9B Routing around Obstructing Entity OOC_Stanton_1c_Magda"
# ("obsruction" = coquille du moteur du jeu lui-même, conservée telle
# quelle dans la regex ci-dessous). Ce nom est bien plus fiable qu'un
# identifiant brut nettoyé à l'aveugle : à utiliser en PRIORITÉ quand il
# est disponible (voir _resolve_destination_label plus bas), en
# particulier pour les identifiants génériques comme "RestStop" qui ne
# disent rien du lieu réel une fois isolés.
RE_ROUTE_OBSTRUCTION = re.compile(
    r"routing from .+? to (?P<label>.+?) (?:Obstructing Entity|Routing around)"
)

# Ligne qui confirme que le joueur a VALIDÉ une cible (pas seulement
# calculé une route parmi d'autres) — vérifiée dans un vrai Game.log :
# "...OnPlayerSelectedQuantumTarget|Player has selected point
#  ab_mine_stanton1_med_008 as their destination, routing locally"
# Sert à repérer le DÉBUT d'une nouvelle sélection de cible (donc à
# réinitialiser un éventuel label d'obstruction laissé par le trajet
# précédent) plutôt qu'à émettre l'événement lui-même — voir
# RE_ROUTE_CALCULATED, qui elle marque la fin du calcul.
RE_TARGET_SELECTED = re.compile(
    r"OnPlayerSelectedQuantumTarget\|Player has selected point "
    r"(?P<destination>[A-Za-z0-9_\-]+) as their destination"
)

# NON VÉRIFIÉ — candidat pour le moment où le saut quantique démarre
# réellement (poussée de la manette pour engager, après le compte à
# rebours de charge). Aucune ligne de ce type n'est apparue dans les
# extraits de Game.log fournis jusqu'ici (qui couvrent le calcul de
# route et l'arrivée, mais pas l'instant précis de l'engagement). Ce
# pattern reste donc désactivé par défaut (voir DEPARTURE_DETECTION_
# ENABLED plus bas) tant qu'il n'a pas été confronté à un vrai log
# capturé pendant l'action d'enclencher le saut.
RE_QUANTUM_JUMP_ENGAGED = re.compile(
    r"(RequestQuantumTravel|OnQuantumDriveEngaged|QuantumTravel.*Engag)"
)
DEPARTURE_DETECTION_ENABLED = False

# Pseudo RSI du joueur, présent en clair dans de nombreuses lignes de log
# (ex. nickname="Ammoniak"). Utile pour pré-remplir automatiquement le
# champ "Handle RSI" des réglages plutôt que de le demander à l'aveugle.
RE_PLAYER_NICKNAME = re.compile(r'nickname="(?P<nickname>[^"]+)"')

# Notifications HUD (celles affichées à l'écran en jeu) — vérifiées dans
# un vrai Game.log : couvrent zones d'armistice, juridiction, objectifs
# de mission, contrats terminés, récompenses reçues, etc. Toutes passent
# par cette même ligne source, avec le texte déjà tel qu'affiché à
# l'écran (français inclus) :
#   <SHUDEvent_OnNotification> Added notification "Nouvel objectif : ..."
#   [20] to queue. New queue size: 2, MissionId: [...]
# Capture tout ce qui suit le guillemet ouvrant JUSQU'À la fin de la
# ligne physique du fichier : certaines notifications (texte long) sont
# coupées par un retour à la ligne DANS le fichier lui-même, avant la
# fermeture du guillemet — voir la gestion multi-lignes dans
# _process_line, qui referme la capture sur les lignes suivantes le cas
# échéant. Les lignes de suivi <UpdateNotificationItem> (Action: Next/
# StartFade/Remove) et le "dump" de la file en cours (lignes indentées
# sans "Added notification") ne matchent pas ce pattern : la même
# notification n'est donc annoncée qu'une seule fois, à son apparition.
RE_HUD_NOTIFICATION_START = re.compile(r'<SHUDEvent_OnNotification> Added notification "(?P<text>.*)$')
RE_HUD_NOTIFICATION_CLOSE = re.compile(r'^(?P<text>.*?)"\s*\[\d+\]')

# Nombre maximum de lignes de continuation acceptées pour une même
# notification multi-lignes, avant d'abandonner (filet de sécurité pour
# ne jamais accumuler indéfiniment si le motif de fermeture attendu
# n'apparaît jamais, ex. format de log qui aurait changé).
_HUD_NOTIFICATION_MAX_CONTINUATION_LINES = 6


def _fix_mojibake(text):
    """Corrige un bug d'encodage vérifié dans le Game.log : les caractères
    accentués français sont doublement mal interprétés (UTF-8 relu comme
    Latin-1), ce qui donne par exemple "terminÃ©" au lieu de "terminé".
    Le correctif consiste à ré-encoder la chaîne (déjà lue en UTF-8) en
    Latin-1 puis à la redécoder en UTF-8 — l'opération inverse de ce bug.
    Reste silencieux (renvoie le texte tel quel) si l'opération échoue,
    ex. sur un texte qui n'était pas concerné par le bug."""
    if not text:
        return text
    try:
        return text.encode("latin1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text

# NOTE : les regex ci-dessous concernant kills/morts/destructions ont été
# retirées après vérification qu'elles ne matchent plus rien dans un vrai
# Game.log de build 4.9 LIVE couvrant une mission de combat complète
# (voir le constat en tête de fichier). Elles ne sont pas remplacées par
# de nouvelles regex "au hasard" : sans accès à un log réel contenant un
# vrai kill (PvP ou PvE) avec un format différent, il n'y a rien de fiable
# à faire matcher. Si un futur patch réintroduit ce genre de traçage, ces
# regex pourront être réajoutées ici sur la base de nouveaux logs réels.


def _extract_ship_name(raw_id):
    """Convertit un identifiant de vaisseau brut du log
    (ex. 'ANVL_Hornet_F7C_Mk2_1234567890123') en un nom plus lisible
    (ex. 'ANVL Hornet F7C Mk2'), en retirant le suffixe numérique
    d'instance. Conservé pour un usage futur (ex. si les événements de
    destruction redeviennent traçables)."""
    if not raw_id:
        return raw_id
    cleaned = re.sub(r"_\d{10,}$", "", raw_id)
    return cleaned.replace("_", " ").strip()


# "ObjectContainer" est un préfixe purement technique (désigne juste "ceci
# est un conteneur d'objets du moteur", pas un nom de lieu) qui apparaît
# devant beaucoup d'identifiants de destination (ex. "ObjectContainer_
# Lorville_City", "ObjectContainer_RestStop_..."). Il ne doit JAMAIS être
# prononcé — retiré en tout premier, avant toute autre transformation,
# insensible à la casse et quelle que soit sa position dans la chaîne.
RE_OBJECT_CONTAINER_PREFIX = re.compile(r"objectcontainer[_\s]*", re.IGNORECASE)

# Alias connus pour des identifiants internes qui ne ressemblent à rien
# une fois simplement "underscore -> espace" (codes de station, points de
# Lagrange...). Table volontairement courte et centrée sur Stanton — à
# étoffer au fil des identifiants réellement rencontrés dans les logs.
# Comparaison insensible à la casse sur l'identifiant NORMALISÉ (espaces/
# tirets/underscores uniformisés), donc "rs_ext_cru-l1", "RS EXT CRU-L1"
# et "RS_EXT_CRU_L1" matchent tous la même entrée.
KNOWN_LOCATION_ALIASES = {
    # Vraie orthographe observée en jeu : "cru-leo1" (pas "cru-l1" comme
    # deviné initialement) — corrigé d'après un vrai message de log.
    "rs ext cru leo1": "Seraphim Station",

    # Stations "Rest & Refinery" (points de Lagrange de Stanton, un par
    # planète L1-L5) — noms vérifiés en jeu, ajoutés par un utilisateur au
    # fil de ses trajets réels (voir set_game_log_destination_alias).
    # S1 = Hurston, S2 = Crusader, S3 = ArcCorp, S4 = microTech.
    "loc rr s1 l1": "Green Glade Station",
    "loc rr s1 l2": "Faithful Dream Station",
    "loc rr s1 l3": "Thundering Express Station",
    "loc rr s1 l4": "Melodic Fields Station",
    "loc rr s1 l5": "High Course Station",
    "loc rr s2 l5": "Beautiful Glen Station",
    "loc rr s3 l1": "Wide Forest Station",
    "loc rr s3 l3": "Modern Express Station",
    "loc rr s3 l4": "Faint Glen Station",
    "loc rr s4 l1": "Shallow Frontier Station",
    "loc rr s4 l2": "Long Forest Station",
    "loc rr s4 l4": "Crossroads Station",
    "loc rr s4 l5": "Modern Icarus Station",

    # Points de saut (voir aussi RE_JUMP_POINT_ID/SYSTEM_NAMES) dont
    # l'identifiant brut réel porte un préfixe "Loc_" non couvert par
    # cette regex (elle attend "rs_..." en tout début de chaîne) — ajoutés
    # ici en repli direct plutôt que d'élargir la regex à l'aveugle.
    "loc rs ext stan terra jp1": "Terra Gateway",
    "loc rs ext stan magnus jp1": "Nyx Gateway",
    "loc rs ext stan pyro jp1": "Pyro Gateway",

    # Système Stanton, autres identifiants de zone/station vérifiés :
    "ooc stanton": "l'étoile Stanton",
    "ooc stanton2 l3": "CRU L3",
    "ab collector gas stanton1": "Wikelo's Emporium - Dasi Station",
    "ab collector gas stanton4": "Wikelo's Emporium - Kinga Station",
    "ab mine stanton3 med 005": "Base minière DYV-JKE",
    "rs ext arc l001": "Lively Pathway Station",

    # Système Stanton — planètes et lunes, en repli direct pour un nom
    # court et naturel à l'oral (préféré par un utilisateur à la forme
    # "<Corps> (système Stanton)" produite automatiquement par
    # RE_OOC_LOCATION, redondante ici puisque le système ne change jamais
    # entre deux corps de Stanton dans une même annonce) :
    "ooc stanton 1 hurston": "Hurston",
    "ooc stanton 1a ariel": "Ariel",
    "ooc stanton 1b aberdeen": "Aberdeen",
    "ooc stanton 1c magda": "Magda",
    "ooc stanton 1d ita": "Ita",
    "ooc stanton 2 crusader": "Crusader",
    "ooc stanton 2a cellin": "Cellin",
    "ooc stanton 2b daymar": "Daymar",
    "ooc stanton 2c yela": "Yela",
    "ooc stanton 3 arccorp": "ArcCorp",
    "ooc stanton 3a lyria": "Lyria",
    "ooc stanton 3b wala": "Wala",
    "ooc stanton 4 microtech": "microTech",
    "ooc stanton 4a calliope": "Calliope",
    "ooc stanton 4b clio": "Clio",
    "ooc stanton 4c euterpe": "Euterpe",

    # Système Stanton — villes/zones d'atterrissage et antennes de
    # communication par planète (identifiant sans underscore entre le nom
    # du système et l'index, contrairement au format des corps célestes) :
    "lorville city": "Lorville City",
    "area18 city": "Area18 City",
    "orison loc": "Orison City",
    "ooc stanton1 commarray": "Réseau de communications Hurston",
    "ooc stanton2 commarray": "Antenne de communication Crusader",
    "ooc stanton3 commarray": "Réseau de communications ArcCorp",
    "ooc stanton4 commarray": "Réseau de communications microTech",

    # Système Pyro — étoile et planètes (noms officiels vérifiés en jeu) :
    "pyrostar": "l'étoile Pyro",
    "pyro1": "Pyro I",
    "pyro2": "Monox",
    "pyro3": "Bloom",
    "pyro5": "Pyro V",
    "pyro5b": "Vatra",
    "pyro5c": "Adir",
    "pyro5e": "Fuego",
    "pyro5f": "Vuur",
    "pyro6": "Terminus",

    # Système Pyro — stations et points de Lagrange :
    "rs ext pyro2 l4": "Checkmate",
    "rs ext pyro3 l1": "Station-service Starlight",
    "rs ext pyro3 l3": "Patch City",
    "rs ext pyro5 l4": "Rod's Fuel 'N Supplies",
    "rs ext pyro5 l5": "Rat's Nest",
    "p5 l3": "Pyro 5 L3",
    "rs ext pyro6 l3": "Endgame",
    "rs ext pyro6 l4": "Nyx Gateway",
    "rs ext pyro6 l5": "Megumi Ravitaillement",

    # Système Pyro — champ d'astéroïdes Keeger (stations/points de service
    # de la People's Alliance) :
    "social 001 keeger segment rckcrk 095": "Qv Breaker Station",
    "social 001 keeger segment rckcrk 101": "Qv Breaker Station",
    "social 001 keeger segment rckcrk 102": "Qv Breaker Station",
    "social 001 keeger segment rckcrk 105": "Qv Breaker Station",
    "social 001 keeger segment rckcrk 112": "Qv Breaker Station",
    "rs asmbl keeger 01": "Station-service Alpha de l'Alliance du Peuple",
    "rs asmbl keeger 02": "Station-service Delta de l'Alliance du Peuple",
    "rs asmbl keeger 03": "Station-service Theta de l'Alliance du Peuple",
    "rs asmbl keeger 04": "Station-service Lambda de l'Alliance du Peuple",

    # Système Pyro — anneau Glaciem :
    "glaciemring transitpoint alpha": "point de transit Glaciem Alpha",
    "glaciemring transitpoint bravo": "point de transit Glaciem Bravo",
    "glaciemring transitpoint charlie": "point de transit Glaciem Charlie",

    # Système Nyx :
    "nyxstar": "l'étoile Nyx",
    "levski all 001": "Levski",
}


def _normalize_for_alias_lookup(raw_id):
    """Uniformise un identifiant brut pour la recherche dans
    KNOWN_LOCATION_ALIASES : minuscules, tirets/underscores réduits à un
    simple espace, espaces multiples compressés."""
    s = re.sub(r"[_\-]+", " ", raw_id.lower())
    return re.sub(r"\s+", " ", s).strip()


# Noms complets des systèmes stellaires, utilisés pour transformer les
# identifiants de points de saut (voir RE_JUMP_POINT_ID) en nom de la
# station "Gateway" côté système d'arrivée. Codes vérifiés dans un vrai
# Game.log (ex. "rs_ext_pyro-stan_jp1", "rs_comm_nyx_pyro_jp1",
# "rs_comm_stan-magnus_jp1", "rs_comm_nyx_castra_jp1"...).
SYSTEM_NAMES = {
    "arc": "ArcCorp",
    "cru": "Crusader",
    "hur": "Hurston",
    "mic": "microTech",
    "pyro": "Pyro",
    "stan": "Stanton",
    "nyx": "Nyx",
    "magnus": "Magnus",
    "terra": "Terra",
    "castra": "Castra",
}

# Identifiant de point de saut, ex. "rs_ext_pyro-stan_jp1",
# "rs_entry_pyro-nyx_jp", "rs_comm_nyx_pyro_jp1", "rs_clinic_pyro-stan_jp1".
# Le séparateur entre les deux codes système est tantôt un tiret, tantôt un
# underscore selon les identifiants réellement observés — les deux sont
# donc acceptés. Le nom du DEUXIÈME système (celui vers lequel mène le
# point de saut) est celui annoncé, ex. "pyro-stan" -> "Stanton Gateway",
# "nyx_pyro" -> "Pyro Gateway" : c'est le nom officiel de la station
# donnant accès au système en question, cohérent avec le principe déjà
# utilisé pour les stations orbitales (voir STATION_BY_PLANET plus bas).
RE_JUMP_POINT_ID = re.compile(
    r"^rs[_-][a-z]+[_-](?P<sys1>[a-z]+)[_-](?P<sys2>[a-z]+)[_-]jp\d*$"
)


# Format "OOC_<Système>_<index planète><lettre lune optionnelle>_<Corps>",
# vérifié dans un vrai Game.log (section PHYSICS INSTANCE STATS) :
#   OOC_Stanton_1_Hurston      -> système Stanton, planète 1 (Hurston)
#   OOC_Stanton_1a_Ariel       -> système Stanton, lune "a" de la planète 1 (Ariel)
#   OOC_Stanton_1b_Aberdeen
#   OOC_Stanton_1c_Magda
#   OOC_Stanton_1d_Ita
#   OOC_Stanton_2_Crusader
#   OOC_Stanton_2a_Cellin
#   OOC_Stanton_4_Microtech
# Le premier segment après "OOC_" est donc toujours le SYSTÈME, et le
# dernier segment est toujours le nom du CORPS céleste (planète ou lune) —
# le segment du milieu (index + lettre) n'apporte rien à l'oral et est
# ignoré.
RE_OOC_LOCATION = re.compile(
    r"^OOC_(?P<system>[A-Za-z]+)_\d+[a-z]?_(?P<body>[A-Za-z0-9]+)$"
)


def _humanize_destination(raw_id, user_aliases=None):
    """Rend un identifiant de destination un peu plus prononçable à voix
    haute.

    Étape 0 (systématique) — retire tout préfixe/occurrence de
    "ObjectContainer", jamais prononcé (voir RE_OBJECT_CONTAINER_PREFIX).

    Priorité 1 — alias PERSONNALISÉ par l'utilisateur (voir user_aliases,
    réglages "Alias de destinations" dans l'appli, table éditable
    équivalente à KNOWN_LOCATION_ALIASES mais remplie par l'utilisateur
    lui-même plutôt que codée en dur ici) : passe toujours devant l'alias
    codé en dur, pour permettre de corriger/personnaliser n'importe quel
    identifiant sans modifier ce fichier.

    Priorité 2 — alias connu (voir KNOWN_LOCATION_ALIASES), ex.
    'rs_ext_cru-leo1' -> 'Seraphim Station'.

    Priorité 3 — point de saut (voir RE_JUMP_POINT_ID), ex.
    'rs_ext_pyro-stan_jp1' -> 'Stanton Gateway'.

    Priorité 4 — format "OOC_<Système>_<index>_<Corps>" (planètes/lunes,
    voir RE_OOC_LOCATION) : donne "<Corps> (système <Système>)", ex.
    'OOC_Stanton_1d_Ita' -> 'Ita (système Stanton)'.

    Priorité 5 — repli générique pour les autres formats rencontrés
    (points de minage, balises de mission...) : 'MISSION_QT_Quantum_
    Beacon_732699457697' -> 'Quantum Beacon' ; 'ab_mine_stanton1_med_008'
    -> 'ab mine stanton1 med 008'. Le nom complet est conservé (aucune
    troncature autre que le suffixe numérique d'instance) : si un nom
    ressort encore incomplet, c'est que le suffixe numérique retiré
    faisait en réalité partie du nom — dans ce cas, il faut ajuster
    RE_OBJECT_CONTAINER_PREFIX/le seuil de troncature ci-dessous sur la
    base de l'identifiant brut réel plutôt que deviner."""
    if not raw_id:
        return raw_id

    without_oc = RE_OBJECT_CONTAINER_PREFIX.sub("", raw_id).strip("_ ")
    normalized = _normalize_for_alias_lookup(without_oc)

    if user_aliases:
        custom = user_aliases.get(normalized)
        if custom:
            return custom

    alias = KNOWN_LOCATION_ALIASES.get(normalized)
    if alias:
        return alias

    m = RE_JUMP_POINT_ID.match(without_oc.lower())
    if m:
        system_name = SYSTEM_NAMES.get(m.group("sys2"))
        if system_name:
            return f"{system_name} Gateway"
        # La structure "rs_..._jp..." est bien celle d'un point de saut,
        # mais le code système (ex. un nouveau système ajouté par CIG,
        # absent de SYSTEM_NAMES) n'est pas reconnu : mieux vaut annoncer
        # une valeur explicitement neutre que de prononcer l'identifiant
        # technique brut ou de risquer d'annoncer un système erroné.
        return "Endroit inconnu"

    m = RE_OOC_LOCATION.match(without_oc)
    if m:
        return f"{m.group('body')} (système {m.group('system')})"

    cleaned = re.sub(r"_\d{6,}$", "", without_oc)  # retire le suffixe d'instance
    cleaned = re.sub(r"^MISSION_QT_", "", cleaned)
    return cleaned.replace("_", " ").strip()


def destination_alias_key(raw_id, user_aliases=None):
    """Renvoie la clé normalisée d'un identifiant de destination brut,
    SAUF s'il a déjà un alias personnalisé NON VIDE dans user_aliases
    (pour ne jamais écraser une personnalisation existante par une
    nouvelle détection de la même destination). Renvoie None si raw_id
    est vide ou déjà aliasé.

    Contrairement à une version précédente, ceci n'exclut PLUS les
    identifiants déjà bien résolus automatiquement (KNOWN_LOCATION_ALIASES,
    point de saut reconnu via RE_JUMP_POINT_ID/SYSTEM_NAMES, format
    OOC_... via RE_OOC_LOCATION) : l'utilisateur doit pouvoir choisir/
    personnaliser le nom de CHAQUE destination rencontrée, pas seulement
    celles qui ne sont pas déjà reconnues — voir
    _maybe_register_destination_alias côté app.py, qui préremplit la
    valeur de départ avec le nom actuellement annoncé (donc aucun
    changement de comportement tant que l'entrée n'est pas éditée).

    Exception : un identifiant AMBIGU PARTAGÉ par plusieurs lieux réels
    différents (voir AMBIGUOUS_SHARED_DESTINATION_IDS) n'est JAMAIS
    proposé — un seul alias ne pourrait de toute façon jamais représenter
    correctement plusieurs lieux différents à la fois (voir
    _resolve_destination_label, qui le résout via un signal contextuel)."""
    if not raw_id:
        return None
    without_oc = RE_OBJECT_CONTAINER_PREFIX.sub("", raw_id).strip("_ ")
    if not without_oc:
        return None
    normalized = _normalize_for_alias_lookup(without_oc)
    if normalized in AMBIGUOUS_SHARED_DESTINATION_IDS:
        return None
    if user_aliases and user_aliases.get(normalized):
        return None
    return normalized


def destination_is_unresolved(raw_id, user_aliases=None):
    """Renvoie True si raw_id ne correspond à AUCUN mécanisme de
    reconnaissance (ni alias utilisateur, ni KNOWN_LOCATION_ALIASES, ni
    point de saut vers un système connu — voir RE_JUMP_POINT_ID/
    SYSTEM_NAMES, ni format OOC_... — voir RE_OOC_LOCATION) : càd que
    _humanize_destination(raw_id) retomberait sur le repli générique OU
    sur "Endroit inconnu" (point de saut vers un système pas encore
    répertorié).

    Sert UNIQUEMENT à décider si le journal système doit signaler la
    découverte (voir _maybe_register_destination_alias côté app.py) —
    càd un identifiant VRAIMENT nouveau/non reconnu, qui pourrait
    indiquer que Star Citizen a changé/ajouté un identifiant depuis la
    dernière fois. Les destinations déjà bien résolues automatiquement
    (planètes, points de saut connus...) sont quand même ajoutées aux
    réglages (voir destination_alias_key), mais silencieusement — pas
    besoin d'alerter l'utilisateur pour celles-là."""
    if not raw_id:
        return False
    without_oc = RE_OBJECT_CONTAINER_PREFIX.sub("", raw_id).strip("_ ")
    if not without_oc:
        return False
    normalized = _normalize_for_alias_lookup(without_oc)
    if normalized in AMBIGUOUS_SHARED_DESTINATION_IDS:
        # Cas connu et déjà géré spécifiquement (voir
        # AMBIGUOUS_SHARED_DESTINATION_IDS) — pas un signe que Star
        # Citizen a changé quelque chose, inutile d'alerter.
        return False
    if user_aliases and user_aliases.get(normalized):
        return False
    if normalized in KNOWN_LOCATION_ALIASES:
        return False
    m = RE_JUMP_POINT_ID.match(without_oc.lower())
    if m:
        return m.group("sys2") not in SYSTEM_NAMES
    if RE_OOC_LOCATION.match(without_oc):
        return False
    return True


# Station orbitale principale connue par planète (Stanton), reprise du
# lore déjà présent dans le contexte personnalisé de l'IA. Sert à
# résoudre un identifiant générique (ex. "RestStop") vers un vrai nom
# quand le label lisible capturé via RE_ROUTE_OBSTRUCTION correspond au
# nom d'une planète plutôt qu'à un lieu déjà nommé.
STATION_BY_PLANET = {
    "hurston": "Everus Harbor",
    "crusader": "Seraphim Station",
    "arccorp": "Baijini Point",
    "microtech": "Port Tressler",
}


def _obstruction_label_is_generic_guess(obstruction_label):
    """Renvoie True si obstruction_label se limite au nom BRUT d'une
    planète connue (voir STATION_BY_PLANET) — un simple repli générique
    ("ArcCorp" -> "Baijini Point" par défaut), potentiellement inexact
    pour la destination précise réellement visée (ex. une base minière
    proche d'ArcCorp, pas Baijini Point elle-même). Renvoie False quand
    obstruction_label est déjà un nom de lieu SPÉCIFIQUE révélé
    directement par le moteur du jeu (ex. "Baijini Point", "Everus
    Harbor", "Port Tressler", "Base minière #ODD-E9B") : cette
    information est alors fiable telle quelle, PLUS fiable qu'un alias
    utilisateur rattaché à raw_destination — vérifié en vrai Game.log :
    'ObjectContainer_RestStop' est l'identifiant brut PARTAGÉ par Baijini
    Point, Everus Harbor et Port Tressler (aucune info de planète dedans),
    seul obstruction_label les distingue au moment de chaque trajet."""
    if not obstruction_label:
        return False
    planet_key = re.sub(r"\s+", "", obstruction_label.strip()).lower()
    return planet_key in STATION_BY_PLANET


# Identifiants bruts connus pour être PARTAGÉS par plusieurs lieux réels
# DIFFÉRENTS — vérifié en vrai Game.log : 'ObjectContainer_RestStop' est
# strictement identique pour Baijini Point, Everus Harbor et Port
# Tressler. Un alias (utilisateur OU codé en dur) rattaché à un tel
# identifiant ne peut donc JAMAIS être fiable : il faudrait qu'il
# s'applique à plusieurs lieux différents à la fois. Exclus de toute
# résolution par alias direct (voir _resolve_destination_label) et de
# l'auto-enregistrement dans les réglages (voir destination_alias_key,
# destination_is_unresolved) — résolus uniquement via un signal
# contextuel : obstruction_label si disponible, sinon la zone de départ
# (voir LANDING_ZONE_TO_PLANET ci-dessous).
AMBIGUOUS_SHARED_DESTINATION_IDS = {"reststop"}

# Correspondance zone d'atterrissage principale -> planète (Stanton),
# utilisée pour deviner la station visée par un identifiant AMBIGU (voir
# AMBIGUOUS_SHARED_DESTINATION_IDS) quand AUCUN obstruction_label n'est
# disponible — cas vérifié en vrai Game.log des trajets "locaux" trop
# courts pour croiser un obstacle (la ligne "OnPlayerSelectedQuantum
# Target" précise elle-même "routing locally" ; fuel estimate proche de
# zéro sur la ligne "Successfully calculated route"). Dans ce cas, la
# zone de DÉPART (capturée via RE_ROUTE_PROJECTED, "Projected Start
# Location is X") reste le seul indice disponible : un saut quantique
# local reste presque toujours dans le système de la planète de départ.
LANDING_ZONE_TO_PLANET = {
    "lorville": "hurston",
    "area18": "arccorp",
    "orison": "crusader",
    "newbabbage": "microtech",
}


def _guess_planet_station_from_start_location(start_location):
    """Renvoie le nom de la station principale (voir STATION_BY_PLANET)
    de la planète correspondant à start_location (une zone d'atterrissage
    connue, voir LANDING_ZONE_TO_PLANET) — ex. 'Lorville' -> 'Everus
    Harbor'. None si start_location est vide ou ne correspond à aucune
    zone connue.

    Heuristique, pas une certitude absolue (un trajet local reste
    PRESQUE toujours dans le système de départ, sans garantie à 100 %) —
    mais nettement préférable à annoncer l'identifiant brut partagé tel
    quel (ex. "RestStop"), qui ne dit rien du lieu réel et est identique
    pour Baijini Point, Everus Harbor et Port Tressler à la fois."""
    if not start_location:
        return None
    key = re.sub(r"\s+", "", start_location.strip()).lower()
    planet = LANDING_ZONE_TO_PLANET.get(key)
    if not planet:
        return None
    return STATION_BY_PLANET.get(planet)


def _resolve_destination_label(raw_destination, obstruction_label=None, user_aliases=None, start_location=None):
    """Détermine le meilleur nom à annoncer pour une destination, en
    donnant la priorité au texte lisible capturé via une ligne
    "Found obsruction while routing from X to Y" (voir
    RE_ROUTE_OBSTRUCTION) quand il est disponible — c'est le moteur du
    jeu lui-même qui fournit ce nom, déjà localisé si besoin, donc
    nettement plus fiable qu'un nettoyage à l'aveugle d'un identifiant
    technique générique (typiquement "RestStop", qui ne dit rien du lieu
    réel une fois isolé).

    - Si obstruction_label est fourni ET correspond au nom BRUT d'une
      planète connue (voir _obstruction_label_is_generic_guess) : c'est
      un simple repli générique (nom de la station principale de la
      planète) — un alias PERSONNALISÉ pour raw_destination (voir
      user_aliases) a alors le droit de le corriger, puisque ce cas peut
      être inexact pour la destination précise réellement visée.
    - Si obstruction_label est fourni et est déjà un nom de lieu
      SPÉCIFIQUE (ex. "Baijini Point", "Base minière #ODD-E9B") : utilisé
      tel quel, SANS consulter user_aliases. C'est volontaire —
      raw_destination peut être un identifiant générique PARTAGÉ par
      plusieurs lieux réels différents (ex. 'ObjectContainer_RestStop'
      pour Baijini Point/Everus Harbor/Port Tressler à la fois) : un
      alias qui lui serait rattaché s'appliquerait alors à tort à tous
      les lieux partageant ce même identifiant brut, écrasant
      l'information — fiable, elle — qu'obstruction_label révèle à
      chaque trajet.
    - Sinon (pas d'obstruction), repli sur
      _humanize_destination(raw_destination, user_aliases) comme avant —
      user_aliases permet à l'utilisateur de personnaliser depuis les
      réglages le nom annoncé pour un identifiant brut précis, ex.
      'rs_entry_nyx_pyro_jp1' -> 'Pyro Gateway'.

    EXCEPTION : si raw_destination est un identifiant AMBIGU PARTAGÉ par
    plusieurs lieux réels différents (voir AMBIGUOUS_SHARED_DESTINATION_
    IDS, ex. "RestStop"), aucun alias direct n'est consulté (ni
    utilisateur, ni codé en dur) — voir _guess_planet_station_from_
    start_location pour la résolution par zone de départ utilisée à la
    place quand obstruction_label est absent."""
    without_oc = RE_OBJECT_CONTAINER_PREFIX.sub("", raw_destination or "").strip("_ ")
    normalized = _normalize_for_alias_lookup(without_oc) if without_oc else ""

    if normalized in AMBIGUOUS_SHARED_DESTINATION_IDS:
        if obstruction_label:
            label = obstruction_label.strip()
            planet_key = re.sub(r"\s+", "", label).lower()
            station = STATION_BY_PLANET.get(planet_key)
            return station if station else label
        guessed = _guess_planet_station_from_start_location(start_location)
        return guessed if guessed else without_oc.replace("_", " ").strip()

    if obstruction_label:
        label = obstruction_label.strip()
        planet_key = re.sub(r"\s+", "", label).lower()
        station = STATION_BY_PLANET.get(planet_key)
        if station:
            if user_aliases and raw_destination:
                custom = user_aliases.get(normalized)
                if custom:
                    return custom
            return station
        return label
    return _humanize_destination(raw_destination, user_aliases=user_aliases)


# --------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------

class GameLogWatcher(threading.Thread):
    """Fil dédié qui "tail" le Game.log (comme `tail -f`) et pousse les
    événements détectés vers un callback, sans jamais bloquer ni
    ralentir le reste de NovaVox (reconnaissance vocale, IA...).

    Usage :
        watcher = GameLogWatcher(
            on_event=my_callback,       # appelé avec un dict événement
            log_path=None,              # None = auto-détection
        )
        watcher.start()
        ...
        watcher.stop()

    L'état courant (dernier système/zone connu, dernier vaisseau,
    compteurs de la session) est accessible à tout moment via
    watcher.get_state(), pensé pour être injecté dans le prompt système
    de l'IA (voir game_state_to_prompt_block ci-dessous).
    """

    POLL_INTERVAL = 0.5  # secondes entre deux lectures du fichier

    def __init__(self, on_event=None, log_path=None, on_debug_line=None, player_name=None):
        super().__init__(daemon=True)
        self.on_event = on_event or (lambda evt: None)
        self.on_debug_line = on_debug_line  # optionnel, pour debug/dev
        self.log_path = log_path
        self.player_name = (player_name or "").strip() or None

        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Dédoublonnage : évite de ré-annoncer la même destination si
        # l'utilisateur clique plusieurs fois de suite sur la même cible
        # (chaque clic redéclenche un calcul de route complet, donc une
        # nouvelle ligne "Successfully calculated" — voir RE_ROUTE_
        # CALCULATED dans _process_line). Comparé sur la paire
        # (destination brute, label d'obstruction) : identique à coup sûr
        # si c'est exactement le même trajet recalculé, sans dépendre
        # d'un minuteur qui pourrait bloquer une VRAIE nouvelle sélection
        # arrivée trop vite après la précédente.
        self._last_route_signature = None

        # Accumulateur pour les notifications HUD dont le texte est coupé
        # par un retour à la ligne dans le fichier avant la fermeture du
        # guillemet (voir RE_HUD_NOTIFICATION_START/_CLOSE). None = aucune
        # notification en cours d'accumulation.
        self._pending_notification = None

        self.state = {
            "connected": False,
            "current_zone": None,
            "current_ship": None,
            "session_kills": 0,
            "session_deaths": 0,
            "session_destructions": 0,
            "last_event_summary": None,
        }

    def stop(self):
        self._stop_event.set()

    def get_state(self):
        with self._lock:
            return dict(self.state)

    def _update_state(self, **kwargs):
        with self._lock:
            self.state.update(kwargs)

    def run(self):
        path = self.log_path or find_game_log_path()
        if not path:
            self._emit({"type": "watcher_error", "message": "Game.log introuvable"})
            return

        self._emit({"type": "watcher_started", "message": f"Surveillance de {path}"})

        # On se positionne à la fin du fichier existant : on ne veut pas
        # rejouer toute une session précédente au démarrage de NovaVox,
        # seulement suivre les nouveaux événements à partir de maintenant.
        try:
            f = open(path, "r", encoding="utf-8", errors="ignore")
            f.seek(0, os.SEEK_END)
        except OSError as e:
            self._emit({"type": "watcher_error", "message": str(e)})
            return

        inode_size = os.path.getsize(path)

        with f:
            while not self._stop_event.is_set():
                line = f.readline()
                if not line:
                    time.sleep(self.POLL_INTERVAL)
                    # Détecte une rotation/reset du log (ex. relance du
                    # jeu) : si le fichier a rapetissé, on se replace au
                    # début pour ne pas rester bloqué en attente sur un
                    # nouveau fichier.
                    try:
                        current_size = os.path.getsize(path)
                    except OSError:
                        continue
                    if current_size < inode_size:
                        f.seek(0)
                    inode_size = current_size
                    continue

                if self.on_debug_line:
                    self.on_debug_line(line.rstrip("\n"))
                self._process_line(line)

    # ------------------------------------------------------------------
    def _process_line(self, line):
        # Priorité absolue : si une notification HUD multi-lignes est en
        # cours d'accumulation (voir plus bas), cette ligne en est la
        # continuation — elle n'a pas à être testée contre les autres
        # patterns (route, arrivée...), qui ne pourraient de toute façon
        # pas matcher une ligne de continuation de texte.
        if self._pending_notification is not None:
            continuation = RE_TIMESTAMP.sub("", line, count=1)
            m = RE_HUD_NOTIFICATION_CLOSE.match(continuation)
            if m:
                self._pending_notification += "\n" + m.group("text")
                text = _fix_mojibake(self._pending_notification.strip())
                self._pending_notification = None
                self._emit({"type": "hud_notification", "text": text})
            else:
                self._pending_notification += "\n" + continuation.rstrip("\n")
                if self._pending_notification.count("\n") > _HUD_NOTIFICATION_MAX_CONTINUATION_LINES:
                    # Filet de sécurité : le motif de fermeture attendu
                    # n'est jamais apparu (ex. format inattendu) — on
                    # abandonne plutôt que d'accumuler indéfiniment, sans
                    # émettre de notification tronquée/douteuse.
                    self._pending_notification = None
            return

        # Notification HUD (voir RE_HUD_NOTIFICATION_START ci-dessus) :
        # soit tout le texte tient sur cette ligne (fermeture trouvée
        # immédiatement), soit il continue sur la/les ligne(s) suivante(s)
        # (voir le bloc juste au-dessus, à l'appel suivant).
        m = RE_HUD_NOTIFICATION_START.search(line)
        if m:
            raw_text = m.group("text").rstrip("\n")
            close_m = RE_HUD_NOTIFICATION_CLOSE.match(raw_text)
            if close_m:
                text = _fix_mojibake(close_m.group("text").strip())
                self._emit({"type": "hud_notification", "text": text})
            else:
                self._pending_notification = raw_text
            return

        # Ligne de VALIDATION d'une NOUVELLE cible : marque le début d'un
        # nouveau calcul, donc on efface tout label d'obstruction laissé
        # par un trajet précédent — sinon il pourrait être réutilisé à
        # tort pour ce nouveau trajet. L'événement "route_set" n'est PAS
        # émis ici : voir RE_ROUTE_CALCULATED plus bas, qui marque la fin
        # réelle du calcul (après qu'un éventuel label lisible ait pu
        # être capturé entre les deux).
        m = RE_TARGET_SELECTED.search(line)
        if m:
            self._pending_destination = m.group("destination")
            self._pending_obstruction_label = None
            self._pending_start_location = None
            return

        # Zone de DÉPART du trajet en cours de calcul (voir
        # LANDING_ZONE_TO_PLANET) — repli utilisé pour deviner la station
        # visée par un identifiant AMBIGU comme "RestStop" quand le trajet
        # est trop court/local pour croiser un obstacle (voir
        # RE_ROUTE_OBSTRUCTION juste en dessous, qui n'apparaît alors
        # jamais). Mémorisé pour le prochain événement à émettre.
        m = RE_ROUTE_PROJECTED.search(line)
        if m:
            self._pending_start_location = m.group("start_location").strip()
            return

        # PÉPITE : nom lisible de la vraie destination, révélé par le
        # moteur quand le trajet croise un obstacle (voir
        # RE_ROUTE_OBSTRUCTION). Mémorisé pour le prochain événement
        # "route_set"/"zone_change" à émettre.
        m = RE_ROUTE_OBSTRUCTION.search(line)
        if m:
            self._pending_obstruction_label = m.group("label").strip()
            return

        # Confirmation FINALE du calcul de route : c'est le bon moment
        # pour annoncer "route tracée vers X", en priorisant le label
        # lisible capturé entre-temps si disponible (voir
        # _resolve_destination_label). Dédoublonnée : un clic répété sur
        # la même destination ne redéclenche pas l'annonce.
        m = RE_ROUTE_CALCULATED.search(line)
        if m:
            destination = m.group("destination")
            obstruction_label = getattr(self, "_pending_obstruction_label", None)
            start_location = getattr(self, "_pending_start_location", None)
            self._last_route_destination = destination
            self._last_obstruction_label = obstruction_label
            self._last_start_location = start_location

            signature = (destination, obstruction_label)
            if signature == self._last_route_signature:
                return  # même destination déjà annoncée : on ne répète pas
            self._last_route_signature = signature

            self._emit({
                "type": "route_set",
                "destination": destination,
                "obstruction_label": obstruction_label,
                "start_location": start_location,
            })
            return

        if DEPARTURE_DETECTION_ENABLED and RE_QUANTUM_JUMP_ENGAGED.search(line):
            destination = getattr(self, "_last_route_destination", None)
            obstruction_label = getattr(self, "_last_obstruction_label", None)
            start_location = getattr(self, "_last_start_location", None)
            self._emit({
                "type": "jump_start",
                "destination": destination,
                "obstruction_label": obstruction_label,
                "start_location": start_location,
            })
            return

        if RE_QUANTUM_ARRIVED.search(line):
            zone = getattr(self, "_last_route_destination", None)
            obstruction_label = getattr(self, "_last_obstruction_label", None)
            start_location = getattr(self, "_last_start_location", None)
            self._update_state(current_zone=zone, connected=True)
            self._emit({
                "type": "zone_change",
                "zone": zone,
                "obstruction_label": obstruction_label,
                "start_location": start_location,
            })
            return

        # Pré-remplissage automatique du pseudo RSI si pas encore connu.
        if not self.player_name:
            m = RE_PLAYER_NICKNAME.search(line)
            if m:
                self._emit({"type": "nickname_detected", "nickname": m.group("nickname")})

    def _emit(self, evt):
        evt.setdefault("ts", time.time())
        with self._lock:
            self.state["last_event_summary"] = evt.get("type")
        try:
            self.on_event(evt)
        except Exception:
            # Un callback défaillant ne doit jamais interrompre la
            # surveillance du log.
            pass


# --------------------------------------------------------------------------
# Aide pour l'injection dans le prompt de l'IA
# --------------------------------------------------------------------------

def game_state_to_prompt_block(state):
    """Construit un petit paragraphe factuel à ajouter au prompt système
    de l'IA, pour qu'elle connaisse l'état réel de la session en cours
    plutôt que de deviner. Retourne une chaîne vide si rien d'utile
    n'est encore connu (ex. juste après le lancement).

    Ne couvre actuellement que la zone/destination courante — voir
    l'avertissement en tête de fichier : les kills/morts/destructions ne
    sont plus détectables via le Game.log dans cette version du jeu."""
    if not state:
        return ""
    parts = []
    if state.get("current_zone"):
        parts.append(f"Destination/zone la plus récente : {state['current_zone']}")
    if not parts:
        return ""
    return (
        "\n\nÉtat de la partie en cours (issu du Game.log en temps réel) :"
        "\n- " + "\n- ".join(parts)
    )