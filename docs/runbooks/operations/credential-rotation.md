# Credential rotation

Use dual credentials where the provider supports them. Never place a secret in
Git, image layers, Kubernetes ConfigMaps, command arguments or incident chat.

1. Create the replacement in the secret manager with the same narrow role.
2. Update only the process that needs it: API app/JWT, worker app/model/object,
   relay delivery, maintenance delivery, scheduler broker, or migrator owner.
3. Roll that deployment and verify readiness plus traces from the new release.
   For JWT, keep the previous verification key during the access-token TTL or
   intentionally invalidate all sessions.
4. Revoke the old credential, verify authentication failures do not increase,
   and audit the rotation metadata without the value.
5. For a suspected leak, rotate immediately, invalidate sessions/tokens, inspect
   access logs and audit chains, and follow the security-incident process.

