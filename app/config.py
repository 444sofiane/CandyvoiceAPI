"""All environment-derived configuration, ported 1:1 from the original
api_server.py constants block. Kept as a single module (rather than
pydantic-settings) so the migration stays a faithful, low-risk port first —
swap in BaseSettings later once this is stable if you want validation.
"""
import os
import uuid

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(SCRIPT_DIR, "uploads")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Must run before any os.environ.get() call below, same reasoning as the
# original file: otherwise .env is silently ignored.
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

# --- Quota model ----------------------------------------------------------
MAX_FILES_PER_FEATURE = int(os.environ.get("MAX_FILES_PER_FEATURE", "10"))
MAX_FILE_DURATION_SECONDS = float(os.environ.get("MAX_FILE_DURATION_SECONDS", "30"))
FILE_DURATION_EPSILON_SECONDS = 0.5

FEATURE_KEY_NOISE_FILTER = "noiseFilter"
FEATURE_KEY_IMITATION = "imitation"
FEATURE_KEY_DEEPFAKE = "deepfake"
FEATURE_KEY_FRAME_RECOVERY = "frameRecovery"

QUOTA_EPSILON = 0.01

# --- Deepfake / imitation / frame-recovery model paths --------------------
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
        "https://candyvoice.com,https://www.candyvoice.com,https://candyvoice.web.app,"
        "http://127.0.0.1:5500,http://localhost:5500,"
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:3000,http://localhost:3000",
    ).split(",")
    if origin.strip()
}

# --- Rate limiting ----------------------------------------------------
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

# --- Upload validation --------------------------------------------------
ALLOWED_AUDIO_MIME_PREFIXES = ("audio/",)
ALLOWED_AUDIO_MIME_EXTRAS = {"video/mp4"}

# --- Download tokens ----------------------------------------------------
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

# Cap on how many pending processedOutputs docs a single call to
# /api/zoho/unsatisfied will attempt to upload, so one webhook invocation
# can't try to process an unbounded backlog in one request.
MAX_UNSATISFIED_ATTACHMENTS = int(os.environ.get("MAX_UNSATISFIED_ATTACHMENTS", "20"))

#---Zoho ------------------------------------------------------------
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN")
ZOHO_ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL")
ZOHO_API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN")
# Must match exactly what's registered in the Zoho API console.
ZOHO_REDIRECT_URI = os.environ.get(
    "ZOHO_REDIRECT_URI", f"http://localhost:{5500}/api/zoho/oauth/callback"
)
ZOHO_UNSATISFIED_WEBHOOK_SECRET = os.environ.get("ZOHO_UNSATISFIED_WEBHOOK_SECRET")
ZOHO_UNSATISFIED_WEBHOOK_HEADER = "X-Webhook-Secret"

# --- Admin reports --------------------------------------------------------
# Emails allowed to call /api/admin/send-report. Client-side (admin-reports.js)
# also checks against this same list for UX gating only — this server-side
# check is the actual access control, since a client-side check alone can
# always be bypassed by calling the API directly.
# TODO: replace with your real admin email(s) before deploying, and/or set
# ADMIN_EMAILS as a comma-separated env var instead of editing this file.
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get(
        "ADMIN_EMAILS"
    ).split(",")
    if email.strip()
}

# Who actually receives the generated report email — defaults to the same
# list as ADMIN_EMAILS, but kept separate in case you ever want the report
# sent somewhere a login-allowed admin isn't (e.g. a shared team inbox).
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
