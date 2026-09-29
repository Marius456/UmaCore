# Horse-photo trivia

Use `/trivia horse mode:Global` to identify real racehorses that have appeared
in released Global game content, or `/trivia horse mode:Japanese` for the full
Japanese-version bank. The mode is required for every new game.
Each round displays a photograph and four shuffled name buttons. You have 20
seconds to answer; a correct answer adds one point and continues the game after
a three-second reveal. A wrong answer or timeout ends the game and reveals the
correct name and a source link. Author, license, and modification details are
kept in the photo manifest and bundled credits rather than the game messages.

Gameplay is visible to the channel, but only the player who started it can use
its answer buttons. One game can run per user across text and photo trivia.
Questions do not repeat until the available photo bank has been used, then a new
cycle starts. Global rounds also restrict all distractor names to the Global
pool. `/trivia horse_leaderboard` publicly shows the combined top ten by highest
streak, then total correct answers, and labels the mode where each personal best
was first achieved. Equal streaks retain the original mode label. Photo scores
and personal bests are separate from `/trivia play` and `/trivia leaderboard`.

## Managing the bank

These commands require the Discord Administrator permission. The bank is shared
across servers, matching the existing text-trivia bank.

| Command | Purpose |
| --- | --- |
| `/trivia horse_add` | Add an image, availability, correct name, three distractors, source, author, and license |
| `/trivia horse_list` | Privately list question IDs, mode availability, answers, choices, and source pages |
| `/trivia horse_delete question_id:123` | Remove a question; deleted starter photos stay deleted after restarts |

When adding a question:

- `image_url` must be a stable public HTTPS URL to an image, at most 1,500 characters.
  Use a host that allows Discord to load it. Expiring Discord attachment links,
  authenticated URLs, and HTML pages are unsuitable. The bot validates the URL's
  form; it cannot confirm that Discord successfully rendered a remote image.
- `source_url` is an HTTPS source page, at most 300 characters, documenting the
  photograph and identity. It appears after the answer is revealed.
- Supply the photographer in `author` (up to 200 characters), and the license or
  rights status in `license` (up to 120). Ensure the intended deployment is
  permitted to display the image, and include the applicable rights information
  on the source page.
- Use four distinct names of 1–80 characters. Leading/trailing whitespace is
  removed; names differing only by case are rejected. The first name,
  `correct_answer`, is the correct answer.
- Choose `Global + Japanese` only after confirming the horse appears in released
  Global client content. Other additions remain Japanese-only. Global rounds
  replace any Japanese-only stored distractors with names from the active Global
  question bank.
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

The repository includes metadata for 278 real-horse photos in
`assets/horse_trivia/`, covering 133 horses in the reviewed 136-character
GameTora support-card roster. Covered horses can have several distinct photos.
The remaining three roster entries have no suitable photo without a legible
horse-name label in the reviewed archives as of 2026-09-29 and are recorded as
unavailable in
[`gametora_support_roster.json`](../assets/horse_trivia/gametora_support_roster.json).
The dated [`global_availability.json`](../assets/horse_trivia/global_availability.json)
snapshot records the canonical Global subset. It uses released Global game data,
including identifiable trainee, support-card, story, event, and named NPC
appearances; promotional-only appearances do not qualify. Update and review this
tracked snapshot as Global content is released. The bot performs no runtime
roster scraping. The 2026-09-29 snapshot marks 84 horses as Global-active, 82
of which currently have photos in the starter bank.
No image files are stored in the repository or Docker image. The
[manifest](../assets/horse_trivia/manifest.json) records names, distractors,
sources, authors, licenses, and modifications. See the
[individual photo credits](../assets/horse_trivia/CREDITS.md) for rights and
source links. The bot never crops or transforms the remote images. The photo licenses and rights statuses
are separate from the repository's MIT license. JRA gallery photos are marked
`Copyrighted - private use only`; JRA requires permission for broader secondary
use. Some historic photographs have lower resolution.

