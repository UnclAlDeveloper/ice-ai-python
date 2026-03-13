from environments import load_environment
import os

load_environment()

from intuitlib.client import AuthClient
from quickbooks import QuickBooks
from quickbooks.objects.account import Account

# read credentials from environment (set in .env.dev or .env.prod)
client_id = os.getenv("ICE_AI_QUICKBOOKS_CLIENT_ID")
client_secret = os.getenv("ICE_AI_QUICKBOOKS_CLIENT_SECRET")
refresh_token = os.getenv("ICE_AI_QUICKBOOKS_REFRESH_TOKEN")
company_id = os.getenv("ICE_AI_QUICKBOOKS_COMPANY_ID")

# fail fast with clear messages if required values are missing
if not client_id or not client_secret:
    raise ValueError(
        "QuickBooks credentials missing. Set ICE_AI_QUICKBOOKS_CLIENT_ID and "
        "ICE_AI_QUICKBOOKS_CLIENT_SECRET in your .env file (e.g. .env.dev)."
    )
if not refresh_token or refresh_token == "REFRESH_TOKEN":
    raise ValueError(
        "QuickBooks refresh token missing. Set ICE_AI_QUICKBOOKS_REFRESH_TOKEN. "
        "Obtain it by completing the OAuth flow: open the authorize URL in a browser, "
        "sign in, then use the callback code to exchange for tokens (see intuitlib/QuickBooks SDK docs)."
    )
if not company_id or company_id == "COMPANY_ID":
    raise ValueError(
        "QuickBooks company ID missing. Set ICE_AI_QUICKBOOKS_COMPANY_ID. "
        "This is the realm_id returned in the OAuth callback when you connect a company."
    )

auth_client = AuthClient(
    client_id=client_id,
    client_secret=client_secret,
    environment="production",
    redirect_uri="http://localhost:8005/quickbooks-redirect",
)

client = QuickBooks(
    auth_client=auth_client,
    refresh_token=refresh_token,
    company_id=company_id,
)

try:
    accounts = Account.all(qb=client)
    print(f"Retrieved {len(accounts)} account(s).")
except Exception as e:
    print(f"QuickBooks API error: {e}")

pass