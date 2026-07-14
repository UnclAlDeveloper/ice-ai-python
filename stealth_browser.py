import itertools
import os
import re
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from playwright.sync_api import Browser, Page, Playwright
from playwright.sync_api import sync_playwright as _sync_playwright
from playwright_stealth import Stealth

__all__ = [
    "Browser",
    "CaptchaSolveError",
    "Page",
    "Playwright",
    "check_proxy_health",
    "detect_captcha_task",
    "detect_cloudflare_interstitial",
    "detect_datadome_captcha_url",
    "get_active_proxy_settings",
    "get_capsolver_api_key",
    "get_decodo_settings",
    "goto_with_captcha_handling",
    "is_captcha_present",
    "is_navigation_timeout",
    "is_proxy_network_error",
    "is_target_closed_error",
    "launch_stealth_browser",
    "launch_stealth_chromium",
    "new_stealth_page",
    "parse_decodo_port_range",
    "solve_captcha_with_capsolver",
    "solve_cloudflare_interstitial",
    "solve_datadome_captcha",
    "sync_stealth_playwright",
    "wait_for_captcha_solve",
]

# CapSolver's AntiCloudflareTask only accepts a Windows Chrome user agent, and
# Cloudflare cross-checks the user agent against the Sec-CH-UA client hints, so
# the whole session must present one coherent Windows Chrome identity even
# though it runs on Linux. The major version tracks the bundled Chromium build;
# override via env when Playwright ships a newer Chromium.
STEALTH_CHROME_MAJOR = os.getenv("STEALTH_CHROME_MAJOR", "148")

STEALTH_USER_AGENT = os.getenv(
    "STEALTH_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{STEALTH_CHROME_MAJOR}.0.0.0 Safari/537.36",
)

STEALTH_SEC_CH_UA = (
    f'"Chromium";v="{STEALTH_CHROME_MAJOR}", '
    f'"Google Chrome";v="{STEALTH_CHROME_MAJOR}", "Not?A_Brand";v="24"'
)

# keep navigation on a short timeout so a stalled proxy exit IP surfaces quickly
# as a timeout that triggers a rotation instead of blocking for minutes
STEALTH_NAVIGATION_TIMEOUT_MS = int(
    os.getenv("STEALTH_NAVIGATION_TIMEOUT_MS", "30000")
)

# element actions run between deliberate human-like pauses, so give them a
# generous budget rather than Playwright's 30s default that trips mid-listing
STEALTH_ACTION_TIMEOUT_MS = int(
    os.getenv("STEALTH_ACTION_TIMEOUT_MS", str(5 * 60 * 1000))
)

_decodo_port_cycle: Iterator[int] | None = None

# proxy settings of the most recently launched browser, reused so CapSolver
# solves cookie-based challenges from the same sticky exit IP as the session
_active_proxy_settings: dict | None = None

CAPSOLVER_API_URL = "https://api.capsolver.com"

CAPTCHA_URL_FRAGMENTS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform",
    "captcha",
    "datadome",
    "perimeterx",
    "px-captcha",
    "/_Incapsula_Resource",
)

CAPTCHA_TITLE_FRAGMENTS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "are you human",
    "access denied",
    "you have been blocked",
)

CAPTCHA_TEXT_FRAGMENTS = (
    "Verify you are human",
    "Press & Hold",
    "Please verify you are a human",
    "Are you human",
    "unusual traffic",
)

# CAPTCHA SOLVE ERROR
class CaptchaSolveError(RuntimeError):
    """
    Raised when a captcha was detected but CapSolver could not clear it, either
    because the solve attempts failed or the challenge persisted afterwards.
    Callers can treat this as a signal that the current exit IP is blocked and
    rotate the proxy rather than misclassifying the page being visited.
    """


PROXY_NETWORK_ERROR_FRAGMENTS = (
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_EMPTY_RESPONSE",
    # surfaced when a dead/expired proxy session returns 407/502/522 during
    # navigation; treat as a transport failure so the caller rotates the proxy
    "ERR_HTTP_RESPONSE_CODE_FAILURE",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
)


# PARSE DECODO PORT RANGE
def parse_decodo_port_range() -> list[int]:
    """
    Parse DECODO_PORT_RANGE into a list of ports, supporting an inclusive
    range such as 10001-10010 or a single port value.
    """

    port_range = os.getenv("DECODO_PORT_RANGE", "").strip()
    if not port_range:
        return []

    # split an inclusive start-end range into individual port numbers
    if "-" in port_range:
        start_text, end_text = port_range.split("-", 1)
        start_port = int(start_text.strip())
        end_port = int(end_text.strip())
        if start_port > end_port:
            raise ValueError(
                f"Invalid DECODO_PORT_RANGE: {port_range!r} "
                f"(start port {start_port} is greater than end port {end_port})"
            )
        return list(range(start_port, end_port + 1))

    return [int(port_range)]


# NEXT DECODO PORT
def _next_decodo_port() -> int | None:
    """
    Return the next port from DECODO_PORT_RANGE using round-robin selection.
    """

    global _decodo_port_cycle

    ports = parse_decodo_port_range()
    if not ports:
        return None

    if _decodo_port_cycle is None:
        _decodo_port_cycle = itertools.cycle(ports)

    return next(_decodo_port_cycle)


