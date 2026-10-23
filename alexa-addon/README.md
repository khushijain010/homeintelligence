# Guardian Alexa+ add-on

Everything Guardian needs on its side is built: the MCP server at `/mcp`, OAuth, the legal pages, the
listing text (`listing.json`) and images (`assets/`). Deploying needs your Amazon developer account,
so these steps are yours to run.

## 1. Start Guardian on a public HTTPS URL

```bash
make keys            # once: RS256 key for access tokens (keys/jwt_private.pem)
make set-password    # once: paste the printed OWNER_PASSWORD_HASH into .env
make dev             # terminal 1
make tunnel          # terminal 2: prints https://<random>.trycloudflare.com
```

Put the tunnel URL in `.env` as `PUBLIC_BASE_URL=https://<random>.trycloudflare.com`, then restart
`make dev`. The OAuth `resource` and token audience are `PUBLIC_BASE_URL/mcp`, so they must match the
URL Alexa uses. A quick tunnel's URL changes each time it restarts; use a named Cloudflare tunnel
if you need it stable.

Also set in `.env`:

```
ENV=production
OWNER_EMAIL=you@example.com
SESSION_SECRET=<long random string>
ALEXA_OAUTH_CLIENT_ID=guardian-alexa
ALEXA_OAUTH_CLIENT_SECRET=<long random string>
ALEXA_OAUTH_REDIRECT_URIS=<the redirect URL(s) Alexa shows you in step 3>
```

Check it: `https://<tunnel>/.well-known/oauth-protected-resource` should show your `/mcp` URL.

## 2. Create the add-on

```bash
cd alexa-addon
alexa-ai configure
alexa-ai new mcp --name "Guardian" --locale en-US --mcp-server-url "https://<tunnel>/mcp"
```

## 3. Fill `addon-package/addon.json`

Copy the values from `listing.json` into the fields the CLI generated:

| addon.json field | value |
|---|---|
| name | `Guardian` |
| shortDescription | `listing.json` → shortDescription (≤ 123 chars) |
| fullDescription | `listing.json` → fullDescription (≤ 4000 chars) |
| examplePhrases | the 4 phrases in `listing.json` |
| privacy policy URL | `https://<tunnel>/legal/privacy` |
| terms of use URL | `https://<tunnel>/legal/terms` |
| icons | `assets/icon-72.png`, `-64`, `-88`, `-126`, `-180`, `-241` |
| carousel image | `assets/carousel-600x900.png` |
| account linking | authorization URL `https://<tunnel>/oauth/authorize`, token URL `https://<tunnel>/oauth/token`, client id/secret from `.env`, scopes `guardian.read guardian.act`, PKCE S256 |

Copy the redirect URL(s) the console shows for account linking into `ALEXA_OAUTH_REDIRECT_URIS`
and restart Guardian.

Regenerate the images any time with `uv run python scripts/make_addon_assets.py`.

## 4. Deploy and test

```bash
alexa-ai deploy
```

Open the Alexa+ web simulator, link the account (you'll see Guardian's login and consent page), then try:

- "Ask Guardian how things are at home."
- Run `make scenario name=leak_single_sensor`, then: "Ask Guardian to approve shutting off the water."
- "Ask Guardian what happened with the water leak."

Alexa+ only refreshes the tool list on deploy, so run `alexa-ai deploy` again after changing tools.

## Local testing without Alexa

Set `DEV_MCP_TOKEN=<anything>` in `.env` (ignored when `ENV=production`), then:

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/mcp --transport http \
  --header "Authorization: Bearer <DEV_MCP_TOKEN>" --method tools/list
```
