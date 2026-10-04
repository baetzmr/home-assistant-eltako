#!/bin/bash
# =============================================================================
# Eltako Integration (baetzmr fork) – Bibliotheks-Patches
# =============================================================================
# Patcht drei Python-Bibliotheken, die Home Assistant für die Eltako-
# Integration installiert (eltakobus, esp2_gateway_adapter, enocean).
# Diese Bibliotheken liegen NICHT im Integrationsordner und werden bei jedem
# HA-Core-Update neu installiert – die Patches gehen dabei verloren.
# Das Skript ist idempotent: bereits gepatchte Stellen werden erkannt und
# übersprungen, es kann also beliebig oft (z.B. bei jedem HA-Start) laufen.
#
# Muss INNERHALB des Home-Assistant-Containers laufen (shell_command, SSH-
# Add-on mit "docker exec homeassistant ..." o.ä.), NICHT auf dem Proxmox-
# oder HAOS-Host – dort findet python3 die falschen site-packages.
#
# Doku: scripts/README.md
# =============================================================================

LOG_FILE="${LOG_FILE:-/config/patch_run.log}"
exec > "$LOG_FILE" 2>&1

# Fix 5 (Integrationsdateien aus Master-Kopien wiederherstellen) ist nur für
# Installationen OHNE HACS gedacht. Bei Installation über HACS liefert das
# Release die gepatchten Integrationsdateien bereits mit – dann MUSS Fix 5
# aus bleiben, sonst werden HACS-Updates durch alte Master-Kopien überschrieben.
RESTORE_INTEGRATION="${RESTORE_INTEGRATION:-0}"

echo "=== Eltako Patches $(date '+%Y-%m-%d %H:%M:%S') ==="

# site-packages dynamisch ermitteln (übersteht Python-Versionswechsel bei Core-Updates)
SP=$(python3 -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)
[ -z "$SP" ] && SP="/usr/local/lib/python3.14/site-packages"
echo "site-packages: $SP"

SERIAL_PY="$SP/eltakobus/serial.py"
ESP3_PY="$SP/esp2_gateway_adapter/esp3_serial_com.py"
EEP_PY="$SP/enocean/protocol/eep.py"

errors=0
restart_needed=0

# Prüft, ob eine Zieldatei existiert; sonst Fehler melden statt "bereits gepatcht"
require_file() {
    if [ ! -f "$1" ]; then
        echo "!!! Datei nicht gefunden: $1 – Patch NICHT angewendet"
        errors=1
        return 1
    fi
    return 0
}

# Fix 1: eltakobus – Busy-Loop (sync + async)
# Die Lese-Schleife schläft nur 10 µs zwischen zwei Abfragen der seriellen
# Schnittstelle und erzeugt so dauerhaft hohe CPU-Last. 10 ms reichen für
# den RS485-Bus völlig aus.
if require_file "$SERIAL_PY"; then
    if grep -qF "time.sleep(.00001)" "$SERIAL_PY" || grep -qF "asyncio.sleep(.00001)" "$SERIAL_PY"; then
        sed -i 's/time\.sleep(\.00001)/time.sleep(0.01)/g; s/asyncio\.sleep(\.00001)/asyncio.sleep(0.01)/g' "$SERIAL_PY"
        echo "✓ eltakobus serial.py Busy-Loop gepatcht"
        restart_needed=1
    else
        echo "- eltakobus serial.py Busy-Loop bereits gepatcht"
    fi
fi

# Fix 2: esp2_gateway_adapter – hasattr-Guard (USB300 / ESP3)
# Nicht jedes ESP3-Paket hat ein .response-Attribut. Ohne Guard wirft der
# Empfangs-Thread einen AttributeError und das USB300-Gateway empfängt nichts mehr.
if require_file "$ESP3_PY"; then
    patched=0
    for n in 4 32; do
        old="elif packet.response == RETURN_CODE.OK and len(packet.response_data) == $n:"
        if grep -qF "$old" "$ESP3_PY"; then
            sed -i "s/elif packet\.response == RETURN_CODE\.OK and len(packet\.response_data) == $n:/elif hasattr(packet, \"response\") and packet.response == RETURN_CODE.OK and len(packet.response_data) == $n:/" "$ESP3_PY"
            patched=1
        fi
    done
    if [ "$patched" -eq 1 ]; then
        echo "✓ esp2_gateway_adapter esp3_serial_com.py gepatcht"
        restart_needed=1
    else
        echo "- esp2_gateway_adapter bereits gepatcht"
    fi