# BUILD DECODO SERVER URL
def _build_decodo_server_url(*, port: int | None = None) -> str:
    """
    Build a Playwright proxy server URL from DECODO_SERVER, optionally
    overriding the port from DECODO_PORT_RANGE or an explicit port argument.
    """

    server = os.getenv("DECODO_SERVER", "").strip()
    if not server:
        return ""

    # ensure urlparse can extract host and port from bare hostnames
    if "://" not in server:
        server = f"http://{server}"

    parsed = urlparse(server)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid DECODO_SERVER: {os.getenv('DECODO_SERVER')!r}")

    scheme = parsed.scheme or "http"

    # prefer an explicit port, then the next port in the range, then the server port
    if port is None:
        port = _next_decodo_port()
    if port is None and parsed.port:
        port = parsed.port
    if port is None:
        port = 7000

    return f"{scheme}://{host}:{port}"


# WITH SESSION ID
def _with_session_id(username: str, session_id: str) -> str:
    """
    Embed a Decodo sticky-session id into the proxy username so every client
    authenticating with it (the browser and CapSolver alike) is routed to the
    same exit IP. The id is inserted before any trailing -sessionduration-N
    segment, appended otherwise, and left untouched when a session is already
    present so an explicitly-configured session is never overridden.
    """

    # never override an explicitly configured session id
    if "-session-" in username:
        return username

    # insert before the duration segment so both parameters remain valid
    if "-sessionduration-" in username:
        head, _, duration = username.partition("-sessionduration-")
        return f"{head}-session-{session_id}-sessionduration-{duration}"

    return f"{username}-session-{session_id}"


# GET DECODO SETTINGS
def get_decodo_settings(
    *, port: int | None = None, session_id: str | None = None
) -> dict | None:
    """
    Build a Playwright proxy configuration from Decodo environment
    variables, returning None when no proxy server has been configured.
    When DECODO_PORT_RANGE is set, each call selects the next port in
    the range using round-robin rotation. When session_id is given it is
    embedded into the username so the browser and CapSolver share one sticky
    exit IP.
    """

    server = _build_decodo_server_url(port=port)
    if not server:
        return None

    settings: dict = {"server": server}
    username = os.getenv("DECODO_USERNAME")
    password = os.getenv("DECODO_PASSWORD")
    if username:
        # pin a shared sticky session so the browser and capsolver share an ip
        if session_id:
            username = _with_session_id(username, session_id)
        settings["username"] = username
    if password:
        settings["password"] = password
    return settings


# CHECK PROXY HEALTH
def check_proxy_health(
    playwright: Playwright,
    *,
    settings: dict | None = None,
    test_url: str | None = None,
    timeout: float = 20000,
) -> str:
    """
    Verify the configured Decodo proxy is actually usable by issuing a request
    through it to a neutral ip-echo endpoint, using Playwright's own request
    stack with the same proxy settings dict the browser uses so the check is a
    faithful mirror of browser authentication. Surfaces the real failure (e.g. a
    407 proxy-auth rejection) up front instead of the opaque navigation errors
    Chromium raises later. Returns the proxy exit IP on success and raises
    RuntimeError with actionable guidance on failure.
    """

    if settings is None:
        settings = get_decodo_settings()
    if not settings:
        raise RuntimeError(
            "No Decodo proxy configured (DECODO_SERVER is not set)."
        )

    url = test_url or os.getenv(
        "PROXY_HEALTH_CHECK_URL", "https://api.ipify.org?format=json"
    )
    gateway = settings.get("server", "")

    # route through playwright's request api with the same proxy settings the
    # browser is launched with, so credentials are handled identically
    request_context = playwright.request.new_context(proxy=settings)
    try:
        try:
            response = request_context.get(url, timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"Could not connect through the Decodo proxy at {gateway}: {e}. "
                "Check the proxy credentials, that the account still has "
                "bandwidth, and that the gateway host/port are correct."
            ) from e

        if response.status == 407:
            raise RuntimeError(
                f"Decodo proxy at {gateway} returned 407 Proxy Authentication "
                "Required. The credentials are likely wrong, or the account is "
                "out of bandwidth or suspended."
            )
        if response.status != 200:
            raise RuntimeError(
                f"Proxy health check to {url} via {gateway} returned HTTP "
                f"{response.status}; the proxy is not serving requests normally."
            )

        # surface the exit ip so a sticky session can be confirmed
        try:
            exit_ip = response.json().get("ip", "")
        except Exception:
            exit_ip = (response.text() or "").strip()

        print(f"Proxy health check OK — exit IP {exit_ip} via {gateway}")
        return exit_ip
    finally:
        request_context.dispose()


