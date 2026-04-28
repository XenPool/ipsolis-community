#!/usr/bin/env bash
# Generate self-signed TLS certs for the nginx reverse proxy if they
# don't already exist.
#
# On a fresh install (or after wiping ./certs) nginx crash-loops
# because /etc/nginx/certs/cert.pem is missing. This script creates a
# 1-year self-signed cert with the right SubjectAltNames so nginx boots
# cleanly. The deploy workflow calls it before `docker compose up`, and
# operators can run it directly the first time too.
#
# **Idempotent.** Re-running on an instance that already has certs is a
# no-op unless --force is passed. Safe to invoke from CI on every run.
#
# Usage:
#   tools/install/bootstrap-certs.sh                       # silent if .env CORS_ORIGINS is set; else prompts
#   tools/install/bootstrap-certs.sh ipsolis.example.com   # explicit FQDN, no prompt
#   IPSOLIS_FQDN=ipsolis.example.com tools/install/...     # same, via env var
#   tools/install/bootstrap-certs.sh --force ...           # overwrite existing certs
#
# FQDN resolution order:
#   1. positional arg
#   2. IPSOLIS_FQDN env var
#   3. CORS_ORIGINS in ./.env  (the admin already typed the hostname there
#      during the standard install — re-using it avoids a second prompt for
#      the same value, and doubles as a sanity check that .env was edited)
#   4. interactive prompt (only when stdin is a tty — CI runners skip this)
#   5. ``hostname -f`` auto-detect (last-resort fallback)
#
# When CORS_ORIGINS contains multiple comma-separated hosts, the first
# becomes the cert CN and all are added as SANs.
#
# Production: replace ./certs/cert.pem + key.pem with files from your
# real CA / Let's Encrypt afterwards. Same paths, same nginx config —
# only the issuer differs.

set -euo pipefail

# Resolve the script's own absolute path BEFORE cd-ing so --help can
# still find this file when the script is invoked relatively from
# elsewhere in the tree.
SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"

# Run from repo root regardless of where the caller is. Prefer git's
# answer; fall back to two-up from the script's own location
# (tools/install/<script> → repo root).
if repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  cd "$repo_root"
else
  cd "$(dirname "$SELF")/../.."
fi

# ── Args ─────────────────────────────────────────────────────────────────
force=false
fqdn=""
for arg in "$@"; do
  case "$arg" in
    --force|-f) force=true ;;
    --help|-h)
      sed -n '2,/^$/p' "$SELF" | sed 's/^# \?//'
      exit 0 ;;
    -*)
      echo "Unknown flag: $arg" >&2
      exit 2 ;;
    *)
      fqdn="$arg" ;;
  esac
done

mkdir -p certs

# ── Idempotency guard (run BEFORE FQDN resolution so a no-op deploy
#    is completely silent and never prompts) ───────────────────────────────
if [[ -f certs/cert.pem && -f certs/key.pem && "$force" == "false" ]]; then
  echo "✓ TLS certs already present in ./certs — nothing to do."
  echo "  Pass --force to regenerate (e.g. for a different FQDN)."
  exit 0
fi

# ── FQDN resolution ──────────────────────────────────────────────────────
# Real-world customer deploys typically run ipSolis on a host with its
# own private hostname (e.g. ``linapp01``) but expose it to users under
# a service-specific DNS alias (e.g. ``ipsolis.acme.com``). The cert's
# CN must match the alias, not the host. Five-tier resolution (see header
# docstring); the ``cors_extra_sans`` array also collects additional CORS
# hosts so a multi-host install gets a single multi-SAN cert.
detected="$(hostname -f 2>/dev/null || hostname)"
cors_extra_sans=()

# Tier 3 helper — pull hosts out of CORS_ORIGINS in ./.env. Returns nothing
# when .env is missing, the key is unset, the value is the placeholder, or
# CORS is wide-open (``*``).
_read_cors_hosts() {
  [[ -f .env ]] || return
  local raw
  raw="$(grep -E '^CORS_ORIGINS=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  [[ -z "$raw" || "$raw" == "*" ]] && return
  # Skip the .env.example placeholder — the operator hasn't customised yet.
  [[ "$raw" == *"yourcompany.com"* ]] && return
  local entry host
  IFS=',' read -ra _entries <<< "$raw"
  for entry in "${_entries[@]}"; do
    entry="${entry# }"; entry="${entry% }"
    host="${entry#*://}"           # strip http:// or https://
    host="${host%%[:/]*}"          # strip port + path
    [[ -n "$host" ]] && echo "$host"
  done
}

