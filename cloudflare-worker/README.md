# Cloudflare Worker — SAP AI Core Proxy

Offloads the SAP AI Core OAuth + Claude inference HTTPS calls from the Render free-tier container (which OOMs trying to do them itself) onto Cloudflare's edge.

## Deploy (~5 min, free)

### 1. Install wrangler + login

```bash
cd cloudflare-worker
npm install
npx wrangler login   # opens browser, free Cloudflare account works
```

### 2. Set the 7 secrets (one-time)

```bash
npx wrangler secret put AICORE_ORCH_AUTH_URL
# paste: https://adesso-ai-nu15vkd3.authentication.eu10.hana.ondemand.com/oauth/token

npx wrangler secret put AICORE_ORCH_CLIENT_ID
# paste your CLIENT_ID

npx wrangler secret put AICORE_ORCH_CLIENT_SECRET
# paste your CLIENT_SECRET

npx wrangler secret put AICORE_ORCH_BASE_URL
# paste: https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com

npx wrangler secret put AICORE_ORCH_RESOURCE_GROUP
# paste: CPI

npx wrangler secret put AICORE_DIRECT_DEPLOYMENT_ID
# paste: db246f9f7a963785

npx wrangler secret put AICORE_DIRECT_MODEL_NAME
# paste: anthropic--claude-4.7-opus

# Generate a random shared secret for the bearer token:
openssl rand -hex 32  # copy this value
npx wrangler secret put PROXY_SECRET
# paste the random hex value
```

### 3. Deploy

```bash
npx wrangler deploy
```

Output gives you a URL like `https://chess-rocket-aicore-proxy.<your-subdomain>.workers.dev`. Save it.

### 4. Test

```bash
WORKER=https://chess-rocket-aicore-proxy.<your-subdomain>.workers.dev
SECRET=<the-PROXY_SECRET-you-just-generated>

curl -s "$WORKER/healthz"
# {"ok":true,"service":"chess-rocket-aicore-proxy"}

curl -s -X POST "$WORKER/v1/chat/completions" \
  -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hello in 3 words"}]}'
# {"content":"Hello there friend!", "stop_reason":"end_turn", "usage":{...}}
```

### 5. Wire the Render container to use the Worker

Set two env vars in Render dashboard → `chess-rocket-backend` → Environment:

| Key | Value |
|---|---|
| `AICORE_PROXY_URL` | `https://chess-rocket-aicore-proxy.<subdomain>.workers.dev` |
| `AICORE_PROXY_SECRET` | (same `PROXY_SECRET` you set in step 2) |

Then patch `scripts/sap_coach.py` to use the proxy when those vars are set (see `../scripts/sap_coach.py` — the `_call_llm` function checks for `AICORE_PROXY_URL` first and falls back to the direct path otherwise). Coming in next commit.

## Limits (free tier)

- 100,000 requests/day
- 10ms CPU per request (cached XSUAA token = always under this)
- 128 MB per request
- Globally distributed, no cold starts

For chess coaching usage that's effectively unlimited.