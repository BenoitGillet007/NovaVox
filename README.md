# NovaVox

Discord NovaVox: https://discord.gg/NhhhGKv7F

**NovaVox** est une application Windows de commandes vocales pour Star
Citizen : tu parles, elle appuie sur les touches correspondantes dans le
jeu à ta place — mains sur le joystick, plus besoin de lâcher les
commandes pour sortir le train d'atterrissage, activer le scanner ou
basculer un bouclier.

## Présentation

- **Reconnaissance vocale française, gratuite et hors-ligne** : aucune
  donnée vocale n'est envoyée sur internet, aucun abonnement.
- **Compréhension large des phrases** : pas besoin de dire la commande
  mot pour mot dans le bon ordre. "Train d'atterrissage", "sors les
  trains d'atterrissage" ou "sors-moi le train d'atterrissage"
  déclenchent tous la même action.
- **N'importe quelle touche, combinaison, ou clic** : une seule touche
  (`n`), une combinaison (`ctrl+n`), un clic souris, et même un maintien
  de 2 secondes au lieu d'un simple appui, pour les actions qui
  l'exigent en jeu.
- **Organisation des commandes en groupes** : des titres de section (ex.
  "Bouclier", "Navigation") pour garder une longue liste de commandes
  lisible, avec réordonnancement libre.
- **Trois façons d'activer l'écoute** :
  - *Toujours active* : le micro écoute en continu dès que tu lances
    l'écoute.
  - *Touche bascule* : une pression sur une touche (clavier, souris ou
    bouton de manette/joystick) active ou coupe le micro.
  - *Push-to-talk* : le micro n'est transmis que pendant que la touche
    est maintenue enfoncée.
- **Nova, l'assistante IA embarquée (optionnelle)** : un mot
  d'activation ("Nova, ...") te permet de poser une question ou de
  discuter, propulsée par un modèle de langage tournant 100% en local et
  gratuitement. Personnalisable : nom de l'IA, ton prénom, longueur des
  réponses, et un contexte libre (lore de ta partie, règles maison...).
- **Voix naturelle (optionnelle)** : synthèse vocale neuronale, locale et
  gratuite, avec plusieurs voix françaises au choix, réglables en
  vitesse et en expressivité, avec un effet optionnel "communication
  radio de vaisseau". Périphérique de sortie et volume de la voix
  réglables indépendamment du volume système.
- **Overlay en jeu (optionnel)** : petite fenêtre superposée à Star
  Citizen affichant l'état du micro, la dernière phrase reconnue, le
  statut de Nova et la zone actuelle. Déplaçable librement, clics
  traversants une fois verrouillée.
- **Surveillance du journal de jeu (Game.log)** : annonce à voix haute
  les changements de zone, et corrige certains éléments d'affichage du
  HUD en temps réel.
- **Profils multiples** : bascule entre plusieurs configurations de
  commandes complètes (par exemple un profil combat, un profil minage,
  un profil exploration) via un raccourci clavier ou un bouton de
  manette/joystick.
- **Annulation d'écho (expérimentale)** : réduit le risque que la voix
  de Nova, captée par ton micro, soit reconnue par erreur comme une
  commande.
- **Lancement automatique avec Star Citizen (optionnel)** : une fois
  activé dans les Réglages, NovaVox s'ouvre tout seul dès que le jeu
  démarre — plus besoin d'y penser.
- **Mises à jour automatiques** : NovaVox vérifie et te propose les
  nouvelles versions tout seul, pas besoin de retélécharger manuellement.
- **Simple à installer** : un seul installeur, aucune connaissance
  technique requise.

## Installation

1. Télécharge `NovaVox_Setup.exe`.
2. Double-clique dessus et suis l'assistant d'installation (accepte la
   demande de droits administrateur si Windows la demande — c'est
   normal, elle est nécessaire pour que NovaVox puisse envoyer des
   touches à Star Citizen).
3. À la fin de l'installation, un raccourci **NovaVox** est créé sur ton
   Bureau et dans le menu Démarrer.
4. Lance NovaVox.

Rien d'autre à installer manuellement au préalable : au premier
lancement, l'application vérifie et installe elle-même ce dont elle a
besoin.

