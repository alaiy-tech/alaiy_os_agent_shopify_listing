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
- category-specific **attributes** (Jewelry / Watches / Accessories), and
- a **needs_review** list flagging every mandatory field it could not fill.

The agent is **read-only and advisory**: it reads the Item and its photos and
returns a JSON object. It does **not** write back to the Item or publish to
Shopify — that is the admin approval step and the Shopify connector's job.

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
| `tools/handlers.py` | The two tool handlers. |

**Tools**

- `get_product(item_code)` — returns the Item's raw fields (title, description,
  brand, item group, selling price, cost, barcode, supplier reference) **and its
  photos as vision image blocks**, so the model can read visual attributes
  (material, gemstones, dial, strap, clasp) off the actual product images.
- `get_reference_values()` — returns existing brands, categories, Shopify tags,
  and locations already in use, so output stays consistent instead of inventing
  near-duplicate values. Guarded: fields added by sibling apps (thesolist,
  shopify connector) are optional.

Neither tool depends on a connector; the agent runs on `alaiy_os` + `erpnext`
alone.

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench --site $SITE install-app alaiy_os_agent_listing
```

The engine's `anthropic_api_key` is managed by Alaiy OS core, not by this app.

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