# SYNC STEALTH PLAYWRIGHT
@contextmanager
def sync_stealth_playwright() -> Iterator[Playwright]:
    """
    Yield a Playwright instance with stealth evasions applied to every
    browser context created within the block. The evasions also pin a Windows
    Chrome identity (user agent, navigator.platform and Sec-CH-UA) so the
    JS-visible fingerprint stays coherent with the Windows user agent the pages
    advertise over HTTP.
    """

    stealth = Stealth(
        navigator_user_agent_override=STEALTH_USER_AGENT,
        navigator_platform_override="Win32",
        sec_ch_ua_override=STEALTH_SEC_CH_UA,
    )
    with stealth.use_sync(_sync_playwright()) as playwright:
        yield playwright


# LAUNCH STEALTH CHROMIUM
def launch_stealth_chromium(
    playwright: Playwright, *, headless: bool = False, use_proxy: bool = False
) -> Browser:
    """
    Launch a stealth-patched Chromium browser, optionally routing traffic
    through the configured Decodo gateway when use_proxy is enabled.
    """

    global _active_proxy_settings

    # pin a fresh per-launch sticky session id so this browser and the CapSolver
    # solve share one Decodo exit ip; relaunches (e.g. proxy rotation) get a new
    # id and therefore a new exit ip
    session_id = uuid.uuid4().hex[:16] if use_proxy else None
    proxy = get_decodo_settings(session_id=session_id) if use_proxy else None

    # fail loudly if a proxy was requested but never configured
    if use_proxy and proxy is None:
        raise RuntimeError(
            "use_proxy=True but DECODO_SERVER is not set in the environment"
        )

    # remember the single proxy chosen for this session so cookie-based captcha
    # solving can reuse the same sticky exit ip that the browser is using
    _active_proxy_settings = proxy

    # container-hardening flags: /dev/shm defaults to 64MB inside docker, which
    # is too small for chromium's renderer on heavy pages and crashes the target
    # (surfacing as a hung sync call after "Target closed"); routing shared
    # memory to /tmp with --disable-dev-shm-usage avoids the crash, and
    # --no-sandbox is required to launch under most containerised/root setups
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]

    return playwright.chromium.launch(
        headless=headless, proxy=proxy, args=launch_args
    )


# GET ACTIVE PROXY SETTINGS
def get_active_proxy_settings() -> dict | None:
    """
    Return the Playwright proxy settings of the most recently launched browser,
    or None when the current session is not routed through a proxy.
    """

    return _active_proxy_settings


# WINDOWS USER AGENT METADATA
def _windows_user_agent_metadata(major: str = STEALTH_CHROME_MAJOR) -> dict:
    """
    Build the userAgentMetadata payload that backs the Sec-CH-UA-* client hints
    for a Windows Chrome build of the given major version, so the high-entropy
    client hints Cloudflare can request stay consistent with the Windows user
    agent rather than leaking the underlying Linux host.
    """

    full_version = f"{major}.0.0.0"
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not?A_Brand", "version": "24"},
    ]
    full_version_list = [
        {"brand": "Chromium", "version": full_version},
        {"brand": "Google Chrome", "version": full_version},
        {"brand": "Not?A_Brand", "version": "24.0.0.0"},
    ]
    return {
        "brands": brands,
        "fullVersion": full_version,
        "fullVersionList": full_version_list,
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "wow64": False,
    }


# APPLY WINDOWS IDENTITY
def _apply_windows_identity(page: Page, user_agent: str = STEALTH_USER_AGENT) -> None:
    """
    Override the page's user agent and client-hint metadata over CDP so the real
    HTTP User-Agent header, navigator.userAgent, navigator.platform and the
    Sec-CH-UA-* headers all report the same Windows Chrome browser. This must be
    applied before the first navigation so Cloudflare never sees the Linux host.
    """

    # derive the chrome major from the user agent so the client-hint versions
    # always match whatever user agent the page is advertising
    match = re.search(r"Chrome/(\d+)", user_agent)
    major = match.group(1) if match else STEALTH_CHROME_MAJOR

    cdp_session = page.context.new_cdp_session(page)
    cdp_session.send(
        "Emulation.setUserAgentOverride",
        {
            "userAgent": user_agent,
            "platform": "Windows",
            "userAgentMetadata": _windows_user_agent_metadata(major),
        },
    )


# NEW STEALTH PAGE
def new_stealth_page(browser: Browser) -> Page:
    """
    Create a new page that advertises the pinned Windows Chrome user agent over
    HTTP and applies matching client-hint metadata before any navigation, so
    every page in the session presents one coherent Windows identity.
    """

    page = browser.new_page(user_agent=STEALTH_USER_AGENT)

    # bound navigations and element actions so a stalled proxy exit ip or a slow
    # render surfaces as a timeout the rotation loop can act on, rather than a
    # sync call blocking indefinitely against an unresponsive target
    page.set_default_navigation_timeout(STEALTH_NAVIGATION_TIMEOUT_MS)
    page.set_default_timeout(STEALTH_ACTION_TIMEOUT_MS)

    # surface a renderer crash (e.g. out-of-memory in a small /dev/shm container)
    # in the logs so a dead session is diagnosable rather than silently hung
    page.on(
        "crash",
        lambda _: print("  Browser page crashed (renderer terminated)"),
    )

    _apply_windows_identity(page)
    return page


