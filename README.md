# Mire synchro A/V — l'instrument de lip-sync de Bobi.Studio

*[English version](README.en.md)*

Mesure le décalage image/son introduit par une chaîne. Sous-module de
[bobi.studio](https://github.com/bob-integration/bobistudio), monté sur `plugins/avsync/`.

## Ce que ça fait

Un flash plein écran et un bip, **calés sur la même horloge**, plus un compteur `sec.frame`, une
barre de balayage et un habillage. Trois sorties sur le bus MXL : la vidéo (`<hôte>`), l'audio
8 canaux (`<hôte>_audio`), et un flux ANC de timecode (`<hôte>_anc`) si le générateur est activé.

On câble la mire à l'entrée de ce qu'on veut mesurer, et on lit l'écart entre le flash et le bip
en sortie : c'est le décalage de tout ce qui se trouve entre les deux. Pour **isoler un étage**,
on place une mire avant et une après, et on soustrait.

## Marque audio

Deux formes de son au choix. Le **bip**, un sinus de 1 kHz identique à chaque battement, mesure un
décalage inférieur à un demi-battement ; au-delà, la mesure est ambiguë d'une période entière.

La **marque** date chaque battement : 20 ms de chirp 1→4 kHz pour l'ancrage, puis deux tons purs
de 20 ms qui portent un compteur sur 6 bits — 64 battements avant répétition. C'est la forme à
prendre derrière un codec : le chirp donne un pic de corrélation franc, là où le front d'un sinus
est étalé, et les tons purs sont ce qu'AAC et Opus préservent le mieux.

## Bandeaux-sonde

Deux bandeaux de 32 blocs binaires en luma, porteurs **par bande** de l'index de grain et du
numéro de la bande, avec un contrôle par XOR. L'index par bande donne l'âge réel de l'image, et
deux bandes d'index différents désignent un déchirement — ce qu'un timecode unique au centre de
l'image ne permet pas de distinguer. Deux dispositions : répartie sur la hauteur, la seule à dire
**quelle** bande décroche ; aux deux extrémités, elle laisse l'image regardable. Les blocs font
encore 3,7 px à l'échelle 1/16. Éteint par défaut, coût nul.

## Timecode

Activé, le générateur incruste l'heure du jour à la place du compteur et publie le **même**
timecode en ANC (ATC RP 188 / RFC 8331), câblable par exemple vers une fenêtre de mur d'images.
L'heure vient de l'horloge du nœud, sur l'échelle PTP, affichée dans le fuseau de la régie. En
genlock, le timecode est calculé sur la grille de présentation de la trame, donc exactement aligné
avec l'image.

Sans PTP fonctionnel sur le nœud, l'heure dérive : l'état PTP du nœud est la première chose à
vérifier devant un timecode douteux.

## Genlock et cadence

Actif par défaut : flash, bip et cadence sont calés sur la grille PTP. Désactivé, la mire tourne
en cadence libre — la mesure de lip-sync reste valable, mais la mire n'est plus comparable aux
autres flux genlockés.

L'audio part par paquets dont la taille vient du réglage du cœur (Réglages → MXL, 1 ms par
défaut) ; le plugin la lit, il ne la décide pas.

## Mode tranche

La trame peut être publiée bande par bande. Le rendu de la mire étant quasi instantané, le gain
propre est faible ici : l'intérêt est de faire tenir le chemin sous-trame de bout en bout sur une
chaîne qui en dépend ailleurs. L'audio et le timing flash/bip sont strictement inchangés.

## Diagnostic

Les métriques `:8080` publient `fps`, `frame_index` et `own_latency_ms` — le coût propre du rendu,
hors attente de cadence, pour vérifier que le générateur lui-même ne dérive pas. `GET :8082/state`
rend la configuration effective complète. Le niveau de journal se règle à chaud.

## Licence

GNU GPL v3 ou ultérieure — voir `LICENSE`.
