const CONTEXT = new TextEncoder().encode("umacore-horse-image-v1");
const TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"]);
const GENERIC_TYPES = new Set(["", "application/octet-stream", "binary/octet-stream"]);
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const DEFAULT_HOSTS = "assets.st-note.com,cdn.netkeiba.com,dir.netkeiba.com,i.daily.jp,jbpress.ismcdn.jp,jra-van.jp,jra.jp,meiba.jp,number.ismcdn.jp,pbs.twimg.com,stat.ameba.jp,static.wikia.nocookie.net,tospo-keiba.jp,uma-furi.com,upload.wikimedia.org,www.meiba.jp";

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
  const configuredHosts = hosts || DEFAULT_HOSTS;
  const allowed = new Set(configuredHosts.split(",").map(host => host.trim().toLowerCase()));
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

function sniffImageType(bytes) {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  if (bytes.length >= 8 && bytes.slice(0, 8).every((value, index) => value === [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a][index])) return "image/png";
  if (bytes.length >= 6 && new TextDecoder().decode(bytes.slice(0, 6)).match(/^GIF8[79]a$/)) return "image/gif";
  if (bytes.length >= 12 && new TextDecoder().decode(bytes.slice(0, 4)) === "RIFF" && new TextDecoder().decode(bytes.slice(8, 12)) === "WEBP") return "image/webp";
  if (bytes.length >= 12 && new TextDecoder().decode(bytes.slice(4, 12)).match(/^ftyp(?:avif|avis)$/)) return "image/avif";
  return null;
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
    let type = (upstream.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
    if (type === "image/jpg") type = "image/jpeg";
    const length = Number(upstream.headers.get("Content-Length"));
    if (upstream.status !== 200 || (!TYPES.has(type) && !GENERIC_TYPES.has(type)) || length > MAX_IMAGE_BYTES) {
      await upstream.body?.cancel();
      return error(502);
    }
    let body = upstream.body;
    let buffered = false;
    if (!TYPES.has(type)) {
      const bytes = new Uint8Array(await upstream.arrayBuffer());
      if (bytes.byteLength > MAX_IMAGE_BYTES || !(type = sniffImageType(bytes))) return error(502);
      body = bytes;
      buffered = true;
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
      if (!buffered) await upstream.body?.cancel();
      return new Response(null, { headers });
    }
    if (buffered) return new Response(body, { headers });
    let received = 0;
    const bounded = new TransformStream({
      transform(chunk, controller) {
        received += chunk.byteLength;
        if (received > MAX_IMAGE_BYTES) throw new Error("Image too large");
        controller.enqueue(chunk);
      },
    });
    return new Response(body.pipeThrough(bounded), { headers });
  } catch {
    // Errors must not echo the decrypted URL or any upstream response body.
    return error(502);
  }
}

export default { fetch: (request, env) => serveImage(request, env) };