# CAPSOLVER PROXY FIELDS
def _capsolver_proxy_fields() -> dict | None:
    """
    Render the active session proxy as CapSolver's structured proxy fields
    (proxyType/proxyAddress/proxyPort/proxyLogin/proxyPassword). The structured
    form sends each credential as its own JSON value, so characters like '+' in
    the password are transmitted verbatim instead of being packed into a
    colon-delimited string that CapSolver might url-decode. Returns None when no
    proxy is active for the session.
    """

    settings = _active_proxy_settings
    if not settings:
        return None

    # reuse urlparse so a bare host:port server string is handled consistently
    server = settings.get("server", "")
    parsed = urlparse(server if "://" in server else f"http://{server}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None

    # capsolver only understands these proxy schemes; default to http otherwise
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https", "socks4", "socks5"):
        scheme = "http"

    fields: dict = {
        "proxyType": scheme,
        "proxyAddress": host,
        "proxyPort": port,
    }
    username = settings.get("username")
    password = settings.get("password")
    if username:
        fields["proxyLogin"] = username
    if password:
        fields["proxyPassword"] = password
    return fields


# LAUNCH STEALTH BROWSER
@contextmanager
def launch_stealth_browser(
    *, headless: bool = False, use_proxy: bool = False
) -> Iterator[tuple[Browser, Page]]:
    """
    Launch Chromium with stealth patches and yield a browser and page pair.
    The browser is closed when the context exits.
    """

    with sync_stealth_playwright() as playwright:
        browser = launch_stealth_chromium(
            playwright, headless=headless, use_proxy=use_proxy
        )
        page = new_stealth_page(browser)
        try:
            yield browser, page
        finally:
            browser.close()


# GET CAPSOLVER API KEY
def get_capsolver_api_key() -> str | None:
    """
    Return the CapSolver client key from the CAPSOLVER_API_KEY environment
    variable, or None when it is unset or blank.
    """

    api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    return api_key or None


# CAPSOLVER CREATE TASK
def _capsolver_create_task(task: dict) -> str:
    """
    Submit a captcha task to CapSolver's createTask endpoint and return the
    assigned task id. Raises RuntimeError when no API key is configured or
    when CapSolver reports an error.
    """

    api_key = get_capsolver_api_key()
    if not api_key:
        raise RuntimeError(
            "CAPSOLVER_API_KEY is not set in the environment; cannot solve captcha"
        )

    payload = {"clientKey": api_key, "task": task}
    response = requests.post(
        f"{CAPSOLVER_API_URL}/createTask", json=payload, timeout=30
    )

    # parse the body before checking status: capsolver returns its error detail
    # in the json even on a 4xx, so raising on status alone hides the real cause
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"CapSolver createTask returned a non-JSON response "
            f"(HTTP {response.status_code}) for task type {task.get('type')!r}: "
            f"{response.text[:500]!r}"
        )

    if data.get("errorId"):
        raise RuntimeError(
            f"CapSolver createTask failed (HTTP {response.status_code}) for task "
            f"type {task.get('type')!r}: "
            f"{data.get('errorCode')} - {data.get('errorDescription')}"
        )

    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"CapSolver createTask returned no taskId: {data!r}")

    print(f"  CapSolver task {task_id} created ({task.get('type')})")
    return task_id


# CAPSOLVER GET RESULT
def _capsolver_get_result(
    task_id: str, *, timeout_s: float = 120.0, interval_s: float = 3.0
) -> dict:
    """
    Poll CapSolver's getTaskResult endpoint until the task is ready and return
    the solution dict. Raises RuntimeError when the task fails or TimeoutError
    when it does not complete within timeout_s seconds.
    """

    api_key = get_capsolver_api_key()
    payload = {"clientKey": api_key, "taskId": task_id}
    started = time.monotonic()
    deadline = started + timeout_s

    while time.monotonic() < deadline:
        response = requests.post(
            f"{CAPSOLVER_API_URL}/getTaskResult", json=payload, timeout=30
        )

        # parse before status-checking so capsolver's json error detail survives
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"CapSolver getTaskResult returned a non-JSON response "
                f"(HTTP {response.status_code}): {response.text[:500]!r}"
            )

        if data.get("errorId"):
            raise RuntimeError(
                f"CapSolver getTaskResult failed (HTTP {response.status_code}): "
                f"{data.get('errorCode')} - {data.get('errorDescription')}"
            )

        status = data.get("status")
        if status == "ready":
            elapsed = time.monotonic() - started
            print(f"  CapSolver task {task_id} solved in {elapsed:.0f}s")
            return data.get("solution", {})
        if status == "failed":
            raise RuntimeError(f"CapSolver task {task_id} failed: {data!r}")

        # still processing, wait before polling again
        time.sleep(interval_s)

    raise TimeoutError(
        f"CapSolver task {task_id} did not complete within {timeout_s} seconds"
    )


