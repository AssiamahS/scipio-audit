// Scipio API Worker - Cloudflare Workers
// Proxies GitHub API calls so frontend never sees the token
// Deploy: npx wrangler deploy
//
// Environment secrets (set via `wrangler secret put`):
//   GITHUB_TOKEN  - GitHub Personal Access Token with repo scope
//
// Environment variables (set in wrangler.toml):
//   REPO_OWNER    - GitHub username (e.g. "AssiamahS")
//   REPO_NAME     - Repo name (e.g. "scipio")
//   ALLOWED_ORIGIN - Dashboard URL (e.g. "https://assiamahs.github.io")

const CORS_HEADERS = {
  'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Scipio-Profile',
  'Access-Control-Max-Age': '86400',
};

function corsHeaders(request, env) {
  const origin = request.headers.get('Origin') || '';
  const allowed = (env.ALLOWED_ORIGIN || 'https://assiamahs.github.io');
  // Allow the configured origin + localhost for dev
  const isAllowed = origin.startsWith(allowed) || origin.includes('localhost') || origin.includes('127.0.0.1');
  return {
    ...CORS_HEADERS,
    'Access-Control-Allow-Origin': isAllowed ? origin : allowed,
  };
}

function json(data, status = 200, request, env) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders(request, env) },
  });
}

async function ghFetch(env, path, opts = {}) {
  const url = `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/contents/${path}`;
  return fetch(url, {
    ...opts,
    headers: {
      'Authorization': `token ${env.GITHUB_TOKEN}`,
      'Accept': 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
      'User-Agent': 'Scipio-Worker/1.0',
      ...(opts.headers || {}),
    },
  });
}

// Read a JSON file from the repo
async function readFile(env, path) {
  const r = await ghFetch(env, path);
  if (r.status === 404) return { data: null, sha: null };
  if (!r.ok) throw new Error(`GitHub API ${r.status}: ${await r.text()}`);
  const file = await r.json();
  const content = atob(file.content.replace(/\n/g, ''));
  return { data: JSON.parse(content), sha: file.sha };
}

// Write a JSON file to the repo
async function writeFile(env, path, data, sha, message) {
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(data, null, 2))));
  const body = { message: message || `Update ${path}`, content };
  if (sha) body.sha = sha;

  const r = await ghFetch(env, path, {
    method: 'PUT',
    body: JSON.stringify(body),
  });

  if (!r.ok) {
    const err = await r.json();
    throw new Error(err.message || `GitHub write failed: ${r.status}`);
  }

  const result = await r.json();
  return { sha: result.content.sha };
}

// === ROUTE HANDLERS ===

// GET /api/jobs - Get all jobs
async function getJobs(env) {
  const { data, sha } = await readFile(env, 'jobs.json');
  return { jobs: data?.jobs || [], next_id: data?.next_id || 1, sha };
}

// POST /api/jobs - Add a new job
async function addJob(env, job) {
  const { data, sha } = await readFile(env, 'jobs.json');
  const db = data || { jobs: [], next_id: 1 };
  job.id = db.next_id++;
  job.applied_date = job.applied_date || new Date().toISOString().slice(0, 10);
  job.updated_date = new Date().toISOString().slice(0, 10);
  job.history = job.history || [{ status: job.status || 'wishlist', date: new Date().toISOString().slice(0, 16).replace('T', ' ') }];
  db.jobs.push(job);
  const result = await writeFile(env, 'jobs.json', db, sha, `Added: ${job.company} - ${job.role}`);
  return { job, sha: result.sha };
}

// PUT /api/jobs/:id - Update a job
async function updateJob(env, id, updates) {
  const { data, sha } = await readFile(env, 'jobs.json');
  if (!data) throw new Error('No jobs data');
  const job = data.jobs.find(j => j.id === id);
  if (!job) throw new Error(`Job ${id} not found`);
  Object.assign(job, updates, { updated_date: new Date().toISOString().slice(0, 10) });
  if (updates.status) {
    job.history = job.history || [];
    job.history.push({ status: updates.status, date: new Date().toISOString().slice(0, 16).replace('T', ' ') });
  }
  const result = await writeFile(env, 'jobs.json', data, sha, `Update: ${job.company} - ${updates.status || 'edit'}`);
  return { job, sha: result.sha };
}

// DELETE /api/jobs/:id - Remove a job
async function deleteJob(env, id) {
  const { data, sha } = await readFile(env, 'jobs.json');
  if (!data) throw new Error('No jobs data');
  data.jobs = data.jobs.filter(j => j.id !== id);
  const result = await writeFile(env, 'jobs.json', data, sha, `Removed job ${id}`);
  return { removed: id, sha: result.sha };
}

