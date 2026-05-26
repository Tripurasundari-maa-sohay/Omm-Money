// Omm-Money GitHub Save Proxy — Cloudflare Worker
//
// Purpose: let the static GitHub Pages dashboard (net-wealth, portfolio) write
// committable JSON files (seed.json, holdings_cost.json) back to the repo
// without exposing a GitHub PAT in the browser.
//
// Flow:
//   browser  ──POST {path, content}──>  this worker  ──PUT /contents──>  github
//
// Auth model:
//   - Origin lock: only accepts requests where Origin header matches your
//     GitHub Pages site. CORS will block other browsers from calling.
//   - Path whitelist: only specific files in the repo can be written.
//   - The actual GitHub PAT lives in worker env var GITHUB_PAT, never in
//     the browser.
//
// Deploy:
//   1. dash.cloudflare.com → Workers & Pages → Create Worker
//   2. Paste this file's contents
//   3. Settings → Variables and Secrets → Add Secret
//        Name:  GITHUB_PAT
//        Value: your fine-grained PAT
//               (scoped to repo Tripurasundari-maa-sohay/Omm-Money,
//                permission: Contents = Read and write)
//   4. Save and Deploy
//   5. Copy the workers.dev URL
//
// Browser usage:
//   await fetch(WORKER_URL, {
//     method: 'POST',
//     headers: { 'Content-Type': 'application/json' },
//     body: JSON.stringify({
//       path: 'net-wealth/data/seed.json',
//       content: JSON.stringify(S.seed, null, 2),
//       message: 'seed: dashboard save'
//     })
//   });

const ALLOWED_ORIGIN = 'https://tripurasundari-maa-sohay.github.io';
const REPO = 'Tripurasundari-maa-sohay/Omm-Money';
const BRANCH = 'main';
const ALLOWED_PATHS = new Set([
  'net-wealth/data/seed.json',
  'portfolio/data/holdings_cost.json'
]);

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') return preflight();

    const origin = request.headers.get('Origin') || '';
    if (origin !== ALLOWED_ORIGIN) {
      return json({ error: 'origin denied', got: origin }, 403);
    }

    if (request.method !== 'POST') {
      return json({ error: 'method not allowed' }, 405);
    }

    let body;
    try { body = await request.json(); }
    catch { return json({ error: 'bad json body' }, 400); }

    const { path, content, message } = body || {};
    if (!ALLOWED_PATHS.has(path)) {
      return json({ error: 'path not allowed', path }, 403);
    }
    if (typeof content !== 'string') {
      return json({ error: 'content must be a string' }, 400);
    }

    const ghHeaders = {
      'Authorization': `Bearer ${env.GITHUB_PAT}`,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'OmmMoney-Worker',
      'X-GitHub-Api-Version': '2022-11-28'
    };

    // Get current file SHA (needed for update). 404 = first-time create.
    const getResp = await fetch(
      `https://api.github.com/repos/${REPO}/contents/${path}?ref=${BRANCH}`,
      { headers: ghHeaders }
    );
    if (!getResp.ok && getResp.status !== 404) {
      const txt = await getResp.text();
      return json({ error: 'github GET failed', status: getResp.status, body: txt }, 502);
    }
    const meta = getResp.ok ? await getResp.json() : null;

    // Encode content as base64 (UTF-8 safe).
    const b64 = btoa(unescape(encodeURIComponent(content)));

    const putBody = {
      message: message || `dashboard save: ${path} @ ${new Date().toISOString()}`,
      content: b64,
      branch: BRANCH,
      committer: { name: 'ODIN Dashboard', email: 'odin@bot.local' }
    };
    if (meta?.sha) putBody.sha = meta.sha;

    const putResp = await fetch(
      `https://api.github.com/repos/${REPO}/contents/${path}`,
      {
        method: 'PUT',
        headers: { ...ghHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify(putBody)
      }
    );

    if (!putResp.ok) {
      const txt = await putResp.text();
      return json({ error: 'github PUT failed', status: putResp.status, body: txt }, 502);
    }

    const result = await putResp.json();
    return json({
      ok: true,
      sha: result?.commit?.sha,
      url: result?.commit?.html_url,
      path
    }, 200);
  }
};

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400'
  };
}
function preflight() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}
function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...corsHeaders(), 'Content-Type': 'application/json' }
  });
}