## Premier lancement

### 1. Choisir un modèle de reconnaissance vocale

Au tout premier lancement, NovaVox te propose de télécharger un modèle
de reconnaissance vocale française (nécessaire pour comprendre ce que tu
dis) :

- **Modèle léger** (~40 Mo) : rapide à charger, précision correcte —
  recommandé pour commencer.
- **Modèle précis** (~1,4 Go) : plus long à télécharger, reconnaissance
  plus fine.

Le téléchargement se fait automatiquement depuis l'application, tu n'as
rien à faire manuellement.

### 2. Démarrer l'écoute

Une fois le modèle installé, clique sur **"Démarrer l'écoute"**. Le
statut affiche "Écoute en cours" quand le micro est actif.

## Configurer tes commandes

Dans le tableau de l'application, chaque ligne associe une **phrase à
dire** à une ou plusieurs **touches** :

| Phrase à dire         | Touche(s) |
|------------------------|-----------|
| train d'atterrissage  | n         |
| scanner                | v         |
| poste combustion       | shift     |
| mode quantique         | b         |

- **Une seule touche** : `n`, `v`, `shift`, `f1`...
- **Une combinaison** : sépare avec `+`, ex. `ctrl+n`, `alt+r`
- **Un clic souris** : clique directement sur le bouton pour l'assigner
- **Un maintien** (au lieu d'un simple appui, ~2 secondes) : coche
  l'option "Maintenir" pour les actions qui l'exigent
- Ajoute des **synonymes** à une commande si tu la formules souvent
  différemment
- Utilise des **titres de groupe** pour organiser une longue liste
  (ex. regrouper toutes les commandes de bouclier ensemble)

Adapte toujours les touches à **tes propres réglages clavier dans Star
Citizen** (Options > Contrôles dans le jeu). Tes commandes sont
sauvegardées automatiquement.

### Plusieurs profils

Si tu joues différents rôles (combat, minage, exploration...), crée
plusieurs profils depuis les Réglages, chacun avec son propre jeu de
commandes complet. Un raccourci clavier ou un bouton de manette/joystick
te permet de passer de l'un à l'autre sans ouvrir les Réglages.

## Choisir un mode d'écoute

Dans Réglages > 🔊 Sons, tu peux choisir comment le micro s'active :

- **Toujours active** : le plus simple, aucune touche à retenir.
- **Touche bascule** : une pression active ou coupe le micro (pratique
  pour ne pas être écouté en continu).
- **Push-to-talk** : maintiens une touche pour parler, comme sur Discord.

Pour les deux derniers modes, assigne une touche clavier, un clic souris,
ou un bouton de ta manette/joystick directement depuis l'interface.

Dans le même onglet, tu peux aussi régler : le périphérique et le volume
de sortie de la voix, le micro utilisé, l'amplification et le seuil de
sensibilité du micro, et activer l'annulation d'écho expérimentale.

## Activer l'assistante IA Nova (optionnel)

Nova peut répondre à tes questions à voix haute pendant que tu joues.
Dans Réglages > 🤖 IA, un bouton te guide pour l'installer (téléchargement
automatique du moteur nécessaire). Une fois installée :

- Dis **"Nova"** suivi de ta question
- Personnalise son nom, ton propre prénom, la longueur de ses réponses,
  et donne-lui des informations sur ta partie qu'elle utilisera dans ses
  réponses
- Active une **voix naturelle** (plusieurs voix françaises au choix) et
  un effet "radio de vaisseau" si tu veux

Cette fonctionnalité est entièrement optionnelle : NovaVox fonctionne
très bien pour les commandes vocales seules, sans jamais l'activer.

## Overlay en jeu (optionnel)

Dans Réglages > ⬡ NovaVox, active "Overlay en jeu" pour afficher une
petite fenêtre par-dessus Star Citizen : état du micro, dernière phrase
reconnue, statut de Nova, zone actuelle. Clique sur "Déplacer l'overlay"
pour la repositionner où tu veux, puis reclique pour la reverrouiller
(les clics traversent alors l'overlay jusqu'au jeu en dessous).

## Zones et HUD (Game.log)

