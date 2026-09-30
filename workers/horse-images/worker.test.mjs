import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import { test } from "node:test";
import { decryptSource, serveImage } from "./worker.mjs";

const vector = JSON.parse(await readFile(new URL("../../tests/fixtures/horse-image-token.json", import.meta.url)));
const env = { HORSE_IMAGE_PROXY_KEY: vector.key, HORSE_IMAGE_ALLOWED_HOSTS: "upload.wikimedia.org" };
const url = `https://photos.example.org/h/v1/${vector.token}.jpg`;

async function tokenFor(source) {
  const key = await webcrypto.subtle.importKey("raw", Buffer.from(vector.key, "hex"), "AES-GCM", false, ["encrypt"]);
  const nonce = webcrypto.getRandomValues(new Uint8Array(12));
  const bytes = await webcrypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: new TextEncoder().encode("umacore-horse-image-v1") },
    key, new TextEncoder().encode(source),
  );
  return Buffer.concat([nonce, new Uint8Array(bytes)]).toString("base64url");
}

test("decrypts the exact token produced by Python", async () => {
  assert.equal(await decryptSource(vector.token, vector.key), vector.source);
});

test("returns image bytes with caching but no source metadata or redirect", async () => {
  let fetches = 0;
  const response = await serveImage(new Request(url, { headers: { Authorization: "private", Cookie: "private" } }), env,
    async (source, options) => {
      fetches++;
      assert.equal(source, vector.source);
      assert.equal(options.redirect, "manual");
      assert.equal(options.cf.cacheTtlByStatus["200"], 86400);
      assert.equal(options.headers.Authorization, undefined);
      assert.equal(options.headers.Cookie, undefined);
      return new Response(new Uint8Array([255, 216, 255]), { headers: {
        "Content-Type": "image/jpeg", "Content-Disposition": 'inline; filename="Gold_Ship.jpg"',
        "Location": vector.source, "Link": vector.source, "Set-Cookie": "secret=true",
      } });
    });
  assert.equal(fetches, 1);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-disposition"), 'inline; filename="photo.jpg"');
  assert.equal(response.headers.get("cache-control"), "public, max-age=86400");
  assert.equal(response.headers.get("location"), null);
  assert.equal(response.headers.get("link"), null);
  assert.equal(response.headers.get("set-cookie"), null);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), new Uint8Array([255, 216, 255]));
});

test("HEAD returns no body and retains neutral headers", async () => {
  const response = await serveImage(new Request(url, { method: "HEAD" }), env,
    async () => new Response("bytes", { headers: { "Content-Type": "image/png" } }));
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
});

test("does not allow direct-source hosts through the Worker", async () => {
  for (const source of [
    "https://jra.jp/gallery/3minmeiba/horse9/img/pic_gallery_1.jpg",
    "https://cdn.netkeiba.com/img.dir/keibamatome/image.php?id=3353",
    "https://dir.netkeiba.com/PhotoExhibition/photodetail/img/cate_02_detail_01.jpg",
    "https://number.ismcdn.jp/mwimgs/c/d/750wm/img_cd0a2409e1309f002ba82de0a3e3896c4381320.jpg",
  ]) {
    const token = await tokenFor(source);
    const request = new Request(`https://photos.example.org/h/v1/${token}.jpg`);
    const response = await serveImage(request, env, () => {
      throw new Error("Must not fetch");
    });
    assert.equal(response.status, 404);
  }
});

test("sniffs safe image bytes from archive CDNs with generic content types", async () => {
  for (const genericType of ["application/octet-stream", "binary/octet-stream", ""]) {
    const headers = genericType ? { "Content-Type": genericType } : {};
    const response = await serveImage(new Request(url), env, async () => new Response(
      new Uint8Array([0xff, 0xd8, 0xff, 0xe0]), { headers },
    ));
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "image/jpeg");
  }
  const rejected = await serveImage(new Request(url), env, async () => new Response(
    new TextEncoder().encode("not an image"), { headers: { "Content-Type": "application/octet-stream" } },
  ));
  assert.equal(rejected.status, 502);
});

test("normalizes the nonstandard image/jpg type used by archive CDNs", async () => {
  const response = await serveImage(new Request(url), env, async () => new Response(
    new Uint8Array([0xff, 0xd8, 0xff]), { headers: { "Content-Type": "image/jpg" } },
  ));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "image/jpeg");
});

test("bad paths, tokens, methods and missing secrets never fetch", async () => {
  const noFetch = () => { throw new Error("Must not fetch"); };
  for (const path of ["/", "/h/v1/Gold_Ship.jpg", `/h/v1/${"x".repeat(90)}.jpg`, `/h/v1/${vector.token}.jpg?url=secret`]) {
    assert.equal((await serveImage(new Request(`https://photos.example.org${path}`), env, noFetch)).status, 404);
  }
  assert.equal((await serveImage(new Request(url, { method: "POST" }), env, noFetch)).status, 405);
  assert.equal((await serveImage(new Request(url), {}, noFetch)).status, 503);
});

test("rejects tampering and unapproved/private sources", async () => {
  let calls = 0;
  const noFetch = () => { calls++; throw new Error("Must not fetch"); };
  const packed = Buffer.from(vector.token, "base64url");
  packed[20] ^= 1;
  const tampered = `https://photos.example.org/h/v1/${packed.toString("base64url")}.jpg`;
  assert.equal((await serveImage(new Request(tampered), env, noFetch)).status, 404);
  for (const source of ["http://upload.wikimedia.org/a.jpg", "https://127.0.0.1/a.jpg", "https://localhost/a.jpg",
    "https://other.example.org/a.jpg", "https://user:pass@upload.wikimedia.org/a.jpg", "https://upload.wikimedia.org:7890/a.jpg"]) {
    const token = await tokenFor(source);
    const result = await serveImage(new Request(`https://photos.example.org/h/v1/${token}.jpg`), env, noFetch);
    assert.equal(result.status, 404);
  }
  assert.equal(calls, 0);
});

test("upstream redirects, HTML, oversized images and errors do not leak details", async () => {
  for (const init of [
    { status: 302, headers: { Location: vector.source } },
    { status: 404 }, { status: 500 },
    { headers: { "Content-Type": "text/html" } },
    { headers: { "Content-Type": "image/svg+xml" } },
    { headers: { "Content-Type": "image/jpeg", "Content-Length": "25000000" } },
  ]) {
    const result = await serveImage(new Request(url), env, async () => new Response(vector.source, init));
    assert.equal(result.status, 502);
    assert.equal(await result.text(), "Image unavailable");
    assert.equal(result.headers.get("location"), null);
    assert.equal(result.headers.get("cache-control"), "no-store");
  }
  const result = await serveImage(new Request(url), env, async () => { throw new Error(vector.source); });
  assert.equal(await result.text(), "Image unavailable");
});

test("limits streamed images even without Content-Length", async () => {
  const upstream = new Response(new Uint8Array(21 * 1024 * 1024), { headers: { "Content-Type": "image/jpeg" } });
  const response = await serveImage(new Request(url), env, async () => upstream);
  await assert.rejects(response.arrayBuffer(), /Image too large/);
});
