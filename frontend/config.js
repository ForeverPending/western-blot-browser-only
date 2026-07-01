const IS_LOCAL_FRONTEND = ["localhost", "127.0.0.1", "::1", "[::1]", ""].includes(window.location.hostname);

const CONFIG = {
    BACKEND_URL: IS_LOCAL_FRONTEND ? "http://127.0.0.1:5001" : "/api",
    USE_VERCEL_BLOB_UPLOADS: !IS_LOCAL_FRONTEND,
    BLOB_ACCESS: "private",
    MAX_ZIP_UPLOAD_BYTES: 262144000,
    MAX_TABULAR_UPLOAD_BYTES: 26214400,
};

// Apply the saved (or system) theme before first paint to avoid a flash.
// This runs synchronously in <head>; the app's toggle updates localStorage.
(function applyStoredTheme() {
    const root = document.documentElement;
    let theme = "light";
    try {
        const stored = window.localStorage.getItem("blot-theme");
        if (stored === "dark" || stored === "light") {
            theme = stored;
        } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
            theme = "dark";
        }
    } catch (_error) {
        /* localStorage/matchMedia unavailable — fall back to light. */
    }
    root.dataset.theme = theme;
})();

// Brand mark (Lanes) as an SVG favicon, kept in sync with the header logo.
(function applyFavicon() {
    const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        + "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        + "<stop offset='0' stop-color='#3b82f6'/><stop offset='1' stop-color='#0ea5a4'/>"
        + "</linearGradient></defs>"
        + "<rect width='32' height='32' rx='8' fill='url(#g)'/>"
        + "<rect x='8' y='14' width='3.2' height='10' rx='1.6' fill='#fff' opacity='.9'/>"
        + "<rect x='13' y='9' width='3.2' height='15' rx='1.6' fill='#fff'/>"
        + "<rect x='18' y='17' width='3.2' height='7' rx='1.6' fill='#fff' opacity='.8'/>"
        + "<rect x='23' y='12' width='3.2' height='12' rx='1.6' fill='#fff' opacity='.95'/></svg>";
    try {
        let link = document.getElementById("favicon");
        if (!link) {
            link = document.createElement("link");
            link.id = "favicon";
            link.rel = "icon";
            document.head.appendChild(link);
        }
        link.type = "image/svg+xml";
        link.href = "data:image/svg+xml," + encodeURIComponent(svg);
    } catch (_error) {
        /* Non-fatal — the page works without a custom favicon. */
    }
})();