# DETECT CAPTCHA TASK
def detect_captcha_task(page: Page) -> dict | None:
    """
    Inspect the page DOM for a supported embedded captcha widget and build the
    matching CapSolver task payload. Supports Cloudflare Turnstile, reCAPTCHA
    v2, and hCaptcha. Returns None when no solvable widget sitekey is found.
    """

    website_url = page.url

    # cloudflare turnstile renders a .cf-turnstile element carrying the sitekey
    turnstile = page.locator(".cf-turnstile[data-sitekey]")
    if turnstile.count() > 0:
        sitekey = turnstile.first.get_attribute("data-sitekey")
        if sitekey:
            task: dict = {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": sitekey,
            }
            action = turnstile.first.get_attribute("data-action")
            if action:
                task["metadata"] = {"action": action}
            return task

    # recaptcha v2 exposes the sitekey on the .g-recaptcha container
    recaptcha = page.locator(".g-recaptcha[data-sitekey]")
    if recaptcha.count() > 0:
        sitekey = recaptcha.first.get_attribute("data-sitekey")
        if sitekey:
            return {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": sitekey,
            }

    # hcaptcha exposes the sitekey on the .h-captcha container
    hcaptcha = page.locator(".h-captcha[data-sitekey]")
    if hcaptcha.count() > 0:
        sitekey = hcaptcha.first.get_attribute("data-sitekey")
        if sitekey:
            return {
                "type": "HCaptchaTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": sitekey,
            }

    return None


# INJECT TURNSTILE TOKEN
def _inject_turnstile_token(page: Page, token: str) -> None:
    """
    Write a solved Cloudflare Turnstile token into the hidden response input
    and dispatch an input event so any page handlers observe the change.
    """

    page.evaluate(
        """(token) => {
            document
                .querySelectorAll('input[name="cf-turnstile-response"]')
                .forEach((el) => {
                    el.value = token;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                });
        }""",
        token,
    )


# INJECT RECAPTCHA TOKEN
def _inject_recaptcha_token(page: Page, token: str) -> None:
    """
    Write a solved reCAPTCHA token into the g-recaptcha-response field, making
    the textarea visible if needed, and invoke the registered callback when one
    can be discovered on the grecaptcha client.
    """

    page.evaluate(
        """(token) => {
            document
                .querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response')
                .forEach((el) => {
                    el.style.display = '';
                    el.value = token;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                });

            // attempt to fire the widget callback so the page proceeds
            try {
                const clients = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
                if (clients) {
                    Object.values(clients).forEach((client) => {
                        Object.values(client).forEach((maybe) => {
                            if (maybe && typeof maybe === 'object') {
                                Object.values(maybe).forEach((inner) => {
                                    if (inner && typeof inner.callback === 'function') {
                                        inner.callback(token);
                                    }
                                });
                            }
                        });
                    });
                }
            } catch (e) {
                // ignore callback discovery failures and rely on the field value
            }
        }""",
        token,
    )


# INJECT HCAPTCHA TOKEN
def _inject_hcaptcha_token(page: Page, token: str) -> None:
    """
    Write a solved hCaptcha token into both the h-captcha-response and
    g-recaptcha-response fields that hCaptcha populates on submission.
    """

    page.evaluate(
        """(token) => {
            document
                .querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]')
                .forEach((el) => {
                    el.value = token;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                });
        }""",
        token,
    )


# EXTRACT CAPSOLVER TOKEN
def _extract_capsolver_token(solution: dict) -> str | None:
    """
    Pull the solution token out of a CapSolver solution dict, accommodating the
    different field names used across captcha task types.
    """

    return (
        solution.get("token")
        or solution.get("gRecaptchaResponse")
        or solution.get("captchaResponse")
    )


# SOLVE CAPTCHA WITH CAPSOLVER
def solve_captcha_with_capsolver(page: Page) -> bool:
    """
    Detect a supported captcha widget on the page, request a solution token
    from CapSolver, and inject it back into the page. Returns True when a token
    was solved and injected, or False when no supported widget was detected.
    Raises on CapSolver/API errors so the caller can surface the failure.
    """

    task = detect_captcha_task(page)
    if task is None:
        return False

    print(f"  Detected embedded captcha widget; solving as {task['type']}...")
    task_id = _capsolver_create_task(task)
    solution = _capsolver_get_result(task_id)

    token = _extract_capsolver_token(solution)
    if not token:
        raise RuntimeError(
            f"CapSolver returned no usable token for task type "
            f"{task.get('type')!r}: {solution!r}"
        )

    # dispatch the token to the matching response field for this captcha type
    task_type = task["type"]
    if task_type == "AntiTurnstileTaskProxyLess":
        _inject_turnstile_token(page, token)
    elif task_type == "ReCaptchaV2TaskProxyLess":
        _inject_recaptcha_token(page, token)
    elif task_type == "HCaptchaTaskProxyLess":
        _inject_hcaptcha_token(page, token)

    # submit the surrounding form when present so the verification completes
    print("  Token injected; submitting the captcha form")
    page.evaluate(
        """() => {
            const field = document.querySelector(
                'input[name="cf-turnstile-response"], '
                + 'textarea[name="g-recaptcha-response"], '
                + 'textarea[name="h-captcha-response"]'
            );
            const form = field && field.closest('form');
            if (form && typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else if (form) {
                form.submit();
            }
        }"""
    )

    return True


