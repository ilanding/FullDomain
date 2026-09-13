# Telegram Website & Domain Manager V2

Workflow:
1. Buy/check domains first.
2. Each purchase requires explicit confirmation.
3. Registration is tracked asynchronously.
4. After registration, Cloudflare zone is created and Spaceship nameservers are updated automatically.
5. Use Check Status after ~10 minutes to see Active/Pending.
6. Create project and upload HTML.
7. Assign active domains manually, sequentially.
8. Deploy to Cloudflare Pages.
9. Email Routing can be added in the next module.

IMPORTANT: Verify current Spaceship API base URL, authentication headers, scopes, contact IDs, and response fields against your account's current official API documentation before live billing. This scaffold intentionally refuses purchase when contact IDs are not configured.
