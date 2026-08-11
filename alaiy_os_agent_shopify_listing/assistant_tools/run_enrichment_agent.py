"""run_enrichment_agent -- trigger the Shopify Listing agent on products.

Enqueues the alaiy_os `shopify_listing` agent (one async OS Agent Run per item)
which produces a Shopify Enriched Listing in 'Needs Review' for admin approval.
Returns the Run names -- poll them with alaiy_os.api.agents.get_run, or review
the results with get_agent_suggestions.

The agent id and DocType are imported rather than hardcoded: this tool used to
carry its own copies ("listing_enrichment" / "Enriched Listing"), neither of
which has ever existed on this bench, so the tool could not work. Importing the
same constants the agent itself uses is what stops that drift recurring.
"""

from typing import Any, Dict

import frappe

from alaiy_os.assistant_tools._base import AlaiyTool

from alaiy_os_agent_shopify_listing.agent_meta import AGENT_ID
from alaiy_os_agent_shopify_listing.bulk import ENRICHED_DOCTYPE

_ALL_MISSING_CAP = 200


class RunEnrichmentAgent(AlaiyTool):
    def __init__(self):
        super().__init__()
        self.name = "run_enrichment_agent"
        self.description = (
            "Trigger the Shopify Listing enrichment agent on one or more products. Each "
            "item is queued as a background run that generates a Shopify-ready listing "
            "(title, description, category, attributes, images) as a Shopify Enriched "
            "Listing in 'Needs Review'. Pass item_codes, or item_codes=['all_missing'] "
            "to queue every sales item that has never been enriched. Returns the run ids."
        )
        self.inputSchema = {
            "type": "object",
            "properties": {
                "item_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Item codes to enrich, or ['all_missing'] for all never-enriched sales items.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "default": "normal",
                    "description": "Advisory priority (the engine runs all on the long queue today).",
                },
            },
            "required": ["item_codes"],
        }

    def run(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from alaiy_os.engine.executor import execute_agent

        if not frappe.db.get_value("OS Agent Registry", AGENT_ID, "is_enabled"):
            return self.fail(f"Agent '{AGENT_ID}' is not installed/enabled on this site.")

        item_codes = self.as_list(arguments.get("item_codes"))
        self.require(arguments, "item_codes")

        if len(item_codes) == 1 and item_codes[0].strip().lower() == "all_missing":
            item_codes = self._items_never_enriched()
            capped = len(item_codes) > _ALL_MISSING_CAP
            item_codes = item_codes[:_ALL_MISSING_CAP]
        else:
            capped = False

        queued = []
        errors = []
        for code in item_codes:
            if not frappe.db.exists("Item", code):
                errors.append({"item_code": code, "error": "Item not found"})
                continue
            try:
                run_name = execute_agent(AGENT_ID, payload={"item_code": code}, trigger_type="API")
                queued.append({"item_code": code, "run": run_name})
            except Exception as e:
                frappe.log_error(frappe.get_traceback(), f"run_enrichment_agent {code}")
                errors.append({"item_code": code, "error": str(e)})

        result = self.ok(agent=AGENT_ID, queued_count=len(queued), queued=queued)
        if errors:
            result["errors"] = errors
        if capped:
            result["note"] = f"Capped to {_ALL_MISSING_CAP} items this run; re-run for the rest."
        return result

    @staticmethod
    def _items_never_enriched():
        # If this set comes back empty because the DocType is missing, every
        # sales item looks un-enriched and the caller silently re-queues the
        # whole catalogue -- so treat an absent DocType as "nothing to do"
        # rather than "everything is missing".
        if not frappe.db.exists("DocType", ENRICHED_DOCTYPE):
            return []
        enriched = {
            r["item_code"]
            for r in frappe.get_all(ENRICHED_DOCTYPE, fields=["item_code"], limit_page_length=0)
            if r.get("item_code")
        }
        items = frappe.get_all(
            "Item", filters={"is_sales_item": 1, "disabled": 0}, fields=["name"], limit_page_length=0
        )
        return [i["name"] for i in items if i["name"] not in enriched]