# IS CAPTCHA PRESENT
def is_captcha_present(page: Page) -> bool:
    """
    Detect whether the current page is showing a captcha or anti-bot challenge
    such as a Cloudflare interstitial, DataDome 'Press & Hold' challenge, or an
    embedded hCaptcha/reCAPTCHA widget.
    """

    # a closed/crashed target cannot host a captcha, and probing it would block
    # the sync call indefinitely, so bail out before touching any locator
    if page.is_closed():
        return False

    try:
        # url-based detection covers cloudflare/datadome/perimeterx redirects
        current_url = (page.url or "").lower()
        if any(fragment in current_url for fragment in CAPTCHA_URL_FRAGMENTS):
            return True

        # title-based detection catches the typical interstitial titles
        title = (page.title() or "").lower()
        if any(fragment in title for fragment in CAPTCHA_TITLE_FRAGMENTS):
            return True

        # iframe-based detection catches embedded challenge widgets
        challenge_iframe = page.locator(
            'iframe[src*="challenges.cloudflare.com"], '
            'iframe[src*="recaptcha"], '
            'iframe[src*="hcaptcha"], '
            'iframe[src*="datadome"], '
            'iframe[src*="perimeterx"], '
            'iframe[src*="captcha-delivery"]'
        )
        if challenge_iframe.count() > 0:
            return True

        # visible-text detection as a final fallback for provider-agnostic prompts
        for fragment in CAPTCHA_TEXT_FRAGMENTS:
            if page.get_by_text(fragment, exact=False).count() > 0:
                return True
    except Exception:
        # if any probe fails (e.g. detached frame) assume no captcha so the
        # caller can decide how to handle the underlying error
        return False

    return False


# PAGE USER AGENT
def _page_user_agent(page: Page) -> str:
    """
    Return the live navigator.userAgent of the page so it can be forwarded to
    CapSolver; cookie-based challenges bind the clearance to this user agent.
    """

    return page.evaluate("() => navigator.userAgent")


# APPLY USER AGENT
def _apply_user_agent(page: Page, user_agent: str) -> None:
    """
    Override the page's user agent via CDP so it matches the user agent a
    clearance cookie was solved with. Cloudflare binds cf_clearance to the
    exact user agent, so any mismatch triggers an immediate re-challenge. The
    matching Windows client-hint metadata is reapplied alongside it so the
    Sec-CH-UA-* headers stay consistent with the new user agent.
    """

    _apply_windows_identity(page, user_agent)


# COOKIE BASE URL
def _cookie_base_url(page: Page) -> str:
    """
    Return the scheme://host origin of the current page, used as the target
    url when applying solved challenge cookies to the browser context.
    """

    parsed = urlparse(page.url)
    return f"{parsed.scheme}://{parsed.hostname}"


# APPLY COOKIES
def _apply_cookies(page: Page, cookies: dict) -> None:
    """
    Attach a name -> value mapping of solved challenge cookies to the active
    browser context, scoped to the current page origin.
    """

    base_url = _cookie_base_url(page)
    page.context.add_cookies(
        [
            {"name": name, "value": value, "url": base_url}
            for name, value in cookies.items()
            if value
        ]
    )


# DETECT CLOUDFLARE INTERSTITIAL
def detect_cloudflare_interstitial(page: Page) -> bool:
    """
    Detect a full-page Cloudflare managed challenge ('Just a moment...') that
    must be cleared with a cf_clearance cookie rather than a widget token.
    """

    try:
        title = (page.title() or "").lower()
        if any(
            fragment in title
            for fragment in ("just a moment", "attention required", "checking your browser")
        ):
            return True

        # the interstitial hosts the challenge in a cloudflare-served iframe/div
        markers = page.locator(
            "#challenge-running, #cf-chl-widget, "
            'iframe[src*="challenges.cloudflare.com"], '
            'div[id^="cf-chl"]'
        )
        return markers.count() > 0
    except Exception:
        return False


# CLEAN CAPSOLVER WEBSITE URL
def _clean_capsolver_website_url(url: str) -> str:
    """
    Strip ephemeral Cloudflare challenge query parameters from a page url so
    CapSolver receives the real target address rather than a one-time token url.
    """

    parsed = urlparse(url)
    if not parsed.query or "__cf_chl_rt_tk" not in parsed.query:
        return url

    # drop the one-time challenge token but keep any other query parameters
    pairs = [(key, value) for key, value in parse_qsl(parsed.query) if key != "__cf_chl_rt_tk"]
    return urlunparse(parsed._replace(query=urlencode(pairs)))


# DETECT DATADOME CAPTCHA URL
def detect_datadome_captcha_url(page: Page) -> str | None:
    """
    Return the src of the DataDome captcha iframe when present, which CapSolver
    needs as the captchaUrl for a DatadomeSliderTask. Returns None otherwise.
    """

    try:
        iframe = page.locator('iframe[src*="captcha-delivery.com"]')
        if iframe.count() == 0:
            return None
        return iframe.first.get_attribute("src")
    except Exception:
        return None


