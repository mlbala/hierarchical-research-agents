# Security model and limitations

Research Hive is a **single-user, unauthenticated** application. There is no
login, and everyone who can reach the server shares one workspace: they can
see every project and report, start runs (which spend your API credits) and
cancel runs. The protections below defend that shared workspace against
other websites, untrusted web content and accidental exposure. **They are
not user authentication.**

## Deployment

* **Default: localhost only.** The server binds to `127.0.0.1:8000`
  (`HOST`, `PORT`) and accepts only the `Host` headers in `ALLOWED_HOSTS`.
* **Private network (deliberate opt-in).** To let other machines on a trusted
  network use it:

  ```ini
  HOST=0.0.0.0                       # or the machine's LAN address
  ALLOWED_HOSTS=localhost,127.0.0.1,research-hive.lan,192.168.1.20
  APP_ORIGIN=http://research-hive.lan:8000,http://192.168.1.20:8000
  ```

  Implications: every person on that network gets full, equal access,
  including starting paid live runs and reading all reports. Traffic is plain
  HTTP unless you put a TLS-terminating reverse proxy in front. Never expose
  the server to the public internet without adding real authentication in a
  proxy (for example an identity-aware proxy or HTTP basic auth over TLS).
* **Behind a reverse proxy.** Set `TRUSTED_PROXIES` to the proxy's IP so
  per-client rate limits use `X-Forwarded-For`; forwarded headers are ignored
  otherwise. Uvicorn runs with `proxy_headers=False`.

## Browser and API protections

| Threat | Control |
| --- | --- |
| Cross-site request forgery | Mutations require `Content-Type: application/json` **and** `X-Research-Hive: 1` (forces a CORS preflight); `Origin` must be in the explicit allowlist; `Sec-Fetch-Site: cross-site` is rejected |
| DNS rebinding against the API | `Host` header allowlist |
| CORS abuse | explicit origins only, never `*`, no credentials |
| Double submissions | required `Idempotency-Key` on run creation and re-run |
| Abuse / runaway cost | per-client run-creation rate limit and a queue cap (`MAX_QUEUED_RUNS`) |
| XSS via reports | server escapes raw HTML, drops unsafe links and images; browser renders Markdown with raw HTML skipped, a URL allowlist and no image loading; strict CSP (`script-src 'self'`, no inline scripts) |
| Clickjacking, sniffing | `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer` |
| Information leaks | consistent error envelope with a safe message and request ID; stack traces only in server logs |
| Input abuse | Pydantic validation of lengths, IDs, URLs, pagination bounds; body-size cap; state-transition checks |
| SQL injection | SQLAlchemy with bound parameters only (the schema name is a validated identifier) |

## Agent guardrails (enforced in code)

* **Untrusted content.** Web pages, search snippets, user input and other
  agents' outputs are wrapped in tagged blocks and labelled as data in every
  prompt; tag break-outs are neutralised. The demo fixtures include a
  prompt-injection page to show it is treated as data.
* **Tool permissions.** Only the two researchers may search or fetch pages;
  writers and leads read run-scoped evidence only. Enforced by
  `TOOL_PERMISSIONS` in `app/agents/roles.py` (`ToolPermissionError`).
* **No dangerous tools.** No shell, eval, code execution, filesystem, SQL or
  arbitrary HTTP tools exist. Tool arguments are validated with strict schemas.
* **Fixed structure.** Agents cannot create agents or tools, change limits or
  system prompts, or choose arbitrary next nodes: routing uses explicit path
  maps and code-computed decisions.
* **Validated outputs.** Every model output is parsed against a Pydantic
  schema before use; source references are run-local handles mapped by code;
  excerpts are verified verbatim; citations are validated and renumbered in
  code; run ownership of cited sources is checked in the database.
* **No hidden reasoning in the UI.** Only short action summaries and results
  are stored and shown.

## Safe web retrieval (SSRF)

* Only `http`/`https` on ports 80/443; no URL credentials; no backslashes or
  control characters; no single-label, `.local`, `.internal`, `localhost` or
  numeric-shorthand hosts (e.g. `127.1`).
* DNS is resolved by our own network backend **at connection time**; every
  returned address must be public (blocks loopback, private, link-local,
  shared, reserved, multicast, metadata, IPv4-mapped, 6to4, Teredo and NAT64
  ranges). The socket connects to one of those validated addresses, so DNS
  rebinding has no window. TLS still verifies the original hostname.
* Every redirect is re-validated; at most three redirects.
* No cookies, auth headers or ambient proxy settings (raw `httpcore` pool, no
  cookie jar, environment proxies ignored).
* Connect/read timeouts plus an overall deadline; streamed 2 MB cap enforced
  after decompression (zip bombs are stopped); HTML and plain text only; no
  JavaScript and no subresources.
* Fails closed whenever safety cannot be established.

## Secrets

* Database credentials and API keys are read only by the backend
  (`SecretStr`); there are no `VITE_*` secrets and nothing secret reaches the
  browser, URLs, reports, prompts, SSE payloads or logs.
* Logs and event payloads pass through a redaction step (sensitive keys, API
  key patterns, bearer tokens, database URLs, URL query strings).
* `.env`, `certs/` and key/certificate files are git-ignored; `.env.example`
  contains placeholders only. The UI shows configuration readiness (which
  keys are missing), never values.
* Database TLS uses `verify-full` with a CA bundle by default.

## Known limitations

* No authentication or per-user isolation (by design for version one).
* Citation validity proves a source exists and belongs to the run; whether it
  supports a claim relies on the research lead's model judgement plus
  verbatim-excerpt checks, which can still be wrong.
* Prompt injection is mitigated, not impossible: a crafted page could still
  bias a model's wording, though it cannot change tools, limits or routing.
* Rate limiting is in-memory and per process.
* robots.txt is not consulted; the fetcher identifies itself with a
  descriptive User-Agent and fetches only a few pages per run.
* Demo mode content is fictional and labelled as such.
