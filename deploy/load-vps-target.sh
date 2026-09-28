# load-vps-target.sh — resolve the laptop→VPS SSH target.
#
# Source this from a sync script after REPO_ROOT is set. It sets VPS to
# "${JARVIS_VPS_USER}@${JARVIS_VPS_HOST}". Values come from the environment
# first, then from the gitignored repo .env. Nothing here is a host or secret.
#
#   REPO_ROOT=...
#   source "$REPO_ROOT/deploy/load-vps-target.sh"

if [[ -z "${REPO_ROOT:-}" ]]; then
    echo "REPO_ROOT must be set before sourcing load-vps-target.sh" >&2
    exit 1
fi

_jarvis_dotenv() {
    # Print the value of KEY from a dotenv file. Empty if missing.
    # Accepts optional leading "export" and one layer of matching quotes.
    local key="$1"
    local file="$2"
    local line val
    if [[ ! -f "$file" ]]; then
        return 0
    fi
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1 || true)"
    [[ -n "$line" ]] || return 0
    val="${line#*=}"
    val="${val%$'\r'}"
    if [[ ${#val} -ge 2 && ( ( "$val" == \"*\" && "$val" == *\" ) || ( "$val" == \'*\' && "$val" == *\' ) ) ]]; then
        val="${val:1:${#val}-2}"
    fi
    printf '%s' "$val"
}

if [[ -z "${JARVIS_VPS_USER:-}" ]]; then
    JARVIS_VPS_USER="$(_jarvis_dotenv JARVIS_VPS_USER "$REPO_ROOT/.env")"
fi
if [[ -z "${JARVIS_VPS_HOST:-}" ]]; then
    JARVIS_VPS_HOST="$(_jarvis_dotenv JARVIS_VPS_HOST "$REPO_ROOT/.env")"
fi

if [[ -z "${JARVIS_VPS_USER:-}" || -z "${JARVIS_VPS_HOST:-}" ]]; then
    echo "Set JARVIS_VPS_USER and JARVIS_VPS_HOST in $REPO_ROOT/.env (see .env.example)." >&2
    exit 1
fi

if [[ ! "$JARVIS_VPS_USER" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "JARVIS_VPS_USER contains unsupported characters." >&2
    exit 1
fi
if [[ ! "$JARVIS_VPS_HOST" =~ ^[A-Za-z0-9.:_-]+$ ]]; then
    echo "JARVIS_VPS_HOST contains unsupported characters." >&2
    exit 1
fi

VPS="${JARVIS_VPS_USER}@${JARVIS_VPS_HOST}"
