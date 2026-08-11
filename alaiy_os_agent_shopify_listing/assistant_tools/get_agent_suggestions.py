"""get_agent_suggestions -- list AI-generated listing suggestions for review.

Agent suggestions are Shopify Enriched Listing records (produced by the Shopify
Listing agent). There is no per-field old_value/suggested_value model; the whole
listing is the suggestion, reviewed and approved as a unit.

The field list below is exactly what the DocType has. An earlier version asked
for brand/seo_title/seo_description/shopify_tags, none of which exist on it --
frappe.get_all raises on an unknown column, so this could never have returned.
"""

from typing import Any, Dict

import frappe

from alaiy_os.assistant_tools._base import AlaiyTool

from alaiy_os_agent_shopify_listing.bulk import ENRICHED_DOCTYPE

_STATUSES = ("Draft", "Needs Review", "Approved")


class GetAgentSuggestions(AlaiyTool):
    def __init__(self):
        super().__init__()
        self.name = "get_agent_suggestions"
        self.description = (
            "Get AI-generated listing suggestions (Shopify Enriched Listing records) "
            "awaiting review. Filter by item_codes and status (Draft / Needs Review / "
            "Approved / all; default Needs Review). Returns each listing's generated "
            "title, description, category, product type, confidence and image status."
        )
        self.inputSchema = {
            "type": "object",
            "properties": {
                "item_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional item codes to filter by.",
                },
                "status": {
                    "type": "string",
                    "enum": list(_STATUSES) + ["all"],
                    "default": "Needs Review",
                },
            },
            "required": [],
        }

    def run(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not frappe.db.exists("DocType", ENRICHED_DOCTYPE):
            return self.fail(
                f"{ENRICHED_DOCTYPE} DocType not present "
                "(alaiy_os_agent_shopify_listing not installed or not migrated)."
            )

        filters = {}
        status = (arguments.get("status") or "Needs Review").strip()
        if status and status.lower() != "all":
            filters["status"] = status
        item_codes = self.as_list(arguments.get("item_codes"))
        if item_codes:
            filters["item_code"] = ["in", item_codes]

        rows = frappe.get_all(
            ENRICHED_DOCTYPE,
            filters=filters,
            fields=[
                "name",
                "item_code",
                "status",
                "category",
                "product_type",
                "confidence",
                "title",
                "description",
                "image_status",
                "needs_review",
                "modified",
            ],
            order_by="modified desc",
            limit_page_length=0,
        )
        return self.ok(status_filter=status, count=len(rows), suggestions=rows)
