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
| `tools/image_generation.py` | `generate_product_images` — a five-shot editorial set (gpt-image-1). |
| `tools/image_translation.py` | `translate_product_images` — supplier photo text into English (alphashop). |
| `tools/images.py` | Shared image primitives both image tools are built from. |
| `api.py` | `get_listing_agent`, what the desk surfaces ask. |
| `setup/install.py` | Registry + sidebar reconcile. |
| `.../doctype/shopify_enriched_listing/` | The shared output DocType and its images child table. |
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

**The two image tools**, each opt-in per request and each gated in code rather than
by the model's judgement — they act only when the product actually has a photo *and*
the request's toggle is true:

- `generate_product_images(briefs, …)` — the full editorial set (hero, detail, angle,
  lifestyle, scale) in one call, every shot produced by *editing the product's real
  photo* so it never invents a product from scratch. Needs `openrouter_api_key`.
- `translate_product_images(…)` — each supplier photo's printed text rendered into
  English, the result re-hosted locally so it survives the vendor's URL expiring.
  Needs `alphashop_ak` / `alphashop_sk`.

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

The engine's `anthropic_api_key` is managed by Alaiy OS core, not by this app. The
image tools read their own keys from `site_config.json` —
`openrouter_api_key` for generation, `alphashop_ak` / `alphashop_sk` for
translation. Only the tool a site actually uses needs its key.

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

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/alaiy_os_agent_shopify_listing
pre-commit install
```

### License

agpl-3.0
