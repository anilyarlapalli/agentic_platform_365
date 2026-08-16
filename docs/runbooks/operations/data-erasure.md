# Data erasure

1. Authenticate the requestor and confirm tenant, document id, legal hold and
   retention obligations. Record the ticket id outside the content system.
2. Withdraw the document first so no new answer retrieves it. Wait for the
   replacement collection build to become live.
3. An owner calls `DELETE /api/documents/{document_id}/purge`. The operation
   deletes object bytes before the database row and fails closed if audit is
   unavailable.
4. Verify the object key is absent, the document/chunks are absent under the
   tenant scope, and a new reindex run is durable. Never query another tenant to
   “double-check” isolation.
5. Verify the tenant audit chain and retain the erasure event (identifiers and
   outcome only, no deleted content). Account for immutable backup expiry and
   record the date at which all backup copies age out.

