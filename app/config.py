"""Toute la configuration dérivée des variables d'environnement, portée
1:1 depuis le bloc de constantes original de api_server.py. Gardé comme
un seul module (plutôt que pydantic-settings) pour que la migration
reste d'abord un portage fidèle et à faible risque — bascule vers
BaseSettings plus tard une fois que c'est stable si tu veux la validation.
"""
import os
import uuid

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(SCRIPT_DIR, "uploads")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Doit s'exécuter avant tout appel à os.environ.get() ci-dessous, même
# raison que le fichier original : sinon .env est ignoré silencieusement.
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8001"))

# --- Firebase -----------------------------------------------------------
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT_JSON",
    os.path.join(SCRIPT_DIR, "candyvoice-f0ceff8ce151.json"),
)
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "candyvoice")
FIREBASE_SERVICE_ACCOUNT_AVAILABLE = os.path.exists(FIREBASE_SERVICE_ACCOUNT_JSON)

# --- Modèle de quota --------------------------------------------------------
MAX_FILES_PER_FEATURE = int(os.environ.get("MAX_FILES_PER_FEATURE", "10"))
MAX_FILE_DURATION_SECONDS = float(os.environ.get("MAX_FILE_DURATION_SECONDS", "30"))
# Deepfake tolère des clips plus longs que les trois autres fonctionnalités
# (voir le miroir côté client MAX_FILE_DURATION_SECONDS=60 dans deepfake.js).
MAX_FILE_DURATION_SECONDS_DEEPFAKE = float(
    os.environ.get("MAX_FILE_DURATION_SECONDS_DEEPFAKE", "60")
)
FILE_DURATION_EPSILON_SECONDS = 0.5

FEATURE_KEY_NOISE_FILTER = "noiseFilter"
FEATURE_KEY_IMITATION = "imitation"
FEATURE_KEY_DEEPFAKE = "deepfake"
FEATURE_KEY_FRAME_RECOVERY = "frameRecovery"

QUOTA_EPSILON = 0.01

# --- Chemins des modèles deepfake / imitation / frame-recovery --------------
DEEPFAKE_NEURONE_DIR = os.environ.get(
    "DEEPFAKE_NEURONE_DIR", os.path.join(SCRIPT_DIR, "imitation", "Model")
)
DEEPFAKE_NEURONE_FILE = os.environ.get(
    "DEEPFAKE_NEURONE_FILE",
    os.path.join(SCRIPT_DIR, "imitation", "Model", "ImitationNRFr.dat"),
)
DEEPFAKE_THRESHOLD_PERCENT = float(os.environ.get("DEEPFAKE_THRESHOLD_PERCENT", "50"))

FRAME_RECOVERY_FACTOR_MAX = float(os.environ.get("FRAME_RECOVERY_FACTOR_MAX", "0.5"))

IMITATION_MODEL_DIR = DEEPFAKE_NEURONE_DIR
IMITATION_NEURONE_FILE = os.environ.get(
    "IMITATION_NEURONE_FILE", os.path.basename(DEEPFAKE_NEURONE_FILE)
)

ALLOWED_VOICE_MODELS = {
    "model_barack", "model_chloe", "model_cortana", "model_degaulle",
    "model_dombasle", "model_etienne", "model_frederic", "model_isabelle",
    "model_jeanne", "model_JLS", "model_marine", "model_mbappe",
    "model_michelleo", "model_mitterrand", "model_pierre", "model_tatiana",
    "model_trump", "model_valentin",
}

# --- CORS -------------------------------------------------------------
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://candyvoice.com,https://www.candyvoice.com,https://candyvoice.web.app,https://demotechno.candyvoice.com,"
        "http://127.0.0.1:5500,http://localhost:5500,"
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:3000,http://localhost:3000",
    ).split(",")
    if origin.strip()
}

# --- Rate limiting ----------------------------------------------------
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

# --- Validation des uploads --------------------------------------------------
ALLOWED_AUDIO_MIME_PREFIXES = ("audio/",)
ALLOWED_AUDIO_MIME_EXTRAS = {"video/mp4"}

# Plafond strict sur la taille d'un upload, appliqué pendant que le corps
# est encore en train d'être lu (voir audio.read_limited_upload) plutôt
# qu'une fois qu'il a déjà été entièrement bufferisé. Assez généreux pour
# un WAV non compressé, à bitrate élevé, de MAX_FILE_DURATION_SECONDS,
# tout en bornant la mémoire/le disque utilisés par upload.
MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_BYTES", str(50 * 1024 * 1024)))

# --- Tokens de téléchargement ----------------------------------------------------
DOWNLOAD_TOKEN_SECRET = os.environ.get("DOWNLOAD_TOKEN_SECRET")
if not DOWNLOAD_TOKEN_SECRET:
    DOWNLOAD_TOKEN_SECRET = uuid.uuid4().hex
    print(
        "WARNING: DOWNLOAD_TOKEN_SECRET is not set — using a random secret for this "
        "process only. Every previously issued download link will stop working on "
        "restart. Set DOWNLOAD_TOKEN_SECRET in production."
    )
