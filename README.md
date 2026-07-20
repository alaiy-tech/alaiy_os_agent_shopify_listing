## Alaiy OS — Listing Enrichment Agent

An **Alaiy OS agent** (standalone Frappe app) that turns one supplier's raw,
incomplete product data into a polished, structured, **Shopify-ready listing**
for **The Solist**. It is the AI half of the Uploadify replacement: the admin no
longer hand-fills mandatory attributes — the agent proposes them, and the admin
reviews, edits, and approves before publish.

Given a product (an ERPNext `Item` created from a supplier CSV), the agent
produces:

- an editorial **title** and **description** in Solist house style,
- selling-point **bullet points**,
- **SEO** title / description / keywords,
- **Shopify tags** and a suggested Shopify **category**,
- category-specific **attributes** (Jewelry / Watches / Accessories),
- a generated **images** set (hero, detail, angle, lifestyle, scale) when supplier
  photos alone aren't enough for a full catalog listing, and
- a **needs_review** list flagging every mandatory field it could not fill.

The agent is **advisory**: it reads the Item and its photos and returns a JSON
object. It does not edit the Item's own fields or publish to Shopify — that is
the admin approval step and the Shopify connector's job. The one exception is
`generate_image`, which attaches generated shots as Files on the Item.

### How it works

On install/migrate the app self-registers in core's `OS Agent Registry` (agent
id `listing_enrichment`) from `agent_meta.py`. Core (`alaiy_os`) owns the engine
— the LLM ⇄ tool loop, `OS Agent Run` history, and the Agents hub. This app owns
just this one agent's definition:

| File | What it holds |
|------|-----------------|
| `agent_meta.py` | Identity, model (`claude-opus-4-8`), `max_turns`, output format, and the tool list. |
| `prompts/system.md` | The system prompt — role, workflow, per-category mandatory attributes, house-style rules. |
| `schemas/output.json` | JSON Schema the final listing object must satisfy. |
| `tools/handlers.py` | The tool handlers. |

**Tools**

- `get_product(item_code)` — returns the Item's raw fields (title, description,
  brand, item group, selling price, cost, barcode, supplier reference) **and its
  photos as vision image blocks**, so the model can read visual attributes
  (material, gemstones, dial, strap, clasp) off the actual product images.
- `get_reference_values()` — returns existing brands, categories, Shopify tags,
  and locations already in use, so output stays consistent instead of inventing
  near-duplicate values. Guarded: fields added by sibling apps (thesolist,
  shopify connector) are optional.
- `generate_image(kind, brief)` — generates one editorial shot (`kind` is
  `hero` / `detail` / `angle` / `lifestyle` / `scale`) via OpenRouter
  (`openai/gpt-image-1`), stores it as a standalone public File, and returns
  `{kind, brief, url}` for the model to copy into the final `images` array.
  Requires `openrouter_api_key` in `site_config.json`.

None of the tools depend on an OS Connector; the agent runs on `alaiy_os` +
`erpnext` alone.

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench --site $SITE install-app alaiy_os_agent_listing
```

The engine's `anthropic_api_key` is managed by Alaiy OS core, not by this app.
This app's own `generate_image` tool needs its own key —
`openrouter_api_key` in `site_config.json`.

### Running the agent

Runs go through core's REST surface (queued; poll the run):

```
POST /api/method/alaiy_os.api.agents.run_agent   {"agent": "listing_enrichment", "payload": {"item_code": "<ITEM>"}}  -> {"run": "RUN-..."}
GET  /api/method/alaiy_os.api.agents.get_run      {"run": "RUN-..."}                                                    -> status/output/error
```

On success `output` is a JSON object matching `schemas/output.json`, ready to
render on the product page for admin review and approval.

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/alaiy_os_agent_listing
pre-commit install
```

### License

agpl-3.0
