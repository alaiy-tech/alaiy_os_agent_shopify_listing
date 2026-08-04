## Alaiy OS — Shopify Listing Agent

An **Alaiy OS agent** (standalone Frappe app) that turns one product's raw,
incomplete data into a polished, structured, **Shopify-ready listing**. The admin
no longer hand-fills mandatory attributes — the agent proposes them, and the admin
reviews, edits, and approves before publish.

This app is **both** things at once:

- **a working listing agent.** Install it and "Shopify Listing" appears in the
  Agents hub, reads a `Shopify Product Listing`, and writes a
  `Shopify Enriched Listing` in *Needs Review* status.
- **overridable per customer.** A customer app adds one markdown file to change the
  agent's prompt — without copying the workflow, the tools, the schema, the DocType
  or the desk surfaces.

Given a product, the agent produces a **title** and **description**,
selling-point **bullet points**, **SEO** title / description / keywords,
**Shopify tags** and a suggested Shopify **category**, structured
**attributes**, whatever **images** its image step produces, and a
**needs_review** list flagging every field it could not fill.

The agent is **advisory**. It reads the listing and its photos and returns JSON. It
does not edit the listing (or the Item behind it) and does not publish to Shopify —
that is the admin approval step and the Shopify connector's job.

### Overriding it for a customer

A customer app drops **one markdown file**:

```
alaiy_os_thesolist/agents/shopify_listing.md
```

Its contents are appended to the vanilla prompt. That is the whole mechanism — no
hook, no registration, no Python. The file being there is the override.

```markdown
---
model: claude-opus-4-8
description: Shown in the Agents hub.
---

## STORE

You are running for **The Solist**, a luxury marketplace for …

## HOUSE STYLE

- **Title:** `Brand + Product Type + key spec` …
- **Units:** dimensions over 1 inch in inches …
```

The frontmatter is optional and exists only for the two things a prompt cannot say
about itself: `model` and `description`. Everything else goes in the prompt.

**All tools are registered on every site.** Which ones the agent actually calls is
decided by the prompt, so choosing image translation over image generation is a
sentence in the markdown, not a setting:

> Your image step is **`translate_product_images`**. Do NOT call
> `generate_product_images` — it exists for a different store.

The one thing the base prompt deliberately does **not** state is the units system and
the title format, because those are the only two places real customers contradict
each other rather than merely differ. Put them in your override.

Two apps overriding the same agent is an error — `find_override()` says so rather
than silently concatenating both prompts in installed-app order.

Live examples: `alaiy_os_thesolist/agents/shopify_listing.md` (luxury jewelry and
watches, three fixed categories, a closed attribute list, image generation) and
`alaiy_os_nayaglobal/agents/shopify_listing.md` (Chinese-sourced supplier data, open
attributes, image translation, compliance rules).

### Layout

Core (`alaiy_os`) owns the engine — the LLM ⇄ tool loop, `OS Agent Run` history,
and the Agents hub. This app owns agent definitions and their tools:

| File | What it holds |
|------|-----------------|
| `agent_meta.py` | The agent: its identity, the tools, and the customer-override discovery. |
| `prompts/system.md` | The vanilla system prompt — role, input contract, workflow, universal rules. A customer override is appended to it. |
| `schemas/output.json` | The output schema the agent must return. |
| `tools/handlers.py` | The four core tools. |
| `tools/image_generation.py` | `generate_product_images` — a five-shot editorial set, via core's AI client seam. |
| `tools/image_translation.py` | `translate_product_images` — supplier photo text into English, via core's AI client seam. |
| `tools/images.py` | Shared image primitives both image tools are built from. |
| `api.py` | `get_listing_agent`, what the desk surfaces ask, plus the bulk entry points. |
| `bulk.py` | Bulk enrichment: chunks a batch across Frappe workers, one run per product. |
| `image_stage.py` | Stage two: the images, rendered on their own queue after the listing is saved. |
| `public/js/listing_agent.js` | The agent and its toggle fields, shared by every desk surface. |
| `public/js/listing_bulk_enrich.js` | "Enrich Listings" in the Shopify Product Listing list view. |
| `setup/install.py` | Registry + sidebar reconcile. |
| `.../doctype/shopify_enriched_listing/` | The shared output DocType and its images child table. |
| `.../doctype/shopify_listing_bulk_enrich/` | A bulk request and the state of each product in it. |
| `.../page/run_agent/` | The Run Agent desk page. |

**The four catalog tools**

- `get_product(item_code)` — the product's `Shopify Product Listing` fields (title,
  description, price, Shopify status, variants) **and its photos as vision image
  blocks**, so the model reads visual attributes off the actual images. Returns both
  `image_urls` and `primary_image_url` so an image tool needs no second
  read.