# SOLVE CLOUDFLARE INTERSTITIAL
def solve_cloudflare_interstitial(page: Page) -> None:
    """
    Clear a Cloudflare managed challenge by solving it with CapSolver's
    AntiCloudflareTask over the session's sticky proxy, applying the returned
    cf_clearance cookie, and reloading the page. Raises when no sticky proxy
    is available or CapSolver fails.
    """

    proxy = _capsolver_proxy_fields()
    if not proxy:
        raise RuntimeError(
            "Cloudflare interstitial requires a sticky proxy to solve, but no "
            "proxy is active for this session (launch with use_proxy=True)."
        )

    print("  Detected Cloudflare interstitial; solving via AntiCloudflareTask...")

    # capsolver needs the challenge-page html and the real target url, not the
    # ephemeral __cf_chl_rt_tk redirect address cloudflare may have navigated to
    website_url = _clean_capsolver_website_url(page.url)
    challenge_html = page.content()

    # forward the live user agent so the clearance is bound to this browser
    page_user_agent = _page_user_agent(page)
    task = {
        "type": "AntiCloudflareTask",
        "websiteURL": website_url,
        "html": challenge_html,
        "userAgent": page_user_agent,
        **proxy,
    }
    task_id = _capsolver_create_task(task)
    solution = _capsolver_get_result(task_id)

    cookies = solution.get("cookies") or {}
    if not cookies and solution.get("token"):
        cookies = {"cf_clearance": solution["token"]}
    if not cookies:
        raise RuntimeError(
            f"CapSolver AntiCloudflareTask returned no cookies: {solution!r}"
        )

    # cloudflare binds cf_clearance to the exact user agent it was solved
    # with, so adopt capsolver's user agent whenever it differs from the page
    solved_user_agent = solution.get("userAgent")
    if solved_user_agent and solved_user_agent != page_user_agent:
        print(
            "  CapSolver solved with a different user agent; overriding the "
            "page user agent to match the clearance cookie"
        )
        _apply_user_agent(page, solved_user_agent)

    _apply_cookies(page, cookies)

    # reload so cloudflare re-evaluates the request with the clearance cookie
    print("  Clearance cookie applied; reloading the page...")
    page.goto(website_url)
    page.wait_for_load_state("domcontentloaded")


# SOLVE DATADOME CAPTCHA
def solve_datadome_captcha(page: Page, captcha_url: str) -> None:
    """
    Clear a DataDome challenge by solving it with CapSolver's DatadomeSliderTask
    over the session's sticky proxy, applying the returned datadome cookie, and
    reloading the page. Raises when no sticky proxy is available, when the IP is
    banned (t=bv), or CapSolver fails.
    """

    proxy = _capsolver_proxy_fields()
    if not proxy:
        raise RuntimeError(
            "DataDome challenge requires a sticky proxy to solve, but no proxy "
            "is active for this session (launch with use_proxy=True)."
        )

    # t=bv in the captcha url means datadome has banned the current exit ip
    if "t=bv" in captcha_url:
        raise RuntimeError(
            "DataDome reports the current proxy IP is banned (t=bv); rotate the "
            "Decodo session/port before retrying."
        )

    print("  Detected DataDome challenge; solving via DatadomeSliderTask...")
    task = {
        "type": "DatadomeSliderTask",
        "websiteURL": page.url,
        "captchaUrl": captcha_url,
        "userAgent": _page_user_agent(page),
        **proxy,
    }
    task_id = _capsolver_create_task(task)
    solution = _capsolver_get_result(task_id)

    # the solution cookie is a full set-cookie string like "datadome=...; Path=/"
    cookie_header = solution.get("cookie")
    if not cookie_header:
        raise RuntimeError(
            f"CapSolver DatadomeSliderTask returned no cookie: {solution!r}"
        )

    name, _, remainder = cookie_header.partition("=")
    value = remainder.split(";", 1)[0]
    _apply_cookies(page, {name.strip(): value.strip()})

    # reload so datadome re-evaluates the request with the new cookie
    print("  DataDome cookie applied; reloading the page...")
    page.goto(page.url)
    page.wait_for_load_state("domcontentloaded")


