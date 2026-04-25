import base64
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request as HttpRequest, urlopen
from urllib.error import URLError, HTTPError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from intuitlib.client import AuthClient

from backup_databases import run_backups
from common import save_oauth_tokens
from environments import load_environment

load_environment()

logger = logging.getLogger(__name__)


# EBAY OAUTH SCOPES
# Space-separated scopes for authorise URL; must cover Browse (Python downloader) and Sell Inventory (website listings).
EBAY_OAUTH_SCOPES = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.account"
)


# EBAY OAUTH ENDPOINTS
def _ebay_oauth_endpoints() -> tuple[str, str]:
    """
    Return (authorize_url_prefix, token_endpoint) for production or sandbox.
    Sandbox is selected when AUTO_ADS_EBAY_API_ROOT contains 'sandbox' or AUTO_ADS_EBAY_SANDBOX is true.
    """

    root = (os.getenv("AUTO_ADS_EBAY_API_ROOT") or "").lower()
    flag = (os.getenv("AUTO_ADS_EBAY_SANDBOX") or "").lower() in ("1", "true", "yes")
    if "sandbox" in root or flag:
        return (
            "https://auth.sandbox.ebay.com/oauth2/authorize",
            "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        )
    return (
        "https://auth.ebay.com/oauth2/authorize",
        "https://api.ebay.com/identity/v1/oauth2/token",
    )


# LIFESPAN
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan that owns the nightly backup scheduler.

    On startup, registers run_backups() to fire daily at 02:00 UTC and starts
    the scheduler. On shutdown, stops the scheduler without waiting for any
    in-flight job. The job is only registered when POSTGRESQL_BACKUPS_BUCKET
    is configured so local development runs do not attempt nightly backups.
    """

    scheduler = AsyncIOScheduler(timezone="UTC")

    # only register the nightly backup when a destination bucket is configured
    if os.getenv("POSTGRESQL_BACKUPS_BUCKET"):
        scheduler.add_job(
            run_backups,
            CronTrigger(hour=2, minute=0),
            id="nightly-postgres-backup",
            coalesce=True,
            misfire_grace_time=3600,
            max_instances=1,
        )
        logger.info("Nightly PostgreSQL backup scheduled for 02:00 UTC")
    else:
        logger.info(
            "POSTGRESQL_BACKUPS_BUCKET not set; skipping nightly backup scheduling"
        )

    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


# ICE AI API
app = FastAPI(
    title="Ice AI API",
    description="API endpoints for Ice AI applications including Auto Ads eBay integration.",
    version="1.0.0",
    lifespan=lifespan,
)


# EBAY CONNECT
@app.get("/ebay-connect")
async def ebay_connect() -> RedirectResponse:
    """
    Initiate the eBay OAuth flow by redirecting to eBay's authorisation page.
    After the user authorises, eBay redirects back to /auto-ads-ebay-redirect with
    an authorisation code that can be exchanged for access and refresh tokens.
    """

    client_id = os.getenv("AUTO_ADS_EBAY_CLIENT_ID")
    redirect_uri = os.getenv("AUTO_ADS_EBAY_REDIRECT_URL")
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="AUTO_ADS_EBAY_CLIENT_ID or AUTO_ADS_EBAY_REDIRECT_URL not configured",
        )

    authorize_base, _token_url = _ebay_oauth_endpoints()
    raw_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": EBAY_OAUTH_SCOPES,
        "state": "auto-ads-connect",
    }

    # log each parameter before encoding
    print("eBay OAuth parameters (raw):")
    for key, value in raw_params.items():
        print(f"  {key}: {value}")

    params = urlencode(raw_params)
    auth_url = f"{authorize_base}?{params}"

    print(f"Redirecting to eBay OAuth: {auth_url}")
    return RedirectResponse(url=auth_url)


# AUTO ADS EBAY REDIRECT
@app.get("/auto-ads-ebay-redirect")
async def auto_ads_ebay_redirect(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> HTMLResponse:
    """
    Handle the OAuth redirect callback from eBay.
    Exchanges the authorisation code for access and refresh tokens, then stores
    them in the database for use by other processes.
    """

    print("eBay redirect received - parameters:")
    print(f"  code: {code[:20]}..." if code and len(code) > 20 else f"  code: {code}")
    print(f"  state: {state}")
    print(f"  error: {error}")
    print(f"  error_description: {error_description}")

    # handle error response from ebay
    if error:
        error_msg = error_description or error
        raise HTTPException(
            status_code=400,
            detail=f"eBay authorisation failed: {error_msg}",
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing authorisation code from eBay",
        )

    # exchange the authorisation code for tokens
    client_id = os.getenv("AUTO_ADS_EBAY_CLIENT_ID")
    client_secret = os.getenv("AUTO_ADS_EBAY_CLIENT_SECRET")
    redirect_uri = os.getenv("AUTO_ADS_EBAY_REDIRECT_URL")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="eBay client credentials or redirect URL not configured",
        )

    # post to ebay's token endpoint with basic auth header
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    token_data = urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()

    _authorize_base, token_endpoint = _ebay_oauth_endpoints()
    token_request = HttpRequest(
        token_endpoint,
        data=token_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
    )

    try:
        with urlopen(token_request) as response:
            token_response = json.loads(response.read().decode())
    except (HTTPError, URLError) as e:
        error_body = ""
        if hasattr(e, "read"):
            error_body = e.read().decode()
        print(f"eBay token exchange failed: {e} {error_body}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to exchange authorisation code for tokens: {e}",
        )

    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in", 7200)
    refresh_expires_in = token_response.get("refresh_token_expires_in", 47304000)

    now = datetime.now(timezone.utc)
    access_token_expiry = now + timedelta(seconds=expires_in)
    refresh_token_expiry = now + timedelta(seconds=refresh_expires_in)

    print("eBay token exchange successful")

    # persist tokens to the database
    save_oauth_tokens(
        provider="ebay",
        access_token=access_token,
        access_token_expiry=access_token_expiry,
        refresh_token=refresh_token,
        refresh_token_expiry=refresh_token_expiry,
    )

    # return success page confirming the connection
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Auto Ads - eBay Connected</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            }
            .container {
                background: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                text-align: center;
                max-width: 400px;
            }
            h1 { color: #333; margin-bottom: 10px; }
            p { color: #666; line-height: 1.6; }
            .success { color: #22c55e; font-size: 48px; margin-bottom: 20px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success">&#10003;</div>
            <h1>eBay Connected</h1>
            <p>Tokens have been exchanged and saved to the database. You can close this window.</p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# AUTO ADS EBAY MARKETPLACE ACCOUNT DELETION CHALLENGE
@app.get("/autoads-ebay-marketplace-account-deletion")
async def autoads_ebay_marketplace_account_deletion_challenge(
    challenge_code: Optional[str] = None,
) -> JSONResponse:
    """
    Handle eBay's challenge code verification for marketplace account deletion subscription.
    eBay sends a GET request with a challenge_code to verify endpoint ownership.
    The response must be a SHA-256 hash of: challengeCode + verificationToken + endpoint.
    """

    # validate challenge code is present
    if not challenge_code:
        raise HTTPException(
            status_code=400,
            detail="Missing challenge_code parameter",
        )

    # get verification token and endpoint from environment
    verification_token = os.getenv("AUTO_ADS_EBAY_VERIFICATION_TOKEN")
    api_base_url = os.getenv("ICE_AI_API_URL", "")
    endpoint = os.getenv(
        "AUTO_ADS_EBAY_DELETION_ENDPOINT",
        f"{api_base_url}/autoads-ebay-marketplace-account-deletion",
    )

    if not verification_token:
        raise HTTPException(
            status_code=500,
            detail="Verification token not configured",
        )

    # compute sha-256 hash of challengeCode + verificationToken + endpoint
    hash_input = challenge_code + verification_token + endpoint
    challenge_response = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    print(f"eBay challenge received. Responding with hash for endpoint: {endpoint}")

    # return the challenge response as json with correct content-type
    return JSONResponse(
        content={"challengeResponse": challenge_response},
        media_type="application/json",
    )


# AUTO ADS EBAY MARKETPLACE ACCOUNT DELETION NOTIFICATION
@app.post("/autoads-ebay-marketplace-account-deletion")
async def autoads_ebay_marketplace_account_deletion_notification(
    request: Request,
) -> JSONResponse:
    """
    Handle eBay marketplace account deletion notifications.
    eBay sends a POST request when a user requests deletion of their personal data.
    The endpoint must acknowledge with HTTP 200 and then process the deletion.
    """

    # parse the notification payload
    try:
        payload = await request.json()
    except Exception as e:
        print(f"Failed to parse notification payload: {e}")
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload",
        )

    # extract notification data
    metadata = payload.get("metadata", {})
    notification = payload.get("notification", {})
    data = notification.get("data", {})

    topic = metadata.get("topic")
    notification_id = notification.get("notificationId")
    event_date = notification.get("eventDate")
    username = data.get("username")
    user_id = data.get("userId")
    eias_token = data.get("eiasToken")

    # log the notification details
    print(f"eBay Account Deletion Notification received:")
    print(f"  Topic: {topic}")
    print(f"  Notification ID: {notification_id}")
    print(f"  Event Date: {event_date}")
    print(f"  Username: {username}")
    print(f"  User ID: {user_id}")
    print(f"  EIAS Token: {eias_token}")

    # TODO: Implement actual account deletion logic
    # This should:
    # 1. Look up user data associated with userId/username/eiasToken
    # 2. Delete all stored personal data for this user
    # 3. Revoke any stored access tokens
    # 4. Log the deletion for compliance records

    # acknowledge receipt immediately with 200 OK
    return JSONResponse(
        content={"status": "acknowledged", "notificationId": notification_id},
        status_code=200,
    )


# QUICKBOOKS CONNECT
@app.get("/quickbooks-connect")
async def quickbooks_connect() -> RedirectResponse:
    """
    Initiate the QuickBooks OAuth flow by redirecting to Intuit's authorisation page.
    After the user authorises, Intuit redirects back to /quickbooks-redirect with
    the authorisation code and realmId (company ID).
    """

    client_id = os.getenv("ICE_AI_QUICKBOOKS_CLIENT_ID")
    redirect_uri = os.getenv("ICE_AI_QUICKBOOKS_REDIRECT_URL")
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="ICE_AI_QUICKBOOKS_CLIENT_ID or ICE_AI_QUICKBOOKS_REDIRECT_URL not configured",
        )

    # build the intuit oauth2 authorisation url
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "state": "ice-ai-connect",
    })
    auth_url = f"https://appcenter.intuit.com/connect/oauth2?{params}"

    print(f"Redirecting to QuickBooks OAuth: {auth_url}")
    return RedirectResponse(url=auth_url)


# QUICKBOOKS REDIRECT
@app.get("/quickbooks-redirect")
async def quickbooks_redirect(
    code: Optional[str] = None,
    realmId: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> HTMLResponse:
    """
    Handle the OAuth redirect callback from Intuit/QuickBooks.
    Exchanges the authorisation code for access and refresh tokens, then stores
    them in the database for use by other processes.
    """

    print("QuickBooks redirect received - parameters:")
    print(f"  code: {code[:20]}..." if code and len(code) > 20 else f"  code: {code}")
    print(f"  realmId (company ID): {realmId}")
    print(f"  state: {state}")
    print(f"  error: {error}")
    print(f"  error_description: {error_description}")

    # handle error response from intuit
    if error:
        error_msg = error_description or error
        raise HTTPException(
            status_code=400,
            detail=f"QuickBooks authorisation failed: {error_msg}",
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing authorisation code from QuickBooks",
        )

    # exchange the authorisation code for access and refresh tokens
    client_id = os.getenv("ICE_AI_QUICKBOOKS_CLIENT_ID")
    client_secret = os.getenv("ICE_AI_QUICKBOOKS_CLIENT_SECRET")
    redirect_uri = os.getenv("ICE_AI_QUICKBOOKS_REDIRECT_URL")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="QuickBooks client credentials or redirect URL not configured",
        )

    auth_client = AuthClient(
        client_id=client_id,
        client_secret=client_secret,
        environment="production",
        redirect_uri=redirect_uri,
    )

    try:
        auth_client.get_bearer_token(code, realm_id=realmId)
    except Exception as e:
        print(f"QuickBooks token exchange failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to exchange authorisation code for tokens: {e}",
        )

    print(f"QuickBooks token exchange successful for realmId: {realmId}")

    # persist tokens to the database
    save_oauth_tokens(
        provider="quickbooks",
        account_id=realmId,
        access_token=auth_client.access_token,
        refresh_token=auth_client.refresh_token,
    )

    # return success page confirming the connection
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>QuickBooks - Connected</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #2CA01C 0%, #0077C5 100%);
            }}
            .container {{
                background: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                text-align: center;
                max-width: 500px;
            }}
            h1 {{ color: #333; margin-bottom: 10px; }}
            p {{ color: #666; line-height: 1.6; }}
            .success {{ color: #2CA01C; font-size: 48px; margin-bottom: 20px; }}
            .detail {{ font-family: monospace; color: #888; font-size: 14px; margin-top: 16px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success">&#10003;</div>
            <h1>QuickBooks Connected</h1>
            <p>Tokens have been exchanged and saved to the database. You can close this window.</p>
            <p class="detail">Company ID: {realmId or 'N/A'}</p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# HEALTH CHECK
@app.get("/health")
async def health_check() -> dict:
    """
    Simple health check endpoint to verify the API is running.
    """

    return {"status": "healthy", "service": "ice-ai-api"}


# ROOT
@app.get("/")
async def root() -> dict:
    """
    Root endpoint providing basic API information.
    """

    return {
        "name": "Ice AI API",
        "version": "1.0.0",
        "endpoints": [
            "/ebay-connect",
            "/auto-ads-ebay-redirect",
            "/autoads-ebay-marketplace-account-deletion",
            "/quickbooks-connect",
            "/quickbooks-redirect",
            "/health",
        ],
    }


 # MAIN
if __name__ == "__main__":
    import uvicorn

    # run the api server
    port = int(os.getenv("ICE_AI_API_PORT", "8005"))
    uvicorn.run(app, host="0.0.0.0", port=port)
