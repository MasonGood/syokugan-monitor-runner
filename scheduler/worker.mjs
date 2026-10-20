const SCHEDULES = new Map([
  ["2,17,32,47 0-12,23 * * *", "day"],
  ["2 13,15,17,19,21 * * *", "night"],
]);
const LEASE_MS = 180000;
const RECOVERY_WINDOW_MS = 86400000;
const MAX_ATTEMPTS = 3;

export function slotId(controller) {
  const schedule = SCHEDULES.get(controller.cron);
  if (!schedule || !Number.isFinite(controller.scheduledTime)) {
    throw new Error("Invalid scheduled event");
  }
  return `syokugan-${Math.floor(controller.scheduledTime / 60000)}-${schedule}`;
}

function headers(env) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "syokugan-scheduler",
    "Content-Type": "application/json",
  };
}

function workflowUrl(env) {
  return `https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/monitor.yml`;
}

async function finish(env, row, status, now, errorCode = null, runId = null) {
  await env.DB.prepare(
    `UPDATE dispatch_slots
     SET status = ?, updated_at = ?, error_code = ?, github_run_id = ?
     WHERE slot_id = ? AND lease_owner = ?`,
  ).bind(status, now, errorCode, runId, row.slot_id, row.lease_owner).run();
}

async function dispatch(env, row, deps) {
  const now = deps.now();
  const permission = await env.DB.prepare(
    `UPDATE dispatch_slots
     SET attempts = attempts + 1, status = 'pending', error_code = NULL,
         updated_at = ?, lease_until = ?
     WHERE slot_id = ? AND lease_owner = ? AND lease_until > ?
       AND attempts < ?
       AND (status = 'failed' OR (status = 'pending' AND attempts = 0))`,
  ).bind(now, now + LEASE_MS, row.slot_id, row.lease_owner, now, MAX_ATTEMPTS).run();
  if (permission.meta.changes !== 1) {
    deps.log({ event: "dispatch_not_authorized", slot_id: row.slot_id });
    return { status: "not_authorized" };
  }
  let response;
  try {
    response = await deps.fetch(`${workflowUrl(env)}/dispatches`, {
      method: "POST",
      headers: headers(env),
      body: JSON.stringify({
        ref: "main",
        inputs: { mode: "monitor", slot_id: row.slot_id },
      }),
      redirect: "manual",
      signal: AbortSignal.timeout(20000),
    });
  } catch {
    await finish(env, row, "uncertain", deps.now(), "dispatch_unconfirmed");
    deps.log({ event: "dispatch_unconfirmed", slot_id: row.slot_id });
    return { status: "uncertain" };
  }
  if (response.status === 204) {
    await finish(env, row, "dispatched", deps.now());
    deps.log({ event: "dispatch_accepted", slot_id: row.slot_id });
    return { status: "dispatched" };
  }
  const status = response.status >= 400 && response.status < 500 && response.status !== 408
    ? "failed"
    : "uncertain";
  await finish(env, row, status, deps.now(), `http_${response.status}`);
  deps.log({ event: "dispatch_failed", slot_id: row.slot_id, http_status: response.status });
  return { status };
}

async function lookupRun(env, row, deps) {
  // Bound the lookup, but never interpret a partial or failed lookup as absence.
  let totalCount;
  const seen = new Set();
  for (let page = 1; page <= 5; page += 1) {
    const url = new URL(`${workflowUrl(env)}/runs`);
    url.searchParams.set("event", "workflow_dispatch");
    url.searchParams.set("branch", "main");
    url.searchParams.set("created", `>=${new Date(row.created_at - 60000).toISOString()}`);
    url.searchParams.set("per_page", "100");
    url.searchParams.set("page", String(page));
    const response = await deps.fetch(url.toString(), {
      method: "GET",
      headers: headers(env),
      redirect: "manual",
      signal: AbortSignal.timeout(20000),
    });
    if (!response.ok) throw new Error("Run reconciliation unavailable");
    const data = await response.json();
    if (
      !Number.isSafeInteger(data.total_count) || data.total_count < 0 ||
      !Array.isArray(data.workflow_runs)
    ) {
      throw new Error("Invalid run reconciliation response");
    }
    if (totalCount !== undefined && totalCount !== data.total_count) {
      throw new Error("Run reconciliation changed during pagination");
    }
    totalCount = data.total_count;
    for (const run of data.workflow_runs) {
      if (
        !run || !Number.isSafeInteger(run.id) || run.id <= 0 ||
        typeof run.display_title !== "string" || !run.display_title ||
        seen.has(run.id)
      ) {
        throw new Error("Invalid run correlation record");
      }
      seen.add(run.id);
    }
    const match = data.workflow_runs.find(
      (run) => run.display_title === `monitor ${row.slot_id}`,
    );
    if (match) {
      if (match.id == null) throw new Error("Run reconciliation missing identifier");
      return String(match.id);
    }
    if (data.total_count <= page * 100) {
      if (data.workflow_runs.length !== data.total_count - (page - 1) * 100) {
        throw new Error("Incomplete run reconciliation");
      }
      return null;
    }
    if (data.workflow_runs.length !== 100) throw new Error("Incomplete run reconciliation");
  }
  throw new Error("Run reconciliation page limit reached");
}

