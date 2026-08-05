# Mire synchro A/V

Générateur de test (catégorie **Sources**) : flash plein écran + bip sinus **synchrones** (calés
sur la même horloge), un compteur `sec.frame`, une barre de balayage (détection de saccades) et
un habillage. Sert à **mesurer le décalage lip-sync** introduit par une chaîne (encodeur,
streamer, réseau…) en comparant visuellement/à l'oreille le flash et le bip en sortie.

## Sorties

- `<hostname>` — vidéo (flash + compteur/timecode + barre de balayage + habillage)
- `<hostname>_audio` — audio 8 canaux, bip synchrone au flash
- `<hostname>_anc` — flux ANC (timecode ATC RP 188), seulement si le générateur de timecode est activé

## Usage

Déployer depuis la palette **Sources**, câbler la sortie vidéo et audio vers l'encodeur/streamer
ou le monitoring à comparer. Observer sur un analyseur (ou un écran avec audio) l'écart entre le
flash blanc et le bip : c'est le décalage lip-sync de tout ce qui se trouve entre la mire et le
point d'observation.

**Isoler le composant fautif** : placer une mire avant et une après l'étage suspect (ex. avant et
après un streamer) et comparer les deux mesures — l'écart entre les deux mires donne le décalage
propre à cet étage.

## Réglages au déploiement (redéploiement nécessaire pour les changer)

Ces champs sont structurels (scope `user` du config_schema) : les modifier redéploie le
conteneur, contrairement aux réglages « à chaud » ci-dessous.

| Réglage | Description |
|---|---|
| Largeur / Hauteur | Résolution de sortie (px) |
| Fréquence (fps) | Cadence de sortie |
| Chroma | 4:2:0 / 4:2:2 / 4:4:4 |
| Police | Habillage + timecode — sélecteur BobiFonts (polices d'image + bibliothèque). Une police de la bibliothèque doit être **embarquée** dans l'image runtime au démarrage (rootfs éphémère) : changer de police redéploie toujours, même à effet visuel proche |
| Afficher la marque | Logo + nom + lieu (repris des réglages **Personnalisation**) |
| Générateur de timecode | Voir « Timecode » ci-dessous |

## Réglages à chaud (page Traitements, sans redéploiement)

| Paramètre | Description |
|---|---|
| Battement (s) | Période entre deux flashs/bips, 0,2 à 10 s |
| Durée flash + bip (ms) | Durée du flash blanc et du bip à chaque battement, 10 à 500 ms |
| Fréquence du bip (Hz) | Tonalité du bip audio, 100 à 12 000 Hz |
| Afficher la marque | Bascule logo/nom/lieu sans redéployer |
| Texte personnalisé | Ligne de texte libre affichée en bas de l'image |
| Générateur de timecode | Active/désactive à chaud (le flux ANC se crée automatiquement à l'activation) |

## Timecode

Activé, le générateur incruste un timecode **heure du jour** (`hh:mm:ss:img`) à la place du
compteur `sec.frame` (les deux ne s'affichent jamais ensemble) et publie le **même** timecode en
flux ANC (`<hostname>_anc`, ATC RP 188 / RFC 8331), câblable par exemple vers le port ANC d'une
fenêtre de multiview.

- L'heure est dérivée de l'horloge du nœud (échelle PTP/TAI) et affichée dans le **fuseau horaire
  de la régie** (réglé en Personnalisation), pas nécessairement celui du conteneur (UTC par
  défaut).
- En genlock, le timecode est calculé sur la **grille de présentation** de la trame : il reste
  exactement aligné avec l'image, y compris sur un multiview qui compose plusieurs sources
  timecodées.
- Sans PTP fonctionnel sur le nœud, l'heure affichée peut dériver de l'heure réelle — vérifier
  l'état PTP du nœud (page Nœuds) en cas de doute sur un timecode.

## Genlock

Actif par défaut : flash, bip et cadence de sortie sont calés sur la grille PTP (horloge
CLOCK_REALTIME disciplinée). Désactivé, la mire tourne en cadence libre (horloge locale du
conteneur) — la mesure de lip-sync reste valable, mais la mire n'est plus traçable/comparable à
d'autres flux genlockés du pipeline.

## Mode tranche (latence réduite)

`slice_mode` (réglage avancé, opt-in) publie la trame déjà rendue **bande par bande** au lieu de
l'attendre entière. Le rendu de la mire étant quasi instantané, le gain propre est faible ici —
l'intérêt est de faire tenir le chemin sous-trame de bout en bout sur une chaîne qui en dépend
ailleurs. L'audio, le timing flash/bip et l'horodatage média sont **strictement inchangés**.

## Notes

- Les métriques `:8080` publient `fps`, `frame_index` et `own_latency_ms` (coût propre du rendu,
  hors attente de cadence) — utile pour vérifier que le générateur lui-même ne dérive pas.
- `GET :8082/state` publie la configuration complète effective (format, réglages live, timecode,
  logo chargé ou non, niveau de log) — premier réflexe en diagnostic.
- Le niveau de log se règle à chaud (`POST :8082/log_level`) sans redéployer, pour un diagnostic
  ponctuel sans perdre l'historique écrit au niveau par défaut.