if [[ -z "$fqdn" ]]; then
  if [[ -n "${IPSOLIS_FQDN:-}" ]]; then
    fqdn="$IPSOLIS_FQDN"
    echo "ℹ Using IPSOLIS_FQDN env: $fqdn"
  else
    # Tier 3: derive from .env. First host becomes the CN, the rest are
    # collected as additional SANs.
    mapfile -t _cors_hosts < <(_read_cors_hosts)
    if (( ${#_cors_hosts[@]} > 0 )); then
      fqdn="${_cors_hosts[0]}"
      cors_extra_sans=("${_cors_hosts[@]:1}")
      if (( ${#cors_extra_sans[@]} > 0 )); then
        echo "ℹ Using FQDN from .env CORS_ORIGINS: $fqdn (+${#cors_extra_sans[@]} additional SAN(s))"
      else
        echo "ℹ Using FQDN from .env CORS_ORIGINS: $fqdn"
      fi
    elif [[ -t 0 ]]; then
      # Interactive — ask the operator. Enter accepts the auto-detect.
      echo ""
      echo "Self-signed TLS cert needed for the nginx reverse proxy."
      echo "Enter the hostname (DNS alias) users will type to reach this install."
      echo "Examples: ipsolis.acme.com, ipsolis-pre.example.local"
      read -r -p "  Hostname [${detected}]: " fqdn
      fqdn="${fqdn:-$detected}"
    else
      fqdn="$detected"
      echo "ℹ Non-interactive run — using auto-detected FQDN: $fqdn"
      echo "  (override via positional arg, IPSOLIS_FQDN env, or CORS_ORIGINS in .env)"
    fi
  fi
fi

# ── Build SubjectAltName list ────────────────────────────────────────────
# Include the FQDN, the short hostname, localhost, and any detectable
# IPv4. Real-world deploys usually add a load-balancer CNAME or two —
# extend ``extra_san`` below or re-run with --force after editing.
short="${fqdn%%.*}"
extra_san=""    # add ",DNS:lb.example.com" etc. here if needed
ips="$(hostname -I 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
san="DNS:$fqdn"
[[ "$short" != "$fqdn" ]] && san+=",DNS:$short"
# Extra SANs from CORS_ORIGINS (Tier 3). De-duped vs the primary FQDN.
for h in "${cors_extra_sans[@]}"; do
  [[ "$h" != "$fqdn" ]] && san+=",DNS:$h"
done
san+=",DNS:localhost,IP:127.0.0.1"
for ip in $ips; do
  san+=",IP:$ip"
done
[[ -n "$extra_san" ]] && san+="$extra_san"

# ── Generate ─────────────────────────────────────────────────────────────
# MSYS_NO_PATHCONV=1 stops Git Bash from path-mangling the leading "/"
# in -subj into "C:/Program Files/Git/CN=...". No-op on Linux/macOS.
MSYS_NO_PATHCONV=1 openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -subj "/CN=$fqdn/O=ipSolis Self-Signed/C=DE" \
  -addext "subjectAltName=$san" \
  -keyout certs/key.pem \
  -out certs/cert.pem 2>/dev/null

chmod 644 certs/cert.pem
chmod 600 certs/key.pem

echo ""
echo "✓ Self-signed TLS cert generated"
echo "    cert: certs/cert.pem"
echo "    key:  certs/key.pem"
echo "    CN:   $fqdn"
echo "    SAN:  $san"
echo "    valid 365 days from today"
echo ""
echo "Next steps:"
echo "  1) Bring up the stack:"
echo "       docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d"
echo "  2) Run migrations (creates the schema on a fresh DB):"
echo "       docker compose exec -T api alembic upgrade head"
echo "  3) First-run wizard:"
echo "       https://$fqdn/ui/login"
echo ""
echo "⚠  Browsers will warn about the self-signed cert until you replace it"
echo "   with one from your real CA / Let's Encrypt. Same path, same nginx"
echo "   config — only the issuer differs."
