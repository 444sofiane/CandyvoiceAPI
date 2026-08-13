# CandyVoice API — Référence développeur

Traitement audio pour produits vocaux : suppression de bruit de fond,
clonage de voix, récupération de trames perdues, et détection de
deepfake vocal par IA. REST classique pour les trois premiers, plus un
endpoint HTTP en streaming et un WebSocket pour la progression de la
détection.

- **URL de base :** `https://api.candyvoice.com`
- **Auth :** token d'ID Firebase, ou une clé API pour un usage serveur-à-serveur — voir [API_KEYS.md](API_KEYS.md)
- **Format :** octets audio bruts en entrée, JSON en sortie

## Vue d'ensemble

Chaque endpoint de traitement prend un fichier audio et retourne un
résultat — pas de file d'attente ni de polling. La détection de deepfake
propose en plus une variante en streaming (NDJSON chunké ou un WebSocket)
pour qu'une UI puisse afficher la progression en direct sur les clips
plus longs.

| Fonctionnalité | Endpoint | À utiliser pour |
|---|---|---|
| Filtre de bruit | `POST /api/noise-filter` | Retirer le bruit de fond d'un enregistrement |
| Imitation vocale | `POST /api/imitation` | Revoicer un clip avec l'une des 18 voix préréglées |
| Récupération de trames | `POST /api/frame-recovery` | Reconstruire des trames audio perdues ou corrompues |
| Détection de deepfake | `POST /api/deepfake-detect` ou `wss:///ws/deepfake` | Évaluer la probabilité qu'un clip soit une voix synthétique |

## Authentification

Chaque appel est rattaché à un utilisateur CandyVoice connecté, de deux
façons possibles :

- **Token d'ID Firebase** (l'auth normale de l'UI web) — authentifie-toi
  avec Firebase Authentication dans le projet CandyVoice, puis envoie le
  token d'ID de cet utilisateur comme bearer token. Usage, quota et
  limitation de débit sont suivis par ID utilisateur (`uid`).
- **Clé API** — pour appeler depuis ton propre backend/app plutôt que
  depuis une session navigateur. Se comporte différemment sur plusieurs
  points (quota/limite de débit suivent une offre choisie plutôt que les
  limites fixes ci-dessous, aucun fichier n'est retenu côté serveur,
  l'audio traité revient en octets bruts plutôt qu'en URL de
  téléchargement) — voir [API_KEYS.md](API_KEYS.md) pour savoir comment en
  obtenir une et ce qui change.

```http
Authorization: Bearer <firebase_id_token>
```
```http
X-API-Key: cvk_...
```

