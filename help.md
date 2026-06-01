# Mire synchro A/V

Génère une mire de test synchronisation audio/vidéo : flash vidéo et bip audio simultanés à intervalles réguliers. Permet de mesurer le décalage lip-sync dans la chaîne.

## Usage

Déployer, câbler la sortie vidéo et audio vers l'encodeur ou le monitoring. Observer le décalage entre le flash visuel et le bip audio sur un analyseur ou un écran avec audio.

## Sorties

- Shm vidéo : mire avec flash blanc périodique
- Shm audio : bip synchrone au flash

## Paramètres

| Paramètre | Description |
|-----------|-------------|
| Intervalle | Fréquence des flashs/bips (secondes) |
| Résolution | Format vidéo de sortie |

## Notes

- Utile pour diagnostiquer le délai introduit par un encodeur, un streamer ou une chaîne complète
- Comparer avec la mire avsync à l'entrée pour isoler le composant responsable du décalage
