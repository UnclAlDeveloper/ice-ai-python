import itertools
import os
import re
import signal
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from playwright.sync_api import Browser, Page, Playwright
from playwright.sync_api import sync_playwright as _sync_playwright
from playwright_stealth import Stealth

T = TypeVar("T")

__all__ = [
    "Browser",
    "CaptchaSolveError",
    "Page",
    "PageUnresponsiveError",
    "Playwright",
    "check_proxy_health",
    "close_browser_quietly",
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
    "page_action_timeout",
    "page_html",
    "parse_decodo_port_range",
    "quick_locator_count",
    "run_quick_page_action",
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

# element actions on a loaded listing page; slow proxy exits are handled by
# wait_for_selector_with_backoff's explicit per-call timeouts instead
STEALTH_ACTION_TIMEOUT_MS = int(
    os.getenv("STEALTH_ACTION_TIMEOUT_MS", str(90 * 1000))
)

# captcha probes must stay short: a hung renderer would otherwise block on
# page.evaluate() / locator.count() forever (those APIs ignore action timeouts)
CAPTCHA_PROBE_TIMEOUT_MS = int(
    os.getenv("CAPTCHA_PROBE_TIMEOUT_MS", "5000")
)

# quick presence checks (locator.count, short reads) on a possibly wedged page
QUICK_ACTION_TIMEOUT_MS = int(
    os.getenv("QUICK_ACTION_TIMEOUT_MS", "15000")
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


# PAGE UNRESPONSIVE ERROR
class PageUnresponsiveError(RuntimeError):
    """
    Raised when the browser page stops responding to Playwright commands during
    navigation or captcha probing. The current Chromium renderer or proxy exit
    IP is unusable; callers should relaunch the browser on a fresh proxy rather
    than retrying against the wedged target.
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
    # broken TLS through a bad/expired Decodo exit (or a mangled CONNECT tunnel)
    # — the page never loaded, so rotate rather than treating it as a site fault
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
    "ERR_SSL_BAD_RECORD_MAC_ALERT",
    "ERR_SSL_OBSOLETE_VERSION",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_CERT_AUTHORITY_INVALID",
    "ERR_CERT_COMMON_NAME_INVALID",
    "ERR_CERT_INVALID",
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


# PAGE ACTION TIMEOUT
@contextmanager
def page_action_timeout(page: Page, timeout_ms: int):
    """
    Temporarily lower the page's default action timeout so quick probes (captcha
    detection, title reads) fail fast on an unresponsive renderer instead of
    inheriting the multi-minute listing-scrape budget. Nested calls restore the
    previous timeout rather than always jumping back to STEALTH_ACTION_TIMEOUT_MS,
    so an outer quick budget is not discarded by an inner probe.
    """

    # track nested budgets on the page so finally restores the caller timeout
    stack = getattr(page, "_ice_timeout_stack", None)
    if stack is None:
        stack = [STEALTH_ACTION_TIMEOUT_MS]
        setattr(page, "_ice_timeout_stack", stack)

    stack.append(timeout_ms)
    page.set_default_timeout(timeout_ms)
    try:
        yield
    finally:
        stack.pop()
        page.set_default_timeout(stack[-1])


# IS PLAYWRIGHT TIMEOUT
def _is_playwright_timeout(exc: Exception) -> bool:
    """
    Return True when an exception is a Playwright timeout ('Timeout NNNms
    exceeded'), which usually means the renderer or proxy is stalled rather than
    the page genuinely lacking a captcha.
    """

    message = str(exc)
    return "Timeout" in message and "exceeded" in message


# DESCENDANT PIDS
def _descendant_pids(root_pid: int) -> list[int]:
    """
    Return every process id that is a descendant of root_pid by walking /proc.
    Used to find Chromium children of this scraper so a hung Playwright close
    can be unblocked without calling the sync API from another thread.
    """

    children_by_ppid: dict[int, list[int]] = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
                    data = handle.read()
                # /proc/pid/stat: pid (comm) state ppid ... — comm may contain
                # spaces/parens, so split on the final ')' before the fields
                rparen = data.rfind(")")
                fields = data[rparen + 2 :].split()
                ppid = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
            children_by_ppid.setdefault(ppid, []).append(pid)
    except OSError:
        return []

    result: list[int] = []
    stack = list(children_by_ppid.get(root_pid, []))
    while stack:
        pid = stack.pop()
        result.append(pid)
        stack.extend(children_by_ppid.get(pid, []))
    return result


# IS CHROMIUM PID
def _is_chromium_pid(pid: int) -> bool:
    """
    Return True when /proc/pid/cmdline looks like a Chromium/Chrome binary.
    """

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            cmdline = handle.read().decode("utf-8", "replace").lower()
    except OSError:
        return False

    return "chrom" in cmdline or "headless_shell" in cmdline


# FORCE KILL CHROMIUM DESCENDANTS
def _force_kill_chromium_descendants() -> None:
    """
    SIGKILL Chromium descendants of this process. Safe to call from a watchdog
    thread because it only uses os.kill — never Playwright's sync API, which is
    greenlet-bound to the thread that started sync_playwright.
    """

    for pid in _descendant_pids(os.getpid()):
        if not _is_chromium_pid(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# FORCE CLOSE PAGE
def _force_close_page(page: Page, description: str, timeout_ms: int) -> None:
    """
    Unblock a hung sync Playwright call by killing the Chromium process from a
    watchdog thread. Calling page.close()/browser.close() here would cross
    greenlet threads and raise 'cannot switch to a different thread'.
    """

    # page is unused; kept so call sites stay readable about which action failed
    _ = page
    print(
        f"  Hard-timeout: {description} after {timeout_ms}ms; killing chromium"
    )
    _force_kill_chromium_descendants()


# CLOSE BROWSER QUIETLY
def close_browser_quietly(
    browser: Browser | None,
    *,
    timeout_s: float = 5.0,
    force_kill_first: bool = False,
) -> None:
    """
    Close a Playwright browser without raising. Close always runs on the calling
    (Playwright) thread so greenlets stay valid. When force_kill_first is set
    (e.g. scheduled proxy rotation), SIGKILL Chromium before close so in-flight
    navigations abort promptly instead of leaving a wedged CDP session. If close
    itself hangs after a wedged CDP session, a watchdog SIGKILLs Chromium so the
    sync call can error out and the rotation loop can relaunch.
    """

    if browser is None:
        return

    if force_kill_first:
        _force_kill_chromium_descendants()

    done = threading.Event()

    def _watchdog() -> None:
        if done.wait(timeout_s):
            return
        print(
            f"  browser.close() hung for {timeout_s:.0f}s; killing chromium"
        )
        _force_kill_chromium_descendants()

    watcher = threading.Thread(
        target=_watchdog, daemon=True, name="browser-close"
    )
    watcher.start()
    try:
        browser.close()
    except Exception:
        pass
    finally:
        done.set()


# PAGE HTML
def page_html(page: Page) -> str:
    """
    Return the page HTML via inner_html('html'), which respects the page action
    timeout. Playwright's page.content() sends no timeout to the driver and can
    block forever on a wedged renderer even inside page_action_timeout.
    """

    return page.inner_html("html")


# RESET PAGE AFTER PROBE TIMEOUT
def _reset_page_after_probe_timeout(page: Page) -> None:
    """
    Navigate to about:blank after a probe timeout so the next goto attempt does
    not inherit a wedged renderer state from the previous navigation.
    """

    if page.is_closed():
        return

    try:
        with page_action_timeout(page, CAPTCHA_PROBE_TIMEOUT_MS):
            page.goto("about:blank", wait_until="domcontentloaded")
    except Exception:
        # the reset is best-effort; a still-dead target will fail the next goto
        pass


# RUN QUICK PAGE ACTION
def run_quick_page_action(
    page: Page,
    action: Callable[[], T],
    *,
    description: str = "page action",
    timeout_ms: int | None = None,
) -> T:
    """
    Run a short Playwright operation under a tight timeout so probes fail fast
    on an unresponsive renderer. set_default_timeout alone is not enough:
    page.content(), locator.count() and page.evaluate() ignore it, so a
    watchdog closes the page when the deadline elapses to unblock the sync call.
    """

    deadline_ms = timeout_ms if timeout_ms is not None else QUICK_ACTION_TIMEOUT_MS
    finished = threading.Event()
    hard_timed_out = False

    def _watchdog() -> None:
        nonlocal hard_timed_out
        if finished.wait(deadline_ms / 1000.0):
            return
        # action may have finished in the race window after wait timed out
        if finished.is_set():
            return
        hard_timed_out = True
        _force_close_page(page, description, deadline_ms)

    watcher = threading.Thread(
        target=_watchdog,
        daemon=True,
        name=f"quick-action:{description}",
    )
    watcher.start()
    try:
        with page_action_timeout(page, deadline_ms):
            return action()
    except PageUnresponsiveError:
        raise
    except Exception as e:
        if (
            hard_timed_out
            or _is_playwright_timeout(e)
            or is_target_closed_error(e)
        ):
            raise PageUnresponsiveError(
                f"{description} timed out after {deadline_ms}ms"
            ) from e
        raise
    finally:
        finished.set()


# QUICK LOCATOR COUNT
def quick_locator_count(locator, *, description: str = "locator count") -> int:
    """
    Return locator.count() under a short hard timeout so a wedged renderer
    cannot block the scrape indefinitely. locator.count() ignores Playwright's
    default action timeout, so run_quick_page_action's watchdog is required.
    """

    return run_quick_page_action(
        locator.page, locator.count, description=description
    )


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
            close_browser_quietly(browser)


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
        # wait_for_function respects the action timeout; page.evaluate does not,
        # so a hung renderer would otherwise block forever on this probe
        return run_quick_page_action(
            page,
            lambda: _probe_captcha_present(page),
            description="captcha probe",
            timeout_ms=CAPTCHA_PROBE_TIMEOUT_MS,
        )
    except PageUnresponsiveError:
        raise
    except Exception:
        # any other probe failure (e.g. detached frame) is treated as no captcha
        return False


# PROBE CAPTCHA PRESENT
def _probe_captcha_present(page: Page) -> bool:
    """
    Return whether a captcha challenge is visible, using wait_for_function so the
    result object is always truthy and Playwright's timeout can fire.
    """

    handle = page.wait_for_function(
        """({ urlFrags, titleFrags, textFrags }) => {
            const url = location.href.toLowerCase();
            if (urlFrags.some((f) => url.includes(f))) {
                return { present: true };
            }

            const title = (document.title || "").toLowerCase();
            if (titleFrags.some((f) => title.includes(f))) {
                return { present: true };
            }

            const iframeSelector = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="recaptcha"]',
                'iframe[src*="hcaptcha"]',
                'iframe[src*="datadome"]',
                'iframe[src*="perimeterx"]',
                'iframe[src*="captcha-delivery"]',
            ].join(", ");
            if (document.querySelector(iframeSelector)) {
                return { present: true };
            }

            const bodyText = document.body ? document.body.innerText : "";
            return {
                present: textFrags.some((f) => bodyText.includes(f)),
            };
        }""",
        arg={
            "urlFrags": list(CAPTCHA_URL_FRAGMENTS),
            "titleFrags": list(CAPTCHA_TITLE_FRAGMENTS),
            "textFrags": list(CAPTCHA_TEXT_FRAGMENTS),
        },
        timeout=CAPTCHA_PROBE_TIMEOUT_MS,
    )
    return bool(handle.evaluate("r => r.present"))


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
        return run_quick_page_action(
            page,
            lambda: _probe_cloudflare_interstitial(page),
            description="cloudflare interstitial probe",
            timeout_ms=CAPTCHA_PROBE_TIMEOUT_MS,
        )
    except PageUnresponsiveError:
        raise
    except Exception:
        return False


# PROBE CLOUDFLARE INTERSTITIAL
def _probe_cloudflare_interstitial(page: Page) -> bool:
    """
    Return whether a Cloudflare managed challenge is showing, via a timed
    wait_for_function probe rather than page.evaluate which has no timeout.
    """

    handle = page.wait_for_function(
        """() => {
            const title = (document.title || "").toLowerCase();
            const titleFrags = [
                "just a moment",
                "attention required",
                "checking your browser",
            ];
            if (titleFrags.some((f) => title.includes(f))) {
                return { present: true };
            }

            const selector = [
                "#challenge-running",
                "#cf-chl-widget",
                'iframe[src*="challenges.cloudflare.com"]',
                'div[id^="cf-chl"]',
            ].join(", ");
            return { present: !!document.querySelector(selector) };
        }""",
        timeout=CAPTCHA_PROBE_TIMEOUT_MS,
    )
    return bool(handle.evaluate("r => r.present"))


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
        return run_quick_page_action(
            page,
            lambda: _probe_datadome_captcha_url(page),
            description="datadome captcha url probe",
            timeout_ms=CAPTCHA_PROBE_TIMEOUT_MS,
        )
    except PageUnresponsiveError:
        raise
    except Exception:
        return None


# PROBE DATADOME CAPTCHA URL
def _probe_datadome_captcha_url(page: Page) -> str | None:
    """
    Read the DataDome captcha iframe src under a timed wait_for_function so a
    wedged renderer cannot hang the probe indefinitely.
    """

    handle = page.wait_for_function(
        """() => {
            const iframe = document.querySelector(
                'iframe[src*="captcha-delivery.com"]'
            );
            return {
                src: iframe ? iframe.getAttribute("src") : null,
            };
        }""",
        timeout=CAPTCHA_PROBE_TIMEOUT_MS,
    )
    return handle.evaluate("r => r.src")


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
    # ephemeral __cf_chl_rt_tk redirect address cloudflare may have navigated to;
    # use page_html so a wedged renderer cannot hang forever on page.content()
    website_url = _clean_capsolver_website_url(page.url)
    challenge_html = run_quick_page_action(
        page,
        lambda: page_html(page),
        description="cloudflare challenge html",
        timeout_ms=QUICK_ACTION_TIMEOUT_MS,
    )

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
        print(f"  Navigating to {url} (attempt {attempt}/{max_retries})...")
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

            # a hung renderer often surfaces as a probe timeout after goto; reset
            # the page and retry, or abandon the session once retries are exhausted
            if _is_playwright_timeout(e):
                if attempt < max_retries:
                    print(
                        f"  Page probe timed out navigating to {url} "
                        f"(attempt {attempt}/{max_retries}); resetting page "
                        f"and retrying..."
                    )
                    _reset_page_after_probe_timeout(page)
                    continue

                raise PageUnresponsiveError(
                    f"Page probe timed out navigating to {url} after "
                    f"{max_retries} attempts"
                ) from e

            # transport-level proxy/TLS failures never produce a captcha page;
            # probing the dead target can trip Playwright's sync greenlet, so
            # raise immediately and let the caller rotate to a fresh exit IP
            if is_proxy_network_error(e):
                print(
                    f"  Proxy/network error navigating to {url}: {e}"
                )
                raise

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