Dans Réglages > 🛰 Game.log, active la surveillance pour que NovaVox
annonce automatiquement à voix haute tes changements de zone en jeu, et
corrige certains affichages du HUD. Renseigne ton pseudo RSI pour que
les annonces te concernent bien toi et pas un autre joueur à proximité.

## Lancement automatique avec Star Citizen (optionnel)

Dans Réglages > ⬡ NovaVox, coche "Lancer NovaVox automatiquement au
démarrage de Star Citizen". Une petite veille se met alors en place au
démarrage de Windows (aucune fenêtre, ressources quasi nulles) et ouvre
NovaVox tout seul dès qu'elle détecte le jeu lancé.

> Après avoir coché la case, déconnecte-toi de Windows puis reconnecte-toi
> (ou redémarre l'ordinateur) pour que la veille s'active — elle ne
> démarre pas immédiatement au moment de cocher.

## Utilisation avec Star Citizen — points importants

- **Lance le jeu en mode "Fenêtré sans bordure"** plutôt qu'en plein
  écran exclusif. Certains jeux en plein écran exclusif bloquent les
  touches simulées par des applications externes.
- **Lance NovaVox en tant qu'administrateur** si Star Citizen tourne
  lui-même avec les droits administrateur (clic droit sur l'icône >
  "Exécuter en tant qu'administrateur"). Windows empêche une application
  normale d'envoyer des touches à une application élevée.
- Le micro capte en continu en mode "Toujours active" : évite de parler
  pendant les communications vocales avec d'autres joueurs si les
  phrases se ressemblent, pour ne pas déclencher une action par erreur —
  ou passe en mode touche bascule / push-to-talk.
- Un petit délai empêche qu'une même commande se déclenche plusieurs
  fois d'affilée par erreur.

## Dépannage

- **"Le dossier du modèle Vosk est introuvable"** → retélécharge le
  modèle depuis l'écran de premier lancement, ou vérifie ta connexion
  internet.
- **Rien ne se passe dans le jeu alors que la phrase est bien reconnue
  dans le journal** → lance NovaVox en administrateur, ou passe le jeu
  en fenêtré sans bordure.
- **Mauvaise reconnaissance vocale** → essaie le modèle précis plutôt
  que léger, rapproche-toi du micro, réduis le bruit ambiant, ou ajuste
  le volume/seuil de sensibilité du micro dans les Réglages.
- **Erreur au démarrage de l'audio** → vérifie qu'un micro est bien
  sélectionné par défaut dans les paramètres Windows (Son > Entrée), ou
  choisis-le manuellement dans les Réglages de NovaVox.
- **L'assistante Nova ne répond pas** → vérifie qu'elle est bien
  installée (panneau IA > Revérifier) et que le modèle IA choisi est
  téléchargé.
- **Nova se déclenche en entendant sa propre voix** → active l'annulation
  d'écho (expérimentale) dans Réglages > 🔊 Sons ; sur certaines
  configurations, il faut aussi activer "Mixage stéréo" dans les
  paramètres d'enregistrement de Windows pour qu'elle fonctionne.
- **Le lancement automatique avec Star Citizen ne se déclenche pas** →
  vérifie que tu t'es bien déconnecté/reconnecté (ou redémarré) après
  avoir coché la case ; cette fonctionnalité n'est disponible que depuis
  la version installée, pas en développement.
- **Repartir de zéro** → le bouton "Réinitialiser l'application" dans
  les Réglages remet NovaVox dans son état d'origine (commandes, voix,
  micro...), comme au tout premier lancement.

## Désinstaller

Depuis le menu Démarrer (dossier NovaVox > Désinstaller NovaVox), ou via
**Paramètres Windows > Applications > NovaVox > Désinstaller**.

## Licence

Ce projet est distribué sous licence [PolyForm Noncommercial 1.0.0](LICENSE) :
libre de lire, utiliser, modifier et forker à titre non commercial, mais
**toute utilisation commerciale (vente, intégration dans un produit ou
service payant...) est interdite** sans autorisation explicite de l'auteur.

## Credit

Développé par Ammoniak007

Discord : NovaVox

https://discord.gg/NhhhGKv7F