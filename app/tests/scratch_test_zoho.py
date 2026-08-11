# scratch_test_zoho.py — delete after use
from app.services import zoho
from app import config

secret = config.ZOHO_CLIENT_SECRET
def get_contacts():
    resp = zoho.requests.get(
        f"{zoho.config.ZOHO_API_DOMAIN}/crm/v8/Contacts",
        headers=zoho._auth_headers(),
        params={"fields": "Last_Name,Email", "per_page": 1},
    )
    return resp

def update_contact(contact_id, data):
    resp = zoho.requests.put(
        f"{zoho.config.ZOHO_API_DOMAIN}/crm/v8/Contacts/{contact_id}",
        headers=zoho._auth_headers(),
        json=data,
    )
    return resp


resp = update_contact("997248000000673164", {"data": [{"isFileConfidential": True}]})
print(resp.status_code)
print(resp.json())