On normal startup the bot creates `horse_trivia_questions` and
`horse_trivia_leaderboard` without changing the existing text-trivia tables.
Existing photo questions gain Japanese-only availability by default, while
seeded rows are synchronized from the tracked Global snapshot. Existing photo
leaderboard records are labeled Japanese because they were earned against the
previous full bank. The cog
seeds the pack transactionally using stable `seed_key` values and
`ON CONFLICT DO NOTHING`. Deletion sets `deleted_at` and preserves the seed key,
so restart neither duplicates nor resurrects questions. Do not physically delete
starter rows to remove them from play. Newly added roster photos use stable seed
keys based on GameTora character IDs and image URLs. Startup also migrates legacy
starter filenames such as `001.jpg` to their image URLs and updates the related photo
metadata. This preserves row IDs, answers, deletion markers, and scores, and
does not overwrite existing custom image URLs.
An unavailable manifest is logged without preventing text trivia from loading.

The bot does not download or store the photos. Discord fetches the embedded URL;
the Worker retrieves and caches the origin for answer-revealing sources.

## Neutral image URLs

Direct Commons and archive URLs can contain a filename that reveals the answer
when a player hovers the image. The Worker in `workers/horse-images/`
fixes that without storing photos in the bot. The bot encrypts those source URLs
into an authenticated opaque path such as `https://<worker>/h/v1/<token>.jpg`.
The Worker decrypts it, fetches only an explicitly allowed HTTPS host, strips
all origin metadata, streams the image, and caches the origin response at
Cloudflare's edge. It never redirects the player to the source URL. Exact
`jra.jp` image URLs, the bundled numeric Netkeiba paths (including profile-photo
endpoints), and hashed Number CDN paths bypass the Worker because those paths do
not expose horse names and these sources reject the deployed proxy route.

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
HORSE_IMAGE_ALLOWED_HOSTS=assets.st-note.com,i.daily.jp,jbpress.ismcdn.jp,jra-van.jp,meiba.jp,pbs.twimg.com,stat.ameba.jp,tospo-keiba.jp,uma-furi.com,upload.wikimedia.org,www.meiba.jp
```

Configure a custom domain such as `images.umacore.app` in Cloudflare if desired,
then use that HTTPS origin for `HORSE_IMAGE_PROXY_URL`. The bot accepts only a
bare HTTPS origin; paths, query strings, credentials, fragments, and non-default
ports are rejected. If either proxy setting is missing or malformed,
configuration fails closed instead of exposing the source URL. Exact recognized
sources with neutral numeric or opaque paths can still be fetched directly.

Administrator-added images must use exact `jra.jp` URLs, one of the supported
numeric Netkeiba or hashed Number image URL formats, or a host present in
`HORSE_IMAGE_ALLOWED_HOSTS` on both the bot and Worker. Keep this list narrow to
prevent the Worker from becoming an open proxy. Rotate the key by updating the
Worker secret and bot environment together; old opaque links then stop working.
Deploy the Worker configuration after changing its allowlist; the committed
configuration permits the bundled Commons and historic racing-archive URLs.

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

1. Both `/trivia horse mode:Global` and `mode:Japanese` are visible to the
   channel, photos render, and all four buttons work only for the player who
   started the game. Confirm Global never shows a Japanese-only answer name.
2. Play multiple starter rounds and an admin URL round; each replaces the prior
   photo without stale attachments. Confirm the timer starts with the new round.
3. Let a round expire and submit a wrong answer; both reveal the correct name
   and disable the buttons. Correct answers increase the final streak.
4. Confirm `/trivia horse_leaderboard` is public, separate from the text board,
   and labels the mode that first established each personal best.
5. Confirm administrators can add/list/delete photos, non-admins cannot, and a
   deleted starter question stays deleted after a test restart.

Live Discord rendering and actual PostgreSQL startup remain environment checks;
the local regression tests do not replace them.
