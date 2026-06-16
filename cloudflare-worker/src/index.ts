// Chess Rocket — SAP AI Core proxy on Cloudflare Workers.
//
// Why this exists:
//   The Render free tier (512 MB RAM) can't simultaneously run Stockfish +
//   Python interpreter + a big TLS HTTPS client speaking to SAP AI Core.
//   The OAuth handshake to XSUAA + Claude inference call double-OOMs the
//   container. By offloading those two TLS handshakes to a Cloudflare
//   Worker (free tier: 100k req/day, 128 MB per request, 0ms cold start)
//   the Render container only needs to open ONE local-ish HTTPS connection
//   to *.workers.dev, not two heavy ones to SAP BTP's IDM + AI Core.
//
// Endpoints:
//   POST /v1/chat/completions
//     Body: {
//       "messages":     [{"role": "system|user|assistant", "content": "..."}],
//       "max_tokens":   number  (default 1024)
//       "temperature":  number  (default 0.4)
//     }
//     Returns: { "content": "...", "stop_reason": "...", "usage": {...} }
//
//   GET /healthz
//     Returns: { ok: true } — used by Render to check Worker is up.
//
// Auth:
//   Worker reads SAP AI Core OAuth credentials from environment secrets
//   (set via `wrangler secret put`). XSUAA token is cached in-memory inside
//   the Worker instance (typically lives ~5–15 min between cold starts) +
//   in Cloudflare KV for cross-instance sharing.
//
// Required secrets (set via wrangler):
//   AICORE_ORCH_AUTH_URL       — XSUAA token endpoint
//   AICORE_ORCH_CLIENT_ID      — XSUAA client id
//   AICORE_ORCH_CLIENT_SECRET  — XSUAA client secret
//   AICORE_ORCH_BASE_URL       — AI Core base URL (no trailing slash)
//   AICORE_ORCH_RESOURCE_GROUP — defaults to "CPI"
//   AICORE_DIRECT_DEPLOYMENT_ID — Claude deployment id
//   AICORE_DIRECT_MODEL_NAME   — defaults to "anthropic--claude-4.7-opus"
//   PROXY_SECRET               — shared bearer token; clients must send
//                                "Authorization: Bearer <PROXY_SECRET>"

export interface Env {
  AICORE_ORCH_AUTH_URL: string;
  AICORE_ORCH_CLIENT_ID: string;
  AICORE_ORCH_CLIENT_SECRET: string;
  AICORE_ORCH_BASE_URL: string;
  AICORE_ORCH_RESOURCE_GROUP?: string;
  AICORE_DIRECT_DEPLOYMENT_ID: string;
  AICORE_DIRECT_MODEL_NAME?: string;
  PROXY_SECRET: string;
  // Optional KV namespace for cross-instance token caching. If not bound
  // the Worker still works (per-instance memory cache only).
  TOKEN_CACHE?: KVNamespace;
}

interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

interface ChatRequest {
  messages: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
}

interface ChatResponse {
  content: string;
  stop_reason?: string;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
  };
}

// In-instance memory cache. Survives across invocations on the same
// Worker isolate (~5-15 min typical lifetime).
let _tokenCache: { token: string; expires_at: number } | null = null;

// Fetch & cache XSUAA OAuth token. Same logic as scripts/sap_coach.py
// `_get_token`, just rewritten in TS.
async function getToken(env: Env): Promise<string> {
  const now = Date.now() / 1000;

  // 1. Check in-memory cache first (fastest path).
  if (_tokenCache && _tokenCache.expires_at > now + 60) {
    return _tokenCache.token;
  }

  // 2. Check KV cache (survives Worker restarts, costs 1 read).
  if (env.TOKEN_CACHE) {
    try {
      const cached = await env.TOKEN_CACHE.get('xsuaa_token', { type: 'json' });
      if (cached && (cached as any).expires_at > now + 60) {
        _tokenCache = cached as any;
        return (cached as any).token;
      }
    } catch (_) {
      // KV unavailable, fall through to token fetch.
    }
  }

  // 3. Fetch fresh token from SAP IDM (XSUAA).
  const credentials = `${env.AICORE_ORCH_CLIENT_ID}:${env.AICORE_ORCH_CLIENT_SECRET}`;
  const auth = btoa(credentials);
  const params = new URLSearchParams({ grant_type: 'client_credentials' });

  const r = await fetch(env.AICORE_ORCH_AUTH_URL, {
    method: 'POST',
    headers: {
      Authorization: `Basic ${auth}`,
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'application/json',
    },
    body: params.toString(),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`XSUAA token fetch failed: HTTP ${r.status} ${text.slice(0, 200)}`);
  }
  const j = (await r.json()) as { access_token: string; expires_in: number };

  // SAP IDM returns expires_in in seconds; we cache slightly less to be safe.
  const expires_at = now + (j.expires_in || 3600) - 30;
  _tokenCache = { token: j.access_token, expires_at };

  // Best-effort write to KV. Non-blocking — don't fail the request if KV is down.
  if (env.TOKEN_CACHE) {
    try {
      await env.TOKEN_CACHE.put('xsuaa_token', JSON.stringify(_tokenCache), {
        expirationTtl: Math.max(60, Math.floor(j.expires_in || 3600) - 30),
      });
    } catch (_) {
      // ignore
    }
  }

  return j.access_token;
}

