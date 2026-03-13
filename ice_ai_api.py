import hashlib
import os
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from environments import load_environment

load_environment()


# ICE AI API
app = FastAPI(
    title="Ice AI API",
    description="API endpoints for Ice AI applications including Auto Ads eBay integration.",
    version="1.0.0",
)


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
    eBay redirects here after user authorises the application, providing an authorisation code
    that can be exchanged for access and refresh tokens.
    """

    # handle error response from ebay
    if error:
        error_msg = error_description or error
        raise HTTPException(
            status_code=400,
            detail=f"eBay authorisation failed: {error_msg}",
        )

    # validate required authorisation code
    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing authorisation code from eBay",
        )

    # log the received authorisation code (in production, exchange this for tokens)
    print(f"Received eBay authorisation code: {code[:20]}..." if len(code) > 20 else f"Received eBay authorisation code: {code}")
    if state:
        print(f"State parameter: {state}")

    # return success page to user
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Auto Ads - eBay Authorisation</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            }}
            .container {{
                background: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                text-align: center;
                max-width: 400px;
            }}
            h1 {{
                color: #333;
                margin-bottom: 10px;
            }}
            p {{
                color: #666;
                line-height: 1.6;
            }}
            .success {{
                color: #22c55e;
                font-size: 48px;
                margin-bottom: 20px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success">✓</div>
            <h1>Authorisation Successful</h1>
            <p>Your eBay account has been successfully connected to Auto Ads.</p>
            <p>You can close this window and return to the application.</p>
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
    endpoint = os.getenv(
        "AUTO_ADS_EBAY_DELETION_ENDPOINT",
        "https://ice-ai-api-dev.ice-group.ai/autoads-ebay-marketplace-account-deletion",
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


# QUICKBOOKS REDIRECT
@app.get("/quickbooks-redirect")
async def quickbooks_redirect(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> HTMLResponse:
    """
    Handle the OAuth redirect callback from eBay.
    eBay redirects here after user authorises the application, providing an authorisation code
    that can be exchanged for access and refresh tokens.
    """

    # handle error response from ebay
    if error:
        error_msg = error_description or error
        raise HTTPException(
            status_code=400,
            detail=f"Quicken authorisation failed: {error_msg}",
        )

    # validate required authorisation code
    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing authorisation code from Quicken",
        )

    # log the received authorisation code (in production, exchange this for tokens)
    print(f"Received Quicken authorisation code: {code[:20]}..." if len(code) > 20 else f"Received Quicken authorisation code: {code}")
    if state:
        print(f"State parameter: {state}")

    # return success page to user
    return HTMLResponse(content="Quicken authorization token received.", status_code=200)


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
            "/auto-ads-ebay-redirect",
            "/autoads-ebay-marketplace-account-deletion",
            "/health",
        ],
    }


 # MAIN
if __name__ == "__main__":
    import uvicorn

    # run the api server
    port = int(os.getenv("ICE_AI_API_PORT", "8005"))
    uvicorn.run(app, host="0.0.0.0", port=port)