// GET /api/profile/:id - Get a user profile
async function getProfile(env, profileId) {
  const path = profileId ? `profiles/${profileId}.json` : 'profiles/default.json';
  const { data, sha } = await readFile(env, path);
  return { profile: data, sha };
}

// PUT /api/profile/:id - Save/update a user profile
async function saveProfile(env, profileId, profile) {
  const path = profileId ? `profiles/${profileId}.json` : 'profiles/default.json';
  let sha = null;
  try {
    const existing = await readFile(env, path);
    sha = existing.sha;
  } catch (e) { /* file doesn't exist yet */ }
  const result = await writeFile(env, path, profile, sha, `Profile: ${profile.first_name} ${profile.last_name}`);
  return { profile, sha: result.sha };
}

// Start the cloudbox codespace; its postStart hook runs the scipio slyci
// pipeline, then the idle timeout shuts it down. 'already available' = it
// was awake, which is fine — the runner processes the queue on each pass.
//
// The pinned CODESPACE_NAME dies whenever the codespace is deleted (GitHub
// retention-reaps stopped codespaces) — then every wake 404s forever with
// nothing but a console.log to show for it. On 404, re-resolve the name by
// listing the cloudbox repo's codespaces; if none exist, open a GitHub
// issue on scipio so the failure lands somewhere a human actually looks.
const ghHeaders = (env) => ({
  'Authorization': `token ${env.GITHUB_TOKEN}`,
  'Accept': 'application/vnd.github.v3+json',
  'User-Agent': 'Scipio-Worker/1.0',
});

async function startByName(env, name) {
  const r = await fetch(`https://api.github.com/user/codespaces/${name}/start`, {
    method: 'POST',
    headers: ghHeaders(env),
  });
  const data = await r.json().catch(() => ({}));
  return { status: r.status, state: data.state || data.message || 'unknown', name };
}

async function raiseWakeAlarm(env, detail) {
  // dedupe: skip if an open wake-failure issue already exists
  const q = await fetch(
    `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/issues?state=open&labels=wake-failure&per_page=1`,
    { headers: ghHeaders(env) });
  const open = await q.json().catch(() => []);
  if (Array.isArray(open) && open.length) return { alarmed: 'already-open' };
  const r = await fetch(
    `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/issues`,
    {
      method: 'POST',
      headers: ghHeaders(env),
      body: JSON.stringify({
        title: 'cloudbox wake FAILED — daily applies are not running',
        body: `The scipio-api worker could not start the cloudbox codespace.\n\n\`\`\`\n${detail}\n\`\`\`\nRecreate the codespace on AssiamahS/cloudbox (or fix the PAT), then update CODESPACE_NAME in worker/wrangler.toml if the name changed and redeploy.`,
        labels: ['wake-failure'],
      }),
    });
  return { alarmed: r.status };
}

async function startCodespace(env) {
  let result = await startByName(env, env.CODESPACE_NAME);
  if (result.status === 404) {
    // pinned name is gone — find any codespace on the cloudbox repo
    const list = await fetch('https://api.github.com/user/codespaces?per_page=100',
      { headers: ghHeaders(env) });
    const data = await list.json().catch(() => ({}));
    const boxes = (data.codespaces || []).filter(
      c => c.repository && c.repository.full_name === 'AssiamahS/cloudbox');
    if (boxes.length) {
      result = await startByName(env, boxes[0].name);
      result.resolved_from = 'repo-list';
    } else {
      const alarm = await raiseWakeAlarm(env,
        `start ${env.CODESPACE_NAME} -> 404 and no codespace exists on AssiamahS/cloudbox`);
      result = { ...result, ...alarm };
    }
  } else if (result.status >= 400 && result.status !== 409) {
    // 401 = PAT dead/expired, 403 = rate-limit or scope loss — also alarm
    const alarm = await raiseWakeAlarm(env,
      `start ${env.CODESPACE_NAME} -> ${result.status} ${result.state}`);
    result = { ...result, ...alarm };
  }
  return result;
}

// The start API answers 409 "already running" for the whole ShuttingDown
// window. Respond to the caller immediately, then keep retrying in the
// background until the start sticks — the queue entry is already committed,
// so the runner will find it whenever the box comes up.
function startCodespaceWithRetry(env, ctx) {
  return startCodespace(env).then(first => {
    if (first.status === 409 && ctx) {
      ctx.waitUntil((async () => {
        for (let i = 0; i < 6; i++) {
          await new Promise(r => setTimeout(r, 20000));
          const again = await startCodespace(env);
          console.log(`start retry ${i + 1}:`, JSON.stringify(again));
          if (again.status !== 409) return;
        }
      })());
      first.retrying = true;
    }
    return first;
  });
}

