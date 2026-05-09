# ip·Solis – Task Backlog

Format: `[open]` / `[done]` / `[blocked]`. Add new tasks at the top of their section.
Strategic roadmap up top; smaller polish/gap items in the middle; pre-existing infra
and historical "done" entries at the bottom.

---

## Status — 2026-04-30

Prio-0 enterprise-procurement push **complete**. The 2026-04-26 round shipped
the headline features (RBAC, external secret management, audit + SIEM,
conditional approval rules, per-classification retention, HA Beat); the
2026-04-30 follow-on round closed every queued slice-2 / slice-3 enrichment:

* External secret management: Conjur + Azure KV + AWS SM backends, residual
  `is_secret` key resolver coverage, one-shot bulk-migration tool, Vault
  AppRole / Kubernetes-JWT auth, AWS native AssumeRole, CCP mTLS file-upload
  bootstrap UX.
* SIEM: Sentinel Logs Ingestion API (replacement for the 2026-08-31 sunset
  Data Collector API).
* Approvals: per-classification routing (compliance officer or owner-of-record
  per PII / PHI / PCI), escalation v2 (assignment mode with tokenized URL),
  per-bucket supersession, recursive AND/OR/NOT rule editor.
* Compliance: API tokens hard-delete + Beat purge.
* HA: multi-replica api / worker docs + Postgres standby + failover docs
  (section 12 of `docs/DEPLOYMENT.md`).

The *Deferred Enterprise Backlog* below is now empty of functional items —
everything previously queued has shipped. The two genuinely-external items
in the *Pre-existing open tasks* section (Entra ID Connect setup; Cloud
group management via Microsoft Graph) remain — both deliberately scoped
outside the application codebase.

---

## Deferred Enterprise Backlog

Items here are **not actively planned** — they're the slices we deliberately
deferred from the 2026-04-26 enterprise push. Each is independently slice-able;
pull one when there's a procurement need or a quiet week.

