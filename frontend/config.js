const IS_LOCAL_FRONTEND = ["localhost", "127.0.0.1", "[::1]", ""].includes(window.location.hostname);

const CONFIG = {
    BACKEND_URL: IS_LOCAL_FRONTEND ? "http://127.0.0.1:5001" : "/api",
    USE_VERCEL_BLOB_UPLOADS: !IS_LOCAL_FRONTEND,
    BLOB_ACCESS: "private",
    BLOB_CLIENT_IMPORT_URL: "https://esm.sh/@vercel/blob@2.4.1/client",
    MAX_ZIP_UPLOAD_BYTES: 262144000,
    MAX_TABULAR_UPLOAD_BYTES: 26214400,
};
