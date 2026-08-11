"""approve_suggestion -- approve / reject / edit an AI listing suggestion.

Operates on the Shopify Enriched Listing record ONLY. It deliberately does NOT
write the listing back onto the Item -- publishing an approved listing to a sales
channel is the connector's job (e.g. the Shopify connector), a separate step.
Approval here is the human-review gate, not the publish action.

Shopify Enriched Listing has no 'Rejected' status, so a reject moves it back to
'Draft' and records the reason in notes.

_EDITABLE lists real fields on the DocType. An earlier version allowed
bullet_points/seo_*/shopify_* fields that do not exist on it -- and because
Document.set() on an unknown fieldname is a silent no-op, an "edit" would report
success while changing nothing. _editable_fields() re-checks against the live
meta so that failure mode surfaces as an error instead.
"""

import json
from typing import Any, Dict

import frappe

from alaiy_os.assistant_tools._base import AlaiyTool

from alaiy_os_agent_shopify_listing.bulk import ENRICHED_DOCTYPE

# Fields on the Shopify Enriched Listing an edit may touch (never the Item).
_EDITABLE = {
    "title",
    "description",
    "category",
    "product_type",
}


class ApproveSuggestion(AlaiyTool):
    def __init__(self):
        super().__init__()
        self.name = "approve_suggestion"
        self.description = (
            "Approve, reject, or edit an AI listing suggestion (Shopify Enriched Listing). "
            "Approve marks it ready (status 'Approved'); reject returns it to 'Draft' "
            "with a reason; edit applies field changes (as a JSON object in edit_value) "
            "to the listing and approves it. This updates the listing only -- it does "
            "NOT modify the Item; publishing to a channel is handled by the connector."
        )
        self.inputSchema = {
            "type": "object",
            "properties": {
                "suggestion_id": {
                    "type": "string",
                    "description": "Shopify Enriched Listing name (the item_code).",
                },
                "action": {"type": "string", "enum": ["approve", "reject", "edit"]},
                "edit_value": {
                    "type": "string",
                    "description": "For action=edit: a JSON object of listing fields to update, e.g. {\"title\": \"...\"}. For reject: an optional reason string.",
                },
            },
            "required": ["suggestion_id", "action"],
        }

    def run(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.require(arguments, "suggestion_id", "action")
        name = arguments["suggestion_id"]
        action = arguments["action"].strip().lower()

        if not frappe.db.exists(ENRICHED_DOCTYPE, name):
            return self.fail(f"{ENRICHED_DOCTYPE} '{name}' not found.")

        doc = frappe.get_doc(ENRICHED_DOCTYPE, name)

        if action == "approve":
            doc.status = "Approved"
        elif action == "reject":
            reason = (arguments.get("edit_value") or "").strip()
            doc.status = "Draft"
            if reason:
                doc.notes = (doc.notes + "\n" if doc.notes else "") + f"Rejected: {reason}"
        elif action == "edit":
            applied = self._apply_edits(doc, arguments.get("edit_value"))
            if isinstance(applied, dict) and applied.get("error"):
                return self.fail(applied["error"])
            doc.status = "Approved"
        else:
            return self.fail("action must be approve, reject, or edit.")

        doc.save()
        frappe.db.commit()

        return self.ok(
            suggestion_id=doc.name,
            item_code=doc.item_code,
            status=doc.status,
            note="Listing updated. Publishing to a channel is a separate connector step; the Item was not modified.",
        )

    @staticmethod
    def _editable_fields():
        """_EDITABLE intersected with what the DocType actually has."""
        meta = frappe.get_meta(ENRICHED_DOCTYPE)
        return {f for f in _EDITABLE if meta.has_field(f)}

    @classmethod
    def _apply_edits(cls, doc, edit_value):
        if not edit_value:
            return {"error": "action=edit requires edit_value (a JSON object of fields)."}
        try:
            changes = json.loads(edit_value) if isinstance(edit_value, str) else edit_value
        except json.JSONDecodeError:
            return {"error": "edit_value must be valid JSON, e.g. {\"title\": \"New title\"}."}
        if not isinstance(changes, dict):
            return {"error": "edit_value must be a JSON object of field: value pairs."}

        editable = cls._editable_fields()
        rejected = [k for k in changes if k not in editable]
        if rejected:
            return {
                "error": f"These fields cannot be edited here: {', '.join(rejected)}. "
                f"Allowed: {', '.join(sorted(editable))}."
            }
        for field, value in changes.items():
            doc.set(field, value)
        return {"applied": list(changes.keys())}
