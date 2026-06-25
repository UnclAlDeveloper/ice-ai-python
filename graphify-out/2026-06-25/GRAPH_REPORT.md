# Graph Report - /Ice-AI/python  (2026-06-25)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 530 nodes · 1219 edges · 36 communities (29 shown, 7 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 45 edges (avg confidence: 0.6)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `a9f82d4e`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]

## God Nodes (most connected - your core abstractions)
1. `scrape_listings()` - 26 edges
2. `ProspectListings` - 26 edges
3. `scrape_listings()` - 23 edges
4. `create_engine_with_retry()` - 22 edges
5. `read_full_prospect_listing()` - 19 edges
6. `EbayDownloader` - 19 edges
7. `update_new_listings_availability()` - 19 edges
8. `AutotraderScrapeConfig` - 18 edges
9. `with_db_retry()` - 18 edges
10. `ListingType` - 18 edges

## Surprising Connections (you probably didn't know these)
- `Conda Environment (Windows)` --semantically_similar_to--> `Python Requirements`  [INFERRED] [semantically similar]
  env.yml → requirements.txt
- `AutotraderScrapeConfig` --uses--> `Images`  [INFERRED]
  autotrader.py → models/auto_ads.py
- `AutotraderScrapeConfig` --uses--> `ProspectListings`  [INFERRED]
  autotrader.py → models/auto_ads.py
- `AutotraderScrapeConfig` --uses--> `ListingSource`  [INFERRED]
  autotrader.py → models/enums.py
- `AutotraderScrapeConfig` --uses--> `ListingTable`  [INFERRED]
  autotrader.py → models/enums.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Vehicle Repair and Resale Analysis Prompts** — car_prompt_md, classic_car_prompt_md, van_prompt_md [EXTRACTED 1.00]
- **Accounting and Transaction Mapping** — accounts_5b258239_md, bank_transaction_summary_571f4c37_md, quickbooks_coa_concept [EXTRACTED 1.00]

## Communities (36 total, 7 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (52): AWSAccess, backup_database(), database_name_from_url(), dump_database(), main(), prune_old_backups(), Nightly PostgreSQL backup job for the three Ice AI databases.  Dumps anna-traine, Delete backups in the bucket whose dated filenames are older than the retention (+44 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (51): Any, Exception, build_simple_video_payload(), create_video(), create_video_from_template(), delete_video(), download_video(), get_video() (+43 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (46): Iterate through all prospect_listings for a given listing source and     regener, regenerate_all_ai_analyses(), BaseException, cleanup_listing_images(), Delete every image whose prospect listing is not in an active status of     New,, create_engine_with_retry(), is_http_not_found(), is_not_found_error() (+38 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (44): accept_cookies(), _apply_newest_listed_sort(), _apply_search_filters(), _canonical_newest_search_url(), car_and_classic(), _dismiss_blocking_overlays(), dismiss_inertia_error_dialog(), extract_gallery_images() (+36 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (23): ASGIApp, BaseHTTPMiddleware, autoads_ebay_marketplace_account_deletion_challenge(), autoads_ebay_marketplace_account_deletion_notification(), McpTrailingSlashMiddleware, Handle eBay's challenge code verification for marketplace account deletion subsc, Handle eBay marketplace account deletion notifications.     eBay sends a POST re, Serve the mounted MCP roots whether or not the client sends a trailing slash. (+15 more)

### Community 5 - "Community 5"
Cohesion: 0.17
Nodes (26): _click_cookie_consent_dismiss_button(), _click_cookie_consent_via_js(), _consent_dismiss_button_locators(), _consent_notice_locators(), dismiss_cookie_consent(), _is_cookie_consent_visible(), _iter_consent_search_roots(), _locator_is_present() (+18 more)

### Community 6 - "Community 6"
Cohesion: 0.12
Nodes (16): API, BaseModel, get_existing_hash_codes(), Query database for existing hash_codes in prospect_listings filtered by     list, EbayDownloader, main(), Download and process van listings from eBay. Holds configuration as model fields, Return cached eBay API instance, creating it with current marketplace if needed. (+8 more)

