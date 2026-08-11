# CandyVoice — API réelle conteneurisée (test d'infra avec imitation.exe via Wine)

Ce dossier prend **ton vrai code** (`FastAPICandyVoice`, tel qu'uploadé) et le
containerise + déploie tel quel sur Kubernetes, en utilisant `imitation.exe`
via Wine comme solution temporaire — pour valider l'infra sans attendre le
binaire Linux. Aucune réécriture : c'est ton `app/` avec **un seul fichier
modifié** (`app/services/detector.py`).

## Le seul changement de code

`resolve_executable()` retournait un `str` (le chemin de l'exe). Il retourne
maintenant une **liste** (`[exe]` ou `["wine", exe]` sur Linux si `wine` est
sur le PATH), et les 4 `build_*_command()` font `[*executable, ...]` au lieu
de `[executable, ...]`. Sur Windows, ou plus tard avec le binaire Linux natif
sans Wine, le comportement est strictement identique à avant (`[*[exe]]`
== `[exe]`). C'est le diff minimal qui laisse tout le reste — quota,
Firestore, Zoho, websocket deepfake, etc. — inchangé.

```diff
- def resolve_executable():
-     ...
-     return candidate
+ def resolve_executable():
+     ...
+     if os.name != "nt" and shutil.which("wine"):
+         return ["wine", candidate]
+     return [candidate]

- command = [executable, "-noiseFilter", "1", ...]
+ command = [*executable, "-noiseFilter", "1", ...]
```
(même remplacement `[executable, ...]` → `[*executable, ...]` dans
`build_deepfake_command` / `build_imitation_command` / `build_frame_recovery_command`)

## Ce que ce test doit prouver (et a fait remonter)

En containerisant le code **sans rien changer d'autre**, deux problèmes que
ton code actuel ne pouvait pas rencontrer (une seule instance, une seule
machine) deviennent visibles dès qu'on passe à plusieurs replicas :

1. **`DOWNLOAD_TOKEN_SECRET` non fixé** → chaque pod tire un secret aléatoire
   au démarrage (`config.py`, ligne ~93). Résultat : un lien de téléchargement
   signé par le pod A est rejeté par le pod B. **Doit** être fixé, identique
   sur tous les pods (`k8s/01-config.yaml`, déjà en place).
2. **`uploads/` et `outputs/` sur disque local du conteneur** →
   `process_flow.py` écrit, `outputs.py` relit, mais rien ne garantit que
   les deux requêtes tombent sur le même pod derrière le Service. Réglé ici
   par un PVC `ReadWriteMany` partagé entre tous les pods (`k8s/02-storage.yaml`).

Un troisième point, non bloquant mais à garder en tête : le rate-limit dans
`deps.py` est un `dict` en mémoire process → avec N pods, la limite de
5 requêtes/60s s'applique **par pod**, pas globalement par utilisateur. Pas
grave pour ce test, mais à corriger (Redis, par ex.) avant un vrai lancement
multi-replica si tu veux un rate-limit réellement global.

## Structure

```
candyvoice-real/
├── app/                      # ton code, inchangé sauf detector.py (voir diff ci-dessus)
├── vendor/
│   ├── README.md             # où mettre ton imitation.exe + Model/ (non fournis ici)
│   └── Model/.gitkeep
├── requirements.txt          # déduit des imports réels du code
├── .env.example               # toutes les variables lues par config.py, avec les warnings qui comptent
├── docker-compose.yml        # test local, 2 replicas, avant de passer sur k8s
├── docker/Dockerfile.api     # Ubuntu + Wine + Python — voir le bloc "à supprimer" en tête du fichier
└── k8s/
    ├── 00-namespace.yaml
    ├── 01-config.yaml        # ConfigMap + 2 Secrets (config générale, secrets, JSON Firebase)
    ├── 02-storage.yaml       # PVC RWX uploads/outputs (voir pourquoi c'est obligatoire, pas optionnel)
    └── 10-api.yaml           # Deployment + Service + HPA + Ingress
```

## Tester en local, avant K8s

```bash
cd candyvoice-real
# 1. dépose ton vrai imitation.exe + Model/ dans vendor/ (voir vendor/README.md)
# 2. dépose ton firebase-adminsdk.json dans ./secrets/
mkdir -p secrets && cp /chemin/vers/ton/firebase-adminsdk.json secrets/
cp .env.example .env   # puis remplis les vraies valeurs Zoho/SMTP2GO/etc.

docker compose up --build --scale api=2
```

Puis, pour vérifier concrètement que le point bloquant est résolu :

```bash
# Traite un fichier via une des deux instances (port 8001 ou 8002)
curl -X POST http://localhost:8001/api/noise-filter \
  -H "Authorization: Bearer <ton_id_token_firebase>" \
  -H "X-File-Name: test.wav" \
  --data-binary @test.wav

# Le lien de téléchargement renvoyé (output_url) doit fonctionner
# QUE TU LE TESTES SUR LE PORT 8001 OU 8002 — c'est exactement ce que
# le volume partagé + DOWNLOAD_TOKEN_SECRET fixe rendent possible.
```

Si tu retires temporairement `DOWNLOAD_TOKEN_SECRET` de `.env` et relances,
tu devrais voir le téléchargement échouer par intermittence selon le pod
qui répond — c'est la preuve en direct du problème n°1 ci-dessus.

## Déployer sur Kubernetes

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl create secret generic candyvoice-firebase-adminsdk \
  --from-file=firebase-adminsdk.json=./secrets/firebase-adminsdk.json -n candyvoice
# édite k8s/01-config.yaml avec tes vraies valeurs (ou passe par --from-literal)
kubectl apply -f k8s/01-config.yaml
kubectl apply -f k8s/02-storage.yaml
kubectl apply -f k8s/10-api.yaml

kubectl get pods -n candyvoice -w
```

Une fois les 2 pods `Ready`, tue-en un (`kubectl delete pod <nom> -n candyvoice`)
pendant qu'une requête est en cours ailleurs : le service continue de
répondre via l'autre pod, et Kubernetes en relance un troisième
automatiquement. C'est la démonstration directe de la disparition du SPOF
initial (une seule machine Windows).

## Quand le binaire Linux sera prêt

1. Dans `vendor/`, remplace `imitation.exe` par `imitation_linux`.
2. Dans `docker/Dockerfile.api`, supprime tout le bloc Wine (voir le
   commentaire en tête du fichier) → l'image repasse sur `python:3.11-slim`,
   build beaucoup plus rapide, image beaucoup plus légère, plus de couche de
   compatibilité en runtime.
3. `resolve_executable()` dans `detector.py` n'a **plus besoin d'être
   retouché** : la branche Wine ne se déclenche que si un fichier appelé
   `imitation.exe` existe — avec `imitation_linux`, la condition
   `os.path.exists(candidate)` sur `imitation.exe` sera simplement fausse. Tu
   peux soit ajouter `imitation_linux` comme second candidat dans
   `resolve_executable()`, soit renommer ton binaire natif en `imitation.exe`
   pour l'instant — au choix, aucune urgence.
4. Le reste (K8s, HPA, PVC, Secrets) ne change pas. C'est justement le point :
   l'infra ne dépend pas de savoir si le binaire tourne nativement ou sous
   Wine.

## Ce qui n'a délibérément pas été touché ici

Pas de refactor vers une queue de jobs (Redis + workers séparés) comme dans
le mockup précédent — ton code actuel traite tout de façon synchrone dans la
requête HTTP (`asyncio.to_thread`), et je n'ai pas voulu mélanger "on
containerise le code existant pour tester l'infra" avec "on change
l'architecture de traitement". Une fois cette étape validée par toi, on peut
regarder si tu veux découpler API/traitement avec une vraie queue — mais ce
n'était pas la question posée ici.