- `get_reference_values()` — existing brands, categories, Shopify tags and
  locations, so output stays consistent instead of inventing near-duplicates.
  Guarded: columns added by sibling apps are optional.
- `view_image(image_url)` — fetches an external URL and returns it as a vision
  block, for a product the model only knows as a bare URL.
- `save_listing(listing, item_code)` — upserts the finished listing into
  `Shopify Enriched Listing` (one row per product) in *Needs Review* status.

The enriched listing is a **review form, not a JSON dump**: attributes, per-variant
observations and images are child tables, laid out like the Shopify Product Listing's
own, so a reviewer edits rows. The Attributes table is what approval publishes as
metafields, so a correction made there reaches Shopify. The agent's verbatim JSON is
kept read-only under *Raw Agent Output* for audit; editing it changes nothing. A
listing enriched before those tables existed is backfilled from its JSON by
`patches/backfill_enriched_tables.py` on the next `bench migrate`.

**The two image tools**, each opt-in per request and each gated in code rather than
by the model's judgement — they act only when the product actually has a photo *and*
the request's toggle is true. Both **queue** their work rather than doing it: see
"Images are produced after the listing" below.

- `generate_product_images(briefs, …)` — the full editorial set (hero, detail, angle,
  lifestyle, scale) in one call, every shot produced by *editing the product's real
  photo* so it never invents a product from scratch.
- `translate_product_images(…)` — each supplier photo's printed text rendered into
  English, the result re-hosted locally so it survives the vendor's URL expiring.
  A photo it has already translated is never translated again.

Both are registered always and both are offered as a toggle on the desk surfaces; the
prompt is what tells the agent which one belongs to this store. They share
`tools/images.py`: turning a photo into something the model can see, resolving a
reference photo, and re-hosting a result.