### Community 7 - "Community 7"
Cohesion: 0.13
Nodes (22): is_listing_no_longer_available(), Walk every page of Autotrader search results and save new listings of the     gi, Return True when the listing page shows the "advert no longer available"     ban, scrape_listings(), Walk every page of Car & Classic search results and save new listings.     Each, scrape_listings(), get_existing_source_ids(), Query database for existing source_ids in prospect_listings filtered by     list (+14 more)

### Community 8 - "Community 8"
Cohesion: 0.16
Nodes (21): RuntimeError, _capsolver_create_task(), _capsolver_get_result(), _capsolver_proxy_fields(), CaptchaSolveError, get_capsolver_api_key(), _page_user_agent(), Clear a DataDome challenge by solving it with CapSolver's DatadomeSliderTask (+13 more)

### Community 9 - "Community 9"
Cohesion: 0.19
Nodes (15): AuthClient, delete_oauth_tokens(), get_oauth_tokens(), Read OAuth token data for a provider from the ia.oauth_tokens table.     Returns, Upsert OAuth token data for a provider into the ia.oauth_tokens table.     Only, Remove the OAuth token row for a provider from the ia.oauth_tokens table.     No, save_oauth_tokens(), HTMLResponse (+7 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (16): _clean_capsolver_website_url(), detect_captcha_task(), detect_cloudflare_interstitial(), detect_datadome_captcha_url(), get_active_proxy_settings(), _inject_hcaptcha_token(), _inject_recaptcha_token(), _inject_turnstile_token() (+8 more)

### Community 11 - "Community 11"
Cohesion: 0.14
Nodes (16): apply_search_filters(), _fill_landing_postcode(), get_specs_and_features(), _pause_delay_seconds(), pause_for_page(), random_english_postcode(), Open the full gallery view, load all images (via carousel or scrolling),     fet, Return a human-like delay using the same gamma distribution as pause(). (+8 more)

### Community 12 - "Community 12"
Cohesion: 0.20
Nodes (14): _open_session(), Launch a fresh proxied Chromium session. Each launch advances to the next     De, Browser, _open_session(), Launch a fresh proxied Chromium session on the Car & Classic search page and, Playwright, launch_stealth_browser(), launch_stealth_chromium() (+6 more)

### Community 13 - "Community 13"
Cohesion: 0.22
Nodes (13): generate_image_hash(), is_playwright_timeout(), pause(), Wait a random amount of time to simulate human browsing behaviour. The     delay, Generate a 16-character random hex string for use as an image filename., Return True when an exception is a Playwright timeout ('Timeout NNNNms     excee, download_and_save_listing_images(), _download_image_bytes() (+5 more)

### Community 14 - "Community 14"
Cohesion: 0.27
Nodes (11): apply_ai_analysis(), convert_prospect_listing_to_markdown(), generate_ai_analysis(), process_ai_analysis_for_listing(), Generate ai analysis for a found listing using Google Gemini API.     Reads the, Parse the markdown ai analysis and populate the AI-generated fields     on the p, Convert a ProspectListings object to a markdown formatted string for use in, Generate ai analysis, apply it to the prospect listing, flush and commit     the (+3 more)

### Community 15 - "Community 15"
Cohesion: 0.27
Nodes (10): Base, Languages, Lookups, PreferredLanguages, Videos, VideoStages, Lookups, ResaleListings (+2 more)

### Community 16 - "Community 16"
Cohesion: 0.17
Nodes (12): _build_decodo_server_url(), check_proxy_health(), get_decodo_settings(), _next_decodo_port(), parse_decodo_port_range(), Parse DECODO_PORT_RANGE into a list of ports, supporting an inclusive     range, Return the next port from DECODO_PORT_RANGE using round-robin selection., Build a Playwright proxy server URL from DECODO_SERVER, optionally     overridin (+4 more)

### Community 17 - "Community 17"
Cohesion: 0.33
Nodes (8): AutotraderScrapeConfig, autotrader_classics(), Scrape new Autotrader classic car listings from the cars landing page using, Scrape new Autotrader search listings for the configured listing type inside, Parameters that distinguish a vans scrape from a classics scrape while     shari, run_autotrader(), autotrader_vans(), Scrape new Autotrader van listings using the shared proxy-rotation driver and

### Community 18 - "Community 18"
Cohesion: 0.20
Nodes (10): extract_source_id(), get_basic_history_check(), get_overview_value(), Extract value from overview section by icon data-gui attribute., Extract full listing details from the detail page and return a ProspectListings, Extract the Autotrader listing reference (e.g. '202606113190878') from a     lis, Extract basic vehicle history check information from the vehicle history section, read_full_prospect_listing() (+2 more)

### Community 19 - "Community 19"
Cohesion: 0.36
Nodes (7): datetime, format_ebay_datetime_string(), is_listing_no_longer_available(), parse_datetime_string(), Return True when the listing page shows that bidding or the listing has ended., Parse a datetime string in eBay Z format or ISO 8601 with a timezone offset., Format a datetime or string for the ebay_rest API (millisecond precision, Z suff

### Community 20 - "Community 20"
Cohesion: 0.33
Nodes (6): configure_search(), Navigate to the configured Autotrader landing page, dismiss cookie consent     o, goto_with_captcha_handling(), is_captcha_present(), Navigate to a url, transparently solving via CapSolver any captcha that     inte, Detect whether the current page is showing a captcha or anti-bot challenge     s

### Community 21 - "Community 21"
Cohesion: 0.33
Nodes (6): _apply_user_agent(), _apply_windows_identity(), Build the userAgentMetadata payload that backs the Sec-CH-UA-* client hints, Override the page's user agent and client-hint metadata over CDP so the real, Override the page's user agent via CDP so it matches the user agent a     cleara, _windows_user_agent_metadata()

### Community 22 - "Community 22"
Cohesion: 0.50
Nodes (4): Car Prompt, Car Repair and Valuation Analysis, Classic Car Prompt, Van Prompt

### Community 23 - "Community 23"
Cohesion: 0.50
Nodes (3): accept_ebay_cookie_consent_if_present(), Click 'Accept all' on the eBay GDPR cookie banner if visible, then wait, Click 'Accept all' on the eBay GDPR cookie banner if visible, then wait     unti

### Community 25 - "Community 25"
Cohesion: 0.50
Nodes (4): _apply_cookies(), _cookie_base_url(), Return the scheme://host origin of the current page, used as the target     url, Attach a name -> value mapping of solved challenge cookies to the active     bro

### Community 26 - "Community 26"
Cohesion: 1.00
Nodes (3): Accounts Chart of Accounts, Bank Transaction Summary, QuickBooks Chart of Accounts

### Community 28 - "Community 28"
Cohesion: 0.67
Nodes (3): db_retry(), Decorator that wraps a function performing a single database unit of work in, T

### Community 29 - "Community 29"
Cohesion: 0.67
Nodes (3): Conda Environment (Linux), Conda Environment (Windows), Python Requirements

## Knowledge Gaps
- **7 isolated node(s):** `entrypoint.sh script`, `Car Prompt`, `Classic Car Prompt`, `Van Prompt`, `Image Prompt` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `run_with_proxy_rotation()` connect `Community 2` to `Community 1`, `Community 3`, `Community 5`, `Community 7`, `Community 8`, `Community 12`, `Community 16`, `Community 17`?**
  _High betweenness centrality (0.119) - this node is a cross-community bridge._
- **Why does `load_environment()` connect `Community 0` to `Community 2`, `Community 3`, `Community 5`, `Community 9`, `Community 14`, `Community 19`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `McpTokenStore` connect `Community 4` to `Community 0`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `ProspectListings` (e.g. with `AutotraderScrapeConfig` and `EbayDownloader`) actually correct?**
  _`ProspectListings` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Convert a ProspectListings object to a markdown formatted string for use in`, `Generate ai analysis for a found listing using Google Gemini API.     Reads the`, `Parse the markdown ai analysis and populate the AI-generated fields     on the p` to the rest of the system?**
  _226 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.05202661826981246 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.06219426974143955 - nodes in this community are weakly interconnected._