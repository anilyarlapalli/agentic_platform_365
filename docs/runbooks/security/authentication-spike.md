# Authentication failure spike

1. Separate invalid credentials from rate-limited attempts using the outcome
   label; correlate only privacy-safe source and principal pseudonyms.
2. Check whether a client deployment, identity-store outage, credential
   rotation, or an attack explains the increase. Do not weaken the uniform 401
   response or expose tenant/user existence.
3. Block abusive sources at the edge where evidence supports it and preserve
   logs for incident response. Keep the distributed login limiter enabled.
4. For a legitimate rotation problem, follow the credential-rotation runbook
   and validate both old-token expiry and new login success.
5. Resolve after attempts return to baseline and any compromised principal or
   credential has been revoked and audited.
