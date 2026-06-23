import { handleUpload } from "@vercel/blob/client";

const MAX_UPLOAD_BYTES = Number(process.env.MAX_ZIP_BYTES || process.env.MAX_ZIP_UPLOAD_BYTES || 250 * 1024 * 1024);
const MAX_TOKEN_BODY_BYTES = 64 * 1024;
const TOKEN_WINDOW_MS = 60 * 1000;
const TOKEN_WINDOW_LIMIT = Number(process.env.BLOB_UPLOAD_TOKEN_RATE_LIMIT || 30);
const uploadTokenHits = globalThis.__westernBlotUploadTokenHits || new Map();
globalThis.__westernBlotUploadTokenHits = uploadTokenHits;

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

export default async function handler(request) {
  if (request.method !== "POST") {
    return json({ error: "Method not allowed." }, 405);
  }
  if (!rateLimitUploadToken(request)) {
    return json({ error: "Too many upload token requests." }, 429);
  }

  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > MAX_TOKEN_BODY_BYTES) {
    return json({ error: "Upload token request is too large." }, 413);
  }

  try {
    const body = await request.json();
    const response = await handleUpload({
      body,
      request,
      onBeforeGenerateToken(pathname, clientPayload) {
        let payload = {};
        try {
          payload = clientPayload ? JSON.parse(clientPayload) : {};
        } catch (_error) {
          throw new Error("Invalid upload payload.");
        }

        const sessionId = safeSessionId(payload.sessionId);
        if (!sessionId || !isValidUploadPath(pathname, sessionId)) {
          throw new Error("Invalid upload path.");
        }

        return {
          allowedContentTypes: ["application/zip", "application/x-zip-compressed"],
          maximumSizeInBytes: MAX_UPLOAD_BYTES,
          tokenPayload: JSON.stringify({ sessionId }),
        };
      },
      onUploadCompleted() {},
    });

    return json(response);
  } catch (error) {
    console.error("Blob upload setup failed.", error);
    return json({ error: "Upload setup failed." }, 400);
  }
}
