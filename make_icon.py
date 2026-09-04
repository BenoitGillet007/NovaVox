"""Génère icon.ico à partir des couleurs du thème de l'appli
(fond #0a0e14, accent cyan #2dd4ff), avec le même glyphe hexagonal
que le logo affiché dans l'en-tête de l'interface (⬡).

Usage : python make_icon.py
Produit : icon.ico (multi-résolutions, à placer à côté de app.py
avant de lancer build_exe.bat).
"""

import math
from PIL import Image, ImageDraw

BG = (10, 14, 20, 255)        # #0a0e14
ACCENT = (45, 212, 255, 255)  # #2dd4ff
ACCENT_DIM = (45, 212, 255, 60)

SIZE = 512


def hexagon_points(cx, cy, radius, rotation_deg=-90):
    """Sommets d'un hexagone régulier (pointe vers le haut)."""
    points = []
    for i in range(6):
        angle = math.radians(rotation_deg + i * 60)
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def rounded_square(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build_icon():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Pas de fond : l'icône reste transparente, seul l'hexagone glyphe
    # est visible (se fond dans le Bureau / la barre des tâches, quel
    # que soit leur thème clair ou sombre).
    cx, cy = SIZE / 2, SIZE / 2

    # Halo discret derrière l'hexagone (rappelle le glow CSS --accent-dim
    # utilisé sur .brand-glyph dans l'interface).
    halo = hexagon_points(cx, cy, radius=190)
    draw.polygon(halo, fill=ACCENT_DIM)

    # Hexagone principal (contour), identique au glyphe ⬡ du logo.
    hexpts = hexagon_points(cx, cy, radius=150)
    stroke_w = 22
    draw.polygon(hexpts, outline=ACCENT, width=stroke_w)

    # Petit point central plein (écho du "core-dot" du radar de statut
    # dans l'UI), pour donner du poids visuel au centre à petite taille.
    dot_r = 26
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=ACCENT)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.save("icon.ico", sizes=[(s, s) for s in sizes])
    print("icon.ico généré avec succès.")


if __name__ == "__main__":
    build_icon()
