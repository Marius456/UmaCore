const CONTEXT = new TextEncoder().encode("umacore-horse-image-v1");
const TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"]);
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

function error(status) {
  return new Response("Image unavailable", {
    status,
    headers: { "Cache-Control": "no-store", "Content-Type": "text/plain" },
  });
}

export async function decryptSource(token, keyHex) {
  const bytes = Uint8Array.from(atob(token.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey(
    "raw", Uint8Array.from(keyHex.match(/../g), hex => parseInt(hex, 16)),
    "AES-GCM", false, ["decrypt"],
  );
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: bytes.slice(0, 12), additionalData: CONTEXT, tagLength: 128 },
    key, bytes.slice(12),
  );
  if (plaintext.byteLength > 1200) throw new Error("Invalid source");
  return new TextDecoder("utf-8", { fatal: true }).decode(plaintext);
}

function validateSource(value, hosts) {
  const url = new URL(value);
  const allowed = new Set((hosts || "upload.wikimedia.org").split(",").map(host => host.trim().toLowerCase()));
  if (url.protocol !== "https:" || url.username || url.password || url.port || url.hash ||
      !allowed.has(url.hostname) || /[\s<>\\]/.test(value)) {
    throw new Error("Invalid source");
  }
  // Even a mistaken allowlist entry must not enable local network targets.
  if (!/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(url.hostname) ||
      /(?:^|\.)(?:localhost|local|internal)$/i.test(url.hostname)) {
    throw new Error("Invalid source");
  }
  return url.href;
}

export async function serveImage(request, env, upstreamFetch = fetch) {
  const url = new URL(request.url);
  if (request.method !== "GET" && request.method !== "HEAD") return error(405);
  const match = /^\/h\/v1\/([A-Za-z0-9_-]{40,1640})\.jpg$/.exec(url.pathname);
  if (!match || url.search) return error(404);
  if (!/^[0-9a-fA-F]{64}$/.test(env.HORSE_IMAGE_PROXY_KEY || "")) return error(503);

  let source;
  try {
    source = validateSource(await decryptSource(match[1], env.HORSE_IMAGE_PROXY_KEY), env.HORSE_IMAGE_ALLOWED_HOSTS);
  } catch {
    return error(404);
  }
  try {
    const upstream = await upstreamFetch(source, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(15000),
      headers: { "User-Agent": "UmaCore-Horse-Images/1.0 (https://github.com/oHaruki/UmaCore)" },
      cf: { cacheEverything: true, cacheTtlByStatus: { "200": 86400, "201-599": -1 } },
    });
    const type = (upstream.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
    const length = Number(upstream.headers.get("Content-Length"));
    if (upstream.status !== 200 || !TYPES.has(type) || length > MAX_IMAGE_BYTES) {
      await upstream.body?.cancel();
      return error(502);
    }
    // Deliberately do not forward Location, Content-Disposition, Link, cookies,
    // or any other origin metadata which may contain the source filename.
    const headers = {
      "Content-Type": type,
      "Content-Disposition": 'inline; filename="photo.jpg"',
      "Cache-Control": "public, max-age=86400",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
    };
    if (request.method === "HEAD") {
      await upstream.body?.cancel();
      return new Response(null, { headers });
    }
    let received = 0;
    const bounded = new TransformStream({
      transform(chunk, controller) {
        received += chunk.byteLength;
        if (received > MAX_IMAGE_BYTES) throw new Error("Image too large");
        controller.enqueue(chunk);
      },
    });
    return new Response(upstream.body.pipeThrough(bounded), { headers });
  } catch {
    // Errors must not echo the decrypted URL or any upstream response body.
    return error(502);
  }
}

export default { fetch: (request, env) => serveImage(request, env) };
