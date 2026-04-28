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
#   tools/install/bootstrap-certs.sh                       # interactive prompt (or auto-detect on CI)
#   tools/install/bootstrap-certs.sh ipsolis.example.com   # explicit FQDN, no prompt
#   IPSOLIS_FQDN=ipsolis.example.com tools/install/...     # same, via env var
#   tools/install/bootstrap-certs.sh --force ...           # overwrite existing certs
#
# FQDN resolution order:
#   1. positional arg
#   2. IPSOLIS_FQDN env var
#   3. interactive prompt (only when stdin is a tty — CI runners skip this)
#   4. ``hostname -f`` auto-detect (last-resort fallback)
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

# ── FQDN resolution: arg > env > interactive prompt > auto-detect ────────
# Real-world customer deploys typically run ipSolis on a host with its
# own private hostname (e.g. ``linapp01``) but expose it to users under
# a service-specific DNS alias (e.g. ``ipsolis.acme.com``). The cert's
# CN must match the alias, not the host. Tier order:
#
#   1. Explicit positional arg          — for scripted / CI calls
#   2. ``IPSOLIS_FQDN`` env var         — same, without editing the call
#   3. Interactive prompt (tty stdin)   — for operators on the box
#   4. ``hostname -f`` auto-detect      — last-resort fallback
#
# CI runners (GitHub Actions, etc.) have no tty so step 3 is skipped
# and they fall through to auto-detect — which is also why workflows
# should pass ``IPSOLIS_FQDN`` via a secret when the host's FQDN
# doesn't match the public DNS alias.
detected="$(hostname -f 2>/dev/null || hostname)"
if [[ -z "$fqdn" ]]; then
  if [[ -n "${IPSOLIS_FQDN:-}" ]]; then
    fqdn="$IPSOLIS_FQDN"
    echo "ℹ Using IPSOLIS_FQDN env: $fqdn"
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
    echo "  (override via positional arg or IPSOLIS_FQDN env)"
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