fi

# Fix 3: enocean – XMLParsedAsHTMLWarning unterdrücken
# Rein kosmetisch: verhindert eine Warnung von BeautifulSoup im HA-Log.
if require_file "$EEP_PY"; then
    if grep -q "html.parser" "$EEP_PY" && ! grep -q "XMLParsedAsHTMLWarning" "$EEP_PY"; then
        sed -i 's/^from bs4 import BeautifulSoup$/from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning\nimport warnings\nwarnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)/' "$EEP_PY"
        if grep -q "XMLParsedAsHTMLWarning" "$EEP_PY"; then
            echo "✓ enocean eep.py XMLParsedAsHTMLWarning unterdrückt"
            restart_needed=1
        else
            echo "!!! enocean eep.py: Import-Zeile nicht gefunden – Patch NICHT angewendet"
            errors=1
        fi
    else
        echo "- enocean eep.py bereits gepatcht"
    fi
fi

# Fix 4: eltakobus – write_timeout von 0.1 auf 0.5 Sekunden
# Bei vielen schnell aufeinanderfolgenden Telegrammen (z.B. Lichtgruppen)
# laufen Schreibvorgänge auf den RS485-Bus sonst in Timeouts.
if require_file "$SERIAL_PY"; then
    if grep -qF "write_timeout=0.1" "$SERIAL_PY"; then
        sed -i 's/write_timeout=0\.1\b/write_timeout=0.5/g' "$SERIAL_PY"
        echo "✓ eltakobus serial.py write_timeout erhöht"
        restart_needed=1
    else
        echo "- eltakobus serial.py write_timeout bereits angepasst"
    fi
fi

# Fix 5 (optional, Standard: AUS): Integrationsdateien aus Master-Kopien
# wiederherstellen. Nur für manuelle Installation ohne HACS!
# Aktivieren mit: RESTORE_INTEGRATION=1 bash /config/patch_libraries.sh
INT_DIR="/config/custom_components/eltako"
MASTER_DIR="/config/eltako_patches"
BACKUP_DIR="/config/eltako_patches_backup"

PATCHED_FILES=(
    binary_sensor.py
    button.py
    climate.py
    config_helpers.py
    cover.py
    device.py
    eep_smoke.py
    eltako_integration_init.py
    gateway.py
    light.py
    schema.py
    sensor.py
    switch.py
)

if [ "$RESTORE_INTEGRATION" != "1" ]; then
    echo "- Fix 5 (Integration aus Master wiederherstellen) deaktiviert – Integration kommt über HACS"
elif [ ! -d "$INT_DIR" ]; then
    echo "!!! Integrationsordner fehlt: $INT_DIR – Fix 5 übersprungen"
    errors=1
else
    missing_master=0
    restored=0
    for f in "${PATCHED_FILES[@]}"; do
        master="$MASTER_DIR/$f"
        target="$INT_DIR/$f"

        if [ ! -f "$master" ]; then
            echo "! Master fehlt: $master – bitte einmalig anlegen!"
            missing_master=1
            continue
        fi

        if ! cmp -s "$master" "$target"; then
            # Überschriebene Version sichern, bevor sie ersetzt wird
            if [ -f "$target" ]; then
                mkdir -p "$BACKUP_DIR"
                cp -p "$target" "$BACKUP_DIR/$f.$(date +%Y%m%d-%H%M%S)"
            fi
            if cp "$master" "$target"; then
                echo "✓ $f aus Master wiederhergestellt"
                restored=1
                restart_needed=1
            else
                echo "!!! $f konnte NICHT wiederhergestellt werden"
                errors=1
            fi
        else
            echo "- $f bereits aktuell"
        fi
    done

    if [ "$missing_master" -eq 1 ]; then
        echo "!!! ACHTUNG: Mindestens eine Master-Datei fehlt – Patches unvollständig!"
        errors=1
    fi
    [ "$restored" -eq 0 ] && [ "$missing_master" -eq 0 ] && echo "  (alle ${#PATCHED_FILES[@]} Dateien unverändert)"
fi

echo "=== Patches abgeschlossen ==="

if [ "$restart_needed" -eq 1 ]; then
    echo ">>> Es wurden Dateien geändert – sie greifen erst nach einem weiteren 'ha core restart'!"
fi
if [ "$errors" -eq 1 ]; then
    echo ">>> Es sind Fehler aufgetreten – Log prüfen!"
    exit 1
fi
exit 0
