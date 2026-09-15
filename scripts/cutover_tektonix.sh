#!/usr/bin/env bash
# Cut the dashboard over from agent.3dcryptobots.com/v2/ to tektonix.io.
#
# Safe to run more than once. Refuses to do anything until DNS actually
# resolves to this box, because every step after that depends on a cert that
# Let's Encrypt cannot issue otherwise.
#
# What it does, in order:
#   1. verify tektonix.io and www resolve to THIS machine
#   2. issue the cert over the ACME webroot (HTTP-only block, already live)
#   3. enable the real vhost and reload
#   4. flip the frontend base path from /v2/ to / and rebuild
#   5. point canonical/og URLs at the new domain
#   6. redirect the OLD domain's / and /v2/ here, leaving /_login, /_review
#      and /_auth_verify alone -- a blanket redirect would kill both tools
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN=tektonix.io
WWW=www.tektonix.io
OLD_VHOST=/etc/nginx/sites-enabled/agent-3dcryptobots
EMAIL="${CERTBOT_EMAIL:-danny@3dcryptobots.com}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1. checking DNS"
MYIP="$(curl -fsS -4 ifconfig.me)"
for host in "$DOMAIN" "$WWW"; do
    got="$(dig +short "$host" A | tail -1)"
    if [ -z "$got" ]; then
        echo "FAIL: $host does not resolve yet (NXDOMAIN or no A record)." >&2
        echo "      Set an A record for @ and www to $MYIP in Namecheap's" >&2
        echo "      Advanced DNS tab, then re-run this script." >&2
        exit 1
    fi
    if [ "$got" != "$MYIP" ]; then
        echo "FAIL: $host resolves to $got, but this box is $MYIP." >&2
        exit 1
    fi
    echo "  ok: $host -> $got"
done

say "2. issuing the certificate"
if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    echo "  cert already present; skipping"
else
    # The HTTP-only block in sites-available/tektonix serves the challenge, so
    # it has to be reachable before certonly runs. Link it without the TLS
    # blocks by letting nginx fail soft: we link AFTER the cert exists, and
    # until then the default vhost's webroot answers the challenge.
    certbot certonly --webroot -w /var/www/html \
        -d "$DOMAIN" -d "$WWW" \
        --non-interactive --agree-tos -m "$EMAIL"
fi

say "3. enabling the vhost"
ln -sfn /etc/nginx/sites-available/tektonix /etc/nginx/sites-enabled/tektonix
nginx -t
systemctl reload nginx
echo "  enabled"

say "4. frontend base path -> /"
cd "$REPO/frontend"
if grep -q "base: command === 'build' ? '/v2/' : '/'," vite.config.ts; then
    sed -i "s|base: command === 'build' ? '/v2/' : '/',|base: '/',|" vite.config.ts
    echo "  vite base flipped"
else
    echo "  vite base already /"
fi

say "5. canonical + og URLs"
sed -i "s|https://agent.3dcryptobots.com/v2/|https://$DOMAIN/|g" index.html
npm run build

say "6. redirecting the old domain"
# Surgical, not blanket -- see scripts/_cutover_rewrite_vhost.py for why.
python3 "$REPO/scripts/_cutover_rewrite_vhost.py" "$OLD_VHOST"
nginx -t && systemctl reload nginx

say "done"
echo "  https://$DOMAIN now serves the dashboard."
echo "  /_login and /_review on the old domain are untouched."
echo "  NOTE: the session cookie is per-origin, so you will be logged out"
echo "        and 2FA re-enrolls against the new domain."