### RBAC slice 4 — **shipped 2026-04-27**
*(originating section: [`Admin RBAC`](#done-admin-rbac--prio-0-show-stopper) further down)*
- [x] **Forced password rotation policy + lockout-on-N-failed-attempts.**
      Migration `0073_rbac_slice4` adds `admin_users.password_set_at`,
      `failed_login_count`, `locked_at`. Three new config keys
      (`rbac.password_rotation_days`, `rbac.lockout_threshold`,
      `rbac.lockout_duration_minutes`) — 0 disables each, defaults
      ship "off" so existing installs are unchanged. `app.utils.password_policy`
      provides `read_policy()` / `is_locked()` / `password_must_be_changed()` /
      `record_failed_login()` / `record_successful_login()` /
      `record_password_change()`. Wired into `admin_auth.py` (login flow),
      `admin_self.py` (self-service password change), `admin_users.py`
      (superadmin reset = unlock + clock reset). Lockout responses use
      HTTP 423 with an "unlock at <UTC>" hint. Auto-unlock fires on the
      next attempt past the duration window so brief flurries clear
      themselves. Settings UI section in the Compliance tab; values
      writable on community but enforcement gated on the
      `password_policy` Enterprise feature key.
- [x] **Auditor read paths on `admin/maintenance/*`.** Router floor
      relaxed from `admin` to `auditor`; every write/trigger route
      (`POST /backups`, `DELETE /backups/{id}`, `PUT /retention`,
      `POST /cleanup`, `POST /queue/purge`, `PUT /schedule`,
      `PUT /alerts`, `POST /alerts/test`) carries an explicit
      `_WRITE_GATE = require_role("admin")`. Backup *download*
      (`GET /backups/{id}/download`) keeps the admin gate — backup
      files contain the full DB and aren't a "read" the way the listing is.
- [x] **SoD per-rule approver opt-out.** Backend plumbing complete:
      `approval_rules` JSON entries accept `sod_exempt: true`; matched
      approvers carry the flag through `evaluate_rules()`; new
      `order_approvals.sod_exempt` column captures it at
      order-creation time so subsequent rule edits don't shift past
      orders' SoD logic. `apply_approval_decision()` skips the SoD
      check when the flag is set. UI checkbox in the asset-type form's
      rules editor is deferred polish (admins can set it via direct
      JSON edit of `approval_rules` for now).
- [x] **Token-role-aware mint guard relaxation.** Router gate on
      `admin_api_tokens` dropped from `superadmin` to `admin`. Mint
      guard (creator role ≥ requested token role) is now the operative
      defense — an `admin` cannot forge a superadmin-bound token, so
      the worst they can do is mint a token at-or-below their own
      privilege, which they already have anyway.

### External secret management slice 2
*(originating section: [`External secret management`](#done-external-secret-management--prio-0-show-stopper) further down)*
- [x] **CyberArk Conjur adapter** *(2026-04-30)*. Fifth backend
      alongside Vault, CCP, Azure KV, AWS SM. New
      `conjur://<identifier>[#<field>]` reference scheme — the
      optional `#field` mirrors the Vault / AWS SM convention and
      pulls a JSON-field out of the variable's value (covers the
      common pattern of storing `{"username":"…","password":"…"}`
      as a single Conjur variable). Migration `0086` seeds 5 config
      keys (url / account / host_id / api_key / verify_tls). Auth
      via the documented two-step host API-key flow: POST the API
      key to `/<account>/host/<host_id>/authn` with
      `Accept-Encoding: base64` to mint a token, then GET
      `/secrets/<account>/variable/<id>` with `Authorization: Token
      token="<base64>"`. Stdlib HTTP throughout — no Conjur SDK on
      either api or worker. Tokens cached for 7 minutes (Conjur's
      default TTL is 8 minutes; 1-minute clock-skew margin) keyed
      by `(url, account, host_id)` so a config drift can't
      cross-pollinate tokens between tenants on a shared resolver
      process. 401 on a secret read invalidates the cached token
      immediately and re-mints on the next call. Identifier may
      contain slashes (`prod/ipsolis/ad-bind-password`) — the
      resolver URL-encodes it as a single path segment so nested
      namespacing works without contortions. The host id `host/`
      prefix is added automatically if missing — operators
      typically type just the bare id. Test-connection probe
      exercises the host login flow (mints a fresh access token);
      doesn't probe a specific variable since the most common
      operator error is "wrong api_key for this host" rather than
      "variable missing". Settings UI adds a sixth backend option
      with url / account / host_id / api_key / verify_tls inputs
      and a reference-shape help block calling out the on-prem vs
      Conjur Cloud URL forms. Verified live: fake creds against
      `conjur.example.invalid` produce `Conjur auth failed: conjur:
      login endpoint unreachable: [Errno -2] Name or service not
      known` — descriptive error surfaces all the way to the
      Settings card; resolver follows the fail-closed-quiet
      contract (returns empty string + WARNING log on failure,
      never crashes the calling request).
- [x] **AWS Secrets Manager adapter** *(2026-04-30)*. Fourth backend
      alongside Vault, CCP, Azure KV. New `awssm://<secret-id>[#<field>]`
      reference scheme — the optional `#field` mirrors the Vault
      convention and pulls a JSON-stringified field out of
      `SecretString` (covers the common AWS pattern of storing
      `{"username":"…","password":"…"}` as one secret). Migration
      `0085` seeds 4 config keys (region / access_key_id /
      secret_access_key / session_token); session_token is optional
      and supports STS-issued temporary credentials. Auth via stdlib
      SigV4 — no boto3 dep on either api or worker. Test connection
      uses `ListSecrets` with `MaxResults=1` to exercise the SigV4
      path without depending on a specific secret existing; the
      docstring + UI explain that the IAM principal needs both
      `secretsmanager:ListSecrets` AND `secretsmanager:GetSecretValue`
      for full ipSolis use. Settings UI adds a fifth backend option
      with region / access-key-id / secret-access-key / session-token
      inputs. Verified live against real AWS:
      `UnrecognizedClientException` from a fake access key confirms
      SigV4 signature shape is correct (otherwise AWS would return
      `InvalidSignatureException`); resolver follows the
      fail-closed-quiet contract.
- [x] **Azure Key Vault adapter** *(2026-04-30)*. New
      `azurekv://<vault-name>/<secret-name>` reference scheme resolved
      via Azure AD client_credentials flow. Migration `0084` seeds 4
      config keys (`secret.azurekv.tenant_id` / `client_id` /
      `client_secret` / `api_version`); the SPN is independent from
      `entra.*` so the KV principal can carry minimum-necessary access
      (`Key Vault Secrets User` only). Stdlib-only auth path (raw
      OAuth POST against `/oauth2/v2.0/token`) so the worker mirror
      doesn't pull MSAL into its image. AAD bearer tokens cache
      separately from the value cache, with a 60-second safety margin
      against clock skew. Settings UI gets a third backend option
      ("Azure Key Vault") with tenant-id / client-id / client-secret /
      api-version inputs; the show/hide toggle generalised to a
      config-driven loop so future backends slot in cleanly. Test
      connection acquires a Key Vault scope token from the configured
      SPN — verifies the most-common config error (SPN itself
      misconfigured) without needing a vault name. Versioned references
      (`?version=<id>`) supported. Verified live against real Azure AD:
      `AADSTS900021` from a fake tenant id confirms the round-trip
      works end-to-end; resolver follows the fail-closed-quiet contract
      (returns empty string + WARNING log on resolution failure).
- [x] **Vault AppRole + Kubernetes-JWT auth methods** *(2026-04-30)*.
      Slice 1 shipped static-token only — production Vault deployments
      generally ban long-lived tokens. New `secret.vault.auth_method`
      key picks `token` (default — back-compat) / `approle` / `kubernetes`.
      Migration `0088` seeds the new keys: `auth_method` plus AppRole
      (`approle_path`, `approle_role_id`, `approle_secret_id` (is_secret))
      and Kubernetes (`k8s_path`, `k8s_role`, `k8s_jwt_path`) groups.
      AppRole flow: POST role_id + secret_id to `/v1/auth/<mount>/login`,
      receive `auth.client_token` + `auth.lease_duration`, cache the
      token until 60s before expiry. Kubernetes flow: read the projected
      service-account JWT fresh from disk on every login (kubelet
      rotation lands without a restart), POST role + JWT to
      `/v1/auth/<mount>/login`. Token cache keyed by
      `(method, identity)` — role_id for AppRole, role for k8s — so
      a config drift can't cross-pollinate tokens between tenants.
      A 403 on a downstream KV read or write invalidates the cached
      token immediately so the next call re-mints; the failing call
      still surfaces, deliberately, so the operator sees the auth error.
      Both `_resolve_vault()` (read path) and `_push_vault()` (migration
      tool's write path) funnel through the new `_get_vault_token(cfg)`
      so all three auth methods cover read + write + the bulk-migration
      tool with one code path. Test-connection probe extended: token
      mode keeps the existing unauth `sys/health` hit; AppRole and
      Kubernetes mode mint a fresh token first (clearing any cached
      one) and then call `sys/health` *with* the new token to verify
      the role's policy actually attaches reads — catches the common
      "AppRole role exists but is bound to an empty policy set" gotcha.
      Worker mirror at `worker/tasks/modules/secrets.py` carries the
      same logic, stdlib HTTP only — no `hvac` dependency. Settings UI
      gets an inner auth-method dropdown that drives a config-driven
      show/hide loop (token / AppRole / Kubernetes field groups);
      page-load handler calls both `onSecretBackendChange()` and
      `onVaultAuthMethodChange()` so the form lands in a consistent
      state. Smoke-tested live: token mode against a real local Vault
      reports `Vault reachable (sys/health responded)`; AppRole with
      fake creds against the same real Vault produces
      `Vault approle login failed: vault approle login HTTP 403:
      {"errors":["permission denied"]}` — descriptive auth error
      surfaces all the way through; kubernetes mode with no JWT file
      produces `Vault kubernetes login failed: vault: kubernetes JWT
      not readable at '<path>': [Errno 2] No such file or directory`
      — pre-flight catches missing mount cleanly.
- [x] **AWS native AssumeRole** *(2026-04-30)*. Slice 1 supported
      static IAM keys plus manually-pasted STS temporary creds — the
      latter expire on the AWS-decided clock and require operator
      intervention before each rotation. New
      `secret.awssm.auth_method` key picks `static` (default —
      back-compat) or `assume_role`. Migration `0089` seeds the new
      keys: `auth_method`, `role_arn`, `role_session_name` (default
      `ipsolis`, surfaces in CloudTrail), `role_external_id`
      (third-party-IAM trust pattern), `role_duration_seconds`
      (default 3600, capped by the role's `MaxSessionDuration`).
      In assume_role mode the configured access keys become a
      *bootstrap identity* whose only required permission is
      `sts:AssumeRole` on the target role; the role itself carries
      the SM permissions, which narrows the blast radius of a
      leaked bootstrap key. ip·Solis calls
      `sts:AssumeRole` (SigV4-signed Query API POST to
      `sts.<region>.amazonaws.com` — STS only exposes the Query API,
      so the body is form-encoded and the response is XML; we parse
      with `xml.etree.ElementTree`), caches the returned
      `(AccessKeyId, SecretAccessKey, SessionToken, Expiration)` in
      a process-local dict keyed by `(role_arn, session_name)`, and
      re-mints automatically 60 seconds before the `Expiration`
      timestamp. A 403 / `ExpiredToken` / `InvalidClientTokenId` on
      a downstream SM call invalidates the cache immediately so the
      next call re-mints; the failing call still raises so the
      operator sees the auth error. Both `_resolve_awssm()` (read
      path) and `_push_awssm()` (migration tool's write path) and
      `_aws_sm_list()` (test-connection probe) funnel through the
      new `_get_aws_creds(cfg)` helper so all three backend
      operations cover both auth methods with one code path.
      Test-connection probe in assume_role mode clears the cached
      session first (otherwise a stale-but-valid cache could mask a
      freshly-broken trust policy), then exercises the full
      bootstrap → AssumeRole → SM `ListSecrets` chain. Worker mirror
      at `worker/tasks/modules/secrets.py` carries the same logic,
      stdlib HTTP + `xml.etree` only — no boto3 on the worker side.
      Settings UI gets a sub-dropdown under the AWS SM card driving
      a show/hide for the AssumeRole sub-fields; static mode
      continues to render the existing access-key /
      secret-access-key / session-token inputs unchanged.
      Smoke-tested live: static mode with fake AKIA keys produces
      the same `UnrecognizedClientException: The security token
      included in the request is invalid` from real AWS as before
      (regression intact); assume_role with no role_arn produces
      `awssm: role_arn is empty (auth_method=assume_role)` from the
      pre-flight; assume_role with a role_arn but fake bootstrap
      creds round-trips to real AWS STS and surfaces the underlying
      XML ErrorResponse: `sts AssumeRole HTTP 403:
      <ErrorResponse>...<Code>InvalidClientTokenId</Code>...
      </ErrorResponse>` — proving SigV4 signing for STS Query API +
      form-encoded body + XML-response parsing all work end-to-end
      (otherwise AWS would have rejected with
      `InvalidSignatureException` / `SignatureDoesNotMatch`).
- [x] **CCP mTLS bootstrap UX (file upload)** *(2026-04-30)*. Today's
      slice-1 UI surfaces a single textarea where operators paste the
      pre-concatenated cert + key PEM. The two most common bootstrap
      mistakes — forgetting the trailing newline between blocks, or
      swapping the cert and key — both fail at the resolver with an
      opaque CCP 4xx that's hard to diagnose. Slice 2 replaces the
      textarea with two `<input type="file">` inputs (one for the
      certificate, one for the key) that the browser reads via
      `FileReader`, validates against PEM-shape regexes, concatenates
      with the canonical `cat client.crt client.key` shape
      (`<cert>\n<key>\n`), and writes into a hidden textarea so the
      existing `saveSecretBackend()` save-path is unchanged — server-
      side: same `secret.ccp.client_cert_pem` config key, same
      `is_secret=true` masking, same resolver materialisation to a
      0600 temp file via `ssl.load_cert_chain`. No new migration, no
      new config key, no API surface change. Validation catches the
      operator-facing failures before they reach the resolver:
      certificate file missing the `-----BEGIN CERTIFICATE-----`
      block ("Did you pick the wrong file?"), key file with the
      `-----BEGIN ENCRYPTED PRIVATE KEY-----` header (clear "ipSolis
      can't decrypt encrypted keys, run `openssl pkcs8 -in
      encrypted.key -out unencrypted.key`" message — passphrases-in-
      config beat the point of an encrypted key file, so the
      adapter doesn't surface a passphrase knob), key file missing
      the `PRIVATE KEY` block ("Did you swap the cert and key
      files?"). An *Advanced: paste PEM directly* toggle keeps the
      old textarea available for operators with a pre-assembled
      bundle (typical of automation pipelines that dump
      `cat *.crt *.key` into a config sync); the two modes write to
      the same backing config key. Status badge above the inputs
      shows the currently-configured state ("✓ Configured — upload
      new files to replace, or leave empty to keep current.") so the
      "(configured — leave blank to keep current)" placeholder
      semantics inherited from the textarea version still apply when
      no file is picked. Smoke-tested live: generated a 2048-bit
      RSA self-signed cert + key with `openssl req -x509 -newkey
      rsa:2048 -nodes`, simulated the JS concatenation logic,
      POSTed the assembled bundle to
      `/admin/config/secret.ccp.client_cert_pem` → 200 OK, returned
      response masked as `***`; resolver-side
      `_load_secret_cfg` round-trip confirms the saved value
      contains both `BEGIN/END CERTIFICATE` and `BEGIN/END PRIVATE
      KEY` blocks (2839 chars total). Container-side template
      inspection confirms the new IDs are rendered (2× cert-file
      input, 2× key-file input, 3× `onCcpFileChange` reference,
      3× `toggleCcpMtlsAdvanced` reference). External secret
      management slice 2 is now complete — closes the last queued
      item in that backlog section.
- [x] **One-shot plaintext → backend migration tool** *(2026-04-30)*.
      `POST /admin/config/secret-backend/migrate?dry_run=true|false`
      walks every `is_secret=true` row in `app_config`, pushes each
      plaintext value to the configured backend at a deterministic
      address, and replaces the row's value with the matching
      reference. Per-row status: `would_migrate` (dry-run),
      `migrated`, `skipped` (empty / already a reference / `secret.*`
      key), `failed` (carries the underlying error message — partial
      failures don't roll back successful sibling rows). Address
      convention driven by `secret.migration_prefix` (default
      `ipsolis`): `ad.password` becomes `vault://ipsolis/ad/password`
      / `azurekv://<vault>/ipsolis-ad-password` (KV names disallow
      slashes — dots become hyphens) / `awssm://ipsolis/ad/password`
      / `conjur://ipsolis/ad/password`. Migration `0087` seeds the
      prefix key plus an Azure-specific
      `secret.azurekv.migration_vault` (Azure KV references include
      the vault name, so the tool needs to know the destination
      vault). CCP is rejected with a clear error since Safes are
      managed via PVWA, not the AIM API. Per-backend write helpers
      are stdlib HTTP only: Vault KV-v2 POST with the wrapped
      `{"data": {"value": …}}` envelope; Azure KV PUT
      `/secrets/<name>` reusing the AAD token cache; AWS SM
      `CreateSecret` with fallback to `PutSecretValue` on
      `ResourceExistsException` (so re-runs overwrite cleanly);
      Conjur `POST /secrets/<account>/variable/<id>` with a focused
      404 hint when the variable isn't policy-declared. Pre-flight
      check refuses to start when backend creds are incomplete or
      Azure's destination vault is unset. Audit: one
      `app_config / migrated_to_backend` row per swap (value content
      elided — only key + resulting reference shape) plus a single
      `secret_migration_run` summary row per real migration.
      Settings UI gets a dedicated section with prefix /
      destination-vault inputs, **Dry run** (read-only preview),
      and **Migrate now** (with confirm dialog and per-row result
      table). Smoke-tested live: dry-run against the Vault backend
      classified all 18 `is_secret=true` rows correctly (8
      `would_migrate`, 10 `skipped` — emptys + `secret.*` excludes,
      0 `failed`); live migration against an unreachable Vault
      surfaced 8 `failed` rows with `vault unreachable: [Errno -2]
      Name or service not known` and zero partial commits, proving
      the per-row error isolation contract.
- [x] **Resolver coverage on residual is_secret keys** *(2026-04-30)*.
      Five raw read sites wired through the existing async/sync
      resolvers so reference values (`vault://…`, `conjur://…`,
      `azurekv://…`, `awssm://…`, `ccp://…`) dereference at read
      time. Sites: SCCM probe (`worker/tasks/workflows/sccm_probe.py`
      `_load_sccm_config()` — switched from raw `psycopg2` to a
      sync SQLAlchemy session so it can call `get_secret_config`),
      Teams webhook URL (3 worker callers — `approval_reminders`,
      `dynamic_runner`, `cost_threshold_alerter` — and 2 API
      callers: the `/admin/config/teams/test` endpoint and the
      certification-campaign kickoff that hands the resolved URL
      to the notifications worker), and the three `is_secret` SIEM
      keys (`siem.token` for Splunk HEC, `siem.shared_key` for
      Sentinel, `siem.webhook_secret` for the generic webhook
      adapter — both worker-side `siem_streamer.py` and API-side
      `/admin/config/siem/test`). The certifications path resolves
      once in the API and forwards the ready-to-POST URL to the
      Celery task so notification workers stay free of resolver
      plumbing. Smoke-tested live: setting
      `teams.webhook_url=vault://teams/webhook` produced
      `WARNING app.utils.secrets: secrets: resolution failed for
      'vault://teams/webhook': HTTP 404 Not Found` in the API log
      and the test endpoint correctly reported "Teams webhook URL
      is not configured" — fail-closed-quiet contract intact, the
      operator sees the underlying cause in the application log
      without a crashed request. Plain string values still pass
      through unchanged so partial migrations remain safe.

### API tokens — residuals
- [x] **Hard-delete purge for revoked / expired tokens** *(2026-04-30)*.
      Slice 1 (migration `0054_api_tokens`) soft-deletes via
      `revoked_at` and leaves rows in `api_tokens` indefinitely —
      good for the "we used to have token X" audit trail, bad for
      tenants under regulated-record-retention policies who must
      not retain dead credentials past a defined window. New
      `api_tokens.purge_after_days` config key (default `0`,
      disabled) seeded by migration `0093` controls the policy.
      New worker Beat task
      `worker/tasks/workflows/api_token_purge.py:purge_old_tokens`
      runs daily at 03:15 Europe/Berlin (slot between audit
      retention at 03:00 and approval auto-decline at 03:30).
      Single SELECT identifies rows where `revoked_at < cutoff`
      OR `expires_at < cutoff` with a `CASE` to attribute each
      to a `'revoked'` or `'expired'` reason (revoked wins when
      both apply — explicit operator intent over clock-driven).
      Per-row audit (`api_token / hard_deleted`, `triggered_by =
      celery:api_token_purge`) captures `name`, `token_prefix`,
      `scopes`, `revoked_at` / `expires_at` snapshots in
      `old_value`, plus `reason`, `purged_after_days`, `cutoff_iso`
      in `new_value`. The `token_hash` is *not* preserved — the
      forensic role of the audit trail is to identify which
      integration owned the token, not to reconstruct the
      credential. Single bulk DELETE after the per-row audit is
      written so audit-row IDs precede the deletion in monotonic
      order (clean SIEM replay). New manual-trigger endpoint
      `POST /admin/api-tokens/purge` dispatches the Beat task
      synchronously (30s timeout) and returns
      `{deleted_revoked, deleted_expired, cutoff_iso}` —
      operator sees exactly what happened. `admin`+ gated. Settings
      UI gets a *Retention policy* card at the top of the API
      tokens page with the `purge_after_days` input + *Save policy*
      + *Purge now* (with a confirm dialog naming the irreversibility
      of hard-delete and pointing at DB backup as the recovery path).
      Status badge above the input shows the currently-active state
      ("Active — hard-delete after N days" / "Disabled — slice-1
      retain-forever behaviour"). Smoke-tested live: created token
      id=20, revoked it, backdated `revoked_at` to 5 days ago, set
      window to 1 day, fired manual purge → `{deleted_revoked: 16,
      deleted_expired: 0, cutoff_iso: ...}` (id=20 plus 15 other
      previously-revoked tokens accumulated during testing); row
      gone from `api_tokens`; audit row written with `name='Smoke
      purge test' reason='revoked' triggered_by=celery:api_token_purge`.
      All 16 deletions attributed to the single Beat tick — clean
      bulk-purge semantics.

### Audit + SIEM — residuals
- [x] **Sentinel via the Logs Ingestion API** *(2026-04-30)*. Microsoft
      announced 2026-08-31 sunset for the Azure Monitor Data Collector
      API used by the legacy `siem.format=sentinel` path. New
      `sentinel_log_ingestion` format value targets the replacement —
      Data Collection Endpoint (DCE) + Data Collection Rule (DCR) +
      named stream, signed with an AAD bearer token from a Service
      Principal granted **Monitoring Metrics Publisher** on the DCR.
      Migration `0090` seeds six new keys (`sentinel_dce_endpoint`,
      `sentinel_dcr_immutable_id`, `sentinel_stream_name` defaulting
      to `Custom-IpsolisAudit_CL`, `sentinel_tenant_id`,
      `sentinel_client_id`, `sentinel_client_secret` (is_secret));
      bumps the `siem.format` description to enumerate the new
      option. Auth path: stdlib OAuth POST to
      `login.microsoftonline.com/<tenant>/oauth2/v2.0/token` with
      `scope=https://monitor.azure.com/.default` — separate from the
      Azure KV / Entra ID SSO scopes so the streaming SPN's role
      assignment stays narrow. AAD bearer tokens cached
      process-locally with a 60-second safety margin (separate
      cache from the Azure KV / Conjur / Vault token caches —
      keyed by `(tenant, client)`). Body shape: same JSON-array of
      audit-row dicts as the legacy adapter, so existing dashboards
      built on `IpsolisAudit_CL` keep working after migration (the
      DCR's `transformKql` copies `timestamp` to the mandatory
      `TimeGenerated` column). Both the API single-event test
      sender (`api/app/utils/siem_export.py`) and the worker batch
      streamer (`worker/tasks/modules/siem_export.py` +
      `worker/tasks/workflows/siem_streamer.py`) carry the new
      format; `siem.sentinel_client_secret` flows through the
      existing secret-resolver so a `vault://…` reference for it
      dereferences before the AAD call. Test-connection probe
      surfaces underlying failures verbatim:
      `AAD auth failed: ... AADSTS700038: <id> is not a valid
      application identifier` for bad SPN, the JSON `error.code` /
      `error.message` shape for bad DCR / DCE / stream from the
      Logs Ingestion side. Settings UI gets a new dropdown option
      ("Microsoft Sentinel — Logs Ingestion API (DCE/DCR)") and a
      dedicated field group for DCE endpoint / DCR immutable id /
      stream name / tenant / client / client secret. Smoke-tested
      live: empty-config preflight error, partial-config AAD-side
      error, full-config real round-trip to
      `login.microsoftonline.com` returning `AADSTS700038` (proving
      OAuth POST + body shape are wire-correct), legacy `sentinel`
      format regression intact.
- [x] **Streaming-failure email alert via the existing health-alert path**
      *(2026-04-30)*. Added `siem` probe to `/admin/maintenance/health` —
      reads `siem.last_error` / `siem.last_success_at` from `app_config`
      and reports `{ok: false, detail: "streaming failure: …"}` when
      streaming has hit an error since the last successful batch. The
      existing `check_health_and_alert` Beat task picks up the new
      service automatically (no code change needed there — it iterates
      the response's services dict and emails on transitions). Probe
      returns `ok: None` ("disabled") when SIEM streaming is off so an
      unconfigured tenant doesn't generate false-positive alerts.

### Multi-instance HA slice 2
- [x] **Postgres standby setup docs** *(2026-04-30)*. New section
      `12.3 Postgres standby + failover` in `docs/DEPLOYMENT.md`
      covers streaming replication + pgBackRest as complementary
      tools (live read replica + DR backups), with explicit
      `postgresql.conf` / `pg_hba.conf` snippets, replication-slot
      creation, `pg_basebackup` bootstrap commands, pgBackRest
      stanza config + cron schedule, manual failover playbook
      (verify lag → stop writes → `pg_ctl promote` → flip
      `DATABASE_URL` → restart api/worker/beat replicas), realistic
      RPO/RTO targets, and a Patroni pointer for tenants who
      need < 1-minute RTO. Wraps with an explicit
      "**Verification before going live**" subsection that frames
      the docs as a *reference architecture* rather than a
      battle-tested playbook — the upgrade-time prose is honest
      that the failover drill hasn't been run end-to-end on this
      project's own stack yet, and lists the five concrete steps
      operators must drill on staging before committing to the HA
      story. Picks the same tone as the existing `## HA Beat
      scheduler` ENTERPRISE_FEATURES section (which *is* battle
      tested). The companion HA Beat scheduler from slice 1 stays
      cross-linked at the top of section 12.
- [x] **Multi-replica API docs** *(2026-04-30)*. New section
      `12.1 Multi-replica API` in `docs/DEPLOYMENT.md`. Verified
      from `api/app/main.py` that the session middleware is
      Starlette's cookie-signed `SessionMiddleware` (not
      Redis-backed) — the entire session payload lives in the
      `xp_session` cookie itself, signed with `API_SECRET_KEY`,
      so every replica handles every request equally without any
      sticky-session affinity at the LB. Docs name the four
      stateless contracts (cookie sessions, HMAC-signed approval /
      certification tokens, all request state in Postgres / Redis,
      no in-process caches that survive across requests) and list
      the three things every replica must share (`API_SECRET_KEY`,
      `DATABASE_URL` / `CELERY_*`, bind-mount paths). Includes
      `docker compose --scale api=3` scale-up command, LB config
      notes (round-robin OK, `/health` for health checks, TLS
      terminates upstream + `https_only=True` on the cookie), and
      a per-replica rolling-restart loop for zero-downtime
      upgrades on stacks with a draining LB.
- [x] **Multi-replica worker docs + sizing table** *(2026-04-30)*.
      New section `12.2 Multi-replica worker` in
      `docs/DEPLOYMENT.md`. Documents the four-queue topology
      (`provision`, `notifications`, `default`, `reclaim`) with a
      "why a separate queue" justification per queue (e.g.
      provision isolation keeps a 30s SCCM call from blocking the
      cost-alerter), and a three-tier sizing table (Lab → single
      worker; Mid → 2 workers split provision vs. housekeeping;
      Enterprise → dedicated provision replicas + dedicated
      notifications replica + housekeeping replica). Includes both
      the simple `--scale worker=3` shape (every replica consumes
      every queue) and the per-queue split via dedicated compose
      services (`worker-provision`, `worker-notifications`,
      `worker-housekeeping`) with example `docker-compose.prod.yml`
      snippet. Wraps with liveness notes (Celery mingle on
      startup, no separate health-check wiring) and a Flower
      pointer for queue-depth visibility.
- [x] **Beat-alive health probe** *(2026-04-30)*. `GET /health` now
      returns a `beat: "alive" | "stale"` field driven by the presence
      of the RedBeat distributed-lock key (`ipsolis:redbeat::lock`) in
      Redis. Aggregates into the top-level `status` so a load balancer
      checking the unauthenticated `/health` endpoint sees `degraded`
      when no Beat replica is dispatching. The same probe also feeds
      `/admin/maintenance/health` (`beat: {ok, detail}`), which the
      existing alerter Beat task at `*/5min` picks up automatically —
      operators get an email on the transition and on every cooldown
      window while it stays down. Lock TTL is 30s so a hard kill of
      the active Beat replica shows up as `stale` within ~30-60s, well
      inside a typical alert window. Verified live: stopping the Beat
      container flipped both `/health` and `/admin/maintenance/health`
      to the failure state; restart returned to `alive` within ~8s.

### Conditional approval rules slice 3
- [x] **Visual editor for deeply-nested compounds** *(2026-04-30)*.
      Slice 1+2 of the conditional-rules editor flattened the rule
      builder to a single-level `AND`/`OR` of leaves with a yellow
      "Edit via API to preserve nested structure" warning when the
      rule had deeper nesting — the engine has supported up to depth
      8 since slice 1, only the UI was shallow. Slice 3 ships the
      recursive editor: each compound renders its `AND`/`OR`/`NOT`
      op selector + a list of child clauses; each leaf renders
      field/op/value plus a `⊕` "wrap in group" affordance that
      replaces the leaf with `{op: 'and', clauses: [leaf]}` so the
      operator can promote a leaf to a sub-tree without retyping.
      `+ Add condition` appends a sibling leaf; `+ Add group`
      appends a sibling `AND` group (operator flips the op
      afterwards). Depth-coloured left borders (blue → amber →
      emerald → purple → rose) make the tree structure obvious at
      a glance; each compound's header carries its current depth
      and a `Remove group` button (root depth 0 doesn't get one —
      the rule card itself is the remove handle). NOT groups
      special-case: hide the Add buttons (engine accepts exactly
      one child), inline hint suggesting AND/OR for multi-clause
      negations, op-flip from AND/OR-with-many-children → NOT
      auto-truncates trailing children on save. Server template
      now emits a `<script type="application/json"
      id="approval-rules-data">` blob with the canonical condition
      JSON; on `DOMContentLoaded` the editor hydrates each card
      via `_renderApprovalRuleCard()` → `renderApprovalCondition()`.
      Save-time walk via `_readApprovalCondition()` returns the
      same JSON shape the engine evaluates. The render+read
      functions are pure on JSON — no listener bookkeeping
      survives mutations: every "+ Add" / "Remove" / op-flip
      click reads → mutates the in-memory tree → re-renders. No
      framework added (no Alpine, no htmx, no build step) — plain
      vanilla JS recursive component pattern fits cleanly into
      the existing HTMX + Tailwind stack. Single-leaf root still
      collapses to flat `{field, op, value}` on save (slice-1
      back-compat); multi-leaf or any nested group serialises as
      `{op, clauses}`. Empty / half-edited rows are dropped on
      save. Smoke-tested live: staged a 3-level nested rule via
      direct DB UPDATE — `(has_pii AND duration_days > 30) OR
      (has_pci AND NOT has_phi)` with quorum 2 of 3 reviewers;
      ran `evaluate_rules` against five synthetic order contexts;
      all five test cases pass — PII + long duration matches
      (left AND branch); PII + short duration no match; PCI
      without PHI matches (right AND with NOT); PCI + PHI no
      match (NOT blocks); no flags no match. Container template
      inspection confirms the new symbols are present (15×
      `renderApprovalCondition`, 8× `_readApprovalCondition`,
      3× `_renderApprovalRuleCard`, 3× `compound_op`, 15× `depth `
      label references).
- [x] **Per-bucket supersession (was: per-bucket reminder optimisation)**
      *(2026-04-30)*. Today (slice 2) when a single quorum bucket meets
      its threshold but other buckets are still pending, the surplus
      pending rows in the now-closed bucket stay `pending` and continue
      attracting reminders / escalations — operators in 2-of-5 rule
      groups got nagged after the first two votes had already closed
      their bucket, training them to ignore reminder emails. Slice 3
      auto-closes those rows the moment their bucket meets quorum,
      decision-time rather than Beat-tick-time. Refactored
      `apply_approval_decision()` in `api/app/utils/approval_decision.py`:
      extracted the inline bucket math into a `_compute_bucket_state()`
      helper returning a `_BucketState` with `bucket_of` (approval_id →
      bucket key), `members`, `thresholds`, `approved_counts`, `met`,
      and `all_met` aggregates. Both the existing whole-order
      supersession path and the new per-bucket path use the same state.
      New "deciding approval closes its own bucket" branch fires when
      `state.met[deciding_bucket]` is true but `state.all_met` is false:
      iterates the bucket's members, flips remaining pending rows to
      `superseded` with `decided_at` stamped, and writes one
      `order_approval / superseded_bucket_quorum_met` audit row per
      supersession capturing the bucket name, `approved/threshold`
      progress, and the deciding approver's email — distinguishable
      from the existing whole-order sweep (which writes a single
      `order / approved_and_dispatched` audit instead of per-row rows
      since the gate-clearance row already names who voted to release).
      Reminder Beat task needs no changes — its query already filters
      `WHERE status = 'pending'`, so superseded rows naturally drop out.
      Smoke-tested live with a 2-bucket order (1 manager in `global`,
      3 sec reviewers in `rule:HighRisk` with threshold 2): first sec
      vote → bucket 1/2, no supersession, all 3 sec rows still pending;
      second sec vote → bucket 2/2 met, `rule:HighRisk` member 23
      auto-flipped to `superseded` with the audit trail capturing
      `bucket=rule:HighRisk progress=2/2 decided_by=sec2@xenpool.de`,
      manager row stayed `pending` (different bucket, not yet met);
      manager vote → all_met → order dispatched (`status=processing`),
      "Approval recorded — order is being dispatched" rendered. The
      audit-trail picture: bucket-mate's "I'm closed early" event is
      attributable to the *deciding* approver, not the superseded
      approver — consistent with how the existing whole-order path
      attributes via the `approved_and_dispatched` row.
- [x] **Escalation v2 — assignment mode** *(2026-04-30)*. Today's
      slice-1 escalation flow notifies `approval.escalation_email`
      contacts via an email pointing at `/ui/orders` — the contact
      intervenes operationally but doesn't decide. The new
      `approval.escalation_assign` boolean (default `false` —
      back-compat) flips the worker into assignment mode: when true,
      the escalation Beat tick creates one new `OrderApproval` row
      per contact (`approver_type='escalation'`, `status='pending'`)
      and emails each one a tokenized `/approve/<token>` URL via the
      new `approval_escalation_assigned` email template. The contact
      can decide directly from their inbox without an admin login —
      same one-click flow the original approver had. Migration `0092`
      seeds the boolean + template. Worker-side wiring in
      `worker/tasks/workflows/approval_reminders.py` adds an
      `if escalation_assign` branch that uses the existing
      `tasks.modules.teams_notify.make_approval_token` helper to
      mint per-row tokens, with explicit de-dup against existing
      approver emails on the same order (an escalation contact who's
      already a rule-driven approver keeps their original row and
      doesn't get a duplicate). The original approval's
      `escalated_at` is stamped only after at least one new row is
      created — if every contact is already an approver or every
      send fails, `escalated_at` stays NULL so the next scan retries.
      Each escalation row is independent — five contacts → five
      rows → five token URLs. The order's existing approval rules
      determine "all granted" semantics (typically "any one
      escalation contact decides" dispatches the order). New
      `send_approval_escalation_assigned()` helper in
      `worker/tasks/modules/notifications.py` mirrors the existing
      `send_approval_escalated` shape but uses the new template and
      sends one email per recipient (with the per-row tokenized URL
      built into the body). Settings UI gets an *Escalation
      behaviour* dropdown (`Notify only` / `Reassign`) right under
      the existing *Escalation contact(s)* field. Smoke-tested live:
      seeded a synthetic at-cap pending approval, ran the Beat task
      in-process, observed two new `escalation` rows created with
      ids 18/19 (one per contact email), original manager row id 17
      stamped with `escalated_at`, GET `/approve/<token>` returned
      the standard confirmation page scoped to the test order, POST
      with `decision=approve` flipped row 18 to `approved` with the
      decision's `comment` recorded — full end-to-end. The other
      escalation row (19) stayed pending, confirming each
      escalation row's decision is independent. Decision audit
      attribution is the regular `api:approval_token (approver:<email>)`
      shape, distinguishable from manager/owner decisions via the
      `approver_type='escalation'` column.

### Per-classification approval routing
- [x] **Per-classification approval routing — defaults path**
      *(2026-04-30)*. Three new config keys
      (`approval.classification_policy.{pii,phi,pci}`, default `none`)
      plus an `approval.compliance_officer_email` /
      `_name` pair seeded by migration `0091`. New helper
      `app.utils.classification_routing.compliance_officer_approver()`
      inspects an asset type's attribute classifications and the
      loaded policy, returning the compliance-officer approver dict
      (or `None`) for the order-creation site to inject. Activation
      precedence is strictest-first (PCI > PHI > PII): an order
      touching multiple classes still gets one compliance step,
      attributed (via the audit row's existing `classification`
      column) to the strictest matching class. Plugged into
      `portal.py` order creation right after the conditional-rules
      evaluation, with explicit de-dup against the manager / app-owner
      / rule approver emails so a manager who's also the compliance
      officer doesn't get two approval requests. Status flips to
      `pending_approval` correctly when the auto-step is the only
      approval-needing trigger (existing static toggles off,
      no rules configured). New `compliance_officer` value for
      the `approver_type` column (`String(30)` so 18 chars fits).
      Settings UI adds a "Per-classification approval routing"
      card in the Compliance tab with three policy dropdowns +
      compliance-officer email/name inputs; client-side guard
      blocks save when any class is enabled but the email is
      empty (server side also skips silently — defense in depth,
      but the Settings-time guard is the operator-facing signal).
      Smoke-tested live with a real asset type stamped with
      various classifications: default policy → no auto-step;
      PII-only on with no email → no auto-step (skipped by the
      email guard); PII on with email set → returns the approver
      with `trigger_class='pii'`; PII+PHI both on → `'phi'` wins
      (strictest-first); PCI+PHI+PII all on → `'pci'` wins. Runs
      alongside the existing `has_pii / has_phi / has_pci`
      conditional-rules engine — operators can keep using rules
      for fine-grained logic ("PII *and* monthly_cost > €1000")
      while the defaults path covers the simple "any PII →
      compliance step" case in one click.
- [x] **Owner-of-record acknowledgement** *(2026-04-30)*. Extends the
      per-classification routing slice with a second policy mode
      alongside the existing `compliance_officer`: `owner_of_record`
      auto-routes the classification-driven step to the asset type's
      `approval_owners` list — one approval row per listed owner —
      instead of the centralised compliance officer. HIPAA's
      canonical use case ("the data steward who actually owns this
      PHI surface must sign off, not a generic compliance team")
      maps directly; same plumbing also applies to PII / PCI for
      tenants whose data-owner accountability sits per-system rather
      than per-team. No new migration, no new config key — same
      `approval.classification_policy.{pii,phi,pci}` keys gain a
      third valid value `owner_of_record`. Refactored
      `app/utils/classification_routing.py`: the single-dict helper
      `compliance_officer_approver()` is replaced by
      `classification_approvers()` returning a *list* of approver
      dicts (compliance_officer = 1 dict, owner_of_record = N dicts
      from the asset type's `approval_owners`, none = empty list).
      Each dict carries `policy` (`compliance_officer` /
      `owner_of_record`) + `trigger_class` (the strictest matching
      class) so the call site can distinguish the two paths. The
      portal order-creation site iterates the returned list, sets
      `approver_type` from `policy` (`compliance_officer` /
      `owner_of_record` — both fit the `String(30)` column), and
      de-dups against the existing `seen_emails` set so an
      owner-of-record contact already covered by the static
      *Requires application-owner approval* flag (which uses
      `approver_type='application_owner'`) keeps just one row, not
      two. Strictest-first precedence preserved: when the asset type
      carries both PCI and PHI fields and both classes have non-none
      policies, the PCI policy wins (only *one* policy fires per
      order). The "skip on `none`" loop logic uses `continue` so
      "PCI=none + PHI=owner_of_record" still picks PHI. Settings UI
      dropdown gains the third option (*Owner of record (asset's
      approval owners)*) on all three class dropdowns; the
      compliance-officer-email Settings-time validation only fires
      when *Compliance officer* mode is selected (owner_of_record
      doesn't need the global email). The order-creation path skips
      silently + logs INFO when an owner_of_record policy fires but
      the asset type's `approval_owners` is empty — defense in
      depth, since the per-asset-type validation can't reasonably
      live in the global policy save handler. Smoke-tested live
      across 6 helper scenarios on a real asset type stamped with
      various classifications + owners: default policy returns `[]`;
      PHI=compliance_officer + no email returns `[]` + INFO log;
      PHI=compliance_officer + email returns 1 dict trigger=phi
      policy=compliance_officer; PHI=owner_of_record returns 2
      dicts (one per `approval_owners` entry, with their
      individual emails / display names);
      PCI=compliance_officer + PHI=owner_of_record on a type with
      both classes returns 1 PCI compliance_officer step
      (strictest-first wins — proves the
      `continue`-past-`none` logic in the loop). PCI=owner_of_record
      with no `approval_owners` returns `[]` + INFO log.

---

## Strategic — Enterprise-class roadmap

These are the gaps that block ipSolis from being drop-in for a 5,000-seat regulated
enterprise. Order = priority (procurement-blocker first).

### [done] Admin RBAC — Prio 0 (show-stopper)
Slice 1 — per-user accounts, role ladder, first-run setup, role-gated
admin user CRUD — **shipped 2026-04-26**. Comprehensive role-gating
across the rest of `/admin/*`, per-asset-type ACLs, and SoD
enforcement (configurer ≠ approver) split into follow-up slices.

**Done — RBAC slice 1 (2026-04-26):**
- Migration `0069_admin_users_rbac.py` adds `admin_users` (id, username
  unique, password_hash, role, is_active, created_at, updated_at,
  last_login_at, created_by). Username is normalised to lowercase at
  write time so the unique index doesn't need funcidx-LOWER.
- Five-tier role enum in `app.utils.rbac.ROLE_HIERARCHY`:
  `superadmin > admin > approver > auditor > helpdesk`.
  `role_at_least(actual, required)` is the single source of truth for
  privilege comparisons — every other role check delegates to it.
- Password hashing in `app.utils.password` (PBKDF2-HMAC-SHA256, 600k
  iterations per OWASP 2023, stdlib only — no bcrypt/passlib build
  dependency). Self-describing string format
  `pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>` so a future argon2id
  migration is a verify-then-rehash on next login.
- Login flow rewritten in `routes/admin_auth.py`:
  * Form takes username + password.
  * Empty username + password matching `settings.ADMIN_API_KEY` falls
    through as the **legacy back-compat path**: virtual superadmin
    session, attributed as `admin:legacy_key`. Existing scripts and
    bookmarked admin sessions don't break on upgrade.
  * Username + password matched against `admin_users` (active rows
    only). On success, `last_login_at` is updated and the session
    carries `admin_user`, `admin_role`, `admin_via=user`.
  * **First-run setup**: when `admin_users` is empty, the login page
    renders a "Create first administrator" form instead of the
    sign-in form. Submitting it creates the first superadmin and
    auto-logs them in. Idempotent against races (re-checks the count
    on the setup POST).
- `require_role(required)` dependency factory in `app.utils.rbac`:
  reads `request.session["admin_role"]`, gates by the ladder, raises
  HTTP 403 with a descriptive message naming both the actor's role
  and the required role. Bypass paths: legacy key (virtual
  superadmin) and bearer tokens (governed by scopes, not roles).
- Audit attribution updated: `actor_by()` now reads
  `admin_role` from the session and emits
  `admin:session:<user>:<role>` (e.g. `admin:session:alice:superadmin`)
  so audit-log filters can match on both *who* and *with what
  authority*.
- Admin user CRUD route + Pydantic schemas in
  `routes/admin_users.py` (gated to `superadmin`):
  list / create / update (role + activation + password rotation) /
  hard-delete. Self-protection guards: a superadmin cannot demote /
  deactivate / delete *themselves*, and the last active superadmin
  is never the last (any of those operations on the last
  superadmin fail with 409). Soft revocation (`is_active=false`)
  preserves the audit trail; hard delete is for test rows.
- Admin UI page at `/ui/admin-users` (linked in nav, superadmin-only
  — non-superadmins see a "Only superadmins can view this" empty
  state when the underlying API returns 403). Inline role dropdown,
  reactivate/deactivate, password reset, delete. New-user modal with
  role selector defaulting to `admin`.
- Nav hiding in `base.html`: Audit Log nav entry hides for roles
  below auditor; Admin Users nav entry hides for non-superadmins.
  Sidebar footer shows the signed-in user + role badge.
- Three role gates applied as proof-of-wiring (broader rollout is
  slice 2): `POST/PUT/DELETE /admin/asset-types*` → `admin`+,
  `GET /admin/audit-log` → `auditor`+,
  `/admin/admin-users*` → `superadmin`.
- Smoke-tested end-to-end:
  * First-run setup created `alice` (superadmin) with a 118-char
    PBKDF2 hash. Login page now renders the regular sign-in form
    instead of the setup form.
  * Login as alice → 303 → `/admin/admin-users` returns the user list.
    Wrong password → 401 with descriptive error.
  * `bob` (admin role) created via API; bob can hit asset-type endpoints
    (passes role gate) but is 403'd from `/admin/admin-users`
    (descriptive message names the gap).
  * `carol` (auditor) created; her asset-type create attempt returns
    403 with `Role 'auditor' is below the required 'admin'`.
  * Legacy `X-Admin-Key` continues to grant unrestricted access (returned
    full admin-user list), proving back-compat.
  * Self-protection guards: alice (sole superadmin) blocked from
    self-demote (409), self-deactivate (409), self-delete (409).
    Promoting bob to superadmin doesn't unlock alice's self-demote
    (deliberate — avoids accidental session lockout).
  * Audit row attribution: `api:create_admin_user (admin:session:alice:superadmin)`.

**Done — RBAC slice 2 (2026-04-26):**
- Comprehensive role gating applied across the rest of `/admin/*`:
  * `admin` minimum on the operational routers — `admin_modules`,
    `admin_runbooks`, `admin_standalone_runbooks`,
    `admin_maintenance`, `admin_approval_delegations`. Plus per-route
    `admin` gates on the previously-unguarded writes in `admin.py`:
    POST/PUT/DELETE `/config`, the `*/test` endpoints (AD, Entra,
    SIEM, Teams, SCCM, Email), assets CRUD + bulk + force-delete +
    revoke, email-template PUT.
  * `superadmin` minimum on the infrastructure routers —
    `admin_license`, `admin_seed_export`, `admin_setup`,
    `admin_api_tokens`. License upload, seed-to-disk export,
    integration provisioning, and integration token issuance are
    all authority-level changes that don't belong to operational
    admins.
  * `auditor` minimum on `admin_cost_report` — read-only chargeback
    breakdown for finance / audit consumers.
  * Audit-log GET stays at `auditor` (slice-1) and asset-type CRUD
    stays at `admin` (slice-1). No regressions.
- Per-asset-type ACL grants — the headline feature:
  * Migration `0070_admin_user_asset_type_grants.py` adds a junction
    table with a composite PK `(admin_user_id, asset_type_id)` and
    cascade-on-delete on both FKs so dropping a user or asset type
    cleans the grant set automatically. Reverse-lookup index on
    `asset_type_id` for fast "which users see this type" queries.
  * ORM `AdminUserAssetTypeGrant` registered in `app.models`.
  * Visibility helper `app.utils.rbac_grants.visible_asset_type_ids`
    returns `None` (= unrestricted) for the bypass set
    (superadmin / approver / auditor / helpdesk / legacy key /
    bearer tokens) and `set[int]` of allowed ids for scoped admins.
    Zero grants → `None` (back-compat — single-team installs see
    everything by default). At least one grant → flips into scoped
    mode and only the granted set is returned.
  * `assert_asset_type_visible(request, db, type_id)` raises 404
    (not 403) for out-of-scope ids. 404 prevents leaking the
    existence of asset types the user has no business knowing
    about — a scoped admin asking for an unrelated team's type
    gets the same response as for a missing id.
  * Wired into the admin UI asset-types list page (filters the
    catalog) and the four asset-type CRUD writes in `admin.py`
    (PUT, clone POST, DELETE — create POST is the auto-grant path).
  * Auto-grant on create: when a scoped admin creates a new asset
    type, the grant is added inside the same transaction so they
    don't lose visibility on their own creation. Superadmins,
    ungranted admins, and integrations are not affected.
  * Grant CRUD on `admin_users.py` (superadmin only): GET/PUT
    `/admin/admin-users/{user_id}/grants`. The PUT replaces the
    full set (idempotent — same set twice is a no-op besides
    audit), validates every supplied id against `asset_types` so a
    typo can't create a dangling grant, and computes diffs for the
    audit row (`{"asset_type_ids": [...]}` old/new).
  * Admin UI: the admin-users page gains an "Asset-type scope"
    column with an inline summary (`unscoped` or `N granted`) and
    an "Edit grants…" button that opens a modal listing every
    asset type as a checkbox. Lazy-loaded summaries so the page
    render isn't blocked on N grant queries.
- Verified end-to-end:
  * `dave` (auditor) → `/admin/maintenance/backups` returns 403
    with the descriptive role-mismatch message; `/admin/api-tokens`
    returns 403 (superadmin required); `/admin/cost-report` returns
    200 with the projected-cost breakdown — three different role
    tiers each producing the right outcome.
  * `eve` (admin, ungranted) sees both `Personal VDI Host` and
    `Shared Remote Desktop` on `/ui/asset-types` (back-compat).
  * `alice` (superadmin) PUTs grants `[16]` for eve →
    `grants_updated` audit row written with diff old/new.
  * Eve (now scoped) PUT type 16 (in-scope) → 200; PUT type 17
    (out-of-scope) → `404 Asset type 17 not found` — same response
    shape as a genuinely missing id.
  * Clearing eve's grants flips her back to unscoped — PUT type 17
    succeeds again.
  * Audit attribution on grant changes:
    `api:set_admin_user_grants (admin:session:alice:superadmin)`.

**Done — RBAC slice 3 (2026-04-26):**
- Self-service password change: new `/admin/me` router carrying
  `GET /admin/me` (whoami snapshot) and
  `POST /admin/me/password` (rotate). Verifies current password as
  a liveness check, enforces ≥12-char new password, rejects no-op
  rotations (new == current). Legacy `ADMIN_API_KEY` and bearer-
  token actors get descriptive 409s pointing to the right rotation
  path. Audit row `password_changed_self` with `{"by_self": true}`
  — no plaintext leakage. New `/ui/my-account` page with identity
  card + change-password form, linked in the nav for any logged-in
  admin. Disables itself when the legacy key is the actor.
- Bearer-token role binding:
  * Migration `0071_api_token_role.py` adds an optional
    `api_tokens.role` column (NULL = pre-slice-3 scope-only authz).
  * `create_token()` accepts a ``role`` kwarg; `TokenCreate` schema
    grew a matching field; `TokenRow` exposes it.
  * `require_role()` rewritten for the token branch: NULL token role
    → bypass (back-compat); set role → standard ladder check via
    `role_at_least`. Error message names both the token's role and
    the route's required role.
  * Mint guard: the creator can only issue tokens at or below their
    own role. Validates against `_creator_role(request)` which
    returns the session role / token role / virtual `superadmin`
    for the legacy key. A non-superadmin attempting to mint a
    superadmin token gets 403 with a descriptive message; the
    router-level `superadmin` gate from slice 2 means today only
    superadmins reach the endpoint anyway, so the guard is
    defense-in-depth that activates once slice 4 relaxes the
    router gate to `admin`.
  * API tokens UI: new "Role" dropdown on the create modal
    (defaults to "no role — scope-only authz, back-compat"); new
    "Role" column on the list with colour-coded badges
    (amber for `superadmin`, blue for the rest, italic "unbound"
    for NULL).
- Separation-of-duties (SoD) enforcement:
  * New `app.utils.sod.is_configurer_of_asset_type(db, type_id, email)`
    walks `audit_log` for `entity_type='asset_type', entity_id=N`
    and matches the actor's `triggered_by` against the approver's
    email, email-local-part, or admin username. Returns
    `(matched, audit_excerpt)` so the SoD-block error can quote
    the original config attribution back at the operator.
  * `apply_approval_decision()` runs the check before mutating the
    approval row, only on `approve` (decline always allowed since
    "I can't reject my own work" doesn't apply). Raises
    `SoDViolation` exception which the route layer translates to
    HTTP 409 with a descriptive message. The approval row stays
    `pending` so a different approver can decide.
  * Wired into both decision paths: the portal route
    (`POST /portal/approvals/{id}/decide`) and the signed-token
    external route (`POST /approve/{token}`). The token path
    renders an HTML status page using the existing `_render_status_page`
    helper; the portal raises HTTPException so the same UI banner
    shape carries the message.
- Verified end-to-end:
  * Password change: alice rotates `verysecurepw1 → newsecurepw2`
    (HTTP 204), old password rejected with 401, new password
    accepted with 303, alice rotates back. Whoami returns
    `{"username":"alice","role":"superadmin","via":"user"}`.
  * Token role: `slice3-auditor` token (role=auditor + admin:*
    scope) → 200 on `GET /admin/audit-log` (auditor satisfies the
    auditor role gate), 403 on `POST /admin/asset-types`
    (`Token role 'auditor' is below the required 'admin'`).
    `slice3-admin` token attempt to mint a superadmin token → 403
    (router-level superadmin gate; mint guard would also have
    caught it).
  * SoD: alice updates asset_type 16 → audit row has
    `admin:session:alice:superadmin`. Direct invocation of
    `apply_approval_decision()` with an approval row having
    `approver_email=alice@xenpool.local` against an order on
    type 16 → `SoDViolation(approver_email='alice@xenpool.local',
    asset_type_id=16, audit_excerpt='api:update_asset_type
    (admin:session:alice:superadmin)')`. Decline path on the same
    setup returns `DecisionResult(status='declined')` cleanly —
    SoD doesn't fire on rejections.
  * Helper unit-checks: matches `alice@xenpool.local`, matches `alice`
    (no @), correctly rejects `bob@example.com` and `ciso@example.com`.

**Slice-4 enrichments → tracked in *Deferred Enterprise Backlog* (top of file).**

### [done] External secret management — Prio 0 (show-stopper)
Slice 1 — Vault + CyberArk CCP/AIM, on-read resolution, no plaintext
removal — **shipped 2026-04-26**. Slice 2 enrichment shipped Azure
Key Vault, AWS Secrets Manager, and CyberArk Conjur (2026-04-30) —
five backends total. Vault AppRole/JWT auth, AWS native AssumeRole,
CCP mTLS bootstrap UX, and the one-shot migration tool stay queued
(see *Deferred Enterprise Backlog* at top of file).

**Done — secrets slice 1 (2026-04-26):**
- Migration `0072_seed_secret_backend_config.py` seeds 11 keys for the
  backend selector + cache TTL + per-backend creds + diagnostic
  surface (`secret.last_test_at`, `secret.last_test_error`).
- Reference grammar: `vault://<path>[#<field>]` (KV v2, default field
  `value`) and `ccp://[<safe>/]<object>` (CyberArk CCP returns the
  `Content` field of the account). Plain strings pass through unchanged
  — partial migrations are fine since the existing DB-plaintext path
  is the default.
- Resolver in `app.utils.secrets`:
  * `resolve_secret_value(db, raw)` (async) and
    `resolve_secret_value_sync(raw)` (sync, psycopg2-backed) sharing
    the dispatch core. Process-local TTL cache, default 60s,
    keyed by `(backend, reference)`. Cache TTL configurable via
    `secret.cache_ttl_seconds`.
  * Backends use stdlib only (`urllib`, `ssl`, `hmac`, `hashlib`).
    No `hvac`/`requests`/Azure SDK pulled into the runtime image.
  * Vault: static-token auth (X-Vault-Token), optional Enterprise
    namespace, configurable KV mount (default `secret`). KV v2
    envelope unwrapped to `data.data.<field>`.
  * CCP: GETs `/api/Accounts?AppID=…&Safe=…&Object=…`. Optional
    mTLS — when `secret.ccp.client_cert_pem` is set, the cert+key
    PEM is materialised to a 0600 temp file just for the duration
    of the request. CCP installs that authorise by AppID + IP
    allow-list leave the field empty.
  * Failures (network / auth / missing path) log at WARNING and
    return empty string — fail-closed-quiet so a Vault outage
    doesn't crash unrelated requests; the calling integration's
    auth-failure error is the user-visible signal.
- Worker mirror at `worker/tasks/modules/secrets.py` (sync only,
  same boundary as `audit_helper.py` — no api-package import).
  `get_secret_config(db, key, default)` is the worker convenience
  that wraps `get_config` + resolution.
- Wired into the high-value credential consumers:
  * `ad_lookup._get_ad_config` resolves `ad.password`.
  * `entra.get_msal_app` resolves `entra.client_secret`.
  * `admin.test_ad_connection` and `admin.test_email` resolve their
    passwords before binding / SMTP-AUTH.
  * Worker `dynamic_runner` resolves `vsphere.password` and
    `xenserver.password` for both the `_global_vars` injection
    and the per-step `config.*.password` template substitutions.
- Test endpoint `POST /admin/config/secret-backend/test` clears the
  process cache, hits the right probe (`/v1/sys/health` for Vault,
  `/api/Verify` for CCP), and stamps `secret.last_test_at` on
  success or `secret.last_test_error` on failure for the Settings
  UI to render.
- `_mask()` exception: reference-shaped values
  (`vault://…`, `ccp://…`) stay in clear when displayed via
  `GET /admin/config/...`. Knowing the path doesn't grant access,
  and admins need to see which store entry each row points to.
  Genuine secrets (`secret.vault.token`, `secret.ccp.client_cert_pem`)
  are still masked as `***`.
- Settings UI: new "External Secret Backend" card on the Compliance
  tab — backend dropdown + per-backend field group with show/hide
  toggle, "Save" + "Test connection" buttons, "Last verified"
  timestamp, last-error inline. Vault group has URL / token (secret) /
  KV mount / namespace; CCP group has URL / AppID / default Safe /
  client cert PEM (secret) / verify-TLS toggle.
- README updated; `docs/ENTERPRISE_FEATURES.md` doc deferred (the
  README line is comprehensive; a dedicated section is slice-2 polish).
- Verified end-to-end with stdlib stubs:
  * Vault stub: `/v1/sys/health` 200 → backend test reports
    "Vault reachable". Resolver returns `S3cretFromVault` for
    `vault://ipsolis/ad/password`, empty + WARNING for missing
    paths. Worker-side resolver returns the same value through
    `get_secret_config`.
  * CCP stub: `/api/Verify` 200 → backend test reports "CCP
    reachable". `ccp://vsphere-svc` resolves to its Content;
    `ccp://OperationsSafe/sccm-svc` (explicit Safe) resolves
    correctly; missing object returns empty + WARNING.
  * Setting `ad.password` to `vault://ipsolis/ad/password` and
    re-reading via `GET /admin/config/ad.password` returns the
    reference in clear (not `***`) — masking exception works.

**Slice-2 enrichments are complete — Conjur, AWS SM, Azure KV, residual key coverage, the one-shot migration tool, Vault AppRole/JWT auth, AWS native AssumeRole, and the CCP mTLS file-upload bootstrap UX have all shipped. No further open items in this backlog section.**

### [done] API tokens with scopes — Prio 0
Slice 1 — table + ORM + bearer auth + Admin UI — **shipped 2026-04-26**.
Scope decorators and the ServiceNow webhook migration are split into
follow-up slices.

**Done — token core (2026-04-26):**
- Migration `0054_api_tokens.py` — `api_tokens` table with SHA-256 hash,
  prefix, JSON scopes, expiry, last-used, soft-delete revocation.
- ORM `app.models.api_token.ApiToken`.
- `app.utils.api_tokens` — `generate_raw_token()`, `create_token()`,
  `verify_raw_token()`, `mark_used()`, `status()`. Tokens are
  `secrets.token_urlsafe(32)` with a recognisable `xpat_` prefix; raw
  value never persisted.
- `app.utils.auth.require_admin_key` extended to accept
  `Authorization: Bearer xpat_…` alongside the legacy `X-Admin-Key`
  and admin session cookie. Stores attribution as
  `request.state.actor` = `token:<name>` / `admin:legacy_key` /
  `admin:session:<user>` so future audit entries can record who
  did what.
- API endpoints `POST /admin/api-tokens` (one-time raw reveal),
  `GET /admin/api-tokens` (list, prefix only), `DELETE /admin/api-tokens/{id}`
  (soft-delete sets `revoked_at`).
- Admin UI page `/ui/api-tokens` with list, create modal (name +
  expiry: 30/90/180/365/730/never), one-time reveal banner with copy
  button, and per-row revoke. Linked from the left nav above License.
- README + `docs/ENTERPRISE_FEATURES.md` updated with auth-paths
  section and the token lifecycle UX.
- Verified end-to-end: create returns plaintext once, list shows
  prefix only, bearer authenticates against admin endpoints,
  bogus tokens return 401, `last_used_at` updates after use,
  revocation returns 204 + immediately blocks further use, row
  preserved with `revoked` status.

**Done — scope catalog + decorators (2026-04-26):**
- 14-scope catalog in `app.utils.api_tokens.AVAILABLE_SCOPES` covering
  orders / asset_types / assets / approvals / audit / config / metrics /
  webhook plus the `admin:*` wildcard.
- `require_scopes(*needed)` factory in `app.utils.auth` — back-compat
  by design: legacy `X-Admin-Key` and admin sessions retain implicit
  `admin:*`, only bearer tokens are scope-gated. Missing scopes return
  HTTP 403 with a message listing both missing and granted scopes so
  integrations can self-diagnose.
- Token create endpoint accepts `scopes: list[str]`; unknown scopes
  filtered silently; empty list defaults to `["admin:*"]` for
  back-compat with slice-1 token UX.
- New `GET /admin/api-tokens/scopes` endpoint exposes the catalog so
  the UI renders checkboxes dynamically.
- Token list response includes `scopes`; UI renders them as badges
  (amber for `admin:*`, neutral for narrow scopes).
- Token create modal: scope picker with checkbox grid, defaults to
  `admin:*` selected, validates at-least-one before submitting.
- Representative endpoints scoped to demonstrate wiring:
  `GET /admin/audit-log` → `audit:read`,
  4× `POST/PUT/DELETE /admin/asset-types/*` → `asset_types:write`,
  `GET /admin/cost-report` → `orders:read`.
- Verified end-to-end: read-only token gets 200 on audit, 403 on
  asset-type create + cost-report (descriptive error). Legacy
  `X-Admin-Key` still grants full access.

**Done — ServiceNow webhook bearer auth (2026-04-26):**
- `POST /webhook/servicenow` accepts either `Authorization: Bearer xpat_…`
  (with `webhook:in` scope, checked first) or the legacy
  `X-Hub-Signature-256` HMAC. Either is sufficient; both paths are
  independent and the legacy one is preserved for back-compat.
- Bearer-path validation: token must exist, not be revoked / expired,
  and carry the `webhook:in` scope. Mismatches return 401 (bad token)
  or 403 (wrong scope) with descriptive bodies that name the token
  and list its granted scopes.
- Audit attribution: `triggered_by` records
  `api:servicenow_webhook (webhook:token:<name>)` for the bearer path
  and `api:servicenow_webhook (webhook:hmac)` for the legacy path —
  so revocation events in the audit log can be tied to specific
  integrations.
- `last_used_at` on the api_token row updates on every successful
  webhook delivery (same path as admin endpoints).
- Verified end-to-end: no auth / bogus bearer / wrong-scope / valid-bearer
  / valid-HMAC all return correct status codes and create or reject
  orders consistently. Audit log shows correct attribution per path.

**Done — Audit log viewer UI (2026-04-26):**
- New `/ui/audit-log` page rendering the existing `/admin/audit-log`
  JSON endpoint. Filter bar with entity-type dropdown, entity ID,
  triggered-by substring search, and from/until timestamp.
- Coloured actor badges: blue for `token:*`, green for
  `admin:session:*`, amber for `admin:legacy_key`, purple for
  `webhook:*`. Makes it instantly obvious whether a change was
  driven by an integration, an admin in the UI, or a fallback path.
- Expandable rows: each entry shows a one-line summary; expanding
  reveals the JSON `before` / `after` diff for the change.
- Pagination (50 per page, "Newer" / "Older" buttons) plus a
  "Reset" filter button.
- Nav entry between API Tokens and License.
- Verified: filtering for `triggered_by=token:` and
  `triggered_by=legacy_key` returns the expected subsets from the
  ~800 audit rows currently in the dev DB.

**Done — audit attribution everywhere (2026-04-26):**
- New `actor_by(request, label)` helper in `app.utils.audit` builds the
  `triggered_by` string from `request.state.actor` (set by
  `require_admin_key` / `_authenticate_webhook`). Falls back to plain
  `api:<label>` when no actor is on state, so the helper is safe to
  use on unauthenticated routes too.
- Updated all 12 mutating admin route call sites in `admin.py` to
  thread `request: Request` and use the helper:
  `create/update/delete config`, `create/update/clone/delete asset_type`,
  `create/update/delete asset`, `force_delete_asset`, `revoke_asset`.
- Webhook path already used an equivalent format; left as-is.
- Verified end-to-end: token-driven `PUT /admin/config/...` produced
  `api:update_config (token:audit-attrib-test)`; legacy-key write to
  the same endpoint produced `api:update_config (admin:legacy_key)`.
  An auditor can now trace every change back to the specific
  credential (token name, admin session user, or legacy key).

**Done — wider scope rollout (2026-04-26):**
- Scope decorators now cover the rest of `/admin/*`'s `app_config`,
  asset, and email-template surface in `routes/admin.py`:
  `config:read` on GET `/config`, GET `/config/{key}`,
  GET `/config/siem/status`, GET `/email-templates`,
  GET `/email-templates/{event_key}`; `config:write` on
  POST/PUT/DELETE `/config`, PUT `/email-templates/{event_key}`;
  `assets:read` on GET `/assets`; `assets:write` on POST `/assets`,
  POST `/assets/bulk`, PUT `/assets/{id}`, DELETE `/assets/{id}`,
  POST `/assets/{id}/force-delete`, POST `/assets/{id}/revoke`.
- Operational test endpoints (`/config/ad/test`, `/config/entra/test`,
  `/config/teams/test`, `/config/email/test`, `/config/sccm/test`,
  `/config/siem/test`) are intentionally left scope-free — they are
  diagnostic actions only meaningful from the admin UI session, never
  driven by an integration token.
- `admin_approval_delegations.py` already carried `approvals:read` /
  `approvals:write`; verified no further changes needed there.
- Smoke-tested with two narrowly-scoped tokens: `config:read`-only
  → 200 on GET `/config` + GET `/config/siem/status`, 403 on POST
  `/config` and GET `/assets`; `assets:read`-only → 200 on GET
  `/assets`, 403 on POST `/assets` and GET `/config`. Error bodies
  name the token, missing scope, and granted scopes as designed.
- Legacy `X-Admin-Key` and admin sessions retain implicit `admin:*`
  by design — UI flows and existing scripts continue working.

**Done — audit attribution on /orders + portal flows (2026-04-26):**
- New `portal_actor_by(current_user, label)` helper in
  `app.utils.audit` mirrors the admin-side `actor_by(request, label)`
  contract. Output formats:
  * Authenticated portal user: `api:<label> (portal:user:<email>)`
  * Anonymous portal mode (Entra disabled): `api:<label> (portal:anonymous)`
  * Empty / missing email: `api:<label> (portal:user:unknown)`
  * No `current_user` dict at all: `api:<label>` (clean fallback)
  Email lower-cased so audit-log filters can match without case juggling.
- Portal mutation routes audit rows now record who drove the change.
  Three previously-silent mutations now emit audit rows:
  * `POST /portal/orders/new` → `order` `created`
  * `POST /portal/orders/{id}/change` → `order` `created` (with
    `ctx="modify_of:<orig_id>"`)
  * `POST /portal/orders/{id}/cancel` → both branches: scheduled
    cancellation logs `order` `status_changed` on the original;
    active cancellation logs the new DELETE order's `created` plus
    the original's `status_changed`. Two rows, same actor.
  All four routes pull classification via `classify_for_asset_type_id()`
  so per-class retention windows apply uniformly across portal +
  admin paths.
- `apply_approval_decision()` reworked to emit per-decision audit rows.
  Each individual approve / decline becomes an `order_approval`
  audit row (with `rule_name` and `comment` in the snapshot) so the
  trail captures each voter even before quorum is met. The order's
  status transition (`status_changed` on decline,
  `approved_and_dispatched` on quorum-met) gets its own row using the
  same actor. New `actor=` kwarg on the helper; portal route passes
  `portal_actor_by(current_user, "decide_approval")`,
  signed-token route passes `api:approval_token (approver:<email>)`.
  Default fallback preserves back-compat for any in-flight callers.
- `/orders/` API router got a non-raising soft-auth dependency
  `attribute_actor_if_present()` that mirrors `require_admin_key`'s
  three-credential recognition (legacy key / session / bearer token)
  but never raises on missing or invalid creds — keeps the public
  ServiceNow contract unchanged. Three audit call sites switched
  from hardcoded `api:create_order` etc. to `actor_by(request, ...)`:
  POST `/orders/`, PATCH `/orders/{id}`, DELETE `/orders/{id}`.
- `portal_delegations.py` aligned: `aaudit(by=...)` for create + revoke
  switched from the ad-hoc `f"portal:{email}"` to `portal_actor_by()`
  so portal-driven delegation rows now consistent with the rest of
  the audit log.
- Verified end-to-end:
  * Anonymous `POST /orders/` → `api:create_order` (no actor —
    fallback is unchanged).
  * `POST /orders/` with `Authorization: Bearer xpat_…` → audit row
    `api:create_order (token:smoke-orders-actor-2)`. Soft-auth path
    correctly captured the token without 401-ing on missing scopes.
  * `portal_actor_by()` produces the right strings for all five
    cases (real user / anonymous / no email / None / mixed-case).

**Hard-delete-vs-soft-delete policy → tracked in *Deferred Enterprise Backlog* (top of file).**

### [done] Tamper-evident audit + SIEM export — Prio 0
SIEM streaming side **shipped 2026-04-26** (Splunk HEC + Microsoft
Sentinel adapters). Tamper-evident DB-grant revocation on `audit_log`
is split into a separate slice because it touches role grants on a
live table and is best paired with the RBAC work.

**Done — SIEM streaming (2026-04-26):**
- Worker module `worker/tasks/modules/siem_export.py` — Splunk HEC
  payload builder + POST sender, stdlib `urllib`, no external deps,
  graceful TLS-verify toggle for self-signed labs.
- Beat task `worker/tasks/workflows/siem_streamer.py` — runs every
  minute, fetches `audit_log WHERE id > :last LIMIT :batch_size`,
  POSTs in HEC format, advances `siem.last_id` only on 2xx.
- Cursor + observability state stored in `app_config`:
  `siem.last_id`, `siem.last_error`, `siem.last_success_at`.
- Migration `0053_seed_siem_config.py` seeds 9 `siem.*` keys.
- API endpoints `POST /admin/config/siem/test` + `GET /admin/config/siem/status`.
- Admin UI: new **Compliance** tab in Settings with mode, endpoint,
  HEC token, batch size, TLS verify, Save / Send Test / Refresh
  Status buttons, plus a live status panel showing cursor / backlog /
  last error / last success.
- README + `docs/ENTERPRISE_FEATURES.md` updated with Splunk HEC
  setup walkthrough.
- Verified end-to-end: connection-refused returns graceful failure,
  cursor doesn't advance on failure, status surface reflects errors,
  payload preview matches HEC's expected newline-delimited JSON
  format with `event` / `sourcetype` / `host` / `time` envelope.

**Done — Tamper-evident audit_log (2026-04-26):**
- Migration `0062_audit_log_append_only.py` installs three
  BEFORE-statement triggers on `audit_log` (DELETE / UPDATE /
  TRUNCATE) that raise an exception unless the transaction sets
  `ipsolis.allow_audit_mutation = 'true'` via `SET LOCAL`.
- Default-deny posture: nobody — including an operator with full
  DB credentials — can quietly mutate audit history. Errors are
  loud and self-documenting (the message names the bypass GUC).
- Documented escape hatch for retention pruning so future
  classification-driven retention work can implement it cleanly.
- Triggers are FOR EACH STATEMENT (single fire per statement
  regardless of row count), implemented via a shared
  `audit_log_no_mutate()` plpgsql function.
- Verified end-to-end: INSERT works, DELETE/UPDATE/TRUNCATE all
  blocked with descriptive errors, bypass via `SET LOCAL` works
  within a single transaction, default-deny returns immediately
  after the bypass commit, and the app-level audit-write flow
  (config PUT → new audit row) is unaffected.

**Done — Microsoft Sentinel adapter (2026-04-26):**
- New `build_sentinel_payload()` + `post_sentinel()` in
  `worker/tasks/modules/siem_export.py`, mirrored on the API side in
  `api/app/utils/siem_export.py`. Uses Azure Monitor's HTTP Data
  Collector API (workspace_id + base64 shared key, HMAC-SHA256 signed
  per request — stdlib `hmac`/`hashlib`, no Azure SDK dependency).
  `validate=True` on the base64 decode so a pasted-with-typos shared
  key fails with a descriptive error instead of producing a wrong
  signature that Sentinel rejects with an opaque 403.
- `Log-Type` header drives the custom table name — Sentinel
  materialises ingest into `{log_type}_CL` (default `IpsolisAudit_CL`)
  on first event; no schema registration needed. The
  `time-generated-field: timestamp` header tells Sentinel to use our
  audit-log timestamp as the row time, not ingest time.
- Migration `0065_seed_sentinel_siem_config.py` seeds three new
  `app_config` keys: `siem.workspace_id` (plain), `siem.shared_key`
  (secret), `siem.log_type` (default `IpsolisAudit`). Existing siem.*
  values are not touched. `siem.format` description updated to list
  both adapters.
- Streamer Beat task picks up the new branch on `siem.format == 'sentinel'`,
  with the same cursor / retry / status semantics as Splunk. Per-format
  precondition checks on missing creds give tighter error messages
  instead of round-tripping out to a misconfigured endpoint.
- Settings UI (Compliance tab) gets a Format dropdown that swaps
  between Splunk and Sentinel field groups via `onSiemFormatChange()`,
  syncs visible fields with the saved format on page load, and persists
  both adapter inputs so admins can flip back without retyping.
  Per-adapter help cards explain where to find each set of credentials.
- Send Test Event button is wired through `/admin/config/siem/test`
  with the new keys; same flow as Splunk.
- README + `docs/ENTERPRISE_FEATURES.md` updated: setup walkthrough,
  full table of stored config keys, note that Microsoft supports the
  Data Collector API through Sept 2026 with a future slice planned
  for the newer Logs Ingestion API (DCE/DCR).
- Smoke-tested end-to-end:
  * Invalid base64 shared key → `Shared key is not valid base64: Only
    base64 data is allowed`.
  * Empty workspace_id → `Workspace ID or shared key is missing.`
  * Valid base64 + bogus workspace GUID → DNS-fails on the
    `{guid}.ods.opinsights.azure.com` resolution (proves URL builder).
  * Streamer dispatched to the Sentinel branch, batched 20 audit rows,
    failed cleanly on the bogus endpoint, kept the cursor at 0 for
    retry, recorded `siem.last_error`, did not advance.
  * Switched format back to splunk_hec mid-test and the original
    Splunk error path returned its old "Endpoint URL or HEC token is
    missing." message — no regression.

**Done — generic HMAC-signed webhook adapter (2026-04-26):**
- New `build_webhook_payload()` + `post_webhook()` in
  `worker/tasks/modules/siem_export.py`, mirrored on the API side in
  `api/app/utils/siem_export.py`. Sends the same flat JSON array of
  events that Sentinel uses; signs the raw body with HMAC-SHA256 and
  emits the digest in a configurable header (default
  `X-Hub-Signature-256: sha256=<hex>`, GitHub-compatible — receivers
  can reuse `hmac.compare_digest` against a recomputed digest with no
  vendor-specific library required).
- Always-emitted headers: `Content-Type: application/json`,
  `User-Agent: ipsolis-siem/1.0`, `X-Ipsolis-Event: audit.batch`,
  plus the configured signature header. Operators can supply
  additional headers as a JSON object in
  `siem.webhook_extra_headers` (e.g.
  `{"DD-API-KEY":"…","Authorization":"Bearer …"}`) — useful for
  Datadog, Sumo, Splunk-cloud, or homegrown receivers that want a
  static auth header alongside HMAC verification. Malformed JSON in
  that field is logged and ignored at runtime so a single typo can't
  silently break streaming.
- Migration `0068_seed_webhook_siem_config.py` seeds four new
  `app_config` keys: `siem.webhook_url` (plain), `siem.webhook_secret`
  (secret), `siem.webhook_signature_header` (default
  `X-Hub-Signature-256`), `siem.webhook_extra_headers` (JSON).
  `siem.format` description updated to list all three adapters.
- Streamer Beat task gets the third branch on `siem.format == 'webhook'`,
  with the same cursor / retry / status semantics as Splunk and
  Sentinel. Per-format precondition checks added so missing
  webhook credentials surface a tight error instead of round-tripping
  out to nothing.
- Settings UI (Compliance tab) format dropdown gains a third option
  "Generic Webhook (HMAC-signed)"; the existing format-toggle
  generalised to a config-driven loop so future adapters can be
  wired without further JS surgery. Per-adapter help cards explain
  HMAC verification with a copy-pasteable Python snippet for receivers.
- README + `docs/ENTERPRISE_FEATURES.md` updated: third adapter in
  feature description, full setup walkthrough including HMAC
  verification example, table of new config keys, hints for Elastic /
  Datadog / Sumo / Loki receivers.
- Smoke-tested end-to-end with a stdlib HMAC-verifying listener:
  * "Send Test Event" with the listener inside the api container →
    `Webhook accepted test event (HTTP 200)`. Listener log confirmed
    the sent `X-Hub-Signature-256` digest matched its independent
    recompute, the `X-Datadog-Test: hello` extra header was
    propagated, and the JSON-array payload contained 1 event.
  * Worker streamer pass with the listener inside the worker
    container forwarded a 40-event batch in a single POST,
    advanced the cursor to id 878, and the listener confirmed
    HMAC-match on the larger payload.
  * Switched format back to `splunk_hec` mid-test → no regression on
    the existing adapters.

**Sentinel Logs Ingestion API + streaming-failure email alert → tracked in *Deferred Enterprise Backlog* (top of file).**

### [done] Multi-instance HA — Prio 0 (show-stopper)
Beat slice — **shipped 2026-04-26**. The remaining HA work
(api/worker replicas, Postgres standby, dedicated Beat-alive health
probe) stays queued — each carries its own risk surface and is best
sliced independently rather than bundled.

**Done — Beat HA via celery-redbeat (2026-04-26):**
- Added `celery-redbeat==2.3.2` to the worker requirements. Redis is
  already the Celery broker, so reusing it for the redbeat schedule
  store + Lua-script distributed lock pulls in zero new infra.
- Wired in `worker/tasks/__init__.py`:
  * `redbeat_redis_url=BROKER_URL` — schedule + lock keys live in
    Redis; the on-disk `celerybeat-schedule` shadow file is gone.
  * `redbeat_lock_timeout=30` — how long a dead lock survives in
    Redis before another replica can claim it.
  * `beat_max_loop_interval=30` — caps the non-leader poll cadence so
    failover happens within ~lock-TTL. Default RedBeat polls every
    5 min, which yields a 5-minute failover and isn't really HA.
  * `redbeat_key_prefix="ipsolis:redbeat:"` — namespace so multiple
    ipSolis tenants on a shared Redis don't collide on schedule keys.
- `docker-compose.yml`:
  * Beat service no longer has a fixed `container_name`, so it can
    be scaled (`docker compose up -d --scale beat=N`).
  * Command switched to `--scheduler redbeat.RedBeatScheduler`.
  * Retired the `beat_schedule` named volume (file-based scheduler
    isn't used any more).
- Static `app.conf.beat_schedule` dict is unchanged — RedBeat
  ingests it on first boot and re-syncs on every restart, so
  "schedule edits ship via container rebuild" stays true.
- Existing Beat tasks audited for idempotence under at-most-once-
  but-rarely-twice semantics: SIEM streamer's cursor-advance,
  retention prune (deterministic cutoff), license-expiry mailer,
  approval reminders (last-reminded-at guard), and
  check_backup_schedule (per-minute dedupe via ``db_backups`` row
  query) are all naturally safe. The handover window is sub-second
  on clean restart and ≤30s on hard kill, so the duplicate-dispatch
  risk is limited to the very narrow lock-handover window.
- Verified end-to-end:
  * Single replica: lock acquired immediately, all 9 scheduled
    tasks dispatch on cadence. Redis shows
    `ipsolis:redbeat:` keys (schedule entries + `:lock` + `:statics`).
  * Two replicas (`docker compose up -d --scale beat=2`): replica 1
    dispatched 6 tasks in a 35-second window; replica 2 dispatched
    0 (idle, polling for the lock). Single-leader guarantee holds.
  * Failover: SIGKILL on the leader → other replica acquired the
    lock in **13 seconds** with the tuned timings (was ~234s with
    RedBeat defaults).
  * Schedule survives restart — stopped both replicas, restarted
    one, schedule keys still in Redis, dispatch resumed without
    re-seed.

**Slice-2 enrichments all shipped (Postgres standby docs, multi-replica api docs, multi-replica worker docs + sizing table, Beat-alive health probe) — see *Deferred Enterprise Backlog* (top of file) for individual entries. The codebase HA story is now: api stateless cookie sessions + LB round-robin, workers per-queue scaling, Beat multi-replica with RedBeat lock, Postgres standby as a documented reference architecture awaiting a real failover drill on staging.**

---

## Differentiators (Prio 1) — table-stakes for upper-mid market

### [done] Access certification campaigns — Prio 1
Quarterly "managers must re-confirm their team's access" workflow with email
reminders, escalation, auto-revoke on no-response. Hard requirement for ISO27001 / SOX / PCI audits.
**Slice 1 shipped 2026-04-30** (schema + admin CRUD + kickoff +
admin-side decision recording). **Slice 2 shipped 2026-04-30**
(signed-token review URL, kickoff emails + Teams card, reminder /
overdue / escalation / auto-revoke Beat task, manager portal page).

**Done — slice 1 (2026-04-30):**
- Migration `0081_certification_campaigns.py` adds two tables:
  * `certification_campaigns` — header per audit cycle. Fields:
    name, description, scope (JSONB filter — `asset_type_ids`,
    `cost_centers`, `departments`, `requester_emails` — empty
    fields are wildcards, AND across keys, OR within), due_at,
    status (`draft` → `running` → `closed` | `cancelled`),
    started_at, closed_at, created_by, created_at, updated_at.
  * `certification_reviews` — one row per (campaign, order) generated
    at kickoff. Reviewer is captured per row at kickoff time so
    later manager changes don't shift the audit trail. Status:
    `pending` → `confirmed` | `revoked` (manager decision) |
    `auto_revoked` (slice 2 — overdue with no decision).
  * Indexes: `(campaign_id, status)` and `(reviewer_email, status)`
    so the dashboard tile queries the UI fires stay O(rows-per-status).
  * Composite UNIQUE on `(campaign_id, order_id)` so re-running the
    kickoff or a Beat-HA edge can't double-create review rows.
- ORM `CertificationCampaign` + `CertificationReview` mapped, with
  the campaign↔reviews relationship for selectinload.
- New admin router `routes/admin_certifications.py` (mounted under
  `/admin/certifications`):
  * Read floor `auditor`+ via the router-level guard, so oversight
    roles can see campaigns + counts without being able to mutate.
  * Per-route `_WRITE_GATE = require_role("admin")` +
    `require_scopes("approvals:write")` on every write endpoint.
  * Endpoints: `GET /` (list with per-status review counts),
    `GET /{id}`, `GET /{id}/reviews`, `POST /` (create draft),
    `PUT /{id}` (edit — only due_at on running campaigns),
    `DELETE /{id}` (only on draft / closed / cancelled),
    `POST /{id}/start`, `POST /{id}/close`, `POST /{id}/cancel`,
    `POST /{id}/reviews/{rid}/decide` (admin-side decision recording).
  * Reviewer resolution at kickoff: prefers the order's first
    `manager` approval row, falls back to `owner_email`, then
    `user_email`. Reviewer email lower-cased so case-insensitive
    matching works cleanly in slice 2's notification code.
  * Revoke decision dispatches the asset's deprovision runbook
    (sets `order.status = REVOKING`, `order.action = DELETE`,
    enqueues via `dynamic_runner` — same path approval-decline uses)
    so access is actually pulled, not just flagged in the review row.
  * Defensive 409 guards: editing/deleting wrong-status campaigns,
    deciding reviews on non-running campaigns, kickoff on a
    non-draft campaign all return descriptive errors.
  * Audit trail: every state transition records a row attributed to
    the actor that drove it (`actor_by(request, "<route>")`).
- Admin UI `/ui/certifications` (template `ui/certifications.html`):
  * Campaign list with per-status counters (Reviews / Pending /
    Confirmed / Revoked) and inline action buttons (Start / Edit /
    Delete on draft, Close / Cancel on running, Reviews → drill-down
    always).
  * Create / Edit modal with name + description + due-date picker
    plus a "Scope filter" fieldset (four CSV inputs — asset type
    IDs, cost centers, departments, requester emails). Default due
    date = 14 days out at 17:00 local.
  * Drill-down panel below the list shows reviews for a chosen
    campaign with a quick filter input (reviewer / status / order
    id substring), per-row Confirm + Revoke buttons (admin only,
    visible only on running campaigns and pending rows), and a
    decision modal that surfaces the runbook side-effect of revoke
    so admins know what they're triggering.
  * Nav entry under "Approval Delegations", visible to `auditor`+
    so finance/audit roles can monitor without admin privileges.
- Verified end-to-end live:
  * `POST /admin/certifications` with scope `{cost_centers:["CC-IT-2100"]}`
    → 201, campaign id 1 in `draft` with empty review counts.
  * `POST /admin/certifications/1/start` → 200, `reviews_created: 3`,
    matched the 3 active orders against CC-IT-2100, reviewers
    resolved correctly from existing manager approvals
    (`jupp@xenpool.de` + `stefan@xenpool.de`).
  * `POST /admin/certifications/1/reviews/1/decide` with
    `{decision: confirmed, comment: "Stefan still uses VDI daily."}`
    → 200, review status flipped to `confirmed`, audit row attributed
    to `api:decide_certification_review (admin:legacy_key)`.
  * `DELETE /admin/certifications/1` → 409 with the descriptive
    "Cannot delete a running campaign — cancel it first" message.
  * Cancel → delete cleared the synthetic test rows; audit rows
    cleaned up via the documented `SET LOCAL` bypass.

**Done — slice 2 (2026-04-30):**
- Migration `0082_certification_slice2.py` — pure config-only.
  Seeds 4 `certification.*` config keys (reminder offsets,
  overdue toggle, auto-revoke toggle, escalation contacts) and 4
  email templates (`certification_kickoff`, `certification_reminder`,
  `certification_overdue`, `certification_escalation`) with sane
  HTML defaults customisable via *Settings → Email Templates*.
- Signed-token URLs: HMAC-SHA256 signed using
  `API_SECRET_KEY` (rotating it invalidates all outstanding links —
  same posture as approval tokens). 14-day TTL. Distinct
  `kind: "cert_review"` field so an approval token can't be
  replayed against a review row. API helper in
  `app.utils.certification_token`; worker mirror in
  `tasks.modules.teams_notify` so reviewer URLs minted on either
  side validate identically.
- New router `routes/certifications_external.py` (mounted at
  module root, no auth):
  * `GET /review/{token}` — render the single-row confirmation
    page with full asset / order / due-date context.
  * `POST /review/{token}` — record the decision (`confirm` /
    `revoke`). Revoke triggers the deprovision runbook via the
    existing `dynamic_runner` path so access is actually pulled.
  * `GET /review-queue/{token}` — same-reviewer expansion: list
    every pending row for the token's reviewer email and link
    each to its own per-row confirmation page. Per-row tokens
    keep individual revocation simple if a reviewer leaves.
  * Status pages: bad token (410), missing review (404),
    already-decided (200), campaign-not-running (409).
- Standalone templates: `review_confirm.html`,
  `review_status.html`, `review_queue.html` (branded, dark-mode
  aware, no portal SSO required so the link works from any client).
- Worker notification helpers in
  `worker/tasks/modules/notifications.py`:
  `send_certification_kickoff` / `_reminder` / `_overdue` /
  `_escalation`, all reading the seeded templates with the right
  variable maps.
- Teams card builder
  `worker/tasks/modules/teams_notify.build_certification_kickoff_card`
  with the same `@mention` pattern the approval cards use so
  Teams fires a real banner notification on the reviewer's client.
- Kickoff dispatch wired into `admin_certifications.start_campaign`:
  per-reviewer aggregation (one email/card per unique reviewer with
  the count, not N separate ones), enqueued via Celery
  (`certification_notifications.send_kickoff_email` task on the
  `notifications` queue) so the start endpoint returns immediately.
  Result dict reports `kickoff_emailed` + `kickoff_teams_sent` so
  the UI can confirm dispatch.
- New Beat task
  `worker/tasks/workflows/certification_reminders.scan_and_remind`
  runs daily at 04:30 Europe/Berlin (after audit prune at 03:00
  and threshold alerter at 04:00). Per running campaign:
  * **Reminders**: for each configured day-offset
    (`certification.reminder_days` default `7,1`), email pending
    reviewers once. Dedup keys off `audit_log` rows the helper
    writes — no extra schema, no per-row "last_reminded_at"
    column. Fires the latest applicable offset per reviewer per
    tick so a missed Beat day still nudges.
  * **Overdue email**: once past `due_at`, one nag email per
    reviewer with pending rows. Same once-per-(campaign, reviewer)
    semantics. Body adapts based on whether auto-revoke is enabled.
  * **Escalation**: one summary email to
    `certification.escalation_email` listing every reviewer with
    pending rows. At most once per campaign.
  * **Auto-revoke**: when `certification.auto_revoke_on_overdue=true`,
    transitions remaining pending rows to `auto_revoked` and
    dispatches the deprovision runbook for each underlying order
    via the existing `dynamic_runner` path. Off by default —
    yanking live access should be explicit opt-in.
- Manager portal page `/portal/certifications` (template
  `portal/certifications.html`) + JSON API
  (`/portal/api/certifications/reviews`,
  `/portal/api/certifications/reviews/{id}/decide`):
  * Lists every review row addressed to the SSO-authenticated
    user, split into "Pending reviews" + "Recent decisions".
  * Per-row Confirm/Revoke buttons + decision modal explaining
    the runbook side-effect of revoke.
  * Identity enforced server-side: `reviewer_email` must match
    the SSO user; cross-user attempts return 404 (not 403) so
    someone else's review row can't be enumerated.
  * Nav entry under "Delegations" in the portal sidebar.
- Verified end-to-end live:
  * Kickoff: `POST /admin/certifications/{id}/start` returned
    `{reviews_created: 3, kickoff_emailed: 2, kickoff_teams_sent: 2}`
    (3 reviews split between 2 reviewers — per-reviewer
    aggregation). Worker logs confirmed both emails delivered with
    correct subject lines including review count and due date.
  * Signed-token URL: `GET /review/{token}` → 200 with
    confirmation page, `GET /review-queue/{token}` → 200 with
    full pending list, `GET /review/garbage` → 410.
    `POST /review/{token}` with `decision=confirm` → 200, review
    row flipped to `confirmed` with attribution
    `api:certification_token (reviewer:jupp@xenpool.de)`.
  * Beat task on a backdated campaign with auto-revoke enabled +
    escalation configured: returned
    `{overdue_emails: 2, escalations: 1, auto_revoked: 2}`.
    Worker logs confirmed all three email types delivered, both
    auto-revoked rows triggered the deprovision runbook, and
    downstream "your access has been revoked" emails landed at
    the requesters.
  * Re-running the same Beat task immediately returned
    `{0,0,0,0}` — audit-log dedup correctly suppressed the
    duplicate notifications, and the no-longer-pending rows are
    invisible to the auto-revoke pass.

### [done] Approval-flow sophistication — Prio 1
Reminder slice **shipped 2026-04-26**. The bigger pieces (escalation,
delegation, N-of-M, conditional rules) remain.

**Done — approval reminders (2026-04-26):**
- Migration `0055_approval_reminders.py` — `last_reminded_at` +
  `reminder_count` on `order_approvals`, plus three `approval.*`
  config keys.
- Beat task `tasks.workflows.approval_reminders.scan_and_remind` runs
  hourly (`crontab(minute=15)` to spread load away from other Beat
  tasks). Picks pending approvals where
  `COALESCE(last_reminded_at, created_at) < NOW() - reminder_after_hours`
  and `reminder_count < max_reminders`.
- Refactored `dynamic_runner.send_approval_requests` per-approval
  block into a shared helper `deliver_approval_notification()` so
  both initial dispatch and reminders use the same email + Teams
  card path. Reminders bump the card title to "Reminder (n): …" so
  recipients can tell it's a nudge.
- Config: `approval.reminders_enabled` (default true),
  `approval.reminder_after_hours` (default 24),
  `approval.max_reminders` (default 3).
- Settings UI: new "Approval Reminders" section in the E-Mail tab
  with status / hours / cap inputs.
- Verified end-to-end: synthetic 48-hour-old approval picked up,
  Teams card delivered to live workflow webhook, reminder counter
  advanced, second run within cutoff window correctly skipped,
  disabled mode skips silently.

**Done — approval delegation / OOO routing (2026-04-26):**
- Migration `0058_approval_delegations.py` — `approval_delegations`
  table with approver/delegate emails+names, from/until window,
  reason, created_by, revoked_at, plus a covering index
  `(approver_email, from_at, until_at)` so the lookup on every
  order-creation is single-index-scan cheap.
- ORM `app.models.approval_delegation.ApprovalDelegation` with a
  CHECK constraint guaranteeing `until_at > from_at`.
- Resolver `app.utils.approval_delegation.resolve_active_delegate`
  finds the most-recent matching active delegation for an email
  (case-insensitive), filters out revoked rows and rows whose
  window doesn't cover NOW(), returns the row or `None`.
- Wired into both portal flows that create approval rows: the
  initial order (`portal_create_order`) and the
  modify/extend re-approval (`portal_modify_order`). Each
  call site uses an inline `_make_approval` helper that checks
  for an active delegation and routes to the deputy when one
  matches; logged at INFO so re-routes are visible in worker logs.
- Admin endpoints `GET/POST/DELETE /admin/approval-delegations`
  with `approvals:read` / `approvals:write` scope gates.
  Validation: 422 when `until_at <= from_at` or when delegate
  email equals approver email.
- Admin UI page `/ui/approval-delegations` with create modal
  (defaults to "tomorrow 09:00 → 14 days later 17:00" in local
  TZ), per-row revoke button, status badges
  (active / scheduled / expired / revoked).
- Audit log captures every create/revoke with `actor_by()` so
  the audit trail names which credential set up the delegation.
- Verified end-to-end: resolver returns the right delegate
  inside the FastAPI process; standalone-Python invocation
  hits a known mapper-init quirk that doesn't affect the
  actual request path.

**Done — approval escalation (2026-04-26):**
- Migration `0059_approval_escalation.py` — adds
  `order_approvals.escalated_at` column, seeds the
  `approval.escalation_email` config key (default empty),
  seeds a new `approval_escalated` email template with full
  variable set (original approver name+email, requester, asset,
  reminder_count, etc.).
- ORM `OrderApproval.escalated_at` mapped.
- `notif.send_approval_escalated()` — new notifications path that
  loads the seeded template, renders branded HTML, sends to a list
  of escalation addresses. Returns silently when no addresses are
  configured.
- `scan_and_remind` Beat task now does both jobs in a single tick:
  reminders for rows below the cap, escalations for rows at or above
  it. The escalation query filters `escalated_at IS NULL` so each
  approval escalates **at most once** — subsequent ticks skip it.
- Settings UI (E-Mail tab → Approval Reminders): new
  "Escalation contact(s)" field accepting comma-separated emails.
  Helper text explains the once-per-approval semantics.
- Verified end-to-end: synthetic approval at `reminder_count=3` with
  `escalated_at=NULL` → first scan returns
  `reminded: 0, escalated: 1`, sets `escalated_at`; second scan
  correctly skips it (`escalated: 0`).

**Done — N-of-M approvals (2026-04-26):**
- Migration `0061_asset_type_min_approvals.py` adds an
  `INTEGER NULL` column. NULL / 0 / >= total rows means "all
  required" (legacy default); set N for any-N-of-M semantics.
- ORM `AssetType.min_approvals_required` mapped; Pydantic Create/
  Update/Read schemas carry the field; audit `_type_snap()` includes
  it so changes are diffable.
- Runtime evaluator in `apply_approval_decision`: after recording
  an approve, counts approved rows, looks up the asset type's
  threshold, and either dispatches the order (threshold met) or
  logs the progress (still waiting). When the threshold is met it
  marks remaining pending rows as `superseded` — a new status
  string that disappears from pending lists, doesn't attract
  reminders / escalations, and can't be retroactively acted on.
- Decline is still a hard veto regardless of N — keeps a clear
  accountability path even with soft N-of-M policies.
- Admin form: new "Minimum approvals required" input next to the
  approval-owners block, blank/0 placeholder = "all". JS submitter
  sends the integer (or null) on save.
- Verified end-to-end: synthetic order with 3 application_owner
  rows + `min_approvals_required=2` → 1st approve = waiting, 2nd
  approve = threshold met, 3rd row = `superseded`,
  ``threshold_met=True`` correctly triggered
  ``_post_approval_dispatch``.

**Done — conditional approval rules slice 1 (2026-04-26):**
- Migration `0064_asset_type_approval_rules.py` adds an
  `approval_rules` JSON column on `asset_types`. Each rule is a dict
  ``{name, condition: {field, op, value}, approvers: [{email, name}]}``.
- Evaluator in `app.utils.approval_rules`:
  * `build_context(order, asset_type)` materialises the dict the
    rule conditions evaluate against.
  * `evaluate_rules(rules, ctx)` walks the list, returns a deduped
    set of approver dicts to add. Each result includes
    ``rule_name`` so the audit trail / UI can show which rule
    triggered the inclusion.
  * `_matches()` honours six fields (`duration_days`,
    `monthly_cost`, `has_pii`, `has_phi`, `has_pci`,
    `requester_department`) and six operators (`>`, `>=`, `<`,
    `<=`, `==`, `contains`). Malformed conditions are logged at
    WARNING and skipped — a hand-edited JSON typo can never block
    order creation.
- Wired into the portal order-creation flow alongside the
  manager / owner approvals: rule-derived approvers go through the
  same `_make_approval()` helper (so delegation re-routing applies),
  approver_type is set to `rule:<truncated rule name>` so the audit
  trail names the rule, and `seen_emails` deduplication prevents
  the rule from creating a second approval row when the same
  person is already covered as manager / owner.
- `order.status` is auto-promoted to `pending_approval` if rules
  trigger when the static toggles were off — so an asset definition
  can rely entirely on rules without setting `requires_manager_approval`.
- ORM, schemas (Create/Update/Read), audit `_type_snap()` all carry
  the new field; admin route handles create / update / clone.
- Admin UI rule builder in the asset-definition form (Approval
  section): a list of rows with name + field + op + value +
  approver-emails (CSV) + remove button, plus an "+ Add rule"
  factory. Submit serializer drops incomplete rows so partial
  edits don't ship as broken rules.
- Verified end-to-end: evaluator unit tests (no-trigger, single,
  double, malformed-rule cases) all behave correctly; round-trip
  through `PUT /admin/asset-types/{id}` persists rules in the JSON
  column verbatim.

**Done — conditional approval rules slice 2 (2026-04-26):**
- Boolean composition: ``_eval_condition()`` now recognises compound
  nodes ``{"op": "and"|"or"|"not", "clauses": [...]}`` alongside the
  slice-1 leaf shape, recursing up to 8 levels. ``and`` is vacuously
  True on empty clauses; ``or`` is False on empty clauses; ``not``
  inverts a single clause. Leaf shape is preserved unchanged so all
  existing rules round-trip.
- Custom-attribute conditions: ``build_context()`` flat-maps every
  ``order.config`` key under ``attr.<key>`` so a rule can reference
  ``attr.cost_center`` or ``attr.justification`` with the same six
  operators. ``contains`` against a list-valued attr (e.g.
  MULTI_ENUM) iterates members instead of stringifying the list.
- Per-rule N-of-M: optional ``min_approvals_required`` on the rule
  itself. Migration ``0066_order_approval_rule_quorum.py`` adds two
  columns to ``order_approvals``:
  * ``rule_name`` (200 chars, NULL on manager / owner rows) carries
    the full untruncated rule name — the existing ``approver_type``
    column is capped at 30 chars and only holds a short prefix.
  * ``rule_threshold`` (int, NULL fold-in-with-global) freezes the
    quorum at order-creation time so subsequent admin edits to the
    asset-type rules don't shift the order's decision logic
    mid-flight.
- Decision logic in ``apply_approval_decision()`` rebuilt: each rule
  with its own threshold forms a private quorum bucket; manager /
  owner / no-threshold-rule approvers fold into a single "global"
  bucket gated by ``asset_type.min_approvals_required``. ``threshold_met``
  is true iff every bucket meets its quorum. Per-bucket thresholds
  are clamped to the bucket size so a rule asking for more approvers
  than it has can't create an unfulfillable quorum. Pending approvals
  are only superseded once every bucket is satisfied — no premature
  "approved" while another bucket still needs decisions.
- Rule builder UI rebuilt as a card-per-rule editor: name + ALL/ANY
  combinator + per-rule quorum input in the header; conditions
  stacked vertically with their own ``+ Add condition`` button; field
  input is a free-text ``<input list="approval-rule-fields">``
  backed by a datalist that includes built-ins plus every
  ``attr.<key>`` from the asset type's ``config`` so admins get
  autocomplete for known custom attributes. Saved rules with deeply-
  nested conditions render with an inline warning, since the simple
  card editor only round-trips top-level clauses.
- Save serializer collapses 1-leaf rules to the flat slice-1 shape
  (cleaner JSON for trivial rules) and emits the compound shape only
  when 2+ leaves exist. Per-rule quorum sent as
  ``min_approvals_required`` only when set.
- README updated; ``docs/ENTERPRISE_FEATURES.md`` left as-is (rules
  weren't called out there in slice 1 — adding a section is a future
  doc-polish slice).
- Verified end-to-end:
  * Evaluator unit tests for leaf, AND, OR, NOT, attr-fields,
    nested compounds, threshold-bearing rules — all green.
  * Bucket-decision smoke tests (single global, mgr+rule, multi-rule
    with clamped threshold, asset-type N-of-M crossed with rule
    N-of-M) — all green.
  * Round-trip via ``PUT /admin/asset-types/{id}``: persisted shape
    matches what the evaluator consumes, rule with
    ``{op:"and",clauses:[duration>30, attr.cost_center contains EU]}``
    + per-rule quorum=1 fires correctly against a synthetic order
    in the EU cost center, and the same rule does NOT fire when
    cost_center is flipped to US (proves attr.* lookup).
  * Admin UI form renders the card structure, datalist with attr
    fields, combinator + quorum inputs.

**Slice-3 enrichments (tree editor for nested compounds, per-bucket reminder optimisation, escalation v2 with assigned approval) → tracked in *Deferred Enterprise Backlog* (top of file).**
**Done — self-service portal delegation (2026-04-26):**
- New router `app.routes.portal_delegations` exposes
  `GET /portal/delegations` (HTML page),
  `GET /portal/api/delegations` (list mine),
  `POST /portal/api/delegations` (create mine),
  `DELETE /portal/api/delegations/{id}` (revoke mine).
- Identity enforcement: every write coerces ``approver_email`` to
  the SSO-authenticated user's email. A portal user **cannot**
  re-route another user's approvals even by tampering with the
  payload. Cross-user revoke attempts return 404 (not 403) so we
  don't leak delegation existence.
- Anonymous mode (`entra.mode = disabled`) returns 403 from the
  write endpoints — no real identity to delegate from.
- Portal sidebar: new "Delegations" entry under "My Approvals",
  visible only when the user has had at least one approval
  (matching the existing `has_any_approvals` gate).
- New template `portal/delegations.html` mirrors the admin page UX
  but pre-fills the approver-email server-side and offers no
  ability to manage other users' rows.
- Audit trail: each portal-driven create / revoke records
  ``portal:<email>`` as ``triggered_by``.
- i18n complete: 21 new keys added to all 5 locales (en/de/fr/es/it),
  validator confirms 167 keys per locale.
- Verified: routes return 302 (portal-login redirect) without a
  session; 167 i18n keys present in every locale.

**Done — auto-decline on extended inactivity (2026-04-30):**
- Migration `0078_seed_auto_decline_config.py` seeds three keys
  (`approval.auto_decline_enabled` = false,
  `approval.auto_decline_after_days` = 0,
  `approval.auto_decline_message` = friendly resubmit hint).
  Off by default so existing installs see no behaviour change
  until a superadmin opts in.
- New Beat task `worker/tasks/workflows/approval_auto_decline.py:
  scan_and_auto_decline` runs daily at 03:30 Europe/Berlin.
  Picks at most one stale pending approval per order
  (`DISTINCT ON (order_id)` ordered by `created_at ASC`) so a
  single decline propagates the existing veto-on-decline
  semantics without writing N redundant audit rows on a
  multi-approver order.
- Mirrors the decline path in
  `app.utils.approval_decision.apply_approval_decision`:
  approval row → `status='declined'` + `decided_at` + the
  configured comment; order → `status='rejected'` +
  populated `error_message`; two `waudit` rows
  (`order_approval` + `order`) attributed to
  `system:auto_decline`; rejection email queued via the
  existing `tasks.workflows.dynamic_runner.send_approval_result_email`
  task so the requester gets the same message a human-driven
  decline produces. Deliberate duplication of the API-side
  decline logic — keeps the worker free of api package imports
  (same boundary `audit_helper.py` already observes).
- Wired into `worker/tasks/__init__.py` `include` list, queue
  routing (`notifications`), and `beat_schedule`.
- Settings UI: new "Auto-decline (opt-in)" sub-card inside the
  existing Approval Reminders section with status dropdown,
  days-input (0 disables), and decline-reason textarea. Single
  Save button persists the whole reminders+escalation+auto-decline
  block via the existing `saveApprovalReminderConfig` PUT loop.
- Verified end-to-end:
  * Synthetic stale approval (30 days old) on a synthetic
    pending_approval order against a real asset_type. With
    `enabled=true, after_days=14`: task returned
    `{declined: 1, after_days: 14}`, approval row flipped to
    `declined` with the configured comment, order flipped to
    `rejected` with the prefixed message, audit log gained two
    rows attributed to `system:auto_decline`.
  * Re-running the same task immediately returned
    `{declined: 0}` — already-rejected order is correctly
    skipped (the `o.status NOT IN ('rejected', 'cancelled')`
    guard in the SELECT).
  * Setting `enabled=false` and re-running returned
    `{skipped: True, reason: 'auto_decline_enabled is false'}`
    — disabled toggle short-circuits before any DB scan.
  * Synthetic test data + audit rows cleaned up via the
    documented `SET LOCAL ipsolis.allow_audit_mutation`
    bypass; tamper-evident triggers from migration 0062
    correctly blocked the initial DELETE before the bypass.

### [partial] HR feed + SCIM — Prio 1
Auto-deprovision on `LeaverEvent` from Workday/SAP HR; SCIM in/out so Okta /
Ping / SailPoint can drive ipSolis as an authoritative target.
**Slice 1 shipped 2026-04-30**: unified leaver flow + HR webhook +
SCIM 2.0 leaver-focused subset + admin UI. Slice 2 (full SCIM filter
grammar, /Groups, bulk operations) queued.

**Done — slice 1 (2026-04-30):**
- Migration `0083_hr_leaver_events.py` adds `hr_leaver_events`
  table — audit trail of every received leaver event with received
  /processed timestamps, raw payload (JSONB), and per-action counts
  (`orders_revoked`, `approvals_superseded`, `reviews_superseded`).
  Reverse-lookup indexes on `(user_email)` and
  `(status, received_at)` so the admin list page is O(rows-per-page).
- ORM `app.models.hr_leaver_event.HrLeaverEvent` mapped.
- New api token scopes: `scim:read`, `scim:write`, `hr:leaver`.
- Three new feature flags: `hr_webhook`, `scim` (both already
  enabled in dev licenses; production tenants need the Enterprise
  edition).
- Unified leaver helper `app.utils.leaver.process_leaver(...)` — the
  meat of the slice. For a given email it:
  * Marks every active order owned by the user as `revoking` and
    dispatches the deprovision runbook via the existing
    `dynamic_runner` path (same code path as approval-decline
    revoke + certification auto-revoke). Status set covers
    `pending` / `pending_approval` / `scheduled` / `processing` /
    `provisioning` / `provisioned` / `delivered`.
  * Marks every pending approval where the leaver was the approver
    as `superseded` so the order's quorum logic doesn't stall
    forever waiting on someone who's gone.
  * Marks every pending certification review where the leaver was
    the reviewer as `superseded` so the campaign's overdue +
    auto-revoke flow can complete naturally.
  * Audit row per transition + a campaign-scoped `processed` row
    on the `hr_leaver_event` itself.
  * Idempotent — re-firing for the same user is harmless because
    the previously revoked orders are no longer active.
  * Best-effort: failures during processing mark the event row
    `failed` with the exception text but the API still returns
    500 so the IDP retries on its cadence.
- HR webhook route `/hr/leaver` (`POST`) with HMAC fallback and
  bearer-token auth (`hr:leaver` scope). Generic JSON shape with
  `_normalise()` adapters for:
  * **ipSolis-native**: `{email, external_id?, source?}`
  * **Workday**: `{workerId, primaryEmail | workEmail | email,
    eventType: "terminated" | "termination" | "leaver"}`
  * **SAP SuccessFactors**: `{PERSON: {PERNR, email}}`
  * **Microsoft Graph subscriptions**:
    `{value: [{resourceData: {userPrincipalName | mail, id}}]}`
  * Unrecognised payloads return 400 with a descriptive message.
- SCIM 2.0 router `/scim/v2/*` — leaver-focused subset of RFC 7644:
  * `GET /ServiceProviderConfig`, `/ResourceTypes`, `/Schemas`
    (RFC-compliant discovery so Okta / SailPoint can introspect us).
  * `GET /Users` (list, with naive `userName eq "<email>"` filter
    + `startIndex` / `count` paging) — returns distinct
    `orders.user_email` values as SCIM User resources.
  * `GET /Users/{id}` returns the matching user or
    SCIM-error 404.
  * `POST /Users` and `PUT /Users/{id}` are acknowledged but no-op
    (ip·Solis users live in Entra ID / AD; SCIM Create from Okta
    just marks the user as "provisioned in ipSolis" on Okta's
    side without us writing anything).
  * `PATCH /Users/{id}` recognises `{op: replace, path: "active",
    value: false}` and the `{op: replace, value: {active: false}}`
    shape — both trigger the leaver flow.
  * `DELETE /Users/{id}` triggers the leaver flow → 204.
  * Bearer-token auth only (no HMAC fallback — modern SCIM clients
    all use OAuth bearer). Required scope is `scim:read` for GET,
    `scim:write` for everything else.
- Admin UI `/ui/leaver-events` (template `ui/leaver_events.html`)
  + read endpoint `GET /hr/admin/leaver-events` (auditor+ floor,
  admin scope `audit:read`) for monitoring incoming leaver events
  with substring filter on email. Nav entry next to "Certifications"
  in the sidebar.
- Verified end-to-end live:
  * `POST /admin/api-tokens` mints scope-only tokens for SCIM and
    HR independently — `scim:read,scim:write` and `hr:leaver`
    scope both gate per-route as expected (401 without token,
    403 with wrong scope).
  * SCIM `GET /scim/v2/ServiceProviderConfig` → 200 with the
    canonical RFC-7644 envelope (`patch.supported=true`,
    `filter.supported=true`, `oauthbearertoken` auth scheme).
  * `GET /scim/v2/Users` → 200, `totalResults: 4` (the dev DB's
    distinct order requesters). With `filter=userName eq
    "stefan@xenpool.de"` → 200 with one matching Resource.
  * `DELETE /scim/v2/Users/leaver-test@example.local` against a
    synthetic test order → 204, the order moved to `revoking` with
    `Leaver flow #1: user … marked as having left` reason, and a
    new `hr_leaver_events` row recorded `orders_revoked: 1`,
    `status: processed`.
  * `POST /hr/leaver` with a Workday-shaped payload
    (`{workerId, eventType: terminated, primaryEmail}`) → 200 with
    `source: workday`, `external_id: WD-EMP-12345`, leaver flow
    revoked the matching order. SAP and MS Graph adapter shapes
    also smoke-tested cleanly. Unrecognised payload → 400.
  * `GET /hr/admin/leaver-events` lists the captured events with
    paging + email-substring filter.
  * Synthetic test data + tokens cleaned up via the documented
    `SET LOCAL ipsolis.allow_audit_mutation = 'true'` bypass +
    `DELETE /admin/api-tokens/{id}`.

**Slice 2 — queued:**
- [ ] Full SCIM filter grammar (RFC 7644 §3.4.2 — `eq`, `ne`,
      `co`, `sw`, `pr`, `gt`, `ge`, `lt`, `le` + `and` / `or`
      / `not` composition). Slice 1 only handles `userName eq
      "..."` which covers the most-common Okta / SailPoint pattern.
- [ ] `/scim/v2/Groups` — ipSolis doesn't model user-group
      membership (groups live in AD), so this is genuinely "not
      applicable". A consumer-driven shim returning empty
      `Resources: []` for Okta-style "you must respond to /Groups"
      health checks would be enough.
- [ ] SCIM `Bulk` operations (RFC 7644 §3.7) for batch
      provisioning. Most IDPs fall back to per-resource ops when
      bulk isn't advertised, so this is a polish slice.
- [ ] HR webhook: kickoff dispatch of a leaver-completion
      summary email to the original requester's manager (so they
      know access was pulled, useful for handover documentation).
- [ ] Reviewer reassignment on supersede — instead of just marking
      pending certification reviews as `superseded`, reassign to
      the leaver's manager (lookup via AD). Today admins handle
      reassignment manually.

### [done] Observability — Prometheus `/metrics` — Prio 1 (2026-04-26)
Standard Prometheus text-format endpoint at `/metrics`. OpenTelemetry tracing
deferred to a separate slice (different dep tree, optional).
- HTTP request count + latency histogram per route template, labelled by
  method / route / status class. Path templates (`/orders/{order_id}`) are
  used so cardinality stays bounded; static / locale paths are bucketed
  to `/static/*` and `/locales/*` so per-file lookups don't blow it up.
- Business gauges refreshed on each scrape (cheap indexed `count GROUP BY`):
  - `ipsolis_orders_in_status{status}` — orders by lifecycle status
  - `ipsolis_approvals_pending` — pending approval rows
  - `ipsolis_pool_assets{asset_type, status}` — pool size per definition
- `metrics.enabled = false` flips the endpoint to 404 — toggle in
  `app_config`. No built-in auth on the endpoint; restrict via reverse
  proxy if exposed beyond the cluster perimeter.
- New module: `api/app/utils/metrics.py` (CollectorRegistry, gauge
  refresher, route-label helpers).
- New route: `api/app/routes/metrics.py`.
- Middleware: `record_request_metrics` in `main.py` records duration via
  `time.perf_counter()` after the response is built. `/metrics` itself
  doesn't count toward the request rate.
- Migration: `0052_seed_metrics_config.py` seeds `metrics.enabled = true`.
- Dep added: `prometheus-client==0.21.0`.
- Verified: real data from a running instance — orders/approvals/pool
  gauges populate correctly; disable toggle returns 404; re-enable
  returns 200; histograms have non-zero values for sample requests.

### [partial] Observability — OpenTelemetry tracing — Prio 1
API-side auto-instrumentation **shipped 2026-04-26**. Celery worker
instrumentation, sample dashboards, and the queue-depth gauge remain.

**Done — API tracing (2026-04-26):**
- Migration `0060_seed_otel_config.py` seeds 5 `otel.*` config keys
  (`enabled`, `service_name`, `endpoint`, `headers`, `console_exporter`).
- `app.utils.tracing.setup_tracing()` reads the config and configures
  the global `TracerProvider`. Two exporter modes that compose:
  OTLP HTTP (production target — Jaeger / Tempo / SigNoz / Honeycomb /
  any OTel collector) and a console exporter for local verification.
- `app.utils.tracing.instrument_app(app, engine)` wires
  `FastAPIInstrumentor` and `SQLAlchemyInstrumentor` after the provider
  is installed; both auto-emit spans for HTTP requests and DB queries.
- Lifespan hook in `main.py` reads `otel.*` from `app_config` at
  startup, calls setup + instrument, logs whether tracing is active.
- New deps: `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`,
  `opentelemetry-instrumentation-fastapi`,
  `opentelemetry-instrumentation-sqlalchemy` — all pinned to 1.29.0
  / 0.50b0. HTTP exporter chosen over gRPC to avoid the grpcio
  compile dependency.
- Settings UI: new "OpenTelemetry Tracing" card in the Compliance tab
  with status / service-name / endpoint / headers (secret) / console
  exporter checkbox. Save handler `PUT`s every key; restart message
  reminds operator that tracing wires up at API startup.
- Verified end-to-end: enabled tracing + console exporter → restart
  → confirmed real spans emitted to API stdout including FastAPI
  request kind and SQLAlchemy DB query kind with correct resource
  attributes (service.name, service.version).

**Done — Celery worker tracing (2026-04-26):**
- New `worker/tasks/tracing.py` mirrors the api's setup module —
  reads `otel.*` from `app_config` via a one-shot psycopg2 query,
  configures `TracerProvider`, hooks `CeleryInstrumentor` and
  `SQLAlchemyInstrumentor`. Pinned to the same OTel version (1.29.0
  / 0.50b0) as the api so propagated trace context parses identically
  on both sides of the Celery boundary.
- Worker service name auto-derived as ``ipsolis-worker`` (or
  ``<custom>-worker`` when admin set a custom service name) so
  trace UIs show distinct services for api vs. worker.
- Setup invoked at module-import time in
  `worker/tasks/__init__.py`, before workers fork — required for
  the Celery instrumentor to hook signals correctly.
- Bug fixed in `_load_otel_config_sync()`: now strips both
  `postgresql+asyncpg://` (api) and `postgresql+psycopg2://`
  (worker) URL prefixes before handing the DSN to psycopg2.
- New deps in `worker/requirements.txt`:
  `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`,
  `opentelemetry-instrumentation-celery`,
  `opentelemetry-instrumentation-sqlalchemy`.
- Verified end-to-end: enabled tracing + console exporter →
  restarted worker → triggered a task → confirmed spans emitted
  with `service.name: ipsolis-worker` (distinct from
  `service.name: ipsolis-api`). When both sides run with the same
  OTLP collector, an http-dispatched runbook produces a single
  distributed trace.

**Done — Celery queue depth gauge (2026-04-26):**
- New `ipsolis_celery_queue_depth{queue}` Prometheus gauge in
  `app.utils.metrics`. Refreshed on every `/metrics` scrape via
  `redis.asyncio` `LLEN` against the four known queues
  (`default`, `provision`, `reclaim`, `notifications`).
- Resilient: missing/non-Redis broker → gauges cleared (no error);
  per-queue LLEN failures logged at WARNING and skipped without
  affecting other queues.
- Verified live: pushed 3 synthetic messages to `provision`,
  next scrape reported `provision=3.0`; cleared the queue, next
  scrape reported `provision=0.0`. Pre-existing `default=2.0`
  matched real Beat-scheduled tasks waiting in the broker.

**Done — Grafana dashboard + Prometheus alerts (2026-04-26):**
- New `docs/grafana/ipsolis-overview.json` — 9-panel dashboard
  (request rate, error rate, p95 latency, pending approvals stats;
  request-rate-by-route + latency-percentiles timeseries; orders by
  status + asset-pool composition; Celery queue depth). Uses
  `${DS_PROMETHEUS}` template variable so it imports against any
  Prometheus datasource UID without editing.
- New `docs/grafana/prometheus-alerts.yaml` — 6 alert rules across
  3 groups: HTTP (high 5xx, slow p95), business (approval backlog,
  Celery queue warning + critical), pool capacity. Uses the labels
  we already emit, so no extra recording rules needed.
- New `docs/grafana/README.md` — Prometheus scrape config snippet,
  Grafana import walkthrough, threshold rationale, plus a section
  on wiring Tempo / Jaeger as a separate datasource for the OTel
  traces we ship.
- Cross-linked from `docs/ENTERPRISE_FEATURES.md` so admins find
  it from the main feature docs.
- Verified the JSON dashboard parses (9 panels) and the YAML alerts
  parse (6 rules / 3 groups) — no field-shape regressions.

The full observability story (Prometheus metrics + business gauges +
Celery queue depth + OpenTelemetry api/worker tracing + ready-to-import
Grafana dashboard + Prometheus alert rules) is now end-to-end complete.

### [done] Cost / chargeback per asset type — Prio 1
Reporting side **shipped 2026-04-26**. Per-order projection on the
portal order detail page + AD snapshot on non-portal order paths
**shipped 2026-04-30**. **Threshold alerts** (with email + optional
Teams card), **historical view** (daily snapshot table + `?as_of=`
query), and **FX conversion** (static rate map +
`?reporting_currency=` cross-rate conversion) all shipped 2026-04-30.

**Done — schema + report (2026-04-26):**
- Migration `0056_asset_type_cost.py` — adds `monthly_cost NUMERIC(12,2)`,
  `currency VARCHAR(3)`, `cost_center VARCHAR(100)` (all nullable so
  legacy definitions stay untracked).
- ORM `AssetType.monthly_cost / currency / cost_center`.
- Pydantic schemas (Create / Update / Read) carry the new fields.
- Admin form: new "Cost & Chargeback" section between Classification
  and Lifecycle, with monthly cost input, currency dropdown
  (EUR/USD/GBP/CHF/JPY/CAD/AUD/SEK/DKK/NOK/PLN), and cost-center text.
  Section nav updated to include the new anchor.
- Admin route: `GET /admin/cost-report?fmt=json|csv` — aggregates
  active orders (same status set as capacity enforcement) per
  (cost_center × asset_type × currency), returns rows + per-cost-center
  totals. CSV export with `Content-Disposition: attachment`.
- Admin UI: new `/ui/cost-report` page with summary cards + detail
  table + CSV download. Linked from the left nav between Maintenance
  and License.
- Audit snapshot updated to capture cost field changes.
- README + `docs/ENTERPRISE_FEATURES.md` updated with the field
  definitions and report behaviour.
- Verified end-to-end: empty report when no definitions priced,
  correct active-order counts and projected totals after seeding
  two test definitions, CSV export with right content-type and
  attachment filename.

**Done — AD-driven consumer breakdown (2026-04-26):**
- Migration `0057_order_requester_attributes.py` — six new columns on
  `orders` (`requester_sam_account`, `requester_department`,
  `requester_cost_center`, `requester_company`, `requester_employee_id`,
  `requester_title`) plus five `ad.attribute.*` config keys with
  sensible defaults.
- `ad_lookup._msldap_lookup` extended to fetch the configured HR
  attributes alongside identity, with empty-mapping entries filtered
  out so we don't ask AD for a literal "" attribute.
- `portal.portal_create_order` snapshots the AD attributes onto the
  Order row on creation (best-effort — AD outage doesn't block the
  order). `MODIFY` and `DELETE` orders inherit the snapshot from the
  original provision order so chargeback stays internally consistent.
- Cost report rewritten to query active orders directly, exposing
  three aggregation views via JSON: provider (asset cost_center),
  consumer (requester cost_center), department (requester department).
  Untracked but priced asset definitions still surface as 0-row
  entries in the provider view so admins can spot misconfigured
  ones.
- CSV export switched to per-order detail (18 columns) — order id,
  status, dates, full requester identity (email, name, sAMAccount,
  employee id, title, department, cost center, company), asset type,
  provider cost center, currency, unit cost, monthly total.
- Cost report UI: three view tabs (By provider / By consumer cost
  center / By department), summary cards swap based on selected
  view, detail table only renders for the provider view.
- Settings → Active Directory: new "AD Attribute Mapping
  (Chargeback)" card with 5 inputs (department / cost center /
  company / employee ID / title). Save handler `PUT`s each key.
- Verified end-to-end: AD attrs populated on a real provisioned
  order, JSON shows by_consumer_cost_center and by_consumer_department
  populated correctly, CSV carries all requester fields per order.

**Done — per-order projection on portal detail (2026-04-30):**
- New `_compute_cost_projection(order, asset_type)` helper in
  `routes/portal.py` returns `{unit_monthly_cost, currency,
  span_days, months_estimate, projected_total}` or `None` when the
  asset type has no `monthly_cost`, the order has no
  `requested_from`/`until` window, or the window is zero days.
  Months use the calendar average (30.4375 days/month) so a 90-day
  request reads as ~2.96 months — same unit the cost report's CSV
  uses when finance pivots per-order data.
- Portal `order_detail.html` Access & Duration card gains two
  trailing rows under a divider: **Monthly cost** (unit price) and
  **Projected total** (unit × months); the total row carries a
  hover title showing the day-count + months for transparency.
  Block is hidden whenever the asset type isn't priced — untracked
  types render no extra rows.
- 5-locale i18n keys added: `portal.order_detail.field_unit_cost`
  + `portal.order_detail.field_projected_total` (en/de/fr/es/it).
  `validate_locales.py` reports OK at 171 keys/locale.

**Done — AD snapshot on non-portal order paths (2026-04-30):**
- New shared helper `app.utils.ad_lookup.snapshot_requester_attrs(email)`
  returns the six `requester_*` columns from a single best-effort
  AD lookup, or an empty dict when AD is unconfigured / lookup
  fails / email is empty. Returning a dict means the caller can
  splat `**` onto the `Order` constructor with no special-casing.
- `routes/orders.py` (public `POST /orders/`) and
  `routes/webhook.py` (`POST /webhook/servicenow`) now call the
  helper before constructing the Order. Both use
  `asyncio.to_thread` since the underlying msldap path is sync.
  ServiceNow- and external-API-driven orders now produce the same
  consumer-side chargeback rows the portal does.
- `routes/portal.py` refactored to use the same helper instead of
  its inline try/except — single source of truth for the snapshot
  shape, no behaviour change to the portal flow.

**Done — threshold alerting (2026-04-30):**
- Migration `0079_cost_thresholds.py` adds a `cost_thresholds` table
  with composite PK `(cost_center, currency)`, `monthly_limit`,
  `recipients`, plus `last_alerted_at` / `last_alerted_amount`
  hysteresis fields. Same migration seeds the
  `cost.threshold_alert_quiet_hours` config key (default 24h) and
  the `cost_threshold_breach` email template (with a copy-pasteable
  variable list, customisable via Settings → Email Templates).
- ORM `app.models.cost_threshold.CostThreshold` mapped; admin CRUD
  endpoints land on the existing `/admin/cost-report` router under
  `/thresholds[/{cost_center}/{currency}]` so the audience and
  scope guard match the report itself. Reads inherit the router
  floor (`auditor`); writes carry an explicit `admin` role gate
  plus `config:write` scope. Recipient validation rejoins on
  whitespace and validates each address with a stdlib regex.
- Beat task `worker/tasks/workflows/cost_threshold_alerter.py:
  scan_and_alert` runs daily at 04:00 Europe/Berlin. Computes
  provider-side projections in one indexed group-by (mirroring the
  cost-report API), iterates configured thresholds, alerts on each
  breach via the new `notif.send_cost_threshold_breach` helper, and
  stamps `last_alerted_at` regardless of email outcome so a flaky
  SMTP relay doesn't lock the alert into a re-fire loop. Hysteresis
  via the quiet-window config key suppresses repeats; editing a
  threshold clears the clock so subsequent breaches re-alert
  immediately.
- Cost Report UI gets an inline **Cost thresholds** card below the
  detail table with a small modal for create/edit and per-row
  Edit/Delete actions. Provider totals cards visually flag breached
  rows (red border, ⚠ icon, "over limit" subtext) and the
  thresholds row highlights breaches in the same colour so the
  whole page reads at a glance.
- Verified end-to-end:
  * `POST /admin/cost-report/thresholds` creates rows; `GET` lists
    them with the live projection joined client-side; `PUT` clears
    `last_alerted_at` so an edit doesn't keep the row in quiet mode
    with stale settings; `DELETE` returns 204.
  * Beat task with two thresholds (one matching, one orphan):
    `{checked: 2, alerted: 1, skipped_quiet: 0}`. Email dispatched
    to both configured recipients; rendered template carries the
    breach amounts and the cost-report URL.
  * Re-running the same task without editing returned
    `{alerted: 0, skipped_quiet: 1}` — quiet window holds.
  * Edit raised the limit to clear the breach AND cleared the
    `last_alerted_at` clock; subsequent runs returned 0 because
    no breach. Synthetic test rows cleaned up.

**Done — Teams card on threshold breach (2026-04-30):**
- New `build_cost_threshold_breach_card()` in
  `worker/tasks/modules/teams_notify.py` — Adaptive Card v1.4 with
  ⚠ Attention-coloured header, FactSet of cost-center / limit /
  projection / over-by / active-orders / asset-types, optional
  *Open Cost Report →* action when the portal base URL is set.
  No `@mention` (alerts go to a finance / ops mailing list, not a
  single approver — channel-level notification rules drive it).
- Wired into the existing `cost_threshold_alerter` Beat task: when
  `teams.mode == enabled` and `teams.webhook_url` is set, the
  alerter posts the card alongside the email. Best-effort and
  additive — Teams failures don't roll back the email or keep us
  from stamping `last_alerted_at`. Result dict gets a new
  `teams_sent` counter.
- Verified live with the dev environment's real Workflows webhook:
  `{checked: 1, alerted: 1, teams_sent: 1, skipped_quiet: 0}` —
  email + Teams card both delivered on the same breach.

**Done — historical view + FX conversion (2026-04-30):**
- Migration `0080_cost_fx_and_history.py` adds:
  * `cost_report_snapshots` table (composite PK
    `snapshot_date / view / dimension_key / currency`, with
    `projected_monthly_total / active_orders / asset_types /
    captured_at`). Reverse-lookup index on `(view, snapshot_date)`
    for the date-range queries the UI fires.
  * Three new config keys:
    `cost.fx.canonical` (default `EUR`),
    `cost.fx.rates` (JSON map of currency → rate-into-canonical,
    default empty),
    `cost.snapshot_retention_days` (default 365; 0 = keep forever).
- ORM `app.models.cost_report_snapshot.CostReportSnapshot` mapped.
- New Beat task
  `worker/tasks/workflows/cost_report_snapshot.py:capture_daily_snapshot`
  runs daily at 02:00 Europe/Berlin (before audit prune at 03:00 +
  threshold alerter at 04:00). Captures all three views
  (`provider`, `consumer_cc`, `consumer_dept`) in one tick;
  idempotent within a day (DELETE today's rows then INSERT). Prunes
  rows past `cost.snapshot_retention_days`.
- API endpoint extended:
  * `?reporting_currency=USD` — converts mixed-currency totals into
    the requested currency using cross-rates derived from
    `cost.fx.rates` (`rate_src / rate_target`). Re-aggregates the
    summary cards so a cost center with EUR + USD orders collapses
    to one figure. Currencies without configured rates surface in
    `meta.fx_excluded_currencies`.
  * `?as_of=YYYY-MM-DD` — reads from `cost_report_snapshots`
    instead of running the live aggregation. Falls back to live
    when no snapshot exists for the date (typical for "today"
    before the daily Beat task has run); the response's
    `meta.snapshot=true|false` reflects which path served the data.
    Per-asset-type detail rows aren't stored in snapshots; the UI
    notes this in a banner when reading historical data.
  * Both params compose: `?as_of=2026-04-15&reporting_currency=GBP`
    reads the 2026-04-15 snapshot then converts to GBP.
  * New `GET /admin/cost-report/fx-config` endpoint exposes the
    canonical currency + the configured rate map so the UI can
    populate the currency selector with only currencies it can
    actually convert to.
- Cost Report UI gets two new view-knobs alongside the existing tab
  bar: an **As of** date picker (with a *Today* clear-link that
  appears once a date is set) and a **Show in** currency selector
  populated from the FX config endpoint. A small blue meta banner
  surfaces whether the response came from a snapshot, whether FX
  was applied, and any excluded currencies.
- Verified live end-to-end:
  * Default `?reporting_currency=` empty → source-currency view
    `[{key:CC-IT-2100, currency:EUR, projected:37.50}]`.
  * `?reporting_currency=USD` with rates `{EUR:1.0, USD:0.92}` →
    same row converted to `40.76 USD`. Math checks out:
    37.50 × (1.0 / 0.92) = 40.76.
  * `?reporting_currency=GBP` with rates `{EUR:1.0, GBP:1.17}` →
    32.05 GBP. 37.50 × (1.0 / 1.17) = 32.05.
  * Snapshot capture: `{rows_written: 5, per_view:{provider:1,
    consumer_cc:2, consumer_dept:2}}` for current state.
  * `?as_of=2026-04-30` (today, snapshot exists) →
    `meta.snapshot=true`, totals match, detail rows empty.
  * `?as_of=2026-04-29` (no snapshot) →
    `meta.snapshot=false`, falls back to live with full detail rows.
  * Combined `?as_of=2026-04-30&reporting_currency=GBP` →
    snapshot data converted to GBP, both meta flags true.

**Cost section now fully shipped** — no more Still-to-do items.

---

## Polish & smaller gaps (Prio 2)

### [done] PS Modules — Linux compatibility flag — Prio 2 (2026-04-30)
The worker runs PowerShell 7 on Linux, but many PSGallery modules ship
with `PSEdition_Desktop` (Windows 5.1) only and won't load. Operators
now declare each module's compatibility when adding it; the modules
table shows the flag and lets admins click any badge to cycle through
`Unverified → Linux ✓ → Windows only ✕ → Unverified`.

**Why no PSGallery probe / search:** most ip·Solis installs are
air-gapped and have no outbound internet from the api/worker
containers. We attempted a PSGallery search + tag-derived
auto-detection slice but cloud PSGallery's `Search()` endpoint
times out on popular modules (e.g. `VMware.PowerCLI` with `$top=20`
exceeds the 12 s budget) and `IsLatestVersion` filters return zero
results when combined with `searchTerm`. Manual operator declaration
is faster, deterministic, and works in air-gapped deployments.

**Components:**
- Migration `0077_ps_module_compatibility.py` adds
  `ps_modules.compatibility VARCHAR(20) NOT NULL DEFAULT 'unknown'`.
  Existing rows backfill to `unknown` so installs upgrade cleanly.
- ORM `PsModule.compatibility` (`api/app/models/ps_module.py`).
- Pydantic `PsModuleCreate.compatibility` accepts
  `core` / `desktop_only` / `unknown` (Literal-typed); defaults to
  `unknown`. New `PsModuleCompatibilityUpdate` schema for the
  dedicated PUT.
- New endpoint `PUT /admin/ps-modules/{id}/compatibility` (gated by
  the existing `_GATE_PS_MODULES` enterprise feature) flips just the
  flag without re-queueing an install. Refreshes the ORM row after
  commit so the response payload doesn't trip
  `MissingGreenlet` on async lazy-load.
- `_ps_module_dict` returns the field, so the existing list /
  create / update endpoints carry it without further plumbing.
- Stripped: the legacy `/admin/ps-modules/search` endpoint and the
  unused `httpx` / `xml.etree.ElementTree` imports — keeping the
  air-gap-friendly contract documented in the comment.
- Frontend `ui/ps_modules.html` rewritten:
  * Add-form has a new **Linux compatibility** dropdown for both
    Gallery and Upload sources.
  * Modules table gains a **Linux compat.** column with the same
    three-state badge (green / red / amber).
  * Each badge is a button that cycles compatibility on click —
    optimistic UI, rolls back on failure with an alert.
  * New page-level explainer banner: "the worker runs PowerShell 7
    on Linux, modules tagged PSEdition_Desktop only won't load."
  * Removed the previous PSGallery autocomplete dropdown wired
    against the now-dead search endpoint.
- Verified end-to-end:
  * Migration applied to dev DB; existing rows show `unknown`.
  * `PUT /admin/ps-modules/10/compatibility` `{"compatibility":"core"}`
    returns 200 with the updated row; subsequent GET reflects the
    change. Cycle-button on the page paints the next state and
    persists.
  * Page renders correctly with the new column + dropdown.

### [done] QA regression sweep — Prio 2 (2026-04-29 / 2026-04-30)
Findings from a Claude-Code browser QA pass over `/portal/` and `/ui/`
(report: `ipsolis-agent-prompt.md`). Walked through all 26 items;
the survivors below are the ones with verified root causes. All
17 survivor items shipped on 2026-04-29 / 2026-04-30. False flags
and items rooted in data (not code) are recorded in the decision
block underneath rather than as work items — kept inline so future
QA rounds don't re-raise them.

**Bugs (real defects):**
- [x] **B2 — Step duration shows `-0.0s`.** *(2026-04-29)* Both the
      admin (`ui.py`) and portal (`portal.py`) duration builders now
      clamp via `max(0.0, …)` and render `< 1s` for sub-second deltas
      so clock-skew negatives never leak to the template formatter.
- [x] **A4 — Order status badges missing colors for half the enum.**
      *(2026-04-29)* `_STATUS_COLORS` extended from 6 → 13 entries
      (every `OrderStatus` value: `pending_approval`, `scheduled`,
      `provisioning`, `provisioned`, `revoking`, `revoked`, `rejected`
      added with appropriate amber/sky/blue/green/orange/gray/red
      hues + dark variants). All 6 templates that render the badge
      (`orders.html`, `order_detail.html`, `portal/index.html`,
      `portal/my_it.html`, `portal/my_it_detail.html`,
      `portal/order_detail.html`) now display values via
      `| replace('_', ' ') | title` so users see "Pending Approval"
      instead of `pending_approval`.
- [x] **A5 — Rejected orders show red "Error" box.** *(2026-04-29)*
      Admin `order_detail.html` now branches on
      `order.status.value == 'rejected'` and renders an amber
      "Rejection reason" box; technical failures keep the red Error
      banner. Portal `order_detail.html` keeps its dedicated rejection
      block (with approver comments) and suppresses the generic Error
      banner when status is `rejected` so only one rejection-related
      element appears.
- [x] **U2 — Raw LDAP error to end-users.** *(2026-04-29)* Portal
      `order_detail.html` now shows `portal.order_detail.error_friendly_message`
      ("Access could not be provisioned. Please contact IT support.")
      instead of the raw `order.error_message`, and per-step errors
      render `portal.order_detail.step_failed_generic`
      ("This step failed — contact IT for details.") instead of the
      raw `step.error` (LDAP DN paths no longer leak). Admin view
      retains full diagnostic text. New i18n keys added in all 5
      locales; `validate_locales.py` reports OK at 169 keys/locale.
- [x] **U8 — Tag chips break dark-mode contrast.** *(2026-04-29)*
      Both Jinja-rendered chips (lines around 79 / 99) and the
      JS-built ones in `my_it_detail.html`'s tag-editor now carry
      `dark:bg-{blue|purple}-500/15 dark:text-{blue|purple}-300` and
      matching dark hover states.

      *Bonus while in the same templates:* dropped `| upper` on
      `order.action.value` in admin + portal order detail (covers
      U7/N2 — readable "Provision" instead of shouty "PROVISION",
      consistent with the N1/N2 keep-English decision).

**Polish (small, contained):**
- [x] **A3 — HTML 404 page for `/ui/*`.** *(2026-04-30)* New
      [`ui/404.html`](api/app/templates/ui/404.html) extends the admin
      `base.html` so the sidebar nav stays put and only the main panel
      shows the styled "Page not found" card with the requested path
      and a back-to-dashboard link. Wired via a catch-all
      `GET /ui/{path:path}` at the bottom of
      [`api/app/routes/ui.py`](api/app/routes/ui.py) (must remain LAST
      so all real routes match first). Auth dependency on the router
      means unauthenticated requests still redirect to login first;
      after login the 404 renders. Returns HTTP 404 (not 200) so
      monitoring still sees the right status. Subsumes A1 / A2.
- [x] **A6 — Update Notifier error placement.** *(2026-04-30)*
      `ui/settings.html` no longer crams the raw exception into the
      "Last check:" status line. The error now renders in a dedicated
      red-border banner below the Save row with the friendly copy
      "Last check failed — token may be missing or invalid. See
      server logs for details." (raw exception stays in `app_config`
      and worker logs).
- [x] **U3 — Delegations duplicate "revoked" text.** *(2026-04-30)*
      `portal/delegations.html` row builder for revoked/expired rows
      now renders an em-dash (`—`) in the action column instead of
      duplicating the status badge text — the status column already
      carries the badge, so the action cell now correctly reads as
      "no action available".
- [x] **U7 / N2 — Drop `| upper` on action labels.** *(2026-04-29
      — bundled with the U2/A5 template pass.)* Both
      `portal/order_detail.html` and admin `order_detail.html` now
      use `| capitalize` so `provision` renders as "Provision"
      instead of "PROVISION".
- [x] **N4 — Standalone Runbooks page heading.** *(2026-04-30)*
      `ui/standalone_runbooks.html` page heading + `<title>` now
      read "Runbooks" to match the sidebar nav. URL slug
      (`/ui/standalone-runbooks`) kept as-is so existing bookmarks
      and links don't break.
- [x] **A7 — Shorten admin login placeholder.** *(2026-04-30)*
      `admin/login.html` username placeholder shortened to
      "Username (or legacy admin key)".
- [x] **P1 — STEPS column tooltip.** *(2026-04-30)* Admin orders
      list `Steps` `<th>` carries `title="Completed steps / Total steps"`.
- [x] **P2 — Approval Delegations admin column header.** *(2026-04-30)*
      `ui/approval_delegations.html` SOURCE header drops the inline
      parenthetical and now exposes the rationale via a small
      info-glyph (`ⓘ`) with the `title=` tooltip carrying the full
      "self-service vs helpdesk" explanation.
- [x] **P3 — Dashboard "Updated:" timestamp lacks date.** *(2026-04-30)*
      `dashboard.html` now formats the pool-status timestamp as
      `%Y-%m-%d %H:%M:%S` so a long-open tab doesn't read as ambiguous.
- [x] **P5 — Asset Pool action icons need a11y labels.** *(2026-04-30)*
      `ui/asset_pool.html` row builder now wraps each action glyph in
      a `<span aria-hidden="true">` and the surrounding `<button>`
      carries both `title=` and `aria-label=` ("Edit asset", "Delete
      asset", "Force delete asset", "Revoke and release asset"). The
      P5 description in the QA report listed the wrong glyphs (→ × ⊙);
      actual glyphs are ✏ × ⊗ ↩ — covered all four.
- [x] **P6 — Empty-state polish on portal "Meine Freigaben".**
      *(2026-04-30)* `portal/approvals.html` empty pending /
      empty recent panels now lead with a circled icon (✓ for
      empty pending, ⌛ for empty recent) and slightly larger text so
      the message reads as intentional empty state rather than a
      loading skeleton. The `data-i18n` key migrated from the outer
      div to a child `<p>` so existing locale strings continue to
      apply without overwriting the icon.

**Needs a visual look-see before triaging:**
- [x] **P4 — Cost Report row hierarchy.** *(2026-04-30)* The "L-tree"
      that the QA reporter spotted was the cost-center dedup logic:
      consecutive rows sharing a cost center rendered "↳" instead of
      repeating the value, which made two rows with `cost_center=null`
      (both rendered as "(unassigned)" by the backend) look like a
      parent/child pair. Dropped the dedup entirely — `cost_report.html`
      now always shows the cost-center value. Rows are still sorted by
      cost_center server-side so the value clusters visually without
      the misleading hierarchy glyph.

### [decision] QA regression sweep — recorded 2026-04-29
Items below were raised in `ipsolis-agent-prompt.md` but are
deliberately *not* work items. Logged here so future QA rounds don't
re-raise them and so the rationale stays alongside the survivors.

- **N1 — Status badge values stay English in all locales.** IT pros
  recognise `pending_approval` / `failed` / `provisioned` across
  languages; localising introduces translation drift on
  ops-critical labels. Reference in code wherever the badge map
  lives so future contributors don't accidentally translate.
- **N2 — Action labels (`provision`, `delete`) stay English.**
  Same rationale as N1; combine with the U7 capitalize fix.
- **A8 — Admin Console stays English-only.** Audience is a small
  number of IT admins for whom English is the lingua franca;
  translating the admin UI would balloon i18n surface ~10× vs. the
  portal for negligible benefit. Document at the top of the admin
  templates so it isn't accidentally i18n-converted later.
- **B3 — "Stefan" vs "Stefan van Boxmer-Fischer" on legacy orders.**
  Historical data only. Today the portal captures `user_name` from
  the Entra `name` claim ([`utils/entra.py`](api/app/utils/entra.py))
  which is `displayName` — consistently full. New orders are
  correct; no migration warranted. If a one-time hygiene pass is
  desired, a SQL `UPDATE orders SET user_name = … FROM ad_lookup`
  is enough — no schema change.
- **A1 / A2 — `/ui/runbooks`, `/ui/asset-definitions` JSON 404.**
  Sidebar navs link to the correct URLs; user-typed URL guesses
  hit a 404. Right fix is **A3** (HTML 404 page), not renaming
  routes to match labels.
- **N3 — Sidebar phrasing variants.** "Zugang anfordern" /
  "Neuen Zugang anfordern" / "Bestellung abschicken →" serve
  different UI roles (terse nav / page heading / submit verb).
  Standardising would *worsen* UX. Skip.
- **U4 — "unscoped now" subtitle on Shared Remote Desktop tile.**
  Whatever the operator typed into the asset-type description —
  it's data, not code. Tell the admin to update.
- **U5 — Order-flow phrasing.** Same as N3; the variants are
  intentional and correct.
- **U6 — Hostname monospace.** Verified consistent across portal
  orders list, order detail, and my_it_detail. The convention the
  QA reporter recommended as preferred is the convention already in
  use.
- **N5 — "(slice 2)" suffix in column header.** Verified absent;
  the QA reporter likely confused a help-text aside in
  `ui/settings.html` with a column header.
- **N6 — `ip·Solis` brand consistency.** All user-facing strings
  use the middle dot. Plain `ipSolis` only appears in dev-only
  compose-file comments and the repo folder name. No churn needed.

**Optional — only revisit if architecture choice changes:**
- **B1 — Language preference not persisted server-side.** Premise
  is wrong: portal i18n is fully client-side via `localStorage.portal_lang`
  + `data-i18n` attributes ([`static/js/i18n.js`](api/app/static/js/i18n.js)).
  No server cookie/session involved. The QA reporter likely saw
  flash-of-untranslated-content (FOUT) before `i18n.js` applies.
  If FOUT becomes a real complaint, the fix is a tiny pre-paint
  inline script that hides body until locale loads — *not* a
  server-side language read.
- **U1 — Browser tab titles hardcoded English.** Same root cause
  as B1: `<title>` sits in `<head>` and renders before `i18n.js`.
  Could be addressed by a `data-i18n-title` hook that updates
  `document.title` after locale apply. Low value (internal tool —
  nobody reads tab titles). Mirror the N1/N2 keep-English decision
  unless a user complains.

### [done] `max_per_user` for pooled types — Prio 2 (2026-04-25)
Per-user quota now enforced everywhere a PROVISION order can be created
(public API, ServiceNow webhook, self-service portal). Quota covers personal
and pooled assignment models; `dedicated_shared` is exempt because everyone
shares a single instance.
- UI: `max_per_user` input lifted out of the personal-only section in the
  asset-definition form; visible for `assigned_personal` + `capacity_pooled`,
  hidden only for `dedicated_shared`. Helper text explains the active-status
  set the count is taken over.
- Runtime: new `enforce_max_per_user()` in `api/app/utils/capacity.py`
  returns HTTP 409 with a descriptive detail when the user is at the limit.
- Wired into `api/app/routes/orders.py` (after `enforce_pool_capacity`),
  `api/app/routes/webhook.py` (ServiceNow path), and
  `api/app/routes/portal.py` (renders error inline via `_render_error`).
- Bonus correctness fix: `_ACTIVE_STATUSES` in `capacity.py` now includes
  `PENDING_APPROVAL` and `SCHEDULED` — closes a hole that let scheduled and
  approval-pending orders bypass both pool capacity *and* the per-user quota.
- Counting uses case-insensitive `user_email` match so Outlook-style casing
  variants don't yield a fresh slot.

### [done] `is_active` flag on asset definitions — Prio 2 (2026-04-25)
Admins can now deprecate without delete. Inactive types are hidden from the
portal catalog (`/portal/orders/new`) but stay visible in the admin list with
an "Inactive" badge so historical orders, audit, and runbook configs stay coherent.
- Migration `0049_asset_type_is_active.py` — adds `is_active BOOLEAN NOT NULL DEFAULT true` column.
- ORM `AssetType.is_active` (`api/app/models/asset.py`).
- Pydantic `AssetTypeCreate` / `AssetTypeUpdate` / `AssetTypeRead` carry `is_active`.
- Admin route POST/PUT/clone honor the field; clone preserves the source's flag.
- Audit snapshot `_type_snap()` includes `is_active` so deprecation events are diffable.
- Form: new "Active" checkbox with explainer in the Identity section, default-checked.
- List: "Inactive" badge + 60% row opacity on deprecated rows.
- Portal: catalog list / re-render error path filter `WHERE is_active = true`.
- Verified end-to-end: PUT `is_active=false` removes from catalog, admin list keeps it with badge.

### [done] Long-form `help_text` per asset definition (markdown) — Prio 2 (2026-04-25)
Admins can now write a multi-paragraph note in markdown that requesters see
when they pick the type on `/portal/orders/new` — separate from the one-line
catalog description. Used for things requesters need *before* ordering:
pre-installed software, expected provision time, support contact, license terms.
- Migration `0050_asset_type_help_text.py` — adds `help_text TEXT` column.
- ORM `AssetType.help_text` (`api/app/models/asset.py`).
- Pydantic Create/Update/Read schemas carry `help_text`.
- Admin form: new textarea below Description in the Identity section, with a
  helper line listing supported markdown features. JSON payload includes
  `help_text` on both create and update; clone preserves it.
- Audit `_type_snap()` includes `help_text` so revisions show up in the audit log.
- Rendering: `api/app/utils/markdown_render.py` uses python-markdown +
  bleach with a strict allowlist (`p, br, strong, em, code, pre, blockquote,
  ul, ol, li, h1-h6, a, hr`; `a` only keeps `href`/`title`; protocols
  limited to `http/https/mailto`). Linkified hrefs auto-set
  `target="_blank" rel="noopener noreferrer"`.
- Filter registered as `| markdown` on the shared Jinja env in
  `templates_instance.py`. Used in `portal/order_new.html` via
  `{{ t.help_text | markdown | safe }}`.
- Portal: per-type panel that toggles when the asset is selected — same
  pattern as the attribute section. Hidden when the selected type has no help.
- Styling: hand-tuned CSS scoped to `.help-md` (paragraphs, headings,
  lists, code, blockquote) — Tailwind via CDN doesn't ship the typography
  plugin, so we don't rely on `prose-*` classes.
- New deps: `markdown==3.7`, `bleach==6.2.0` in `api/requirements.txt`.
- Verified: XSS attempts (`<script>`, `<img onerror>`, `javascript:` href)
  are stripped by the bleach pass; round-trip via direct SQL update + render.

### [done] Microsoft Teams approval cards — Prio 2 (2026-04-25)
Approvers now receive an Adaptive Card in Teams alongside the email when a
request needs sign-off. The card has a single "Review request →" button
that opens a tokenized confirmation page with no portal login required.
Slack adapter is deferred — same token + endpoint is reusable when needed.

**Architecture**: Microsoft Teams **Workflows** webhook (no Azure Bot
registration, no Graph permissions). Admin creates a Workflow once per
target channel/chat with the template "Post to a channel when a webhook
request is received", pastes the URL into Settings → E-Mail → Microsoft
Teams. Card delivery is done by the worker, best-effort, never blocks the
order on Teams misconfiguration.

**Why not bot/GET-auto-approve**: GET-based one-click would let Outlook /
Teams link previewers prefetch and accidentally approve. Bot Framework
needs a publicly reachable bot endpoint and Microsoft App ID/Secret —
overkill for the value delta over the link-to-confirmation-page UX.

**Components**:
- `api/app/utils/approval_token.py` — HMAC-SHA256 stateless token, signed
  with `API_SECRET_KEY` (rotating that env var invalidates all outstanding
  links — usually the right thing on incident response). 14-day TTL.
- `api/app/utils/approval_decision.py` — shared decision-recording helper;
  portal route and tokenized route both call it so the two paths can never
  drift on what counts as "approved" or how downstream effects fire.
- `api/app/routes/approvals_external.py` — `GET /approve/{token}` renders
  the confirmation page; `POST /approve/{token}` records the decision.
  No portal session required. Status pages for already-decided / expired /
  invalid token cases.
- `api/app/templates/approve_confirm.html` + `approve_status.html` —
  standalone branded pages, dark-mode aware, work without Entra SSO.
- `api/app/utils/teams_notify.py` + `worker/tasks/modules/teams_notify.py` —
  the worker copy duplicates the token signer + card builder verbatim
  (separate Docker images, no cross-image imports). Cross-verified that a
  token minted in the worker validates on the API endpoint.
- `worker/tasks/workflows/dynamic_runner.py` — `send_approval_requests`
  posts the card after sending the email when `teams.mode = enabled` and
  `teams.webhook_url` is set. Failures are logged at WARNING and don't
  abort the email loop.
- `api/alembic/versions/0051_seed_teams_config.py` — seeds `teams.mode`
  (default `disabled`) and `teams.webhook_url` (`is_secret=true`).
- `api/app/routes/admin.py` — `POST /admin/config/teams/test` posts a
  test card to the configured webhook so admins can verify the workflow
  end-to-end before enabling.
- `api/app/templates/ui/settings.html` — new "Microsoft Teams — Approval
  Cards" section in the E-Mail tab with Mode dropdown, Webhook URL field,
  Save + Send Test Card buttons, and a setup hint.
- `api/app/routes/portal.py` — refactored `portal_decide_approval` to
  delegate to the shared helper (40 lines deleted, 2 added).
- `api/app/main.py` — registers the new router.

**Verified**:
- Token round-trip works in both directions; tampered/expired/garbage
  tokens all reject cleanly.
- API endpoint serves 200 for valid pending approval, 200 for already-
  decided, 404 for missing-approval-row (cleanup / cascade), 410 for
  invalid/expired token. Each path renders its own status page so the
  approver can tell what happened.
- Test endpoint returns descriptive error for missing/disabled config and
  network errors (no 500s on misconfiguration).
- Worker can import the mirror module; cross-verified token validates
  on the API side (shared `API_SECRET_KEY` from `.env`).
- Adaptive Card includes a Teams `@mention` (`msteams.entities`) of the
  approver — verified live, fires a Windows system-tray banner on the
  approver's client. Approver's display name is also used as the
  `<at>...</at>` placeholder so the body renders gracefully even when
  a Workflow template strips entities.

### [done] Field-level data classification — Prio 3
Slice 1 — schema (in JSON), admin UI tagging, portal badges, audit
trail capture — **shipped 2026-04-26**. Slice 2 — per-classification
retention windows + audit-log classification column — **shipped
2026-04-26**. Approval-routing UX (slice 3) deferred to backlog.

**Done — classification tagging (2026-04-26):**
- `asset_types.config` per-attribute JSON gains a new optional
  `classification` field. Allowed values: `""` (public default),
  `internal`, `pii`, `phi`, `pci`. No DB migration needed — the
  column was already JSON.
- Admin form (`asset_type_form.html`): each attribute row now has
  a "Classification" sub-row with a 5-option dropdown and an
  inline hint. Both the existing-data branch and the addAttrRow JS
  factory include the field. The submit serializer reads it and
  attaches `classification` to the JSON entry only when set.
- Admin list (`asset_types.html`): each rendered attribute key is
  followed by a small classification badge — amber for PII, red for
  PHI/PCI, neutral for `internal`. Public attributes stay
  badge-free.
- Portal (`order_new.html`): attribute labels render a matching
  badge with a tooltip explaining the classification when the
  requester is filling in the form. PII shows a shield icon and
  amber badge; PHI/PCI show red badges with cross/card icons.
- Audit log automatically captures the classification because
  `_type_snap()` already serialises `t.config` verbatim — every
  asset-type create/update/clone audit row carries the per-attribute
  classification.
- Verified end-to-end: tagged `manager_email` as PII and
  `cost_center` as internal on a real asset definition, confirmed
  admin list renders the badges and JSON persists correctly.

**Done — audit retention pruning slice 1 (2026-04-26):**
- Migration `0063_seed_retention_config.py` seeds three keys:
  `retention.audit_log_days` (window, 0 = disabled),
  `retention.last_run_at`, `retention.last_pruned` (auto-managed
  status fields).
- New Beat task `worker/tasks/workflows/audit_retention.py:prune_old_rows`,
  scheduled daily at 03:00 Europe/Berlin via crontab. Reads the
  window, opens a transaction, sets the documented
  `ipsolis.allow_audit_mutation` GUC via `SET LOCAL`, and DELETEs
  rows past the window with a CTE that returns the count.
- Status fields updated atomically with the prune so the Settings
  UI can show "Last run: <ts> · Pruned: <N> rows".
- Settings UI (Compliance tab → "Audit Log Retention" card):
  retention-days input + status panel showing last run + last pruned.
- Verified end-to-end: 5 stale rows + 5 fresh rows seeded → set
  window to 30 days → prune ran → returned `pruned: 5`, only
  fresh rows survived; status fields updated correctly; direct
  DELETE outside the prune transaction still blocked by the
  tamper-evident trigger (bypass is properly txn-scoped).

**Done — per-classification retention slice 2 (2026-04-26):**
- Migration `0067_audit_log_classification.py` adds a
  `classification` column on `audit_log` (default `internal`,
  indexed) and seeds three new windows + a status field:
  `retention.pii_days`, `retention.phi_days`, `retention.pci_days`,
  `retention.last_pruned_by_class` (auto-managed JSON breakdown).
  Backfilled existing rows to `internal` inside a `SET LOCAL`
  bypass transaction so the immutability triggers from 0062 don't
  block the migration.
- Classification is set at write time (not at prune time, as
  originally sketched in slice-1 notes). The strictest of any
  attribute on the touched asset type wins (`pci > phi > pii > internal`)
  via shared `classify_asset_type()` / `classify_for_asset_type_id()`
  helpers in `app.utils.audit`. Classifying at write time freezes
  each row's retention class against subsequent attribute edits on
  the type — the row's regulatory category is determined by the
  type's state at the moment of the audited change, not the type's
  state at prune time.
- Wired into all high-value audit writes: asset_type CRUD (4 sites),
  asset CRUD (5 sites), order create/update/cancel (3 sites),
  webhook order create. Other audit writes default to `internal` —
  config / approval delegation / api token / etc. fall under the
  global window. `waudit()` (worker side) gets the same kwarg.
- Beat task rewritten to iterate buckets: one DELETE per
  classification scoped via `SET LOCAL ipsolis.allow_audit_mutation`
  + COMMIT, so a single huge bucket can't starve the others. The
  global window applies to `internal` + NULL only; per-class
  windows apply to that class only and **do not fall back to the
  global default** when set to 0 — explicit opt-in to retention so
  PII/PHI/PCI rows are never accidentally dropped under the
  catch-all. `retention.last_pruned_by_class` records the per-class
  count for ops visibility.
- Settings UI (Compliance tab → "Audit Log Retention" card)
  rebuilt: default window unchanged, plus a sub-card with three
  per-class day inputs (PII / PHI / PCI). Status panel renders the
  last-run-by-class breakdown when non-empty.
- Verified end-to-end:
  * Asset type 16 with one `pii`-tagged attribute → audit rows
    from `PUT /admin/asset-types/16` come out with
    `classification='pii'`. Pre-existing rows backfilled to
    `internal`. Total counts match.
  * Backdated 5 internal rows + 1 PII row by 30 days. With
    `audit_log_days=1, pii_days=0`: prune deleted 5 internal rows,
    PII row preserved. With `audit_log_days=1, pii_days=14`: PII
    row (30d > 14d) was deleted in the next pass; per-class JSON
    `{"internal":0,"pii":1}` matches.
  * Tamper-evident triggers still hold outside the prune
    transaction — direct DELETE/UPDATE without the GUC bypass
    raises the original error.

**Per-classification approval routing UX → tracked in *Deferred Enterprise Backlog* (top of file).**

### [done] Catalog search & filter in the portal — Prio 3 (2026-04-25)
Pure client-side filter on `/portal/orders/new`: a search input matches
against name + description + help_text (lowercased), and a category dropdown
narrows by the existing `AssetCategory` enum. The controls auto-hide when
there are six or fewer definitions to avoid clutter on small catalogs.
- Server pre-renders `data-search` and `data-category` on every card so
  filtering is one DOM pass — no fetches, no extra round-trip.
- "No definitions match" empty state replaces the grid when nothing matches.
- "Clear" link appears once any filter is set, resets both controls.
- If the user already had a card selected and the new filter hides it,
  the selection is cleared and the help / attribute / user-list panels reset
  so a stale `asset_type_id` can't be submitted.
- i18n: 9 new keys (`catalog_search_placeholder`, `catalog_filter_all`,
  five `catalog_filter_*` category labels, `catalog_no_match`,
  `catalog_clear_filters`) added across all five locales (en/de/fr/es/it).
  `tools/validate_locales.py` confirms 143 keys per locale, all aligned.

### [done] Dashboard pool-capacity warnings — Prio 3 (2026-04-26)
A capacity-pressure band that auto-renders above the status tiles when
any active asset pool is at ≥80% fill, listed by severity with direct
links to the affected definition / pool view. Surfaces capacity
problems before users hit a 409 from the existing per-pool quota
enforcement.

- New `_pool_warnings(db)` helper in `api/app/routes/ui.py` —
  computes per-asset-type fill in two batched queries (one for
  active orders on capacity_pooled types, one for AssetPool grouped
  by `(asset_type_id, status)`); no N+1 regardless of catalog size.
- `assigned_personal` / `dedicated_shared` types: anything not in
  `Free` status counts as a consuming slot — busy, reserved,
  maintenance, Failed, Reinstall all keep the row from satisfying
  a new request, so the operator sees real availability pressure.
- `capacity_pooled` types: count active orders against
  `pool_capacity` using the same status set as quota enforcement.
- Severity: ≥80% → `warning` (amber), ≥95% → `critical` (red).
  Banner copy adapts: "N pools at critical capacity, M approaching",
  "N pools at critical capacity", or "M pools approaching capacity".
- Each warning row is a clickable link — `pooled` → asset-definition
  edit page (where capacity is configured); `personal/shared` →
  the asset-pool list filtered to that type.
- Inactive asset definitions are excluded — they can't accept new
  orders so flagging them as "full" is noise.
- Renders inside the existing `fragments/pool_summary.html` so it
  participates in the existing dashboard auto-refresh path; no
  schema or migration needed.
- Verified: a real pool with 2/2 personal VDIs renders as
  `1 pool at critical capacity · Personal VDI Host · 100% (2/2)`
  in red/critical styling.

### [done] In-app setup checklist — Prio 3 (2026-04-26)
Replaced the originally-planned "guided tour" with a persistent setup
checklist on the dashboard — more useful for both first-run setup
*and* ongoing operational health checks (e.g. someone deletes the only
asset definition → the relevant item flips back to ☐). No external JS
library; pure server-side detection from current DB state.

- New endpoint `GET /admin/setup/state` returns 9 checklist items
  (6 essential, 3 recommended) with `done` / `label` / `hint` / `link` /
  `tier` per item plus per-tier and overall summaries.
- Items: app branding, SMTP, AD, Entra ID, asset definitions exist,
  asset pool has assets, Teams card delivery, SIEM streaming,
  per-integration API token issued.
- Dashboard card with circular progress ring and percent badge,
  auto-expands when there's anything incomplete, collapses when
  everything is green.
- "Hide until next setup change" persists a signature of the current
  done-state in `localStorage`. If the state later changes (regression
  or new config), the signature mismatch re-shows the card.
- Each pending row is a direct link to the relevant settings tab
  anchor (e.g. `/ui/settings#ad`) so admins skip the navigation step.

---

## Open — Distribution & Licensing Architecture

### [open] Open Core Model: Community + Business (two tiers, one repo)

**Decision:** ip·Solis will be offered as an Open Core product.
- **Community Edition** — public GitHub repo, free to use, no feature-gating in code
- **Business Edition** — pre-built images from private registry (ghcr.io), includes all additional modules; no separate Enterprise tier

All current Enterprise and Business features move into the Business Edition.
Feature flags in code (`require_enterprise`, `require_business`, `BUSINESS_FEATURE_KEYS`, `ENTERPRISE_ONLY_FEATURE_KEYS`) are removed entirely — protection is achieved by the absence of code in the Community Edition, not by runtime gates.

**Steps:**

- [x] **Module inventory:** Identify and document all Business-only modules/files (vsphere, xenserver, sccm, ServiceNow webhook, SCIM, HR webhook, Leaver Events, Audit Retention, Custom Deprovision, RBAC extensions, Password Policy)
- [x] **Two Dockerfiles:** Build `Dockerfile.community` (copies only Community files) and `Dockerfile.pro` (copies everything) from a shared mono-repo
- [x] **Remove feature gates:** Remove `require_enterprise()`, `require_business()`, `BUSINESS_FEATURE_KEYS`, `ENTERPRISE_ONLY_FEATURE_KEYS` from code; clean up `is_feature_enabled()` and all `{% if is_enterprise %}` / `{% if is_business %}` template checks
- [x] **Simplify license mechanism:** Keep Ed25519 signature and install UUID (for expiry dates + user limits), but remove feature control via license file — the license only controls `max_users`, `max_asset_types`, `expires_at`
- [x] **GitHub Actions pipeline:** CI automatically builds both images on every release and pushes them to the private registry; community mirror repo is automatically populated with filtered files
- [x] **Registry token management:** Pro customers get a revocable registry token; define process for issuance (after purchase) and revocation (on cancellation)
- [x] **Customer onboarding docs:** `docker-compose.yml` + `.env.example` + installation guide for Business customers (docker login → compose up, done)
- [x] **Set up public community repo:** github.com/xenpool/ipsolis as a public mirror without Business modules

---

## Pre-existing open tasks

### [open] Entra ID Connect / Cloud Sync setup — infrastructure (no code change needed)
Sync `xenpool.local` on-prem users to the Entra ID tenant so they can use portal SSO with
their existing domain credentials. Pure Windows Server / Azure infrastructure task.
- [ ] Install Entra ID Connect (or Entra Cloud Sync agent) on a domain-joined server
- [ ] Configure UPN suffix (`xenpool.de`) for synced accounts
- [ ] Verify synced users can log into the portal (no code change required)

### [open] Cloud group management via Microsoft Graph — future
Extend `target_executor` to manage Entra cloud-only security groups for asset types
that define `{"type": "entra_group", "group_id": "..."}` targets. Requires
Microsoft Graph API integration (separate sprint).

### [open] Okta as 2nd Identity Provider — future
Add Okta as an optional second IDP alongside Entra ID for portal SSO. Estimated effort: 4–6 days.

**Context:** Okta uses standard OIDC (same protocol as Entra underneath MSAL), so no exotic library is needed. The main work is abstracting the auth layer away from Entra-specific assumptions.

**Key design decisions to resolve before starting:**
- [ ] IDP routing strategy: domain-based auto-routing (e.g. `@corp-a.com` → Entra, `@partner.com` → Okta) vs. a picker page at `/portal/login/select`
- [ ] Whether `entra_with_onprem` mode (UPN → on-prem LDAP check) needs a per-IDP equivalent for Okta users

**Implementation slices:**
- [ ] Extract generic OIDC helper (`api/app/utils/oidc.py`) — authlib or httpx + raw OIDC; covers auth URL, code exchange, claim extraction (Entra can be refactored to use it too)
- [ ] New `okta.*` app_config keys: `okta.mode`, `okta.domain`, `okta.client_id`, `okta.client_secret`, `okta.redirect_uri`, `okta.allowed_domains`
- [ ] DB migration for new config keys
- [ ] Auth routing: IDP selection logic in `/portal/login`, second callback endpoint `/portal/auth/okta/callback`
- [ ] Okta logout support (`https://{okta.domain}/oauth2/v1/logout`)
- [ ] Admin UI: Okta settings section + "Test connection" button (same pattern as Entra)
- [ ] Make `entra.mode` / portal auth gate provider-agnostic (currently hardcoded to `entra.mode` in `portal.py`)

**No user-table impact:** ipSolis is session-only; identity is keyed by `email`. Same email via Okta = same user in order history. No account-merging complexity.

---

## Done

### [done] Portal Authentication — Entra ID SSO (2026-03-23)
- `msal` added to `api/requirements.txt`
- `SessionMiddleware` added to `main.py` (signed cookie, 8h TTL)
- `api/app/utils/entra.py` — MSAL helper (auth URL, token exchange, domain check)
- `api/app/routes/auth.py` — `/portal/login`, `/portal/auth/callback`, `/portal/logout`
- `api/app/routes/portal.py` — `require_portal_auth` dependency on all routes; when `entra.mode = disabled` the portal is open with a shared anonymous identity
- `base_portal.html` — user name chip + Sign out link in nav bar
- `portal/auth_error.html` — error page for login failures
- `api/app/templates/ui/settings.html` — "Entra ID / Azure AD" section in Identity & Directory tab
- `POST /admin/config/entra/test` — verifies credentials via client-credentials token flow
- Migration 0019 — seeds 6 `entra.*` config keys (`entra.mode` defaults to `disabled`)

### [done] Beat-Scheduler → migrate to dynamic_runner (2026-03-23)
- `check_expiring_assets` now creates a `delete` order per expired asset
  (copies `provisioned_state` from the provision order for deterministic revoke)
  and dispatches `dynamic_runner.run` instead of the hardcoded `vdi_reclaim.run`
- Original provision order is immediately set to `expired`; the new delete
  order progresses through `dynamic_runner` with the asset type's configured
  runbook/strategy
- Note: a `delete` runbook must be configured per asset type in the Admin UI
  for `runbook_only` / `composite` asset types; `group_only` types work without

### [done] Legacy Workflow Cleanup — Prio 1b (2026-03-23)
- `check_expiring_assets` moved into `dynamic_runner.py`; beat_schedule updated
- Deleted: `vdi_provision.py`, `vdi_modify.py`, `vdi_reclaim.py`
- Removed from `__init__.py`: legacy includes + task_routes entries

### [done] Basic Tests (Happy Path) — Prio 3 (2026-03-24)
- `pytest>=8.0.0` + `pytest-asyncio` added to `api/requirements.txt`
- `api/tests/conftest.py` — adds `worker/` to sys.path
- `api/tests/test_happy_path.py` — 14 tests, 31 total passing
- `docker-compose.yml`: added `./api/tests` and `./worker` volume mounts
- Run: `docker compose exec api python -m pytest tests/ -v`

### [done] SCCM VDI Group Configuration Script (2026-03-23)
- `scripts/sccm/Configure-VDI-Groups.ps1` — executed during SCCM Task Sequence setup
- Creates RDP/ADM groups in `OU=VDI,OU=XenPool GmbH,DC=xenpool,DC=local`
- Dual-channel logging: Windows Event Log + `C:\Windows\debug\Configure-VDI-Groups.log`

### [done] XenServer Script Library — VMware conversions (2026-03-16)
- `XenServer - VM reboot or startup (gracefully)` (ID 10)
- `XenServer - VM change boot order (disk-cd-net)` (ID 11) — `hvm_boot_params["order"]="cdn"`
- `XenServer - VM change boot order (net-cd-disk)` (ID 12) — `"ndc"`
- `XenServer - VM shutdown (gracefully)` (ID 13) — CleanShutdown + HardShutdown fallback
- `XenServer - VM stop (force)` (ID 14) — HardShutdown with retry logic

### [done] XCP-ng / XenServer Hosting Infrastructure (2026-03-16)
- Settings page: vSphere + XenServer credential sections
- Migration 0017: seeds `vsphere.*` and `xenserver.*` config keys
- Module editor: auto-injects hosting vars
- `dynamic_runner`: exposes `config.xenserver.*` / `config.vsphere.*`
- PS preamble: SSL cert bypass injected globally

### [done] PS Module Manual Upload — non-Gallery SDKs (2026-03-16)
- Migration 0018: `source_type` + `upload_data BYTEA` columns on `ps_modules`
- API: `POST /admin/ps-modules/{id}/upload`
- Worker: `_install_from_upload()` — extracts zip to `~/.local/share/powershell/Modules/`

### [done] Pool Capacity Enforcement + Display (2026-03-16)
- `api/app/utils/capacity.py`: `enforce_pool_capacity()` — HTTP 409 if pool full
- Orders + webhook routes: pre-flight capacity check for PROVISION actions
- Asset types list: shows `X / Y in use` with color coding for capacity_pooled types
