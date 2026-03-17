import pyotp
import json
import os
import datetime
from growwapi import GrowwAPI
from config import GROWW_TOTP_TOKEN, GROWW_TOTP_SECRET

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".access_token.json")

def get_access_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == str(datetime.date.today()):
            print("Using cached token ✅")
            return data["token"]

    print("Generating fresh token...")
    code  = pyotp.TOTP(GROWW_TOTP_SECRET).now()
    token = GrowwAPI.get_access_token(api_key=GROWW_TOTP_TOKEN, totp=code)

    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token, "date": str(datetime.date.today())}, f)

    print("Fresh token generated ✅")
    return token

if __name__ == "__main__":
    t = get_access_token()
    print(f"Token: {t[:30]}...")