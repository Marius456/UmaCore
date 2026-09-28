# Horse-photo trivia

Use `/trivia horse` to identify real racehorses associated with Uma Musume.
Each round displays a photograph and four shuffled name buttons. You have 20
seconds to answer; a correct answer adds one point and continues the game after
a three-second reveal. A wrong answer or timeout ends the game and reveals the
correct name and a source link. Author, license, and modification details are
kept in the photo manifest and bundled credits rather than the game messages.

Gameplay is visible to the channel, but only the player who started it can use
its answer buttons. One game can run per user across text and photo trivia.
Questions do not repeat until the available photo bank has been used, then a new
cycle starts. `/trivia horse_leaderboard` publicly shows the global top ten by
highest streak, then total correct answers. Photo scores and personal bests are
separate from `/trivia play` and `/trivia leaderboard`.

## Managing the bank

These commands require the Discord Administrator permission. The bank is shared
across servers, matching the existing text-trivia bank.

| Command | Purpose |
| --- | --- |
| `/trivia horse_add` | Add an image, correct name, three distractors, source, author, and license |
| `/trivia horse_list` | Privately list question IDs, answers, choices, and source pages |
| `/trivia horse_delete question_id:123` | Remove a question; deleted starter photos stay deleted after restarts |

When adding a question:

- `image_url` must be a stable public HTTPS URL to an image, at most 1,500 characters.
  Use a host that allows Discord to load it. Expiring Discord attachment links,
  authenticated URLs, and HTML pages are unsuitable. The bot validates the URL's
  form; it cannot confirm that Discord successfully rendered a remote image.
- `source_url` is an HTTPS source page, at most 300 characters, documenting the
  photograph and identity. It appears after the answer is revealed.
- Supply the photographer in `author` (up to 200 characters), and the reuse
  license in `license` (up to 120). Ensure the image may be reused, and include
  the applicable license information on the source page.
- Use four distinct names of 1–80 characters. Leading/trailing whitespace is
  removed; names differing only by case are rejected. The first name,
  `correct_answer`, is the correct answer.
- Use full, uncropped photographs with an identifiable horse. Photos are
  displayed as provided, including any visible labels. Use neutral image
  filenames where possible.

If an admin-hosted photo breaks, delete it and add a replacement using a stable
URL. All photos are embedded directly from their HTTPS image URLs; the bot
does not download, cache, or upload image files. Discord loads the image. Invalid
URL records are skipped for that session without a penalty. Remote availability
cannot be detected by the bot. If no usable questions remain, the game ends and
saves earned points. Session state is released even after errors; earned photo
points are also saved on a later-round delivery failure when the database is
available. Ambiguous database failures are not retried to avoid double scoring.

## Starter pack and deployment

The repository includes metadata for 20 starter photos in `assets/horse_trivia/`.
No image files are stored in the repository or Docker image. The
[manifest](../assets/horse_trivia/manifest.json) records names, distractors,
sources, authors, licenses, and modifications. See the
[individual photo credits](../assets/horse_trivia/CREDITS.md) for license links,
and original-image hashes. The image URLs point to the complete original images
without cropping or resizing. The photo licenses are separate from the
repository's MIT license. Some historic photographs have lower resolution.

On normal startup the bot creates `horse_trivia_questions` and
`horse_trivia_leaderboard` without changing the existing trivia tables. The cog
seeds the pack transactionally using stable `seed_key` values and
`ON CONFLICT DO NOTHING`. Deletion sets `deleted_at` and preserves the seed key,
so restart neither duplicates nor resurrects questions. Do not physically delete
starter rows to remove them from play. Startup also migrates legacy starter
filenames such as `001.jpg` to their image URLs and updates the related photo
metadata. This preserves row IDs, answers, deletion markers, and scores, and
does not overwrite existing custom image URLs.
An unavailable manifest is logged without preventing text trivia from loading.

The bot makes no runtime image requests; Discord fetches the embedded URLs.

## Neutral image URLs

Direct Commons URLs contain the original filename and can reveal the answer when
a player hovers the image. The optional Worker in `workers/horse-images/` fixes
that without storing photos in the bot. The bot encrypts each source URL into an
authenticated opaque path such as `https://<worker>/h/v1/<token>.jpg`. The
Worker decrypts it, fetches only an explicitly allowed HTTPS host, strips all
origin metadata, streams the image, and caches the origin response at
Cloudflare's edge. It never redirects the player to the source URL.

Deploy it from `workers/horse-images/`:

```powershell
npm install
npm test
npx wrangler login
npx wrangler deploy
```

Generate one 32-byte key and save the same value in the Worker secret and the
bot's protected `.env`. Do not commit it or put it in `wrangler.toml`:

```powershell
$key = python -c "import secrets; print(secrets.token_hex(32))"
$key | npx wrangler secret put HORSE_IMAGE_PROXY_KEY
```

Then configure the bot and restart it:

```env
HORSE_IMAGE_PROXY_URL=https://umacore-horse-images.<account>.workers.dev
HORSE_IMAGE_PROXY_KEY=<the same 64-character key>
HORSE_IMAGE_ALLOWED_HOSTS=upload.wikimedia.org
```

Configure a custom domain such as `images.umacore.app` in Cloudflare if desired,
then use that HTTPS origin for `HORSE_IMAGE_PROXY_URL`. The bot accepts only a
bare HTTPS origin; paths, query strings, credentials, fragments, and non-default
ports are rejected. If either proxy setting is missing or malformed, partial
configuration fails closed instead of exposing the source URL. When both are
unset, direct URLs remain available for local development.

Administrator-added images must use a host present in
`HORSE_IMAGE_ALLOWED_HOSTS` on both the bot and Worker. Keep this list narrow to
prevent the Worker from becoming an open proxy. Rotate the key by updating the
Worker secret and bot environment together; old opaque links then stop working.

The Worker has no image files, database, cookies, or access to UmaCore's private
API. Its tests verify cross-language encryption, tamper rejection, host and
protocol restrictions, response-header stripping, content types, size limits,
and generic errors that do not expose the decrypted URL.
Commands register through the bot's existing startup command sync. This change
does not deploy or restart the running bot.

## Verification

Run `ruff check .`, `pytest -q`, and `npm test` inside
`workers/horse-images/`. The horse regression tests use fake Discord
interactions and database connections and require no live credentials.

After deploying to a test Discord server, verify:

1. `/trivia horse` is visible to the channel, the photo renders, all four buttons
   work only for the player who started it, and the answer and source link appear
   after answering, without photo-credit or modification text.
2. Play multiple starter rounds and an admin URL round; each replaces the prior
   photo without stale attachments. Confirm the timer starts with the new round.
3. Let a round expire and submit a wrong answer; both reveal the correct name
   and disable the buttons. Correct answers increase the final streak.
4. Confirm `/trivia horse_leaderboard` is public and separate from the text board.
5. Confirm administrators can add/list/delete photos, non-admins cannot, and a
   deleted starter question stays deleted after a test restart.

Live Discord rendering and actual PostgreSQL startup remain environment checks;
the local regression tests do not replace them.