// Call SAP AI Core orchestration: Claude /chat/completions endpoint.
async function callClaude(env: Env, req: ChatRequest): Promise<ChatResponse> {
  const token = await getToken(env);
  const base = env.AICORE_ORCH_BASE_URL.replace(/\/+$/, '');
  const dep = env.AICORE_DIRECT_DEPLOYMENT_ID;
  const url = `${base}/v2/inference/deployments/${dep}/invoke`;
  // ↑ /invoke matches sap_coach.py line 114; if your AI Core deployment
  //   uses a different path (e.g. /chat/completions) update both places.

  const rg = env.AICORE_ORCH_RESOURCE_GROUP || 'CPI';
  const model = env.AICORE_DIRECT_MODEL_NAME || 'anthropic--claude-4.7-opus';

  // Anthropic Messages API shape — split out system from messages.
  const systemMessages = req.messages.filter((m) => m.role === 'system');
  const otherMessages = req.messages.filter((m) => m.role !== 'system');
  const system = systemMessages.map((m) => m.content).join('\n\n');

  // Claude 4.7 Opus on AI Core rejects `temperature` ("deprecated for this
  // model"). Newer Claude reasoning-class models pick their own sampling.
  // We keep the field optional so older Anthropic models on the same proxy
  // still get the requested temperature.
  const body: Record<string, unknown> = {
    anthropic_version: 'bedrock-2023-05-31',
    max_tokens: req.max_tokens ?? 1024,
    system: system || undefined,
    messages: otherMessages.map((m) => ({ role: m.role, content: m.content })),
  };
  const isOpus47 = (model || '').toLowerCase().includes('claude-4.7');
  if (req.temperature !== undefined && !isOpus47) {
    body.temperature = req.temperature;
  }

  const r = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'AI-Resource-Group': rg,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!r.ok) {
    const text = await r.text();
    throw new Error(`AI Core invoke failed: HTTP ${r.status} ${text.slice(0, 500)}`);
  }
  const j = (await r.json()) as any;

  // Anthropic on Bedrock returns { content: [{type:"text", text:"..."}], stop_reason, usage }
  let content = '';
  if (Array.isArray(j.content)) {
    content = j.content
      .filter((b: any) => b.type === 'text')
      .map((b: any) => b.text)
      .join('');
  } else if (typeof j.content === 'string') {
    content = j.content;
  } else if (j.choices?.[0]?.message?.content) {
    // OpenAI-shape fallback
    content = j.choices[0].message.content;
  }

  return {
    content,
    stop_reason: j.stop_reason,
    usage: j.usage,
  };
}

// Minimal CORS — accept any Origin for now. Tighten if you want only the
// Render URL. Workers are stateless so this is purely a header thing.
function corsHeaders(origin: string): Record<string, string> {
  return {
    'Access-Control-Allow-Origin': origin || '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    'Access-Control-Max-Age': '600',
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin') || '';
    const baseHeaders = {
      'Content-Type': 'application/json; charset=utf-8',
      ...corsHeaders(origin),
    };

    // CORS preflight.
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    // Health probe — used by Render to verify Worker is reachable on boot.
    if (url.pathname === '/healthz' && request.method === 'GET') {
      return new Response(
        JSON.stringify({ ok: true, service: 'chess-rocket-aicore-proxy' }),
        { headers: baseHeaders }
      );
    }

    // The only non-trivial route. Same shape as Anthropic's Messages API
    // so the Render-side wrapper is a thin one-liner.
    if (url.pathname === '/v1/chat/completions' && request.method === 'POST') {
      // Bearer-token auth: anyone with PROXY_SECRET can use the Worker.
      // This is a coarse guard — fine for personal use; for multi-tenant
      // you'd want per-user tokens, rate-limiting, etc.
      //
      // We trim both sides because wrangler's "secret put" interactive prompt
      // can leave a trailing newline in the stored value (silent gotcha).
      const authHeader = (request.headers.get('Authorization') || '').trim();
      const expected = `Bearer ${(env.PROXY_SECRET || '').trim()}`;
      if (authHeader !== expected) {
        return new Response(
          JSON.stringify({
            error: 'unauthorized',
            // Debug hint: show how many chars we received vs expected, never
            // the secret itself. Lets us tell whitespace from wrong-token.
            hint: `received auth len=${authHeader.length}, expected len=${expected.length}`,
          }),
          { status: 401, headers: baseHeaders }
        );
      }

      let body: ChatRequest;
      try {
        body = await request.json();
      } catch (_) {
        return new Response(JSON.stringify({ error: 'invalid JSON body' }), {
          status: 400,
          headers: baseHeaders,
        });
      }
      if (!Array.isArray(body.messages) || body.messages.length === 0) {
        return new Response(
          JSON.stringify({ error: 'messages array required' }),
          { status: 400, headers: baseHeaders }
        );
      }

      try {
        const out = await callClaude(env, body);
        return new Response(JSON.stringify(out), { headers: baseHeaders });
      } catch (e: any) {
        // Surface the AI Core error verbatim so the Render server's logs
        // show what went wrong (auth failure, deployment not found, etc.).
        return new Response(
          JSON.stringify({ error: String(e?.message || e) }),
          { status: 502, headers: baseHeaders }
        );
      }
    }

    return new Response(JSON.stringify({ error: 'not found' }), {
      status: 404,
      headers: baseHeaders,
    });
  },
} satisfies ExportedHandler<Env>;