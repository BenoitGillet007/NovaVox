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

# Capture la destination projetée d'un trajet en cours de calcul, ex. :
# "...CalculateRoute|Projected Start Location is Magda for route to
#  destination ab_mine_stanton1_med_008 [Team_CGP4][QuantumTravel]"
RE_ROUTE_PROJECTED = re.compile(
    r"CalculateRoute\|Projected Start Location is .*? for route to "
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
}


def _normalize_for_alias_lookup(raw_id):
    """Uniformise un identifiant brut pour la recherche dans
    KNOWN_LOCATION_ALIASES : minuscules, tirets/underscores réduits à un
    simple espace, espaces multiples compressés."""
    s = re.sub(r"[_\-]+", " ", raw_id.lower())
    return re.sub(r"\s+", " ", s).strip()


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


def _humanize_destination(raw_id):
    """Rend un identifiant de destination un peu plus prononçable à voix
    haute.

    Étape 0 (systématique) — retire tout préfixe/occurrence de
    "ObjectContainer", jamais prononcé (voir RE_OBJECT_CONTAINER_PREFIX).

    Priorité 1 — alias connu (voir KNOWN_LOCATION_ALIASES), ex.
    'rs_ext_cru-leo1' -> 'Seraphim Station'.

    Priorité 2 — format "OOC_<Système>_<index>_<Corps>" (planètes/lunes,
    voir RE_OOC_LOCATION) : donne "<Corps> (système <Système>)", ex.
    'OOC_Stanton_1d_Ita' -> 'Ita (système Stanton)'.

    Priorité 3 — repli générique pour les autres formats rencontrés
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

    alias = KNOWN_LOCATION_ALIASES.get(_normalize_for_alias_lookup(without_oc))
    if alias:
        return alias

    m = RE_OOC_LOCATION.match(without_oc)
    if m:
        return f"{m.group('body')} (système {m.group('system')})"

    cleaned = re.sub(r"_\d{6,}$", "", without_oc)  # retire le suffixe d'instance
    cleaned = re.sub(r"^MISSION_QT_", "", cleaned)
    return cleaned.replace("_", " ").strip()


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


def _resolve_destination_label(raw_destination, obstruction_label=None):
    """Détermine le meilleur nom à annoncer pour une destination, en
    donnant la priorité au texte lisible capturé via une ligne
    "Found obsruction while routing from X to Y" (voir
    RE_ROUTE_OBSTRUCTION) quand il est disponible — c'est le moteur du
    jeu lui-même qui fournit ce nom, déjà localisé si besoin, donc
    nettement plus fiable qu'un nettoyage à l'aveugle d'un identifiant
    technique générique (typiquement "RestStop", qui ne dit rien du lieu
    réel une fois isolé).

    - Si obstruction_label est fourni ET correspond au nom d'une planète
      connue (voir STATION_BY_PLANET) : renvoie le nom de sa station
      principale plutôt que le nom de la planète elle-même — cohérent
      avec le fait qu'un identifiant "RestStop" désigne une station en
      orbite, pas la planète en surface.
    - Si obstruction_label est fourni mais ne correspond à aucune planète
      connue : c'est déjà un nom de lieu lisible (ex. "Base minière
      #ODD-E9B"), utilisé tel quel.
    - Sinon, repli sur _humanize_destination(raw_destination) comme
      avant."""
    if obstruction_label:
        label = obstruction_label.strip()
        planet_key = re.sub(r"\s+", "", label).lower()
        station = STATION_BY_PLANET.get(planet_key)
        if station:
            return station
        return label
    return _humanize_destination(raw_destination)


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
            self._last_route_destination = destination
            self._last_obstruction_label = obstruction_label

            signature = (destination, obstruction_label)
            if signature == self._last_route_signature:
                return  # même destination déjà annoncée : on ne répète pas
            self._last_route_signature = signature

            self._emit({
                "type": "route_set",
                "destination": destination,
                "obstruction_label": obstruction_label,
            })
            return

        if DEPARTURE_DETECTION_ENABLED and RE_QUANTUM_JUMP_ENGAGED.search(line):
            destination = getattr(self, "_last_route_destination", None)
            obstruction_label = getattr(self, "_last_obstruction_label", None)
            self._emit({
                "type": "jump_start",
                "destination": destination,
                "obstruction_label": obstruction_label,
            })
            return

        if RE_QUANTUM_ARRIVED.search(line):
            zone = getattr(self, "_last_route_destination", None)
            obstruction_label = getattr(self, "_last_obstruction_label", None)
            self._update_state(current_zone=zone, connected=True)
            self._emit({
                "type": "zone_change",
                "zone": zone,
                "obstruction_label": obstruction_label,
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