> L'endpoint WebSocket ne peut pas porter d'en-tête `Authorization`
> navigateur sur son handshake, donc il prend le même token comme premier
> message sur la connexion à la place — voir [Détection de deepfake (en direct)](#wss-wsdeepfake).

> Le CORS est restreint aux origines web propres de CandyVoice, donc le
> JavaScript d'un site tiers ne peut pas appeler cette API directement.
> Appeler depuis un backend, un script, ou une app mobile n'est pas
> affecté — le CORS est une restriction propre aux navigateurs.

## Faire une requête

Chaque endpoint de traitement prend le fichier comme **corps binaire
brut** — pas en `multipart/form-data` — plus le nom de fichier original
dans un en-tête.

| En-tête | Requis | Notes |
|---|---|---|
| `Authorization` | requis | `Bearer <firebase_id_token>` |
| `X-File-Name` | requis | Nom de fichier original. Peut aussi être envoyé en paramètre de requête `?file_name=`. |

> **Envoie du WAV.** L'upload est accepté s'il ressemble simplement à de
> l'audio par son contenu, mais la durée du clip est lue depuis un en-tête
> WAV — un fichier non-WAV peut passer la vérification initiale et quand
> même échouer un instant plus tard avec `400 Could not determine audio
> duration`. Transcode en WAV avant d'uploader.
>
> Les clips doivent faire **30 secondes ou moins**. Les fichiers plus
> longs sont rejetés avec un 400 avant que tout traitement ne commence.

```bash
# filtre de bruit, comme exemple travaillé — même forme pour chaque endpoint de traitement
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav
```

## Forme de la réponse

Les trois endpoints synchrones (filtre de bruit, imitation, récupération
de trames) retournent tous la même enveloppe, plus quelques champs
propres à chaque endpoint.

```json
{
  "ok": true,
  "exit_code": 0,
  "output_url": "/outputs/uid123_9f2c..._meeting_filtered.wav?token=eyJhbGciOi...",
  "output_file": "/app/outputs/uid123_9f2c..._meeting_filtered.wav",
  "uid": "uid123",
  "duration_seconds": 18.2,
  "files_used": 4,
  "max_files": 10,
  "stdout": "...",
  "stderr": "",
  "command": ["…diagnostic uniquement…"]
}
```

| Champ | Signification |
|---|---|
| `output_url` | Chemin relatif pour [télécharger le résultat](#get-outputsoutput_name). Porte son propre token à courte durée de vie — à utiliser tel quel. |
| `output_file` | Le chemin du fichier sur le serveur. Informatif uniquement ; pas accessible directement. |
| `files_used` / `max_files` | Le compteur de quota de cette fonctionnalité — voir [Quotas](#erreurs-quotas-et-limites-de-débit). |
| `stdout` / `stderr` / `command` | Diagnostics bruts du moteur de traitement. Utile pour une demande de support ; pas un contrat stable à parser. |

> Les fichiers de sortie sont supprimés automatiquement environ une heure
> après leur création. Télécharge ou transmets `output_url` rapidement
> plutôt que de le stocker pour plus tard.

## Erreurs, quotas et limites de débit

Les erreurs sont en JSON : `{"error": "..."}` pour les échecs simples, ou
un objet plus riche avec `stdout`/`stderr` quand le moteur de traitement
lui-même a échoué.

| Statut | Signification |
|---|---|
| 400 | Requête invalide — nom de fichier manquant, audio non parsable/trop volumineux, valeur de paramètre invalide. |
| 401 | Token Firebase manquant, invalide, ou expiré. |
| 429 | Limite de débit ou quota par fonctionnalité dépassé (le message précise lequel). |
| 500 | Le moteur de traitement a échoué, ou erreur serveur inattendue. |
| 503 | Service de quota temporairement indisponible — sûr à réessayer. |
| 504 | Le traitement a dépassé le timeout serveur de 10 minutes. |

**Limite de débit** — jusqu'à 5 requêtes par 60 secondes, par utilisateur
authentifié, tous endpoints confondus :

```json
{ "error": "Too many requests — max 5 per 60s. Try again shortly." }
```

**Quota par fonctionnalité** — chaque fonctionnalité porte sa propre
allocation de 10 fichiers traités, suivie indépendamment. Épuiser le
filtre de bruit ne touche pas à ton allocation d'imitation ou de
deepfake :

```json
{ "error": "You've used all 10 files allowed for this feature." }
```

---

## Endpoints

### `GET /health`

Vérification de disponibilité non authentifiée — sûre pour les moniteurs
de disponibilité et les sondes de santé de load-balancer.

```bash
curl https://api.candyvoice.com/health
```

```json
{ "ok": true }
```

### `POST /api/noise-filter`

Alias : `/process`, `/api/process`

Retire le bruit de fond d'un clip de parole.

| En-tête | | Notes |
|---|---|---|
| `Authorization` | requis | Bearer token |
| `X-File-Name` | requis | Ou `?file_name=` |
| `X-Output-Name` | optionnel | Ou `?output_file=`. Par défaut `<entrée>_filtered.wav`. |
| `X-Confidential-Check` | optionnel | `true` évite de sauvegarder une copie dans ton historique de sorties traitées. |

```bash
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav
```

Réponse : [l'enveloppe standard](#forme-de-la-réponse), sans champ supplémentaire.

### `POST /api/imitation`

Alias : `/api/voice-imitation`

Revoice la parole d'entrée avec l'un des dix-huit modèles de voix
préréglés.

| En-tête | | Notes |
|---|---|---|
| `Authorization` | requis | Bearer token |
| `X-File-Name` | requis | Ou `?file_name=` |
| `X-Voice-Model` | requis | Un des ID de modèle ci-dessous. |
| `X-Output-Name` | optionnel | Ou `?output_file=` |
| `X-Confidential-Check` | optionnel | `true` évite la sauvegarde dans ton historique de sorties. |

**Modèles de voix :** `model_barack` `model_chloe` `model_cortana` `model_degaulle`
`model_dombasle` `model_etienne` `model_frederic` `model_isabelle`
`model_jeanne` `model_JLS` `model_marine` `model_mbappe` `model_michelleo`
`model_mitterrand` `model_pierre` `model_tatiana` `model_trump` `model_valentin`

```bash
curl -X POST https://api.candyvoice.com/api/imitation \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: line.wav" \
  -H "X-Voice-Model: model_trump" \
  --data-binary @line.wav
```

Réponse : [l'enveloppe standard](#forme-de-la-réponse) plus `"voice_model": "model_trump"`.

### `POST /api/frame-recovery`

Alias : `/api/frameRecovery`

Reconstruit les trames perdues ou corrompues d'un clip.

| En-tête | | Notes |
|---|---|---|
| `Authorization` | requis | Bearer token |
| `X-File-Name` | requis | Ou `?file_name=` |
| `X-Frame-Recovery-Factor` | requis | Nombre, `0 < facteur ≤ 0.5` |
| `X-Output-Name` | optionnel | Ou `?output_file=` |
| `X-Confidential-Check` | optionnel | `true` évite la sauvegarde dans ton historique de sorties. |

```bash
curl -X POST https://api.candyvoice.com/api/frame-recovery \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: dropout.wav" \
  -H "X-Frame-Recovery-Factor: 0.3" \
  --data-binary @dropout.wav
```

Réponse : [l'enveloppe standard](#forme-de-la-réponse) plus `"frame_recovery_factor": 0.3`.

### `POST /api/deepfake-detect`

Alias : `/api/deepfake`, `/api/deepfake-detection`

Évalue la probabilité qu'un clip soit de la parole synthétique
("deepfake"). Contrairement aux endpoints ci-dessus, celui-ci envoie la
progression en streaming au format JSON délimité par sauts de ligne
(`application/x-ndjson`) plutôt que de retourner un seul objet JSON.

| En-tête | | Notes |
|---|---|---|
| `Authorization` | requis | Bearer token |
| `X-File-Name` | requis | Ou `?file_name=` |

```bash
curl -N -X POST https://api.candyvoice.com/api/deepfake-detect \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: clip.wav" \
  --data-binary @clip.wav
```

```jsonl
{"type": "info", "total_frames": 2582, "estimated_duration_sec": 25.8}
{"type": "progress", "percent_processed": 3.9, "elapsed_sec": 1.0, "instant_percent": 0.0, "average_percent": 0.0}
… une ligne "progress" par seconde d'audio traité …
{"type": "result", "ok": true, "exit_code": 0, "deepfake_percent": 0.0, "threshold_percent": 50.0, "verdict": "genuine", "uid": "uid123", "duration_seconds": 25.8, "files_used": 5, "max_files": 10}
```

| `type` | Quand | Champs |
|---|---|---|
| `warning` | 0 ou plus, informatif | `message` |
| `info` | 0–1, une fois le nombre de trames connu | `total_frames`, `estimated_duration_sec` |
| `progress` | 0 ou plus, pendant le traitement | `percent_processed`, `elapsed_sec`, `instant_percent`, `average_percent` |
| `result` | exactement 1, terminal | `deepfake_percent`, `threshold_percent`, `verdict` ("genuine" / "synthetic"), `files_used`, `max_files` |
| `error` | terminal, à la place de `result` | `error`, `stdout` |

### `WSS /ws/deepfake`

Le même détecteur de deepfake que ci-dessus, sur un WebSocket — mieux
adapté que le HTTP chunké pour une barre de progression en direct dans
une UI.

**Protocole :**

1. Ouvre `wss://api.candyvoice.com/ws/deepfake`.
2. Envoie `{"type": "auth", "token": "<firebase_id_token>"}`.
3. Reçois `{"type": "auth_ok"}` — ou une frame d'erreur suivie d'une fermeture.
4. Envoie `{"type": "start", "file_name": "clip.wav"}`.
5. Envoie une frame binaire : les octets bruts du fichier.
6. Reçois zéro ou plusieurs frames `warning` / `info` / `progress`, mêmes
   formes que l'endpoint HTTP en streaming.
7. Reçois exactement une frame finale `result` ou `error`, puis le
   serveur ferme la connexion.

```javascript
const ws = new WebSocket("wss://api.candyvoice.com/ws/deepfake");

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "auth", token: firebaseIdToken }));
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  // une fois l'auth confirmée, lance le job
  if (msg.type === "auth_ok") {
    ws.send(JSON.stringify({ type: "start", file_name: "clip.wav" }));
    ws.send(fileBytes); // ArrayBuffer / Blob, juste après "start"
    return;
  }

  if (msg.type === "progress") updateProgressBar(msg.percent_processed);
  if (msg.type === "result" || msg.type === "error") ws.close();
};
```

**Codes de fermeture :**

| Code | Signification |
|---|---|
| 4403 | En-tête `Origin` navigateur présent et absent de la liste d'autorisation. |
| 4401 | Violation de protocole — un message inattendu là où `auth` ou `start` était requis. |
| 1013 | Limité en débit — même budget de 5 req/60s que les endpoints HTTP. |
| 1008 | Upload rejeté (audio invalide, quota dépassé, etc.) — vérifie la frame `error` envoyée juste avant la fermeture. |

> La vérification d'origine `4403` ne se déclenche que quand un en-tête
> `Origin` est présent — c'est une chose de navigateur. Les clients
> WebSocket serveur-à-serveur qui n'en envoient pas ne sont pas
> restreints par elle.

### `GET /outputs/{output_name}`

Télécharge un fichier produit par n'importe quel endpoint ci-dessus.

**Autorisation — l'un des deux :**

| Méthode | Notes |
|---|---|
| `?token=` | Le token signé à courte durée de vie déjà intégré dans `output_url` — utilise l'URL telle que retournée. C'est ce qu'une balise `<audio>` ou `<a download>` doit utiliser, puisqu'elles ne peuvent pas joindre d'en-tête Authorization. |
| En-tête Authorization | Un bearer token appartenant au même `uid` que celui qui a produit le fichier. |

```bash
curl -L "https://api.candyvoice.com/outputs/uid123_9f2c..._meeting_filtered.wav?token=eyJhbGciOi..." \
  -o meeting_filtered.wav
```

> "Le fichier n'existe pas" et "tu n'es pas autorisé à le voir" retournent
> tous deux un simple `404`, à dessein — un échec de permission ici n'a
> pas l'air différent d'une faute de frappe dans le nom de fichier.

---

## Tous les endpoints

| Méthode | Chemin | Auth |
|---|---|---|
| GET | `/health` | — |
| POST | `/api/noise-filter` | Bearer |
| POST | `/api/imitation` | Bearer |
| POST | `/api/frame-recovery` | Bearer |
| POST | `/api/deepfake-detect` | Bearer |
| WSS | `/ws/deepfake` | Premier message |
| GET | `/outputs/{output_name}` | Token ou Bearer |

Les limites indiquées (plafond de clip à 30s, 10 fichiers par
fonctionnalité, 5 req/60s) sont les valeurs par défaut actuelles du
serveur et peuvent être ajustées avec le temps — traite le message
d'erreur d'une réponse 400/429 comme la source de vérité.
