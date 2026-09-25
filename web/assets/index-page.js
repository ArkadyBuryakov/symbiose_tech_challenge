/**
 * Dataset browser: sign in, switch organization, inspect versions and roll back.
 * Publication jobs live on the upload page.
 *
 * Every read degrades gracefully for an anonymous visitor — the catalogue
 * serves them public datasets — so the page is useful before signing in.
 */

import { ApiError, api, formatBytes, formatTime, notify as showNotice, pill } from "./api.js";

const els = {
    alert: document.getElementById("alert"),
    signinCard: document.getElementById("signin-card"),
    signinForm: document.getElementById("signin-form"),
    sessionArea: document.getElementById("session-area"),
    uploadLink: document.getElementById("upload-link"),
    datasets: document.getElementById("datasets"),
    detail: document.getElementById("detail"),
    detailTitle: document.getElementById("detail-title"),
    versions: document.getElementById("versions"),
};

let session = null;
// The edge serves upload.html only when DEMO_UPLOAD_ENABLED=true.
const uploadEnabled = fetch("/upload.html", { method: "HEAD" })
    .then((response) => response.ok)
    .catch(() => false);

const notify = (message, kind) => showNotice(els.alert, message, kind);

function emptyRow(tbody, columns, text) {
    tbody.innerHTML = "";
    const tr = tbody.insertRow();
    const td = tr.insertCell();
    td.colSpan = columns;
    td.className = "empty";
    td.textContent = text;
}

// --------------------------------------------------------------------------
// Session
// --------------------------------------------------------------------------
async function loadSession() {
    try {
        session = await api.session();
    } catch {
        // Auth unreachable: the read-only parts of this page still work.
        session = null;
    }
    renderSession();
}

function renderSession() {
    els.sessionArea.innerHTML = "";
    const signedIn = Boolean(session?.user);
    els.signinCard.hidden = signedIn;
    els.uploadLink.hidden = true;
    if (signedIn) void uploadEnabled.then((ok) => (els.uploadLink.hidden = !ok));

    if (!signedIn) {
        const span = document.createElement("span");
        span.className = "muted";
        span.textContent = "Not signed in";
        els.sessionArea.appendChild(span);
        return;
    }

    const who = document.createElement("span");
    who.className = "muted";
    who.textContent = session.user.email;
    els.sessionArea.appendChild(who);

    void renderOrganizationPicker();

    const out = document.createElement("button");
    out.textContent = "Sign out";
    out.onclick = async () => {
        await api.signOut();
        session = null;
        renderSession();
        await loadDatasets();
    };
    els.sessionArea.appendChild(out);
}

/**
 * The active organization is what the gateway puts in the internal token as
 * `tenant_id`, so switching it changes what every tenant-scoped endpoint returns.
 */
async function renderOrganizationPicker() {
    let organizations = [];
    try {
        organizations = (await api.listOrganizations()) ?? [];
    } catch {
        return;
    }
    if (organizations.length === 0) return;

    const select = document.createElement("select");
    select.setAttribute("aria-label", "Active organization");
    for (const org of organizations) {
        const option = document.createElement("option");
        option.value = org.id;
        option.textContent = org.name ?? org.slug;
        option.selected = org.id === session?.session?.activeOrganizationId;
        select.appendChild(option);
    }
    select.onchange = async () => {
        await api.setActiveOrganization(select.value);
        session = await api.session();
        notify(`Active organization: ${select.selectedOptions[0].textContent}`);
        await loadDatasets();
    };
    els.sessionArea.insertBefore(select, els.sessionArea.lastChild);
}

els.signinForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    notify("");
    const button = els.signinForm.querySelector("button");
    button.disabled = true;
    try {
        await api.signIn(
            document.getElementById("email").value,
            document.getElementById("password").value,
        );
        session = await api.session();
        renderSession();
        await loadDatasets();
    } catch (error) {
        notify(
            error instanceof ApiError ? `Sign-in failed: ${error.message}` : String(error),
            "error",
        );
    } finally {
        button.disabled = false;
    }
});

// --------------------------------------------------------------------------
// Datasets
// --------------------------------------------------------------------------
async function loadDatasets() {
    let page;
    try {
        page = await api.listDatasets({ limit: 50 });
    } catch (error) {
        emptyRow(els.datasets, 7, `Could not load datasets: ${error.message}`);
        return;
    }
    if (page.items.length === 0) {
        emptyRow(els.datasets, 7, "No datasets visible to you yet.");
        return;
    }

    els.datasets.innerHTML = "";
    for (const dataset of page.items) {
        const tr = els.datasets.insertRow();
        tr.insertCell().textContent = dataset.name;

        const slug = tr.insertCell();
        slug.className = "mono";
        slug.textContent = dataset.slug;

        tr.insertCell().appendChild(pill(dataset.visibility));
        tr.insertCell().textContent = dataset.current_seq ? `v${dataset.current_seq}` : "—";
        tr.insertCell().textContent = dataset.latest_seq;
        tr.insertCell().textContent = formatTime(dataset.updated_at);

        const actions = tr.insertCell();
        if (dataset.current_seq) {
            const map = document.createElement("a");
            map.href = `/map.html?dataset=${dataset.id}`;
            map.textContent = "Map";
            map.style.marginRight = "10px";
            actions.appendChild(map);
        }
        const versions = document.createElement("a");
        versions.href = "#";
        versions.textContent = "Versions";
        versions.onclick = (event) => {
            event.preventDefault();
            void showVersions(dataset);
        };
        actions.appendChild(versions);
    }
}

async function showVersions(dataset) {
    els.detail.hidden = false;
    els.detailTitle.textContent = `Versions of ${dataset.name}`;
    emptyRow(els.versions, 8, "Loading…");

    let versions;
    try {
        versions = await api.listVersions(dataset.id);
    } catch (error) {
        emptyRow(els.versions, 8, `Could not load versions: ${error.message}`);
        return;
    }
    if (versions.length === 0) {
        emptyRow(els.versions, 8, "No versions published yet.");
        return;
    }

    els.versions.innerHTML = "";
    for (const version of versions) {
        const tr = els.versions.insertRow();
        tr.insertCell().textContent = `v${version.seq}${version.is_current ? " (current)" : ""}`;

        const sha = tr.insertCell();
        sha.className = "mono";
        sha.title = version.sha256;
        sha.textContent = version.sha256.slice(0, 12) + "…";

        // Same content with a different spec is a separate version; showing
        // both digests is what makes that visible.
        const spec = tr.insertCell();
        spec.className = "mono";
        spec.title = version.spec_sha256;
        spec.textContent = version.spec_sha256.slice(0, 8) + "…";

        tr.insertCell().textContent = formatBytes(version.size_bytes);
        tr.insertCell().appendChild(pill(version.status));
        tr.insertCell().textContent = formatTime(version.created_at);

        const build = tr.insertCell();
        build.className = "mono";
        build.textContent = version.worker_build ?? "—";

        const actions = tr.insertCell();
        if (!version.is_current && version.status === "AVAILABLE") {
            const button = document.createElement("button");
            button.textContent = "Make current";
            button.onclick = async () => {
                button.disabled = true;
                try {
                    await api.rollback(dataset.id, version.seq);
                    notify(`${dataset.name} rolled back to v${version.seq}.`);
                    await loadDatasets();
                    await showVersions(dataset);
                } catch (error) {
                    notify(`Rollback failed: ${error.message}`, "error");
                    button.disabled = false;
                }
            };
            actions.appendChild(button);
        }
    }
}

await loadSession();
await loadDatasets();
