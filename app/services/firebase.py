"""Amorçage de Firebase Admin, vérification du token d'ID, et accès au
client Firestore. Porté 1:1 depuis api_server.py."""
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app import config

GOOGLE_REQUEST = google_requests.Request()


class FirestoreUnavailableError(RuntimeError):
    pass


def init_firebase_admin():
    if firebase_admin._apps:
        return
    if config.FIREBASE_SERVICE_ACCOUNT_AVAILABLE:
        cred = credentials.Certificate(config.FIREBASE_SERVICE_ACCOUNT_JSON)
        firebase_admin.initialize_app(cred, {"projectId": config.FIREBASE_PROJECT_ID})


def verify_firebase_id_token(token):
    if config.FIREBASE_SERVICE_ACCOUNT_AVAILABLE:
        from firebase_admin import auth as firebase_auth

        return firebase_auth.verify_id_token(token)

    return google_id_token.verify_firebase_token(
        token, GOOGLE_REQUEST, audience=config.FIREBASE_PROJECT_ID
    )


def get_firestore_client():
    if not config.FIREBASE_SERVICE_ACCOUNT_AVAILABLE:
        raise FirestoreUnavailableError(
            "Server-side quota enforcement requires a Firebase service-account JSON file. "
            "Set FIREBASE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS to a valid "
            "file before starting the API."
        )
    if not firebase_admin._apps:
        init_firebase_admin()
    return admin_firestore.client()
