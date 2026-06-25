import { handleUpload } from "@vercel/blob/client";

const MAX_UPLOAD_BYTES = Number(process.env.MAX_ZIP_BYTES || process.env.MAX_ZIP_UPLOAD_BYTES || 250 * 1024 * 1024);
const MAX_TOKEN_BODY_BYTES = 64 * 1024;
const TOKEN_WINDOW_MS = 60 * 1000;
const TOKEN_WINDOW_LIMIT = Number(process.env.BLOB_UPLOAD_TOKEN_RATE_LIMIT || 30);
const uploadTokenHits = globalThis.__westernBlotUploadTokenHits || new Map();
globalThis.__westernBlotUploadTokenHits = uploadTokenHits;

function logEvent(event, details = {}) {
  console.log(JSON.stringify({ event, ...details }));
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "application/json",
      "x-content-type-options": "nosniff",
    },
  });
}

function safeSessionId(value) {
  return String(value || "")
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^[._]+|[._]+$/g, "")
    .slice(0, 80);
}

function requestIp(request) {
  return (request.headers.get("x-forwarded-for") || "")
    .split(",")[0]
    .trim() || request.headers.get("x-real-ip") || "unknown";
}

function rateLimitUploadToken(request) {
  const now = Date.now();
  const cutoff = now - TOKEN_WINDOW_MS;
  const key = requestIp(request);
  const hits = (uploadTokenHits.get(key) || []).filter((timestamp) => timestamp > cutoff);
  hits.push(now);
  uploadTokenHits.set(key, hits);

  if (uploadTokenHits.size > 1000) {
    for (const [entryKey, timestamps] of uploadTokenHits.entries()) {
      if (!timestamps.some((timestamp) => timestamp > cutoff)) uploadTokenHits.delete(entryKey);
    }
  }

  return hits.length <= TOKEN_WINDOW_LIMIT;
}

function isSafePathSegment(value) {
  return /^[A-Za-z0-9._-]{1,200}$/.test(value) && ![".", ".."].includes(value);
}

function isValidUploadPath(pathname, sessionId) {
  if (typeof pathname !== "string" || pathname.length > 300 || pathname.includes("\\") || pathname.includes("\0")) {
    return false;
  }
  const parts = pathname.split("/");
  return parts.length === 3
    && parts[0] === "uploads"
    && parts[1] === sessionId
    && isSafePathSegment(parts[1])
    && isSafePathSegment(parts[2])
    && parts[2].toLowerCase().endsWith(".zip");
}

function blobAccess() {
  return process.env.BLOB_ACCESS === "public" ? "public" : "private";
}

function publicErrorMessage(error) {
  const message = error instanceof Error ? error.message : "";
  if (message.includes("BLOB_READ_WRITE_TOKEN")) {
    return "BLOB_READ_WRITE_TOKEN is not configured for this deployment.";
  }
  if (message.includes("access must be")) {
    return message;
  }
  return "Upload setup failed.";
}

function callbackUrl(request) {
  return new URL("/api/blob-upload", request.url).href;
}

async function handleBlobUploadRequest(request) {
  if (request.method === "GET") {
    logEvent("blob_upload_status", {
      blobAccess: blobAccess(),
      hasBlobReadWriteToken: Boolean(process.env.BLOB_READ_WRITE_TOKEN),
      maxUploadBytes: MAX_UPLOAD_BYTES,
    });
    return json({
      status: "ok",
      blobAccess: blobAccess(),
      hasBlobReadWriteToken: Boolean(process.env.BLOB_READ_WRITE_TOKEN),
      maxUploadBytes: MAX_UPLOAD_BYTES,
    });
  }

  if (request.method !== "POST") {
    return json({ error: "Method not allowed." }, 405);
  }
  if (!process.env.BLOB_READ_WRITE_TOKEN) {
    logEvent("blob_upload_missing_token");
    return json({ error: "BLOB_READ_WRITE_TOKEN is not configured for this deployment." }, 500);
  }
  if (!rateLimitUploadToken(request)) {
    logEvent("blob_upload_rate_limited");
    return json({ error: "Too many upload token requests." }, 429);
  }

  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > MAX_TOKEN_BODY_BYTES) {
    logEvent("blob_upload_token_body_too_large", { contentLength });
    return json({ error: "Upload token request is too large." }, 413);
  }

  try {
    const body = await request.json();
    logEvent("blob_upload_request", { type: body?.type || "unknown" });
    const response = await handleUpload({
      body,
      request,
      async onBeforeGenerateToken(pathname, clientPayload, multipart) {
        let payload = {};
        try {
          payload = clientPayload ? JSON.parse(clientPayload) : {};
        } catch (_error) {
          throw new Error("Invalid upload payload.");
        }

        const sessionId = safeSessionId(payload.sessionId);
        if (!sessionId || !isValidUploadPath(pathname, sessionId)) {
          logEvent("blob_upload_invalid_path", { pathname, hasSessionId: Boolean(sessionId) });
          throw new Error("Invalid upload path.");
        }

        logEvent("blob_upload_token_generated", {
          access: blobAccess(),
          callbackUrl: callbackUrl(request),
          multipart: Boolean(multipart),
          pathname,
          maximumSizeInBytes: MAX_UPLOAD_BYTES,
        });
        return {
          allowedContentTypes: ["application/zip", "application/x-zip-compressed", "application/octet-stream"],
          callbackUrl: callbackUrl(request),
          maximumSizeInBytes: MAX_UPLOAD_BYTES,
          tokenPayload: JSON.stringify({ sessionId }),
        };
      },
      onUploadCompleted({ blob, tokenPayload }) {
        let sessionId = "";
        try {
          sessionId = JSON.parse(tokenPayload || "{}").sessionId || "";
        } catch (_error) {
          sessionId = "";
        }
        logEvent("blob_upload_completed", {
          sessionId,
          pathname: blob?.pathname,
          size: blob?.size,
        });
      },
    });

    logEvent("blob_upload_response", { type: response?.type || "unknown" });
    return json(response);
  } catch (error) {
    console.error("Blob upload setup failed.", error);
    logEvent("blob_upload_error", {
      message: error instanceof Error ? error.message : "Unknown error",
    });
    return json({ error: publicErrorMessage(error) }, 400);
  }
}

export function GET(request) {
  return handleBlobUploadRequest(request);
}

export function POST(request) {
  return handleBlobUploadRequest(request);
}
