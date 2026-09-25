/**
 * Demo upload flow plus the tenant's publication jobs.
 *
 * The form reports only the two steps it owns:
 * 1. Upload to the customer's private staging bucket. The file is hashed in the
 *    browser and the digest is bound into a presigned PUT, so the bytes that
 *    arrive are provably the bytes that were declared. The URL is rewritten onto
 *    the edge origin, which is why no CORS configuration is needed.
 * 2. POST /publications, which returns the job id.
 *
 * What happens to the job afterwards is shown in the jobs table, kept live by a
 * Server-Sent Events stream the backend feeds from the Kafka topics.
 */

import {
    ApiError,
    api,
    formatTime,
    newIdempotencyKey,
    notify as showNotice,
    pill,
} from "./api.js";

const form = document.getElementById("upload-form");
const submit = document.getElementById("submit");
const alertEl = document.getElementById("alert");
const progressCard = document.getElementById("progress-card");
const stepsEl = document.getElementById("steps");
const jobsEl = document.getElementById("jobs");
const liveEl = document.getElementById("live");

const notify = (message, kind) => showNotice(alertEl, message, kind);

function step(text) {
    const li = document.createElement("li");
    li.textContent = text;
    stepsEl.appendChild(li);
    return (suffix) => {
        li.textContent = `${text} ${suffix}`;
    };
}

/** Hex SHA-256 of the whole file; the API binds it into the presigned PUT. */
async function sha256Hex(file) {
    const buffer = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

// Loading the spec from a file fills the text box rather than being kept aside,
// so what gets published is always exactly what is on screen and can be
// reviewed or tweaked first. Invalid JSON is reported immediately, not at submit.
const specBox = document.getElementById("spec");
const specStatus = document.getElementById("spec-status");

document.getElementById("spec-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    specStatus.classList.remove("error");
    if (!file) {
        specStatus.textContent = "";
        return;
    }
    const text = await file.text();
    try {
        const parsed = JSON.parse(text);
        specBox.value = JSON.stringify(parsed, null, 2);
        const layers = Array.isArray(parsed.layers) ? parsed.layers.length : 0;
        specStatus.textContent = `loaded ${file.name} (${layers} layer${layers === 1 ? "" : "s"})`;
    } catch (error) {
        specBox.value = text;
        specStatus.textContent = `${file.name} is not valid JSON: ${error.message}`;
        specStatus.classList.add("error");
    }
});

api.session()
    .then((s) => {
        document.getElementById("who").textContent = s?.user?.email ?? "not signed in";
        if (!s?.user) notify("Sign in on the datasets page before uploading.", "error");
    })
    .catch(() => {});

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    notify("");
    stepsEl.innerHTML = "";
    progressCard.hidden = false;
    submit.disabled = true;

    const file = document.getElementById("file").files[0];
    try {
        let spec = null;
        const specText = document.getElementById("spec").value.trim();
        if (specText) {
            try {
                spec = JSON.parse(specText);
            } catch {
                throw new Error("The spec is not valid JSON.");
            }
        }

        const uploading = step(`Uploading ${file.name} to the private staging bucket…`);
        const upload = await api.createDemoUpload({
            sha256: await sha256Hex(file),
            content_length: file.size,
        });
        const response = await fetch(upload.url, {
            method: "PUT",
            // Exactly the headers that were signed, and nothing else.
            headers: upload.headers,
            body: file,
        });
        if (!response.ok) {
            throw new Error(
                `Upload failed with HTTP ${response.status}. ` +
                    `${(await response.text()).slice(0, 200)}`,
            );
        }
        uploading(`→ ${upload.source_key}`);

        const requesting = step("Creating the processing job…");
        const accepted = await api.createPublication(
            {
                dataset_slug: document.getElementById("slug").value,
                source_key: upload.source_key,
                name: document.getElementById("name").value || undefined,
                visibility: document.getElementById("visibility").value,
                ...(spec ? { spec } : {}),
            },
            newIdempotencyKey(),
        );
        requesting(`→ job ${accepted.job_id}`);

        // The stream will announce it too; fetching makes the row appear even
        // if the stream is reconnecting.
        upsertJob(await api.getPublication(accepted.job_id));
    } catch (error) {
        const detail = error instanceof ApiError ? `${error.message} [${error.code}]` : error.message;
        notify(detail, "error");
        step(`Failed: ${detail}`);
    } finally {
        submit.disabled = false;
    }
});

// --------------------------------------------------------------------------
// Jobs table, live over SSE
// --------------------------------------------------------------------------
const JOBS_SHOWN = 20;
const jobs = new Map();

function upsertJob(job) {
    jobs.set(job.id, job);
    renderJobs();
}

async function loadJobs() {
    try {
        const page = await api.listPublications({ limit: JOBS_SHOWN });
        jobs.clear();
        for (const job of page.items) jobs.set(job.id, job);
        renderJobs();
    } catch (error) {
        emptyJobs(`Could not load jobs: ${error.message}`);
    }
}

function emptyJobs(text) {
    jobsEl.innerHTML = "";
    const td = jobsEl.insertRow().insertCell();
    td.colSpan = 7;
    td.className = "empty";
    td.textContent = text;
}

function renderJobs() {
    const rows = [...jobs.values()]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, JOBS_SHOWN);
    if (rows.length === 0) {
        emptyJobs("No publication jobs yet.");
        return;
    }

    jobsEl.innerHTML = "";
    for (const job of rows) {
        const tr = jobsEl.insertRow();

        const id = tr.insertCell();
        id.className = "mono";
        id.title = job.id;
        id.textContent = job.id.slice(0, 8);

        tr.insertCell().appendChild(pill(job.status));
        tr.insertCell().textContent = job.result
            ? `${job.result}${job.result_version_seq ? ` (v${job.result_version_seq})` : ""}`
            : "—";
        tr.insertCell().textContent = job.attempts;

        const error = tr.insertCell();
        error.className = "mono";
        error.textContent = job.error_code ?? "—";
        if (job.error_code) {
            error.title = job.error_message ?? "";
            error.classList.add("error");
        }

        tr.insertCell().textContent = formatTime(job.created_at);

        const actions = tr.insertCell();
        if (job.status === "SUCCEEDED") {
            const map = document.createElement("a");
            map.href = `/map.html?dataset=${job.dataset_id}`;
            map.textContent = "Map";
            actions.appendChild(map);
        } else if (job.status === "FAILED") {
            const retry = document.createElement("button");
            retry.textContent = "Retry";
            retry.onclick = async () => {
                retry.disabled = true;
                try {
                    await api.retryPublication(job.id);
                    upsertJob(await api.getPublication(job.id));
                } catch (err) {
                    notify(`Retry failed: ${err.message}`, "error");
                    retry.disabled = false;
                }
            };
            actions.appendChild(retry);
        }
    }
}

// EventSource reconnects on its own (the backend ends each stream after a few
// minutes so the gateway re-checks the session). Every (re)connect reloads the
// list, so nothing that happened while disconnected is missed.
const events = new EventSource("/api/v1/publications/events");
events.onopen = () => {
    liveEl.textContent = "● live";
    void loadJobs();
};
events.onerror = () => {
    liveEl.textContent = events.readyState === EventSource.CLOSED ? "offline" : "reconnecting…";
    if (events.readyState === EventSource.CLOSED) void loadJobs();
};
events.addEventListener("job", (event) => upsertJob(JSON.parse(event.data)));