async function recover(env, deps) {
  const now = deps.now();
  const { results } = await env.DB.prepare(
    `SELECT * FROM dispatch_slots
     WHERE status IN ('pending', 'failed', 'uncertain') AND lease_until < ?
     ORDER BY lease_until, created_at, slot_id LIMIT 3`,
  ).bind(now).all();
  for (const stale of results) {
    const claimTime = deps.now();
    const owner = crypto.randomUUID();
    const claim = await env.DB.prepare(
      `UPDATE dispatch_slots SET lease_owner = ?, lease_until = ?
       WHERE slot_id = ? AND lease_until < ?
       AND status IN ('pending', 'failed', 'uncertain')`,
    ).bind(owner, claimTime + LEASE_MS, stale.slot_id, claimTime).run();
    if (claim.meta.changes !== 1) continue;
    const row = await env.DB.prepare(
      "SELECT * FROM dispatch_slots WHERE slot_id = ? AND lease_owner = ?",
    ).bind(stale.slot_id, owner).first();
    if (!row) continue;
    const exhausted = () =>
      deps.now() - row.created_at > RECOVERY_WINDOW_MS || row.attempts >= MAX_ATTEMPTS;
    let runId;
    try {
      runId = await lookupRun(env, row, deps);
    } catch {
      if (exhausted()) {
        await finish(env, row, "needs_attention", deps.now(), "recovery_limit");
      }
      deps.log({ event: "reconciliation_unavailable", slot_id: row.slot_id });
      continue;
    }
    if (runId !== null) {
      await finish(env, row, "dispatched", deps.now(), null, runId);
      deps.log({ event: "dispatch_reconciled", slot_id: row.slot_id, run_id: runId });
      continue;
    }
    if (exhausted()) {
      await finish(env, row, "needs_attention", deps.now(), "recovery_limit");
      deps.log({ event: "needs_attention", slot_id: row.slot_id });
      continue;
    }
    // Absence in an eventually consistent API cannot prove a POST was rejected.
    if (row.status !== "failed" && !(row.status === "pending" && row.attempts === 0)) {
      await finish(env, row, "uncertain", deps.now(), "dispatch_absence_unconfirmed");
      deps.log({ event: "dispatch_still_uncertain", slot_id: row.slot_id });
      continue;
    }
    await dispatch(env, row, deps);
  }
}

export async function handleScheduled(controller, env, overrides = {}) {
  const deps = {
    now: Date.now,
    fetch: (url, options) => fetch(url, options),
    log: (entry) => console.log(JSON.stringify(entry)),
    ...overrides,
  };
  if (env.DISPATCH_ENABLED !== "true") {
    return { status: "disabled" };
  }
  if (
    !env.DB || !env.GITHUB_TOKEN ||
    !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPOSITORY ?? "")
  ) {
    throw new Error("Scheduler configuration incomplete");
  }
  const id = slotId(controller);
  const now = deps.now();
  const owner = crypto.randomUUID();
  const claim = await env.DB.prepare(
    `INSERT INTO dispatch_slots (
      slot_id, cron, scheduled_minute, status, lease_owner,
      lease_until, created_at, updated_at
     ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
     ON CONFLICT(slot_id) DO NOTHING`,
  ).bind(
    id, controller.cron, Math.floor(controller.scheduledTime / 60000),
    owner, now + LEASE_MS, now, now,
  ).run();
  let result = { status: "duplicate" };
  if (claim.meta.changes === 1) {
    result = await dispatch(env, { slot_id: id, lease_owner: owner }, deps);
  }
  // A duplicate may also perform recovery, but per-row leases serialize it.
  await recover(env, deps);
  await env.DB.prepare(
    `DELETE FROM dispatch_slots
     WHERE status = 'dispatched' AND created_at < ?`,
  ).bind(now - 30 * RECOVERY_WINDOW_MS).run();
  return result;
}

export default {
  async scheduled(controller, env) {
    try {
      await handleScheduled(controller, env);
    } catch {
      console.error("scheduler_failed: inspect dispatch state and configuration");
      throw new Error("Scheduler failed");
    }
  },
  async fetch() {
    return new Response("syokugan scheduler", {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