DOWNLOAD_TOKEN_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TOKEN_TTL_SECONDS", str(60 * 60)))
OUTPUT_FILE_TTL_SECONDS = int(os.environ.get("OUTPUT_FILE_TTL_SECONDS", str(60 * 60)))

# Plafond sur le nombre de documents processedOutputs en attente qu'un seul
# appel à /api/zoho/unsatisfied va tenter d'uploader, pour qu'un seul
# déclenchement du webhook ne puisse pas essayer de traiter un backlog
# illimité en une seule requête.
MAX_UNSATISFIED_ATTACHMENTS = int(os.environ.get("MAX_UNSATISFIED_ATTACHMENTS", "20"))

#---Zoho ------------------------------------------------------------
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN")
ZOHO_ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL")
ZOHO_API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN")
# Doit correspondre exactement à ce qui est enregistré dans la console API Zoho.
ZOHO_REDIRECT_URI = os.environ.get(
    "ZOHO_REDIRECT_URI", f"http://localhost:{8001}/api/zoho/oauth/callback"
)
ZOHO_UNSATISFIED_WEBHOOK_SECRET = os.environ.get("ZOHO_UNSATISFIED_WEBHOOK_SECRET")
ZOHO_UNSATISFIED_WEBHOOK_HEADER = "X-Webhook-Secret"

# --- Offres de clés API (paliers tarifaires SaaS) ---------------------------
# Chiffres provisoires correspondant aux paliers "$X"/"$Y" de la page
# tarifs — ajuste-les librement, rien d'autre ne dépend des valeurs
# exactes. Contrairement au chemin session Firebase (MAX_FILES_PER_FEATURE
# ci-dessus, un plafond fixe à vie compté au fichier), l'usage par clé API
# n'est pas compté au fichier mais au budget de secondes d'une "session"
# par fonctionnalité — un appel /ws/deepfake en direct (cumulé sur tous
# ses chunks — la seule fonctionnalité multi-chunks), ou un seul clip pour
# les trois autres endpoints en one-shot. Voir api_key_quota.py pour
# l'enforcement (temps réel, pas de réservation Firestore) et le
# reporting (secondes cumulées loguées chaque mois à titre indicatif).
# Les limites de débit sont la colonne "limites de débit" de la page
# tarifs.
API_KEY_PLANS = {
    "starter": {
        "max_session_seconds": 60,
        "rate_limit_max_requests": 5,
        "rate_limit_window_seconds": 60,
    },
    "pro": {
        "max_session_seconds": 180,
        "rate_limit_max_requests": 20,
        "rate_limit_window_seconds": 60,
    },
    "enterprise": {
        # "Sur mesure" sur la page tarifs — pas de plafond fixe ici pour
        # l'instant (aucun stockage de limite personnalisée par clé
        # n'existe), juste une valeur par défaut généreuse. Une vraie
        # offre "sur mesure" nécessiterait une surcharge par clé, pas une
        # valeur de palier partagée ; TODO si/quand ça devient nécessaire.
        "max_session_seconds": 1200,
        "rate_limit_max_requests": 100,
        "rate_limit_window_seconds": 60,
    },
}
DEFAULT_API_KEY_PLAN = "starter"

# --- Rapports admin --------------------------------------------------------
# E-mails autorisés à appeler /api/admin/send-report. Le côté client
# (admin-reports.js) vérifie aussi cette même liste, mais uniquement pour
# le confort d'usage — cette vérification côté serveur est le vrai
# contrôle d'accès, puisqu'une vérification côté client seule peut
# toujours être contournée en appelant l'API directement.
# TODO : remplace par ton/tes véritable(s) e-mail(s) admin avant de
# déployer, et/ou définis ADMIN_EMAILS comme variable d'environnement
# séparée par des virgules plutôt que de modifier ce fichier.
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get(
        "ADMIN_EMAILS", "jl.crebouw@candyvoice.com,s.saou@candyvoice.com,m.iwanow@candyvoice.com"
    ).split(",")
    if email.strip()
}

# Qui reçoit vraiment l'e-mail de rapport généré — par défaut la même
# liste que ADMIN_EMAILS, mais gardée séparée au cas où tu voudrais un
# jour envoyer le rapport quelque part où un admin autorisé à se
# connecter ne l'est pas (ex. une boîte mail d'équipe partagée).
ADMIN_REPORT_RECIPIENTS = [
    email.strip()
    for email in os.environ.get("ADMIN_REPORT_RECIPIENTS", ",".join(ADMIN_EMAILS)).split(",")
    if email.strip()
]

# --- SMTP2GO --------------------------------------------------------------
SMTP2GO_HOST = os.environ.get("SMTP2GO_HOST", "mail.smtp2go.com")
SMTP2GO_PORT = int(os.environ.get("SMTP2GO_PORT", "587"))
SMTP2GO_USERNAME = os.environ.get("SMTP2GO_USERNAME")
SMTP2GO_PASSWORD = os.environ.get("SMTP2GO_PASSWORD")
SMTP2GO_FROM_EMAIL = os.environ.get("SMTP2GO_FROM_EMAIL", "reports@candyvoice.com")
SMTP2GO_FROM_NAME = os.environ.get("SMTP2GO_FROM_NAME", "CandyVoice Reports")
