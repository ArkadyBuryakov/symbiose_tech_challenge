/**
 * Demo upload flow.
 *
 * 1. Hash the file in the browser. The digest is bound into the presigned PUT,
 *    so the bytes that arrive are provably the bytes that were declared.
 * 2. Ask the API for a presigned PUT. The URL is signed against the internal
 *    object store but rewritten onto the edge origin, which is why this page
 *    needs no CORS configuration and the store stays off the host network.
 * 3. PUT the file with exactly the headers the API returned — every one of them
 *    is part of the signature.
 * 4. Call POST /publications, then poll the job to a terminal state.
 */

import { ApiError, api, formatBytes, newIdempotencyKey, waitForJob } from "./api.js";

const form = document.getElementById("upload-form");
const submit = document.getElementById("submit");
const alertEl = document.getElementById("alert");
const progressCard = document.getElementById("progress-card");
const stepsEl = document.getElementById("steps");
const resultEl = document.getElementById("result");

function notify(message, kind = "info") {
    alertEl.innerHTML = "";
    if (!message) return;
    const div = document.createElement("div");
    div.className = kind === "error" ? "notice error" : "notice";
    div.textContent = message;
    alertEl.appendChild(div);
}

function step(text) {
    const li = document.createElement("li");
    li.textContent = text;
    stepsEl.appendChild(li);
    return (suffix) => {
        li.textContent = `${text} ${suffix}`;
    };
}

/** SHA-256 of the whole file, hex and base64 (S3 wants base64 in the header). */
async function digest(file) {
    const buffer = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    const bytes = new Uint8Array(buffer);
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    const base64 = btoa(String.fromCharCode(...bytes));
    return { hex, base64 };
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
    resultEl.innerHTML = "";
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

        const hashing = step(`Hashing ${file.name} (${formatBytes(file.size)})…`);
        const { hex, base64 } = await digest(file);
        hashing(`→ ${hex.slice(0, 16)}…`);

        const presigning = step("Requesting a presigned upload…");
        const upload = await api.createDemoUpload({ sha256: hex, content_length: file.size });
        presigning(`→ ${upload.source_key}`);

        const uploading = step("Uploading to the staging bucket…");
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
        uploading("→ done");

        const publishing = step("Requesting publication…");
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
        publishing(`→ job ${accepted.job_id.slice(0, 8)}`);

        const waiting = step("Waiting for the worker…");
        const job = await waitForJob(accepted.job_id, {
            onUpdate: (j) => waiting(`→ ${j.status}${j.result ? ` (${j.result})` : ""}`),
        });

        renderResult(job, accepted.dataset_id);
    } catch (error) {
        const detail = error instanceof ApiError ? `${error.message} [${error.code}]` : error.message;
        notify(detail, "error");
        step(`Failed: ${detail}`);
    } finally {
        submit.disabled = false;
    }
});

function renderResult(job, datasetId) {
    resultEl.innerHTML = "";
    if (job.status !== "SUCCEEDED") {
        const p = document.createElement("p");
        p.className = "error";
        p.textContent = `Publication failed: ${job.error_code} — ${job.error_message}`;
        resultEl.appendChild(p);
        return;
    }

    const summary = document.createElement("p");
    summary.textContent =
        job.result === "CREATED"
            ? `Published as version ${job.result_version_seq}.`
            : job.result === "DEDUPLICATED"
              ? "These exact bytes were already the current version; nothing changed."
              : `Pointer moved back to existing version ${job.result_version_seq}.`;

    const link = document.createElement("a");
    link.className = "button";
    link.href = `/map.html?dataset=${datasetId}`;
    link.textContent = "Open the map";

    resultEl.append(summary, link);
}
