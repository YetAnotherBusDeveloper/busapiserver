# YABus MCP and account authorization

This server exposes standard Streamable HTTP MCP for ChatGPT, Claude and Gemini
Connected Apps. Platform enrollment is separate from deploying this code.

## Endpoints

| Endpoint | Access | Tools |
| --- | --- | --- |
| `/mcp/public` | Anonymous | Route/stop search, nearby stops, route stops, arrivals, station arrivals, service alerts and schedules |
| `/mcp` | OAuth `favorites:read` | All public tools plus `get_my_favorites` and `get_favorite_arrivals` |

The separate public endpoint gives clients that authenticate an entire connection
a consistent workflow. Connecting `/mcp` requires account linking before tools
can run. Both endpoints use the existing transit services and caches. Tool names,
input schemas, read-only annotations and structured output schemas are discoverable
using `tools/list`.

Favorites are the **last cloud-synced** document, including v1 boarding favorites
and v2 route/station/boarding groups. The result includes revision/update timestamps.
No document means “upload favorites from YABus,” not “this user has no favorites.”
Legacy boarding favorites without a raw StopID return a labelled whole-route result;
the assistant must resolve the stop by name instead of treating a local integer
identifier as a TDX stop ID. Favorite arrival queries are paginated at five entries
per request. Query timestamps are not substituted for source freshness timestamps.

## Deployment

1. Install `requirements.txt` using Python 3.13. MCP SDK 1.30.x handles the protocol,
   OAuth authorization requests and PKCE verification.
2. Configure `AUTH_PUBLIC_BASE_URL` to the public HTTPS **origin** of this server
   (no path/query). The same origin is issuer and basis for the `/mcp` audience.
   Existing Google/Discord login credentials and registered callback URLs are reused.
3. Start the existing app normally. MCP tables are created idempotently in the
   application SQLite database during startup, never in the downloadable transit DB.
4. Preserve Authorization, Origin, Accept and MCP-Protocol-Version headers through
   the proxy. Permit POST and GET to the MCP endpoints. Configure Uvicorn's trusted
   proxy addresses so HTTPS and client addresses are correct; never trust forwarded
   headers from arbitrary clients. Host/Origin validation uses the configured origin
   and existing allowed app origins. Configure edge rate limits as appropriate.
5. Exclude query strings on `/authorize`, `/oauth/*` and `/api/v1/auth/*` in reverse
   proxy/CDN access logs. The application installs the corresponding Uvicorn log
   filter, and application request logs already record only the path.

HTTP on localhost/127.0.0.1 is allowed for local development only. No new model API
key is needed: the AI client calls YABus; YABus does not invoke a model itself.

## OAuth

Discovery: `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource/mcp`. Dynamic registration: `/register`.
Authorization/token/revocation endpoints: `/authorize`, `/token`, `/revoke`.

Supported client authentication: public clients (`none`), `client_secret_post`,
and `client_secret_basic`. Choose DCR or register a client and configure its issued
credentials in the platform. CIMD is not advertised. Redirect URIs are registered
exactly and must use HTTPS (loopback HTTP for development); wildcards are not accepted.
An absent resource parameter defaults to this server; a different resource is rejected.

The user sees the client name, callback host and permission before approving.
Google/Discord sign-in completes on the existing server callback, with a browser-bound
state. The temporary application token is immediately revoked and replaced with a
10-minute HttpOnly consent session. Application tokens are never sent to an AI client.
Consent POSTs require a CSRF token and same-origin Origin header.
Consent and authorization-management HTML uses `Referrer-Policy: strict-origin`
so native browser form POSTs keep their Origin while omitting paths/query parameters
from Referer. OAuth redirects retain `no-referrer`. Consent's `form-action` CSP
permits only this server and the registered callback origin for that transaction;
this lets browsers follow the POST redirect back to the AI client.

Authorization codes expire after two minutes and are consumed atomically. Access
tokens last at most one hour; refresh tokens have a 30-day inactivity limit and an
absolute 90-day authorization lifetime. Public clients receive rotating refresh
tokens with reuse detection; authenticated confidential clients receive stable
refresh tokens for Google's persistent account-linking flow. Credentials are stored hashed in SQLite, including
client secrets. Revoking any token invalidates the complete authorization, including
already-issued access tokens. Scopes cannot be expanded by refreshing.

Users can revoke individual authorizations at `/oauth/connections`, linked from the
web account page. Ordinary App device sessions and MCP authorizations are independent;
signing out of one does not implicitly revoke the other. Existing authorized clients
must be revoked here when no longer wanted.

## Connect clients

