import { issueSignedToken } from "@vercel/blob";
import { handleUpload, handleUploadPresigned } from "@vercel/blob/client";

const MAX_UPLOAD_ENV_KEYS = ["MAX_ZIP_BYTES", "MAX_ZIP_UPLOAD_BYTES"];
const TOKEN_RATE_LIMIT_ENV_KEYS = ["BLOB_UPLOAD_TOKEN_RATE_LIMIT"];
const DEFAULT_MAX_UPLOAD_BYTES = 250 * 1024 * 1024;
const DEFAULT_TOKEN_WINDOW_LIMIT = 6;
const MAX_TOKEN_BODY_BYTES = 64 * 1024;
const TOKEN_WINDOW_MS = 60 * 1000;
const MAX_RATE_LIMIT_CLIENTS = 1000;
const UPLOAD_TOKEN_TTL_MS = 5 * 60 * 1000;
const MAX_UPLOAD_BYTES = positiveIntegerEnv(MAX_UPLOAD_ENV_KEYS, DEFAULT_MAX_UPLOAD_BYTES);
const TOKEN_WINDOW_LIMIT = positiveIntegerEnv(TOKEN_RATE_LIMIT_ENV_KEYS, DEFAULT_TOKEN_WINDOW_LIMIT);
const ALLOWED_UPLOAD_CONTENT_TYPES = ["application/zip", "application/x-zip-compressed", "application/octet-stream"];
const uploadTokenHits = globalThis.__westernBlotUploadTokenHits || new Map();
globalThis.__westernBlotUploadTokenHits = uploadTokenHits;

function positiveIntegerEnv(names, fallback) {
  for (const name of names) {
    const raw = process.env[name];
    if (raw === undefined || raw === "") continue;
    const value = Number(raw);
    if (Number.isInteger(value) && value > 0) return value;
    throw new Error(`${name} must be a positive integer.`);
  }
  return fallback;
}

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
  if (typeof value !== "string") return "";
  return value
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^[._]+|[._]+$/g, "")
    .slice(0, 80);
}

function parseClientPayload(clientPayload) {
  try {
    return clientPayload ? JSON.parse(clientPayload) : {};
  } catch (_error) {
    throw new Error("Invalid upload payload.");
  }
}

function sessionIdFromTokenPayload(tokenPayload) {
  try {
    return safeSessionId(JSON.parse(tokenPayload || "{}").sessionId);
  } catch (_error) {
    return "";
  }
}

function requestIp(request) {
  const forwarded = process.env.VERCEL === "1"
    ? request.headers.get("x-vercel-forwarded-for") || request.headers.get("x-forwarded-for") || request.headers.get("x-real-ip")
    : "";
  return (forwarded || "")
    .split(",")[0]
    .trim() || "unknown";
}

function pruneUploadTokenHits(cutoff) {
  for (const [entryKey, timestamps] of uploadTokenHits.entries()) {
    const recent = timestamps
      .filter((timestamp) => timestamp > cutoff)
      .slice(-(TOKEN_WINDOW_LIMIT + 1));
    if (recent.length) uploadTokenHits.set(entryKey, recent);
    else uploadTokenHits.delete(entryKey);
  }
}

