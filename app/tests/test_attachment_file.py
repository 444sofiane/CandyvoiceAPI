# app/tests/scratch_test_zoho_attachment.py
from app import config
from app.services import zoho

CONTACT_ID = "997248000000673164"  # le contact "Marrier (Sample)" de ton dernier test

# 1. Crée un petit fichier factice à uploader
dummy_path = "dummy_test_file.txt"
with open(dummy_path, "w") as f:
    f.write("CandyVoice Zoho attachment test — safe to delete.")

# 2. Upload-le via la fonction de service existante
try:
    result = zoho.upload_file_to_zoho(dummy_path, CONTACT_ID, "dummy_test_file.txt")
    print("Upload response:")
    print(result)
except Exception as e:
    print("Upload failed:", e)

# 3. Vérifie en listant les pièces jointes du contact
verify_resp = zoho.requests.get(
    f"{config.ZOHO_API_DOMAIN}/crm/v8/Contacts/{CONTACT_ID}/Attachments",
    headers=zoho._auth_headers(),
    params={"fields": "File_Name,Size,Created_Time"},
)
print(verify_resp.status_code)
print(verify_resp.json())