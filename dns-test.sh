#!/usr/bin/env zsh

domains=(
    google.com bbc.co.uk reddit.com amazon.com github.com
    wikipedia.org apple.com microsoft.com netflix.com spotify.com
    twitter.com linkedin.com instagram.com nytimes.com cnn.com
    yahoo.com ebay.com paypal.com stackoverflow.com dropbox.com
    salesforce.com adobe.com oracle.com ibm.com intel.com
    samsung.com sony.com nike.com airbnb.com uber.com
    slack.com zoom.us shopify.com wordpress.com wikimedia.org
    mozilla.org cloudflare.com digitalocean.com heroku.com atlassian.com
    trello.com notion.so figma.com canva.com twitch.tv
    pinterest.com tumblr.com quora.com medium.com etsy.com
    commbank.com.au anz.com.au westpac.com.au nab.com.au telstra.com.au
    optus.com.au woolworths.com.au coles.com.au news.com.au realestate.com.au
)

typeset -A dns_providers
dns_providers=(
    OpenDNS    "208.67.222.222 208.67.220.220"
    Cloudflare "1.1.1.1 1.0.0.1"
    Google     "8.8.8.8 8.8.4.4"
    Quad9      "9.9.9.9 149.112.112.112"
    ISP        "61.9.134.49 61.9.133.193"
)

order=("OpenDNS" "Cloudflare" "Google" "ISP")

# Tunables
delay=0.8          # seconds to sleep between queries — raise this if you suspect rate-limiting
dig_timeout=1      # seconds dig waits for a reply before giving up
dig_tries=1        # number of attempts per query (1 = no retry, so a dropped packet reports as a miss, not a multi-second spike)

for name in "${order[@]}"; do
    servers="${dns_providers[$name]}"
    times=()

    for server in ${=servers}; do
        for d in "${domains[@]}"; do
            t=$(dig +timeout="$dig_timeout" +tries="$dig_tries" @"$server" "$d" | grep "Query time:" | awk '{print $4}')
            t="${t:-0}"
            times+=("$t")
            if (( t > 200 )); then
                echo "  [slow] $name $server $d -> ${t}ms" >&2
            fi
            sleep "$delay"
        done
    done

    stats=$(echo "${times[@]}" | tr ' ' '\n' | sort -n | awk '
        {
            a[NR] = $1
            sum += $1
        }
        END {
            n = NR
            mean = sum / n
            median = (n % 2) ? a[(n+1)/2] : (a[n/2] + a[n/2+1]) / 2

            # Nearest-rank percentile: index = ceil(p/100 * n), clamped to [1, n]
            p80_idx = int((80/100) * n + 0.999999)
            p95_idx = int((95/100) * n + 0.999999)
            p98_idx = int((98/100) * n + 0.999999)
            if (p80_idx < 1) p80_idx = 1; if (p80_idx > n) p80_idx = n
            if (p95_idx < 1) p95_idx = 1; if (p95_idx > n) p95_idx = n
            if (p98_idx < 1) p98_idx = 1; if (p98_idx > n) p98_idx = n

            printf "mean=%.1f median=%.1f p80=%d p95=%d p98=%d min=%d max=%d n=%d",
                mean, median, a[p80_idx], a[p95_idx], a[p98_idx], a[1], a[n], n
        }')
    echo "$name: $stats"
done