# CandyVoice API — Clés API

Comment un utilisateur connecté obtient une clé API longue durée pour sa
propre application, comment cette clé se comporte différemment d'un appel
en session Firebase normale, et les endpoints réservés au staff pour
gérer les clés et faire du reporting sur l'usage de tous les
utilisateurs. Complément à
[`API.md`](API.md) — commence là-bas pour les endpoints de traitement
eux-mêmes ; ce document ne couvre que ce qui est spécifique aux clés.

## Pourquoi une clé, et en quoi ça diffère de ta session Firebase

Le site web lui-même continue d'utiliser l'auth Firebase exactement comme
aujourd'hui — rien ne change là-dessus. Une clé est pour le cas où un
utilisateur veut appeler les endpoints de traitement depuis **son propre
backend/app**, en dehors de la session navigateur, sans qu'un humain se
reconnecte à chaque appel.

Appeler avec une clé plutôt qu'un bearer token Firebase change le
comportement du traitement, pas seulement la façon dont tu t'authentifies :

| | Session Firebase | Clé API |
|---|---|---|
| Quota | Plafond fixe à vie, compté au fichier (10 fichiers/fonctionnalité, pas de réinitialisation) | **Budget de secondes par session et par fonctionnalité, selon l'offre** — pas de comptage par fichier ni par mois. Voir [Offres et limites](#offres-et-limites) |
| Limite de débit | 5 req/60s, partagée entre tous tes appels en session Firebase | Par clé, par offre (5–100 req/60s) — voir [Offres et limites](#offres-et-limites) |
| Forme de la réponse | Enveloppe JSON, lien de téléchargement `output_url` (fichier gardé ~1h) | **Octets audio bruts comme corps de réponse** (`audio/wav`), métadonnées dans les en-têtes ; aucun fichier gardé sur le serveur — voir [plus bas](#utiliser-une-clé-avec-les-endpoints-de-traitement) |
| Historique des sorties traitées / synchro Zoho | Sauvegardé | **Entièrement sauté** — rien n'est persisté ni synchronisé |
| Fichier d'entrée uploadé | Gardé brièvement (flux de pièce jointe Zoho) | **Supprimé immédiatement** après la construction de la réponse |

En bref : une clé sert quand "ce sont les données de ma propre
application, traitées et rendues directement" — rien concernant la
requête n'est retenu côté serveur une fois la réponse envoyée, mais
l'usage compte quand même dans l'offre de cette clé.

## Offres et limites

Choisie à la création de la clé (voir `POST /api/keys` plus bas, dont la
réponse ne renvoie que l'offre choisie, pas ses limites) et affichée sur
la page tarifs. Les chiffres ci-dessous sont les valeurs par défaut
provisoires actuelles, pas un contrat stable — traite les champs
`maxSessionSeconds`/`rate_limit` retournés par l'[endpoint
d'usage](#get-apikeyskey_idusage) comme la source de vérité, de la même
façon qu'`API.md` te le dit déjà pour les limites de la session Firebase.

| Offre | Budget / session / outil | Limite de débit |
|---|---|---|
| `starter` | 60s | 5 req/60s |
| `pro` | 3 min | 20 req/60s |
| `enterprise` | 20 min (limites personnalisées : contacte-nous) | 100 req/60s |

Aucune des quatre fonctionnalités (filtre de bruit, imitation,
récupération de trames, détection de deepfake) n'est comptée au fichier
ni au mois avec une clé API — chacune a son propre budget de secondes,
appliqué par **session**, indépendant des trois autres (épuiser le
filtre de bruit ne touche pas ton budget d'imitation). Une "session" est :

- **un seul clip**, pour `/api/noise-filter`, `/api/imitation`,
  `/api/frame-recovery`, et `/api/deepfake-detect` — ces quatre endpoints
  sont one-shot, donc le clip peut faire jusqu'au budget entier de
  l'offre en une fois (ex. jusqu'à 3 minutes sur `pro`, pas juste 30s —
  il n'y a plus de plafond fixe de 30s pour ces chemins avec une clé
  API) ;
- **une connexion WebSocket entière**, pour `/ws/deepfake` — seul
  endroit de l'API où une session peut couvrir plusieurs chunks envoyés
  à la suite, avec un budget qui se cumule sur toute la connexion ; voir
  [plus bas](#websocket-wsdeepfake-avec-une-clé--sessions-multi-chunks).

Le budget se réinitialise à chaque nouvelle session (nouvelle requête
HTTP, ou nouvelle connexion WebSocket) — **pas** chaque mois. L'usage
cumulé est quand même enregistré chaque mois à titre indicatif (visible
dans [l'endpoint d'usage](#get-apikeyskey_idusage) et le rapport admin
mensuel), mais rien n'y est appliqué — c'est toujours le budget par
session qui bloque une requête, jamais un total mensuel.

> **Frontière de confiance :** il n'y a pas encore de vérification de
> paiement, puisque le paiement lui-même n'est encore qu'une maquette —
> donc `POST /api/keys` en self-service (ci-dessous) n'émet que des clés
> `starter`, point final. Rien d'envoyé par le client n'est pris en
> compte pour choisir une offre payante ; une clé ne peut être placée sur
> `pro`/`enterprise` que via les endpoints admin
> ([Administration](#administration-réservé-au-staff)), qui sont le point
> d'intégration naturel pour un futur événement de paiement vérifié côté
> serveur (ex. un webhook Stripe). Tant que ce câblage n'existe pas,
> passer un utilisateur sur une offre payante est une action manuelle
> côté staff.

## Obtenir une clé (self-service, depuis ton frontend)

Ces endpoints prennent le **même token d'ID Firebase** que ton frontend
envoie déjà partout ailleurs — une clé ne peut jamais être créée pour ou
rattachée qu'au propre compte de l'utilisateur connecté, il n'y a aucun
moyen de toucher la clé de quelqu'un d'autre via ceux-ci.

### `POST /api/keys`

Crée une nouvelle clé `starter` pour l'utilisateur connecté. Self-service
ne peut pas produire une clé `pro`/`enterprise` — voir l'encart
"Frontière de confiance" ci-dessus. Si ta page tarifs laisse choisir une
offre payante, ce choix doit déclencher un vrai paiement puis une
attribution d'offre côté admin, pas un appel direct à cet endpoint avec
`"plan": "pro"`.

| En-tête | Requis | Notes |
|---|---|---|
| `Authorization` | requis | `Bearer <firebase_id_token>` |

Corps :

```json
{ "label": "my mobile app" }
```

Les deux champs sont optionnels — `label` vaut `null` par défaut, `plan`
vaut `"starter"` par défaut et **doit** valoir `"starter"` s'il est
fourni ; toute autre valeur (`"pro"`, `"enterprise"`, ou une offre
inconnue) donne un `400`.

```bash
curl -X POST https://api.candyvoice.com/api/keys \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label": "my mobile app"}'
```

```json
{
  "ok": true,
  "key_id": "3f9c2a...e01b",
  "api_key": "cvk_wK8h2s9F3n...Qz",
  "label": "my mobile app",
  "plan": "starter"
}
```

> **`api_key` n'est montrée qu'une seule fois.** Seul son hash est stocké
> côté serveur — il n'y a pas de récupération "j'ai oublié ma clé", juste
> révoquer-et-en-créer-une-nouvelle. Montre-la à l'utilisateur une fois,
> dans un encart copier-coller, et dis-lui de la sauvegarder quelque part
> de sûr (un gestionnaire de mots de passe, le coffre à secrets de sa
> propre app). Traite-la comme un mot de passe : ne la logue jamais, ne la
> mets jamais dans une URL.

### `GET /api/keys`

Liste les propres clés de l'utilisateur connecté — métadonnées
uniquement, la clé brute n'est plus jamais retournée après la création.

```bash
curl https://api.candyvoice.com/api/keys \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

```json
{
  "ok": true,
  "keys": [
    {
      "key_id": "3f9c2a...e01b",
      "label": "my mobile app",
      "plan": "pro",
      "createdAt": "2026-08-12T10:03:00Z",
      "lastUsedAt": "2026-08-12T14:22:11Z",
      "revoked": false
    }
  ]
}
```

Utilise ceci pour afficher une page de paramètres de clés API : label,
badge d'offre, date de création, date de dernière utilisation, et un
bouton de révocation par ligne. Il n'y a pas de "4 derniers caractères" de
la clé à afficher, puisque la valeur brute n'a jamais été stockée —
`label` (défini à la création) est ce sur quoi l'utilisateur s'appuie
pour distinguer ses clés. Relie chaque ligne à l'endpoint d'usage
ci-dessous pour les vraies barres de quota.

### `GET /api/keys/{key_id}/usage`

Usage et limites de la période de facturation en cours pour une clé —
tout ce dont un tableau de bord d'usage a besoin, en un seul appel.

```bash
curl https://api.candyvoice.com/api/keys/3f9c2a...e01b/usage \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

```json
{
  "ok": true,
  "key_id": "3f9c2a...e01b",
  "label": "my mobile app",
  "plan": "pro",
  "revoked": false,
  "period": "2026-08",
  "usage": {
    "noiseFilter": { "secondsUsed": 340, "maxSessionSeconds": 180 },
    "imitation": { "secondsUsed": 0, "maxSessionSeconds": 180 },
    "frameRecovery": { "secondsUsed": 0, "maxSessionSeconds": 180 },
    "deepfake": { "secondsUsed": 812, "maxSessionSeconds": 180 }
  },
  "rate_limit": { "max_requests": 20, "window_seconds": 60 }
}
```

`period` est le mois calendaire UTC en cours (`YYYY-MM`) — l'usage d'une
période passée n'est pas exposé par cet endpoint aujourd'hui, seulement
celui en cours.

Les quatre fonctionnalités ont la même forme : `secondsUsed` est le cumul
purement indicatif de toutes les sessions ce mois-ci (pas un compteur qui
bloque quoi que ce soit — il peut légitimement dépasser
`maxSessionSeconds`, ex. `340` ci-dessus sur un budget de `180`, puisque
c'est la somme de plusieurs sessions distinctes), et `maxSessionSeconds`
est le budget appliqué à **chaque** session — pas un plafond mensuel. Un
gros `secondsUsed` ne rapproche donc pas la clé d'une limite ; c'est
juste le total affiché pour visibilité/facturation.

### `POST /api/keys/{key_id}/revoke`

Révoque immédiatement une des propres clés de l'utilisateur connecté.
Tout appel ultérieur fait avec elle reçoit un `401`.

```bash
curl -X POST https://api.candyvoice.com/api/keys/3f9c2a...e01b/revoke \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

```json
{ "ok": true, "key_id": "3f9c2a...e01b", "revoked": true }
```

Un `key_id` qui n'existe pas, est déjà révoqué, ou appartient à un autre
utilisateur retournent tous le même `404 API key not found` — un
utilisateur ne peut pas s'en servir pour sonder si la clé d'un autre
compte existe.

## Administration (réservé au staff)

Pour l'outillage interne — un tableau de bord support/ops, pas le
frontend orienté client. Protégé par la même liste d'autorisation
`ADMIN_EMAILS` que `/api/admin/send-report` : envoie un token d'ID
Firebase appartenant à un compte admin en `Authorization: Bearer ...`,
comme toute autre route admin. Contrairement à tout ce qui précède, ces
endpoints agissent sur les clés de *n'importe quel* utilisateur, pas
seulement celles de l'appelant.

### `POST /api/admin/api-keys`

Émet une clé pour un `uid` arbitraire — ex. en provisionner une pour le
compte d'un client, ou combler après coup une clé que le support doit
remettre manuellement.

```bash
curl -X POST https://api.candyvoice.com/api/admin/api-keys \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"uid": "uid456", "label": "support-issued", "plan": "pro"}'
```

```json
{ "ok": true, "key_id": "8b1e0f...4a2c", "api_key": "cvk_...", "uid": "uid456", "label": "support-issued", "plan": "pro" }
```

### `GET /api/admin/api-keys?uid=`

Liste les clés d'un utilisateur — même forme que le `GET /api/keys` en
self-service, pour n'importe quel `uid` que tu fournis.

```bash
curl "https://api.candyvoice.com/api/admin/api-keys?uid=uid456" \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN"
```

```json
{ "ok": true, "uid": "uid456", "keys": [ { "key_id": "8b1e0f...4a2c", "label": "support-issued", "plan": "pro", "createdAt": "2026-08-10T09:15:00Z", "lastUsedAt": null, "revoked": false } ] }
```

### `GET /api/admin/api-keys/all`

Chaque clé de tous les utilisateurs, la plus récente d'abord — c'est
celui-ci pour un tableau admin "liste toutes les clés API, leurs offres,
et à qui elles appartiennent". Paginé : passe `?limit=` (50 par défaut,
plafonné à 200) et, pour la page suivante, le `next_cursor` de la réponse
précédente en `?cursor=` ; `next_cursor: null` signifie qu'il n'y en a
plus.

```bash
curl "https://api.candyvoice.com/api/admin/api-keys/all?limit=50" \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN"
```

```json
{
  "ok": true,
  "keys": [
    { "key_id": "3f9c2a...e01b", "uid": "uid123", "email": "alice@example.com", "label": "my mobile app", "plan": "pro", "createdAt": "2026-08-12T10:03:00Z", "lastUsedAt": "2026-08-12T14:22:11Z", "revoked": false },
    { "key_id": "8b1e0f...4a2c", "uid": "uid456", "email": null, "label": "support-issued", "plan": "pro", "createdAt": "2026-08-10T09:15:00Z", "lastUsedAt": null, "revoked": false }
  ],
  "next_cursor": "8b1e0f...4a2c"
}
```

`email` est résolu depuis Firebase Auth en une recherche par lot par page
(`firebase_admin.auth.get_users()`, jusqu'à 100 uids par appel — peu
coûteux même à la taille de page max de 200, qui fait au plus 2 appels)
plutôt qu'une recherche par clé. Il vaut `null` quand cet `uid` n'a plus
de compte Firebase Auth (ex. supprimé) plutôt qu'une requête en échec —
un seul e-mail non résolu ne bloque jamais le reste de la liste.

### `POST /api/admin/api-keys/{key_id}/revoke`

Révoque n'importe quelle clé, peu importe le propriétaire — pour une
demande de support pour une clé perdue/compromise. Même forme de réponse
et comportement `404` que la version self-service ci-dessus, juste sans
la vérification de propriétaire.

```bash
curl -X POST https://api.candyvoice.com/api/admin/api-keys/8b1e0f...4a2c/revoke \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN"
```

```json
{ "ok": true, "key_id": "8b1e0f...4a2c", "revoked": true }
```

### `POST /api/admin/api-keys/{key_id}/plan`

Change directement l'offre d'une clé — un abonnement Enterprise ("sur
mesure") manuel, ou un changement d'offre géré par le support en dehors
du parcours self-service. `plan` doit être l'un de `starter` / `pro` /
`enterprise` (`400` sinon) ; un `key_id` inconnu donne un `404`.

```bash
curl -X POST https://api.candyvoice.com/api/admin/api-keys/8b1e0f...4a2c/plan \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan": "enterprise"}'
```

```json
{ "ok": true, "key_id": "8b1e0f...4a2c", "plan": "enterprise" }
```

### `POST /api/admin/send-api-usage-report`

Envoie par e-mail un récapitulatif par utilisateur de l'usage des clés
API pour un mois calendaire à `ADMIN_REPORT_RECIPIENTS` (la même liste de
diffusion — et la même livraison SMTP2GO — que le `/api/admin/send-report`
propre au site web ; pas nécessairement identique à `ADMIN_EMAILS`, la
liste de *contrôle d'accès* de ces routes). L'usage de chaque clé — les
mêmes compteurs par fonctionnalité que `GET /api/keys/{key_id}/usage`
expose pour une clé — est sommé par utilisateur propriétaire, donc
quelqu'un avec trois clés apparaît comme une seule ligne.

Corps (optionnel) :

```json
{ "period": "2026-07" }
```

`period` est au format `"YYYY-MM"`, vaut par défaut le mois calendaire
UTC en cours si omis, et te permet de récupérer à la demande un mois
passé plutôt que de ne voir que le mois en cours. Une valeur mal formée
donne un `400`.

```bash
curl -X POST https://api.candyvoice.com/api/admin/send-api-usage-report \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

```json
{ "ok": true, "period": "2026-08", "users": 12, "sentTo": ["jl.crebouw@candyvoice.com"] }
```

L'e-mail lui-même porte une pièce jointe `.xlsx` — une ligne par
utilisateur avec de l'usage cette période (e-mail, uid, offre(s), nombre
de clés, une colonne de secondes traitées par fonctionnalité, et un total
en secondes), triée par usage décroissant. Les utilisateurs sans aucune
activité de clé API ce mois-là ne sont pas inclus, pour que la feuille
reste centrée sur l'usage réel plutôt que sur chaque compte inscrit.
L'usage des clés révoquées compte quand même — une clé révoquée en milieu
de mois a quand même consommé du budget pendant qu'elle était active. Ces
secondes sont purement indicatives (voir [Offres et
limites](#offres-et-limites)) — le budget qui bloque réellement une
requête est appliqué par session, pas récupérable depuis ce total mensuel.

En plus de l'e-mail, cet appel écrit un document `apiUsage/{uid}` par
utilisateur possédant au moins une clé (même pour un total à 0, pour ne
pas laisser un compteur du mois précédent affiché comme si c'était le
mois en cours) — `{"period", "noiseFilter": {"secondsUsed"},
"imitation": {...}, "frameRecovery": {...}, "deepfake": {...}}`. C'est ce
document que lit une Cloud Function côté Zoho (`syncApiUsageToZoho`, hors
de ce dépôt) pour pousser ces compteurs vers les champs personnalisés du
contact Zoho correspondant.

> **Pas encore sur un planning.** Cet endpoint doit être déclenché — rien
> ne l'appelle automatiquement une fois par mois. Mets ça en place en
> externe (un `CronJob` k8s qui appelle cet endpoint sur un planning, ou
> tout autre ordonnanceur) si tu veux que ça se déclenche vraiment tout
> seul chaque mois — et donc que la synchro Zoho ci-dessus reste à jour.

## Utiliser une clé avec les endpoints de traitement

Remplace `Authorization: Bearer ...` par `X-API-Key`, sur n'importe lequel
de `/api/noise-filter`, `/api/imitation`, `/api/frame-recovery`,
`/api/deepfake-detect`. Le WebSocket `/ws/deepfake` accepte aussi une clé
API — voir [plus bas](#websocket-wsdeepfake-avec-une-clé--sessions-multi-chunks) — mais sa
mécanique d'auth diffère un peu puisque ce n'est pas un en-tête.

> Envoyer les deux en-têtes sur une même requête n'est pas une façon
> supportée de les combiner — `X-API-Key` est essayé en premier. S'il est
> valide, c'est lui qui est utilisé. S'il est manquant/invalide et qu'un
> bearer token `Authorization` est aussi présent, la requête retombe
> dessus plutôt que d'échouer directement (ex. une clé de test périmée
> laissée à côté d'une vraie session navigateur ne devrait pas bloquer une
> requête qui réussirait autrement comme une connexion normale). Ce n'est
> que quand ni l'un ni l'autre ne fonctionne, ou que `X-API-Key` a été
> envoyé seul et est mauvais, que tu obtiens `401 Invalid or revoked API
> key`.

### Filtre de bruit / imitation / récupération de trames — audio brut, pas du JSON

Ces trois produisent un fichier audio traité. Avec une clé, une réponse
réussie est **les octets audio bruts comme corps HTTP** (`Content-Type:
audio/wav`), pas une enveloppe JSON — les métadonnées voyagent dans les
en-têtes de réponse plutôt que dans des champs JSON :

```bash
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "X-API-Key: cvk_wK8h2s9F3n...Qz" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav \
  -o filtered.wav -D -
```

```
HTTP/1.1 200 OK
Content-Type: audio/wav
X-Exit-Code: 0
X-Uid: uid123
X-Duration-Seconds: 18.2
X-Max-Session-Seconds: 180
X-Plan: pro
<...octets WAV bruts...>
```

Pourquoi pas du JSON avec l'audio encodé en base64 en ligne, comme le
faisait une version antérieure de cet endpoint ? Le base64 ajoute ~33% de
surcharge de taille par-dessus un WAV déjà non compressé — pour un clip
complet de 30 secondes, ça poussait certaines réponses dans la gamme des
dizaines de Mo, ce qui est pénible à la fois pour la mémoire serveur et
le parsing JSON côté client. Les octets bruts évitent ça complètement et
te permettent de streamer/sauvegarder la réponse directement (`-o
filtered.wav` ci-dessus, ou `response.arrayBuffer()` en JS).

`/api/imitation` et `/api/frame-recovery` ajoutent chacun leur propre
en-tête supplémentaire — `X-Voice-Model` / `X-Frame-Recovery-Factor`
respectivement — reflétant les champs supplémentaires `voice_model` /
`frame_recovery_factor` que ces deux-là ajoutent à l'enveloppe JSON sur
le chemin session Firebase.

`X-Max-Session-Seconds` est le budget de cette clé pour cette
fonctionnalité (voir [Offres et limites](#offres-et-limites)) — puisque
chacun de ces trois appels est one-shot, `X-Duration-Seconds` (la durée
du clip lui-même) *est* déjà l'usage de cette session ; il n'y a pas de
compteur "used" séparé à renvoyer. Il n'y a rien à télécharger depuis
`/outputs/` dans ce mode : le fichier a été supprimé du disque juste
après la construction de cette réponse.

Le journalement du total mensuel indicatif (voir [Offres et
limites](#offres-et-limites)) se fait après coup et n'affecte jamais la
réponse — s'il échoue (un pépin Firestore transitoire), c'est
silencieusement loggé côté serveur, sans en-tête d'avertissement côté
client : l'audio a déjà été traité avec succès et ce total n'est de toute
façon jamais ce qui bloque une requête.

**Les erreurs reviennent quand même en JSON** (`{"error": "..."}` ou un
objet plus riche avec `stdout`/`stderr`), exactement comme documenté dans
[`API.md`](API.md#erreurs-quotas-et-limites-de-débit) — seule la réponse
de succès de ces routes diffère selon la méthode d'auth, pas la forme des
erreurs.

### Détection de deepfake (HTTP) — flux NDJSON, même budget par session

`/api/deepfake-detect` n'a pas de sortie audio traitée propre (juste un
score), donc la forme du flux NDJSON reste celle documentée dans
[`API.md`](API.md#post-apideepfake-detect) — JSON, pas de réponse binaire
comme les trois endpoints ci-dessus. Le quota suit la même logique
qu'eux : un appel HTTP one-shot est traité comme **une session d'un seul
clip** (voir [Offres et limites](#offres-et-limites)).

L'événement `result` final n'a plus `files_used`/`max_files` (toujours
`null` avec une clé) mais `plan` et `max_session_seconds` :

```jsonl
{"type": "result", "ok": true, "exit_code": 0, "deepfake_percent": 3.1, "threshold_percent": 50.0, "verdict": "genuine", "uid": "uid123", "duration_seconds": 12.4, "files_used": null, "max_files": null, "plan": "pro", "max_session_seconds": 180}
```

### WebSocket /ws/deepfake avec une clé — sessions multi-chunks

Contrairement aux quatre endpoints HTTP ci-dessus, le WebSocket ne prend
pas de clé via un en-tête — la connexion elle-même n'a pas de "en-têtes
de requête" au sens HTTP une fois le handshake terminé. À la place,
envoie `api_key` au lieu de `token` sur le tout premier message,
[le message `auth`](API.md#wss-wsdeepfake) :

```json
{ "type": "auth", "api_key": "cvk_wK8h2s9F3n...Qz" }
```

C'est le seul endroit de l'API où une "session" couvre plus qu'une seule
requête. Une fois authentifiée par clé API, la connexion **reste ouverte
après un `result`** et accepte un nouveau cycle `start` → frame binaire →
`progress`/`result`, autant de fois que le budget de la session (voir
[Offres et limites](#offres-et-limites)) n'est pas épuisé — c'est
exactement le scénario "détection en direct pendant un appel" : découpe
l'audio du call en clips au fil de l'eau et envoie-les l'un après
l'autre sur la même connexion, sans ré-authentifier à chaque fois.

```json
{ "type": "result", "ok": true, "deepfake_percent": 2.0, "verdict": "genuine", "duration_seconds": 24.0, "plan": "pro", "max_session_seconds": 180, "session_seconds_used": 24.0 }
```

`session_seconds_used` est le cumul de tous les chunks réussis de cette
connexion jusqu'ici (pas juste ce chunk) — surveille-le côté client pour
savoir combien de budget il reste (`max_session_seconds -
session_seconds_used`) avant d'envoyer le prochain `start`.

Ce qui met fin à la session (fermeture de la connexion) :

| Cause | Code de fermeture |
|---|---|
| Le client ferme la connexion (fin normale, ex. l'appel est terminé) | — (initié côté client) |
| Le budget de la session ne permet plus le chunk suivant | `1000` (fermeture normale, côté serveur) |
| Un chunk échoue (audio invalide, l'exe plante, etc.) | `1008` |
| Limite de débit dépassée (chaque chunk compte comme une requête distincte) | `1013` |

Pour une session Firebase (token, pas clé API), le comportement est
inchangé : un seul chunk, puis fermeture — pas de boucle multi-chunks.

`token` et `api_key` ne sont pas censés être envoyés ensemble ; si les
deux sont présents, `api_key` est essayé en premier, avec le même repli
vers `token` en cas de clé invalide que sur le chemin HTTP (voir
l'encart plus haut). Un `api_key` manquant/invalide sans `token` de
secours ferme la connexion avec le même comportement `1013`/violation de
protocole que pour un `token` invalide.

## Erreurs spécifiques aux clés

| Statut | Signification |
|---|---|
| 400 | Upload invalide (voir `API.md`), ou — spécifique aux clés API — ce clip/chunk dépasse ce qu'il reste au budget de la session en cours, sur n'importe laquelle des quatre fonctionnalités (le message précise combien il reste). |
| 401 | Clé API manquante, inconnue, ou révoquée. |
| 429 | Limite de débit dépassée — chaque requête (ou chaque chunk d'une session `/ws/deepfake`) compte, sur les quatre fonctionnalités. Chaque clé a son propre budget indépendant, dimensionné selon son offre, non partagé avec les appels en session Firebase de cet utilisateur ni avec ses autres clés. Il n'y a plus de "quota mensuel" séparé qui puisse déclencher un 429 — le budget par session (400 s'il est dépassé) est la seule limite de volume. |
| 503 | Service de vérification de clé / de quota (Firestore) temporairement indisponible — sûr à réessayer. |

Tout le reste (upload invalide générique en 400, échecs de traitement en
500/504) se comporte pareil quelle que soit la méthode d'auth — voir
[`API.md`](API.md#erreurs-quotas-et-limites-de-débit).