function rateLimitUploadToken(request) {
  const now = Date.now();
  const cutoff = now - TOKEN_WINDOW_MS;
  const key = requestIp(request);

  pruneUploadTokenHits(cutoff);
  if (!uploadTokenHits.has(key) && uploadTokenHits.size >= MAX_RATE_LIMIT_CLIENTS) {
    return false;
  }

  const hits = uploadTokenHits.get(key) || [];
  hits.push(now);
  uploadTokenHits.set(key, hits.slice(-(TOKEN_WINDOW_LIMIT + 1)));

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

function envFlag(name) {
  return ["true", "1", "yes"].includes(String(process.env[name] || "").trim().toLowerCase());
}

function blobAccess() {
  if (String(process.env.BLOB_ACCESS || "").trim().toLowerCase() !== "public") {
    return "private";
  }
  if (!envFlag("BLOB_PUBLIC_ACCESS_ACK")) {
    logEvent("blob_upload_public_access_blocked");
    return "private";
  }
  return "public";
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

function normalizeOrigin(value) {
  if (!value) return "";
  try {
    const text = String(value).trim().replace(/\/+$/, "");
    const url = new URL(/^https?:\/\//i.test(text) ? text : `https://${text}`);
    if (!["http:", "https:"].includes(url.protocol) || !url.hostname) return "";
    return url.origin;
  } catch (_error) {
    return "";
  }
}

function configuredCallbackOrigins() {
  const origins = [
    process.env.BLOB_UPLOAD_CALLBACK_ORIGIN,
    process.env.PUBLIC_APP_URL,
    process.env.VERCEL_PROJECT_PRODUCTION_URL,
    process.env.VERCEL_URL,
    ...(process.env.ALLOWED_ORIGINS || "").split(","),
  ].map(normalizeOrigin).filter(Boolean);
  return [...new Set(origins)];
}

function isLocalOrigin(origin) {
  try {
    const hostname = new URL(origin).hostname;
    return ["localhost", "127.0.0.1", "[::1]", "::1"].includes(hostname);
  } catch (_error) {
    return false;
  }
}

function callbackUrl(request) {
  const requestOrigin = normalizeOrigin(new URL(request.url).origin);
  const configuredOrigins = configuredCallbackOrigins();
  const callbackOrigin = configuredOrigins.includes(requestOrigin)
    ? requestOrigin
    : configuredOrigins[0];
  if (!callbackOrigin && !isLocalOrigin(requestOrigin)) {
    throw new Error("BLOB_UPLOAD_CALLBACK_ORIGIN or ALLOWED_ORIGINS must be configured for Blob uploads.");
  }
  return new URL("/api/blob-upload", callbackOrigin || requestOrigin).href;
}

function webhookPublicKey() {
  return process.env.BLOB_WEBHOOK_PUBLIC_KEY || process.env.blob_webhook_public_key;
}

function logUploadPath(pathname) {
  const parts = String(pathname || "").split("/");
  if (parts.length === 3 && parts[0] === "uploads") return "uploads/<session>/<upload>.zip";
  return "";
}

async function handleBlobUploadRequest(request) {
  if (request.method === "GET") {
    return blobUploadStatusResponse();
  }

  const rejection = validateBlobUploadRequest(request);
  if (rejection) return rejection;

  try {
    const body = await request.json();
    logEvent("blob_upload_request", { type: body?.type || "unknown" });
    if (body?.type === "blob.generate-presigned-url" || body?.type === "blob.upload-completed") {
      return handlePresignedBlobEvent(request, body);
    }
    return handleClientBlobEvent(request, body);
  } catch (error) {
    console.error("Blob upload setup failed.", error);
    logEvent("blob_upload_error", {
      message: error instanceof Error ? error.message : "Unknown error",
    });
    return json({ error: publicErrorMessage(error) }, 400);
  }
}

function blobUploadStatusResponse() {
  const body = {
    status: "ok",
    blobAccess: blobAccess(),
    hasBlobReadWriteToken: Boolean(process.env.BLOB_READ_WRITE_TOKEN),
    hasBlobWebhookPublicKey: Boolean(webhookPublicKey()),
    maxUploadBytes: MAX_UPLOAD_BYTES,
  };
  logEvent("blob_upload_status", {
    blobAccess: body.blobAccess,
    hasBlobReadWriteToken: body.hasBlobReadWriteToken,
    hasBlobWebhookPublicKey: body.hasBlobWebhookPublicKey,
    maxUploadBytes: body.maxUploadBytes,
  });
  return json(body);
}

function validateBlobUploadRequest(request) {
  if (request.method !== "POST") return json({ error: "Method not allowed." }, 405);
  if (!process.env.BLOB_READ_WRITE_TOKEN) {
    logEvent("blob_upload_missing_token");
    return json({ error: "BLOB_READ_WRITE_TOKEN is not configured for this deployment." }, 500);
  }
  if (!rateLimitUploadToken(request)) {
    logEvent("blob_upload_rate_limited");
    return json({ error: "Too many upload token requests." }, 429);
  }
  return validateTokenBodyLength(request);
}

function validateTokenBodyLength(request) {
  const contentLengthHeader = request.headers.get("content-length");
  if (contentLengthHeader === null) return null;

  const contentLength = Number(contentLengthHeader);
  if (!Number.isFinite(contentLength) || contentLength < 0) {
    logEvent("blob_upload_invalid_content_length", { contentLength: contentLengthHeader });
    return json({ error: "Invalid Content-Length header." }, 400);
  }
  if (contentLength > MAX_TOKEN_BODY_BYTES) {
    logEvent("blob_upload_token_body_too_large", { contentLength });
    return json({ error: "Upload token request is too large." }, 413);
  }
  return null;
}

async function handlePresignedBlobEvent(request, body) {
  if (body?.type === "blob.upload-completed" && !webhookPublicKey()) {
    // Fail closed: without a public key the upload-completed webhook signature
    // cannot be verified, so a forged completion event could be accepted. The
    // handler is log-only today, but reject an unverifiable event rather than
    // trust it (defense-in-depth against future side effects on this path).
    logEvent("blob_upload_webhook_key_missing");
    return json({ error: "Upload webhook verification is not configured." }, 500);
  }
  const response = await handleUploadPresigned({
    body,
    request,
    webhookPublicKey: webhookPublicKey(),
    async getSignedToken(pathname, clientPayload, multipart) {
      return signedTokenResponse(request, pathname, clientPayload, multipart, {
        invalidPathEvent: "blob_upload_invalid_presigned_path",
        generatedEvent: "blob_upload_presigned_generated",
        includeToken: true,
      });
    },
    async onUploadCompleted({ blob, tokenPayload }) {
      logUploadCompleted("blob_upload_presigned_completed", blob, tokenPayload);
    },
  });

  logEvent("blob_upload_presigned_response", { type: response?.type || "unknown" });
  return json(response);
}

async function handleClientBlobEvent(request, body) {
  const response = await handleUpload({
    body,
    request,
    async onBeforeGenerateToken(pathname, clientPayload, multipart) {
      return signedTokenResponse(request, pathname, clientPayload, multipart, {
        invalidPathEvent: "blob_upload_invalid_path",
        generatedEvent: "blob_upload_token_generated",
        includeToken: false,
      });
    },
    onUploadCompleted({ blob, tokenPayload }) {
      logUploadCompleted("blob_upload_completed", blob, tokenPayload);
    },
  });

  logEvent("blob_upload_response", { type: response?.type || "unknown" });
  return json(response);
}

async function signedTokenResponse(request, pathname, clientPayload, multipart, events) {
  const sessionId = uploadSessionId(pathname, clientPayload, events.invalidPathEvent);
  const uploadOptions = signedUploadOptions(request, sessionId);
  logEvent(events.generatedEvent, {
    access: blobAccess(),
    callbackUrl: uploadOptions.callbackUrl,
    multipart: Boolean(multipart),
    pathname: logUploadPath(pathname),
    maximumSizeInBytes: MAX_UPLOAD_BYTES,
  });

  if (!events.includeToken) return uploadOptions;
  return {
    token: await issueSignedToken({
      token: process.env.BLOB_READ_WRITE_TOKEN,
      pathname,
      operations: ["put"],
      validUntil: Date.now() + UPLOAD_TOKEN_TTL_MS,
      allowedContentTypes: ALLOWED_UPLOAD_CONTENT_TYPES,
      maximumSizeInBytes: MAX_UPLOAD_BYTES,
    }),
    urlOptions: {
      addRandomSuffix: false,
      allowOverwrite: false,
      ...uploadOptions,
    },
  };
}

function uploadSessionId(pathname, clientPayload, invalidPathEvent) {
  const payload = parseClientPayload(clientPayload);
  const sessionId = safeSessionId(payload.sessionId);
  if (!sessionId || !isValidUploadPath(pathname, sessionId)) {
    logEvent(invalidPathEvent, { pathname: logUploadPath(pathname), hasSessionId: Boolean(sessionId) });
    throw new Error("Invalid upload path.");
  }
  return sessionId;
}

function signedUploadOptions(request, sessionId) {
  return {
    allowedContentTypes: ALLOWED_UPLOAD_CONTENT_TYPES,
    callbackUrl: callbackUrl(request),
    maximumSizeInBytes: MAX_UPLOAD_BYTES,
    tokenPayload: JSON.stringify({ sessionId }),
  };
}

function logUploadCompleted(event, blob, tokenPayload) {
  const sessionId = sessionIdFromTokenPayload(tokenPayload);
  logEvent(event, {
    hasSessionId: Boolean(sessionId),
    pathname: logUploadPath(blob?.pathname),
    size: blob?.size,
  });
}

export function GET(request) {
  return handleBlobUploadRequest(request);
}

export function POST(request) {
  return handleBlobUploadRequest(request);
}