// === MAIN ROUTER ===
export default {
  // Cloudflare cron (see wrangler.toml): daily wake for the 13:00 UTC batch.
  async scheduled(event, env, ctx) {
    const wake = await startCodespaceWithRetry(env, ctx);
    console.log('cron wake:', JSON.stringify(wake));
  },
  async fetch(request, env, ctx) {
    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    try {
      // GET /api/jobs
      if (path === '/api/jobs' && request.method === 'GET') {
        const result = await getJobs(env);
        return json(result, 200, request, env);
      }

      // POST /api/jobs
      if (path === '/api/jobs' && request.method === 'POST') {
        const body = await request.json();
        const result = await addJob(env, body);
        return json(result, 201, request, env);
      }

      // PUT /api/jobs/:id
      const jobMatch = path.match(/^\/api\/jobs\/(\d+)$/);
      if (jobMatch && request.method === 'PUT') {
        const body = await request.json();
        const result = await updateJob(env, parseInt(jobMatch[1]), body);
        return json(result, 200, request, env);
      }

      // DELETE /api/jobs/:id
      if (jobMatch && request.method === 'DELETE') {
        const result = await deleteJob(env, parseInt(jobMatch[1]));
        return json(result, 200, request, env);
      }

      // GET /api/profile/:id?
      const profileMatch = path.match(/^\/api\/profile(?:\/(.+))?$/);
      if (profileMatch && request.method === 'GET') {
        const result = await getProfile(env, profileMatch[1]);
        return json(result, 200, request, env);
      }

      // PUT /api/profile/:id?
      if (profileMatch && request.method === 'PUT') {
        const body = await request.json();
        const result = await saveProfile(env, profileMatch[1], body);
        return json(result, 200, request, env);
      }

      // POST /api/apply {url, dry_run?} — paste a job URL, the codespace
      // runner applies unattended. GitHub Actions is out of minutes (private
      // repo), so this commits a queue entry to the repo, then wakes the
      // cloudbox codespace whose postStart runner processes the queue.
      if (path === '/api/apply' && request.method === 'POST') {
        const body = await request.json().catch(() => ({}));
        const jobUrl = String(body.url || '').trim();
        let parsed;
        try { parsed = new URL(jobUrl); } catch { return json({ error: 'not a valid URL' }, 400, request, env); }
        if (parsed.protocol !== 'https:') return json({ error: 'https URLs only' }, 400, request, env);

        // Same ATS set applier.py handles; anything else still runs but is
        // flagged so the caller knows it may land in bad_url quarantine.
        const KNOWN = ['greenhouse.io', 'lever.co', 'myworkdayjobs.com', 'workday.com', 'ashbyhq.com'];
        const knownAts = KNOWN.some(d => parsed.hostname === d || parsed.hostname.endsWith('.' + d));

        // 1. append to queue.json via the contents API
        const getR = await ghFetch(env, 'queue.json');
        if (!getR.ok) return json({ error: `queue read failed (${getR.status})` }, 502, request, env);
        const file = await getR.json();
        let queue;
        try { queue = JSON.parse(atob(file.content.replace(/\n/g, ''))); } catch { queue = []; }
        queue.push({
          url: jobUrl,
          dry_run: !!body.dry_run,
          ts: new Date().toISOString(),
          status: 'pending',
        });
        const putR = await ghFetch(env, 'queue.json', {
          method: 'PUT',
          body: JSON.stringify({
            message: `queue: ${parsed.hostname}${body.dry_run ? ' (dry run)' : ''}`,
            content: btoa(JSON.stringify(queue, null, 2) + '\n'),
            sha: file.sha,
          }),
        });
        if (!putR.ok) return json({ error: `queue write failed (${putR.status})` }, 502, request, env);

        // 2. wake the codespace runner (retries in background on 409)
        const wake = await startCodespaceWithRetry(env, ctx);

        return json({
          queued: true,
          url: jobUrl,
          dry_run: !!body.dry_run,
          known_ats: knownAts,
          runner: wake,
          watch: `https://github.com/${env.REPO_OWNER}/${env.REPO_NAME}/commits/main`,
        }, 200, request, env);
      }

      // Health check
      if (path === '/api/health') {
        return json({ status: 'ok', version: '1.0.0' }, 200, request, env);
      }

      return json({ error: 'Not found' }, 404, request, env);
    } catch (e) {
      console.error('Worker error:', e);
      return json({ error: e.message }, 500, request, env);
    }
  },
};
