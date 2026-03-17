from auth import get_access_token
token = get_access_token()
print(f"Token works: {token[:30]}...")