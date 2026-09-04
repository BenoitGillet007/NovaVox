"""
Annulation d'écho acoustique (AEC) — expérimental.

Reprend le même principe que Discord/Zoom/Teams : le système CONNAÎT déjà
en temps réel ce qu'il envoie vers la sortie audio (le son du jeu, d'une
vidéo, de l'IA qui répond...) — ce signal est appelé le signal de
"référence" (ou "far-end"). Il suffit de le SOUSTRAIRE, en continu, du son
capté par le micro pour ne garder que ce que le micro entend en plus de ce
signal connu — en pratique, la voix de l'utilisateur.

La soustraction n'est pas une simple différence terme à terme : le trajet
entre "ce qui est joué" et "ce que le micro en capte" déforme et retarde
le signal (délai de quelques dizaines à ~100 ms selon le matériel/pilotes,
plus une réponse en fréquence propre au haut-parleur/casque et au micro).
Un filtre adaptatif — ici un NLMS (Normalized Least Mean Squares) — apprend
en continu cette déformation et ajuste ses coefficients pour prédire au
mieux, à partir du signal de référence, ce que le micro va capter comme
écho, puis soustrait cette prédiction du son micro réel.

Ce module ne dépend que de NumPy (pas de bibliothèque C tierce à compiler
type WebRTC/Speex — plus simple à embarquer avec PyInstaller), au prix
d'un algorithme plus simple que ceux utilisés par les vraies applications
de visioconférence. C'est un compromis assumé : suffisant pour empêcher le
son d'une vidéo/d'un jeu de déclencher des commandes vocales par erreur,
mais pas garanti aussi propre qu'une vraie conversation audio pro.
"""

import threading

import numpy as np


def resample_linear(samples, orig_sr, target_sr):
    """Ré-échantillonnage simple par interpolation linéaire. Pas de
    filtrage anti-repliement (contrairement à un vrai ré-échantillonneur
    type scipy/soxr) — suffisant ici puisque le signal de référence ne
    sert qu'à alimenter le filtre d'annulation d'écho, pas à être écouté
    directement, et évite une dépendance supplémentaire (scipy) pour un
    gain de qualité qui ne changerait rien au résultat final."""
    n = len(samples)
    if n == 0 or orig_sr == target_sr:
        return samples
    duration = n / float(orig_sr)
    target_len = max(1, int(round(duration * target_sr)))
    orig_positions = np.linspace(0.0, n - 1, num=n)
    target_positions = np.linspace(0.0, n - 1, num=target_len)
    return np.interp(target_positions, orig_positions, samples)


class AecReferenceBuffer:
    """Tampon circulaire thread-safe des derniers échantillons du signal
    de référence (le son actuellement joué par le PC, capturé en
    "loopback"), déjà ré-échantillonnés à la fréquence utilisée par la
    reconnaissance vocale (voir SAMPLE_RATE dans app.py) et réduits à un
    seul canal (mono). Alimenté en continu par le flux loopback (voir
    Api._aec_loopback_callback), consommé à chaque bloc micro (voir
    Api._apply_echo_cancellation)."""

    def __init__(self, sample_rate, max_seconds=3.0):
        self._sample_rate = sample_rate
        self._max_samples = max(1, int(max_seconds * sample_rate))
        self._buffer = np.zeros(0, dtype=np.float64)
        self._lock = threading.Lock()

    def push(self, mono_samples):
        if mono_samples is None or len(mono_samples) == 0:
            return
        with self._lock:
            self._buffer = np.concatenate([self._buffer, mono_samples])
            excess = len(self._buffer) - self._max_samples
            if excess > 0:
                self._buffer = self._buffer[excess:]

    def pull_latest(self, n):
        """Retourne les n derniers échantillons disponibles, ou None si le
        tampon n'en contient pas encore assez (ex. juste après le
        démarrage de la capture loopback) — dans ce cas l'appelant doit
        simplement laisser passer le bloc micro tel quel, sans y toucher,
        plutôt que de risquer un mauvais alignement."""
        with self._lock:
            if len(self._buffer) < n:
                return None
            return self._buffer[-n:].copy()

    def clear(self):
        with self._lock:
            self._buffer = np.zeros(0, dtype=np.float64)


class NLMSEchoCanceller:
    """Filtre adaptatif NLMS mono-canal.

    filter_length : nombre de coefficients du filtre, c'est-à-dire la
    durée maximale de "mémoire" du trajet écho qu'il peut apprendre à
    prédire (ex. 1600 échantillons à 16 kHz = 100 ms). Doit couvrir le
    délai réel entre la sortie audio et sa capture par le micro (délai de
    traitement Windows + trajet acoustique/électrique) : trop court, le
    filtre ne "voit" jamais l'écho qu'il doit annuler ; trop long, il
    devient plus lent à converger et plus coûteux en calcul.

    mu : pas d'adaptation (0 < mu <= 1). Plus élevé = le filtre apprend
    plus vite mais est plus sensible au bruit/instable ; plus bas = plus
    lent mais plus stable.
    """

    def __init__(self, filter_length=1600, mu=0.4, eps=1e-6):
        self.filter_length = int(filter_length)
        self.mu = float(mu)
        self.eps = float(eps)
        self.weights = np.zeros(self.filter_length, dtype=np.float64)
        # Les (filter_length - 1) derniers échantillons de référence du
        # bloc précédent, pour que le tout premier échantillon d'un
        # nouveau bloc dispose déjà de son historique complet (sans ça,
        # chaque frontière de bloc recommencerait avec un filtre "à
        # trous", dégradant l'annulation en début de bloc).
        self._ref_tail = np.zeros(self.filter_length - 1, dtype=np.float64)

    def reset(self):
        self.weights = np.zeros(self.filter_length, dtype=np.float64)
        self._ref_tail = np.zeros(self.filter_length - 1, dtype=np.float64)

    def process_block(self, mic_block, ref_block):
        """mic_block, ref_block : np.ndarray float64 de même longueur,
        alignés dans le temps (voir Api._apply_echo_cancellation pour la
        façon dont l'alignement approximatif est obtenu). Retourne le
        signal micro débarrassé de l'écho estimé — même longueur, même
        échelle."""
        L = self.filter_length
        n = len(mic_block)
        if n == 0 or len(ref_block) != n:
            return mic_block

        extended_ref = np.concatenate([self._ref_tail, ref_block])
        # windows[i] = les L derniers échantillons de référence qui
        # précèdent (et incluent) celui aligné avec mic_block[i]. Vue
        # (pas de copie mémoire) sur extended_ref grâce à stride_tricks —
        # c'est ce qui rend la boucle ci-dessous praticable en Python pur
        # malgré les milliers d'itérations par bloc.
        windows = np.lib.stride_tricks.sliding_window_view(extended_ref, L)

        out = np.empty(n, dtype=np.float64)
        w = self.weights
        for i in range(n):
            x = windows[i]
            echo_estimate = w @ x
            err = mic_block[i] - echo_estimate
            out[i] = err
            norm = (x @ x) + self.eps
            w = w + (self.mu * err / norm) * x

        self.weights = w
        self._ref_tail = extended_ref[-(L - 1):] if L > 1 else extended_ref[0:0]
        return out