# WAIT FOR CAPTCHA SOLVE
def wait_for_captcha_solve(page: Page, max_attempts: int = 3) -> None:
    """
    Automatically solve any captcha on the page using CapSolver, dispatching to
    the embedded-widget token flow, the DataDome cookie flow, or the Cloudflare
    interstitial cookie flow as appropriate. Returns once the captcha is no
    longer detected. Raises RuntimeError when no CapSolver API key is
    configured, and CaptchaSolveError when the challenge type is unrecognised,
    a solve attempt fails, or the challenge remains after max_attempts rounds.
    """

    if not is_captcha_present(page):
        return

    # record the offending url for diagnostics in any failure branch
    current_url = ""
    try:
        current_url = page.url or ""
    except Exception:
        pass

    # fail fast when automatic solving is not configured
    if not get_capsolver_api_key():
        raise RuntimeError(
            f"CAPTCHA encountered at {current_url!r} but CAPSOLVER_API_KEY is "
            "not set; cannot solve it automatically."
        )

    print(f"CAPTCHA detected at {current_url}; solving with CapSolver...")

    # solve repeatedly because some challenges re-render after the first round
    for attempt in range(1, max_attempts + 1):
        if not is_captcha_present(page):
            print("CAPTCHA cleared; continuing")
            return

        print(f"  Solve attempt {attempt}/{max_attempts}")

        # wrap solver/api failures so callers can distinguish a blocked exit ip
        # (worth a proxy rotation) from a genuine problem with the target page
        try:
            # first try an embedded widget token (turnstile/recaptcha/hcaptcha)
            if solve_captcha_with_capsolver(page):
                page.wait_for_timeout(3000)
                continue

            # next try a datadome challenge, identified by its captcha iframe
            datadome_url = detect_datadome_captcha_url(page)
            if datadome_url:
                solve_datadome_captcha(page, datadome_url)
                page.wait_for_timeout(2000)
                continue

            # finally try a full-page cloudflare managed interstitial
            if detect_cloudflare_interstitial(page):
                solve_cloudflare_interstitial(page)
                page.wait_for_timeout(2000)
                continue
        except CaptchaSolveError:
            raise
        except Exception as e:
            raise CaptchaSolveError(
                f"CapSolver failed to solve the CAPTCHA at {current_url!r}: {e}"
            ) from e

        raise CaptchaSolveError(
            f"CAPTCHA encountered at {current_url!r} is an unrecognised "
            "challenge type that CapSolver cannot solve automatically."
        )

    if is_captcha_present(page):
        raise CaptchaSolveError(
            f"CAPTCHA at {current_url!r} still present after {max_attempts} "
            "CapSolver solving attempts."
        )

    print("CAPTCHA cleared; continuing")


# IS PROXY NETWORK ERROR
def is_proxy_network_error(exc: BaseException) -> bool:
    """
    Return True when an exception looks like a transport-level failure of the
    upstream proxy (navigation timeouts, tunnel/connection failures) rather than
    a problem with the page itself. Callers use this to decide when to rotate to
    a fresh proxy session, e.g. after a sticky session has expired mid-run.
    """

    message = str(exc)
    return any(
        fragment in message for fragment in PROXY_NETWORK_ERROR_FRAGMENTS
    )


# IS NAVIGATION TIMEOUT
def is_navigation_timeout(exc: BaseException) -> bool:
    """
    Return True when an exception is a Playwright navigation timeout
    ('Timeout NNNNms exceeded'), which usually means a slow or stalled proxy
    exit IP and is worth retrying or rotating the proxy rather than aborting.
    """

    message = str(exc)
    return "Timeout" in message and "exceeded" in message


TARGET_CLOSED_ERROR_FRAGMENTS = (
    "Target page, context or browser has been closed",
    "Target closed",
    "TargetClosedError",
    "Page crashed",
    "has crashed",
)


# IS TARGET CLOSED ERROR
def is_target_closed_error(exc: BaseException) -> bool:
    """
    Return True when an exception means the page/target was closed or the
    renderer crashed (e.g. Chromium ran out of shared memory in a container).
    The whole session is unusable afterwards, so the caller should relaunch a
    fresh browser rather than keep retrying against the dead target.
    """

    message = str(exc)
    return any(
        fragment in message for fragment in TARGET_CLOSED_ERROR_FRAGMENTS
    )


# GOTO WITH CAPTCHA HANDLING
def goto_with_captcha_handling(
    page: Page, url: str, max_retries: int = 3
) -> Optional[object]:
    """
    Navigate to a url, transparently solving via CapSolver any captcha that
    interrupts the navigation. Returns the Playwright response from the final
    successful page.goto call.
    """

    attempt = 0
    last_error: Optional[Exception] = None

    while attempt < max_retries:
        attempt += 1
        try:
            # wait only for domcontentloaded: a full "load" can hang or abort on
            # challenge interstitials and heavy client-rendered pages
            response = page.goto(url, wait_until="domcontentloaded")

            # a renderer that crashed during navigation leaves a dead target;
            # raise so the caller relaunches a fresh session instead of probing
            # it for a captcha and hanging on the unresponsive connection
            if page.is_closed():
                raise RuntimeError(
                    f"Target page closed while navigating to {url} "
                    "(renderer likely crashed)"
                )

            # if a captcha appeared after a successful load, solve then retry
            if is_captcha_present(page):
                wait_for_captcha_solve(page)
                continue

            return response
        except CaptchaSolveError:
            raise
        except Exception as e:
            last_error = e

            # a mid-flight challenge redirect typically surfaces as ERR_ABORTED
            # and the interstitial needs a moment to render, so settle before
            # probing for a captcha rather than checking the half-loaded page
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass

            if is_captcha_present(page):
                wait_for_captcha_solve(page)
                continue

            # ERR_ABORTED is often a superseded/transient navigation, and a
            # navigation timeout is often a slow/stalled proxy exit; retry both a
            # few times before giving up rather than failing the whole run
            if (
                ("ERR_ABORTED" in str(e) or is_navigation_timeout(e))
                and attempt < max_retries
            ):
                page.wait_for_timeout(1500)
                continue

            # unrelated failure — let the caller decide
            raise

    # exhausted retries while still failing to navigate cleanly
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"Failed to navigate to {url} after {max_retries} captcha retries"
    )