None of the tools depend on an OS Connector; the agent runs on `alaiy_os` +
`erpnext` alone.

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench --site $SITE install-app alaiy_os_agent_shopify_listing
```

This app holds no credentials. Everything — the agent's text turns and both image
tools — goes through Alaiy OS core's `ai_client` seam, so whichever client is
installed supplies the key:

- **Managed** (`alaiy_os_ai_client` installed): text routes through the LiteLLM
  gateway, images through the billing service, which owns both provider keys and
  meters image spend against the same per-site balance. Nothing to configure.
- **BYOK**: core's default client uses the site's own `ai_api_key` for text and
  `openrouter_api_key` for image generation. Image *translation* is not available
  on BYOK — the tool reports that rather than half-working.

Whether a site can do either is checked before the agent commits to imagery, via
the seam's `image_support()`.

### Running the agent

From the Desk: the **Run Agent** page (Agents in the OS sidebar), an **Enrich**
button on the Item form, or **Enrich Listing** on the Shopify Product Listing form.
All three read the agent from `api.get_listing_agent` and render the toggles its
tools declare; none of them names an agent.

Or through core's REST surface (queued; poll the run):

```
POST /api/method/alaiy_os.api.agents.run_agent   {"agent": "shopify_listing", "payload": {"item_code": "<ITEM>"}}  -> {"run": "RUN-..."}
GET  /api/method/alaiy_os.api.agents.get_run     {"run": "RUN-..."}                                                -> status/output/error
```

On success `output` is a JSON object matching `schemas/output.json`, and the
same object has been persisted as a `Shopify Enriched Listing` for review.

### Images are produced after the listing

Enrichment runs in **two stages**, because the two halves have opposite shapes. The
agent's run is LLM-bound and takes about half a minute; rendering a five-shot image
set is minutes of waiting on a paid image service. Sharing one worker pool means a
single image product holds a slot that could have cleared nine text listings, and it
delays the listing text — the part a human actually reviews — behind imagery they
will look at later.

So the image tools do not produce images. They decide *whether* imagery happens and
*what* it should be, queue it, and hand the model placeholders with `url: null`. The
run finishes; `image_stage.run_step` renders the images afterwards on its own queue
and attaches them to the listing. Within a shot set the images are produced
concurrently, not one after another.

The enriched listing therefore has **two notions of done**. `status` is the review
state as always; `image_status` is the imagery:

| | |
|---|---|
| `Not Required` | no image step ran for this listing |
| `Queued` / `Running` | stage two owes it pictures |
| `Ready` | every queued image arrived |
| `Partial` | some arrived; the rest carry their own note, and `image_error` summarises |
| `Failed` | none arrived — the listing text is still saved and reviewable |

A listing is reviewable as soon as stage one finishes. A bulk batch reports
`Completed` on the same basis, with `images_pending` saying how many of its products
are still having pictures made.

**Translation is never paid for twice.** Re-running the agent over a product whose
photos were already translated reuses those results instead of sending them back to
the service — per photo, not per product, so a listing that gained a photo pays only
for the new one. "Already translated" means the listing holds a translated image for
that source photo, which makes the retry case fall out for free: a photo that failed
has no url, so it is not already translated and gets another attempt. The reused
entries are *returned* by the tool rather than skipped, because `save_listing`
rebuilds the image table from what the run reports — a translation left out would be
erased from the listing.

Image *generation* is deliberately not deduplicated the same way: each run writes
fresh briefs, so re-running is a request for new imagery rather than a repeat of the
old.

**Translation covers the variants too, and does not trust the model to say so.** One
`translate_product_images` call takes the listing's own photos *and* every enabled
variant's `variant_image`, with no per-product cap — a variant left with Chinese text
on its photo is worse than the cost of translating it. A photo shared by the listing
and a variant (or by two variants) is translated **once** and written to every row
that uses it, each row carrying the `item_variant` it belongs to.

Which photo belongs to which variant is settled in the tool and travels with the
queued job, so stage two writes those rows from that plan rather than from the
`images` array the model returned. A model that drops a variant's entry — or just its
`item_variant` — costs that variant nothing: the row is restored on delivery. The job
is queued even when every photo was already translated, because reconciling those
rows is its other half, and it re-delivers as a no-op rather than duplicating rows.

Nothing reaches the Shopify Product Listing until an admin approves: on approval,
rows with an `item_variant` are written onto that variant's `variant_image` (the
variant rows are updated in place, so `sh_shopify_variant_id` and `variant_price`
survive), and the rest become the listing's own images.

The handoff cannot be corrupted by the model: the tool enqueues the job itself with
`enqueue_after_commit=True`, so it fires on `save_listing`'s commit — the listing is
guaranteed to exist by then — and is dropped entirely if the run fails first, because
a rollback resets the pending after-commit callbacks.

**Giving images their own queue.** Stage two runs on `long` unless the bench declares
a dedicated queue, which is the whole point of splitting — image work should not be
able to starve everything else. In `sites/common_site_config.json`:

```json
"workers": { "images": { "timeout": 1800, "background_workers": 4 } }
```

and in each site's `site_config.json`:

```json
"listing_image_queue": "images"
```

A queue named there but never declared under `workers` falls back to `long` and logs
it, rather than failing the enrichment that queued it.

### Running it over many products

From the Desk: tick any number of rows in the **Shopify Product Listing** list and pick
**Enrich Listings** from the Actions menu. The dialog offers the same toggles as the
single-product button (both build it from `api.get_listing_agent`, via
`public/js/listing_agent.js`), plus how many products each job should take. It opens the
batch, which follows its own progress.

A **Shopify Listing Bulk Enrich** is a list of products plus the toggles they share.
Starting it queues the work on Frappe workers and **still creates one `OS Agent Run`
per product**, so every product keeps its own output, transcript and token count —
the batch row just links to it.

```
POST /api/method/alaiy_os_agent_shopify_listing.api.bulk_enrich
     {"item_codes": ["<ITEM-A>", "<ITEM-B>"], "batch_size": 5, "generate_images": 1}   -> {"batch": "BULK-...", "items": 2, "jobs": 1}
GET  /api/method/alaiy_os_agent_shopify_listing.api.get_bulk_status
     {"batch": "BULK-..."}                                                             -> status/counters/one row per product
```

Any extra argument is a per-request toggle, taken from whatever
`get_listing_agent` reports in `input_options` — the endpoint names no tool.

Products are **chunked**, not fanned out one job apiece: the batch is split into
chunks of `batch_size` and one background job is queued per chunk, which then runs
its products in order. Chunk count is the parallelism knob, chunk size the per-job
length — 200 products at the default put 40 jobs on the `long` queue rather than 200,
which is what keeps a big batch from starving everything else on it.

The batch's own `status` covers the agent runs only — imagery is stage two, so a
`Completed` batch can still have pictures rendering. `get_bulk_status` reports
`images_pending` for that, and each row carries its product's `image_status`.

One product failing only fails its own row (`Failed`, with the reason; the run holds
the traceback) and the batch ends *Completed with Errors*. From the batch form you can
**Cancel** mid-flight — workers stop before their next product — and **Retry Failed**
to re-queue just those rows. `skip_enriched` skips products that already have a
`Shopify Enriched Listing`, so re-running a batch only fills the gaps.

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/alaiy_os_agent_shopify_listing
pre-commit install
```

### License

agpl-3.0
