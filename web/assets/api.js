/**
 * Thin client for the platform API.
 *
 * Everything goes through the edge on the same origin, so there is no CORS and
 * no base URL to configure: the page works identically behind localhost and
 * behind a CloudFront domain.
 */

const BASE = "/api/v1";

export class ApiError extends Error {
    constructor(status, problem) {
        super(problem?.detail || problem?.title || `HTTP ${status}`);
        this.status = status;
        this.problem = problem ?? {};
        this.code = problem?.type?.split("/").pop() ?? "error";
        this.requestId = problem?.request_id;
    }
}

async function request(path, { method = "GET", body, headers = {} } = {}) {
    const response = await fetch(path, {
        method,
        // Session cookies are what authenticate a browser caller.
        credentials: "same-origin",
        headers: body ? { "content-type": "application/json", ...headers } : headers,
        body: body === undefined ? undefined : JSON.stringify(body),
    });

    if (response.status === 204) return null;

    const text = await response.text();
    const payload = text ? safeJson(text) : null;

    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
}

function safeJson(text) {
    try {
        return JSON.parse(text);
    } catch {
        return { title: text.slice(0, 200) };
    }
}

/** A stable-enough idempotency key for a browser-initiated publication. */
export function newIdempotencyKey() {
    return `web-${crypto.randomUUID()}`;
}

export const api = {
    listDatasets: (params = {}) =>
        request(`${BASE}/datasets?${new URLSearchParams(params)}`),
    getDataset: (id) => request(`${BASE}/datasets/${id}`),
    listVersions: (id) => request(`${BASE}/datasets/${id}/versions`),
    getCurrent: (id) => request(`${BASE}/datasets/${id}/current`),
    rollback: (id, seq) =>
        request(`${BASE}/datasets/${id}/current`, { method: "PUT", body: { seq } }),

    listPublications: (params = {}) =>
        request(`${BASE}/publications?${new URLSearchParams(params)}`),
    getPublication: (jobId) => request(`${BASE}/publications/${jobId}`),
    retryPublication: (jobId) =>
        request(`${BASE}/publications/${jobId}/retry`, { method: "POST" }),
    createPublication: (body, idempotencyKey) =>
        request(`${BASE}/publications`, {
            method: "POST",
            body,
            headers: { "Idempotency-Key": idempotencyKey },
        }),

    createDemoUpload: (body) => request(`${BASE}/demo/uploads`, { method: "POST", body }),
    createTileSession: (datasetId) =>
        request(`${BASE}/tiles/session`, { method: "POST", body: { dataset_id: datasetId } }),

    // --- auth (BetterAuth, passed through by the gateway untouched) ---
    session: () => request("/api/auth/get-session"),
    signIn: (email, password) =>
        request("/api/auth/sign-in/email", { method: "POST", body: { email, password } }),
    signOut: () => request("/api/auth/sign-out", { method: "POST", body: {} }),
    listOrganizations: () => request("/api/auth/organization/list"),
    setActiveOrganization: (organizationId) =>
        request("/api/auth/organization/set-active", {
            method: "POST",
            body: { organizationId },
        }),
};

/** Poll a publication job until it reaches a terminal state. */
export async function waitForJob(jobId, { onUpdate, intervalMs = 1000, timeoutMs = 120000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        const job = await api.getPublication(jobId);
        onUpdate?.(job);
        if (job.status === "SUCCEEDED" || job.status === "FAILED") return job;
        if (Date.now() > deadline) throw new Error(`job ${jobId} did not finish in time`);
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
}

export function formatBytes(bytes) {
    if (bytes == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatTime(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
    });
}

export function pill(value) {
    const span = document.createElement("span");
    span.className = `pill pill-${value}`;
    span.textContent = value;
    return span;
}