* **ChatGPT:** add a custom MCP server using the HTTPS `/mcp` URL; select OAuth and
  dynamic registration. The public URL can be connected without authentication.
  See [custom MCP servers](https://developers.openai.com/api/docs/guides/custom-mcp-server)
  and [authorization](https://developers.openai.com/plugins/build/auth).
* **Claude:** add a custom web connector with `/mcp`, select automatic registration,
  then connect with a YABus account. The server must be accessible from Anthropic's
  cloud, including when used from Claude Mobile. See [custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).
* **Gemini:** register the endpoint and OAuth discovery URL in Google Connected
  Console; test in Gemini Developer Mode, then complete Google's preview enrollment
  and publishing review. Do not claim public availability from a successful MCP test.
  See [Connected Apps](https://developers.google.com/connected-apps/mcp) and
  [OAuth](https://developers.google.com/connected-apps/mcp/oauth).

Suggested prompts: “找台北 307 的方向”, “查我的『上班』收藏到站時間”,
“列出我的收藏分類”. The assistant should ask for city/direction when ambiguous.

## Verification

### Reviewer account without Google/Discord verification

For review, use the optional, isolated YABus demo-password login. No account or
login is enabled without operator provisioning or a private bootstrap file.

#### Pterodactyl / hosts without a terminal

Upload the supplied private `mcp-review-bootstrap.json` beside the server's
`app/` directory, then restart after deploying the updated server code. The file
contains a username, a salted password hash and `enabled: true`; it does not
contain the plaintext password. The server creates the isolated sample account
automatically. Store the separately supplied plaintext credentials in your
password manager and in the submission portal's secure reviewer fields. Never
upload those credentials as website content or include them in a public ZIP.

Repeated starts with the same bootstrap file preserve the account, sample
document, password and existing authorizations. A replacement file with a new
password hash rotates access and revokes the previous sessions/authorizations.
The file is excluded from Git. Removing it does not delete or disable an already
provisioned account; to deliberately disable the login without a terminal, set
its `enabled` value to `false` in the panel file manager and restart. Leave it
enabled for ongoing review. Malformed bootstrap configuration prevents startup
without logging its contents.

If using the supplied server-update ZIP, extract it at the server root (the
directory containing `app/`). This is a private deployment bundle, not the
plugin ZIP for OpenAI. It includes the bootstrap file; no terminal or database
editing is required. It does not include a plaintext password.

#### Interactive terminal setup

Alternatively, run this inside the server's Python environment, using its normal
runtime configuration:

```powershell
python -m app.mcp_review setup --username yabus-review
```

Enter a unique 20–256 character password twice at the hidden prompts. The command
creates a separate, ordinary-role account with no Google/Discord identities or
application device tokens. It seeds the `送審示範` group with the Taipei 307 莒光
boarding stops `臺北客運板橋前站(藝文)` and `新月橋`, and a separate `測試其他`
route group. The password is stored only as a salted PBKDF2-SHA256 hash; the
command never prints it or accepts it as a command argument. Run setup again to
rotate the password, reset the sample document and revoke previous demo sessions
and MCP authorizations. This command cannot bind credentials to an existing user.

In the submission portal's secure reviewer fields, choose username/password and
enter the dedicated demo credentials. The login URL is the server's public HTTPS
origin plus `/oauth/review-login`; tenant/workspace is `N/A — isolated YABus demo
account`. Reviewers can sign in there first, then connect YABus from the AI client.
Alternatively, during connection, choose `使用示範帳號登入` on the YABus consent
page. After sign-in they must still approve `favorites:read`. Sample favorites
are ready to query; no registration, phone, email code or identity-provider login
is needed. Keep these credentials out of source control and plugin ZIPs.

This password grants only a ten-minute, HttpOnly consent session. It cannot be
used for application APIs, changing favorites or accessing another account. The
login form requires the normal same-origin CSRF check and limits attempts per
client and globally. Demo provisioning/login is refused if its account gains a
privileged role, a Google/Discord identity or an application device/session.

Keep the demo login working for ongoing review. Without a bootstrap file, the
terminal command below deliberately disables it and revokes all its sessions
and MCP authorizations. If a bootstrap file is present, set `enabled` to `false`
there as well so a later restart does not re-enable the account:

```powershell
python -m app.mcp_review disable
```

### Automated checks

`python -m pytest test_mcp.py -q` uses temporary databases, fake transit data and
synthetic account identities. It tests discovery, actual JSON-RPC requests, PKCE,
redirect/resource/scope rejection, CSRF, account isolation, token persistence,
refresh, revocation and public/private separation without contacting an identity
provider. Real provider login, platform directory submission and Android Gemini
preview access still require the deployed service and platform account.

Native Chromium form regressions additionally exercise approval, denial and
revocation without injecting an Origin header. They use isolated browser storage,
synthetic accounts and an in-process HTTP bridge; no identity provider is contacted.

```powershell
python -m pip install playwright
python -m playwright install chromium
$env:RUN_MCP_BROWSER_TESTS = "1"
python -m pytest test_mcp_browser.py -q
```

`MCP_TEST_BROWSER_EXECUTABLE` can optionally select an existing Chromium executable.
Without `RUN_MCP_BROWSER_TESTS=1`, these tests are skipped by the offline suite.

For the existing offline suite, exclude the two live TDX probe scripts:

```powershell
python -m pytest -q --ignore=test_tdx_metro.py --ignore=test_tdx_tra_station_of_line.py
```
