"""Route modules.

Registration itself lives in :mod:`platform_core.api.route_table`, alongside the
route-table construction, so the list of routers that gets *served* and the list
that gets *authorisation-checked* are literally the same tuple. Keeping them in
two places is how a route ends up served but unchecked.

Handlers are thin by design in Phase 1: what matters is that every route exists,
is declared in the policy table, and reads through a tenant session — so even a
handler that forgets a WHERE clause cannot return another tenant's rows.
"""
