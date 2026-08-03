#!/usr/bin/env bash
set -euo pipefail

SSID="MyHotspot"
DOMAIN="smrcnabet.cz"

APP_PORT=8080
HOTSPOT_NAME="temporary-hotspot"

NM_DNSMASQ_DIR="/etc/NetworkManager/dnsmasq-shared.d"
NM_DNSMASQ_FILE="$NM_DNSMASQ_DIR/hotspot.conf"

HOSTS_BACKUP="/tmp/hosts.backup.$DOMAIN"

IFACE=$(nmcli -t -f DEVICE,TYPE device status \
    | awk -F: '$2=="wifi"{print $1; exit}')

if [[ -z "$IFACE" ]]; then
    echo "No Wi-Fi interface found"
    exit 1
fi

echo "Using Wi-Fi interface: $IFACE"

HOTSPOT_IP=""


cleanup() {
    echo
    echo "Cleaning up..."

    # Remove incoming hotspot port forwarding
    sudo iptables -t nat -D PREROUTING \
        -i "$IFACE" \
        -p tcp \
        --dport 80 \
        -j REDIRECT \
        --to-ports "$APP_PORT" \
        2>/dev/null || true

    # Remove local machine port forwarding
    if [[ -n "$HOTSPOT_IP" ]]; then
        sudo iptables -t nat -D OUTPUT \
            -p tcp \
            -d "$HOTSPOT_IP" \
            --dport 80 \
            -j REDIRECT \
            --to-ports "$APP_PORT" \
            2>/dev/null || true
    fi


    # Restore hosts file
    if [[ -f "$HOSTS_BACKUP" ]]; then
        sudo cp "$HOSTS_BACKUP" /etc/hosts
        sudo rm -f "$HOSTS_BACKUP"
    fi


    # Remove NetworkManager DNS override
    sudo rm -f "$NM_DNSMASQ_FILE"


    # Remove hotspot
    nmcli connection down "$HOTSPOT_NAME" 2>/dev/null || true
    nmcli connection delete "$HOTSPOT_NAME" 2>/dev/null || true


    sudo systemctl restart NetworkManager

    SNAPSHOT=db-snapshots/betting.db.$(date +"%Y-%m-%d_%H%M%N")
    cp db/betting.db $SNAPSHOT
    echo "Saved db snapshot to $SNAPSHOT"


    echo "Done."
}

trap cleanup EXIT


echo "Configuring NetworkManager DNS..."

sudo mkdir -p "$NM_DNSMASQ_DIR"

echo "address=/$DOMAIN/10.42.0.1" | \
    sudo tee "$NM_DNSMASQ_FILE" >/dev/null

sudo systemctl restart NetworkManager


echo "Creating hotspot..."

nmcli connection add \
    type wifi \
    ifname "$IFACE" \
    con-name "$HOTSPOT_NAME" \
    ssid "$SSID"


nmcli connection modify "$HOTSPOT_NAME" \
    connection.interface-name "$IFACE"


nmcli connection modify "$HOTSPOT_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared


nmcli connection up "$HOTSPOT_NAME"


echo "Waiting for hotspot IP..."
sleep 3


HOTSPOT_IP=$(ip -4 addr show "$IFACE" \
    | awk '/inet / {print $2}' \
    | cut -d/ -f1 \
    | head -n1)


if [[ -z "$HOTSPOT_IP" ]]; then
    echo "Could not determine hotspot IP"
    exit 1
fi


echo "Hotspot IP: $HOTSPOT_IP"


echo "Updating DNS override..."

echo "address=/$DOMAIN/$HOTSPOT_IP" | \
    sudo tee "$NM_DNSMASQ_FILE" >/dev/null


echo "Adding local hosts entry..."

sudo cp /etc/hosts "$HOSTS_BACKUP"

if ! grep -qE "[[:space:]]$DOMAIN([[:space:]]|$)" /etc/hosts; then
    echo "$HOTSPOT_IP $DOMAIN" | sudo tee -a /etc/hosts >/dev/null
fi


echo "Forwarding hotspot clients: port 80 -> $APP_PORT"

sudo iptables -t nat -A PREROUTING \
    -i "$IFACE" \
    -p tcp \
    --dport 80 \
    -j REDIRECT \
    --to-ports "$APP_PORT"


echo "Forwarding host PC: port 80 -> $APP_PORT"

sudo iptables -t nat -A OUTPUT \
    -p tcp \
    -d "$HOTSPOT_IP" \
    --dport 80 \
    -j REDIRECT \
    --to-ports "$APP_PORT"


echo
echo "======================================"
echo "Hotspot: $SSID"
echo "URL:      http://$DOMAIN"
echo "Target:   $HOTSPOT_IP:$APP_PORT"
echo "======================================"
echo


##################################################
# Your application
##################################################

source .venv/bin/activate
python main.py --port $APP_PORT
# python3 -m http.server "$APP_PORT"
