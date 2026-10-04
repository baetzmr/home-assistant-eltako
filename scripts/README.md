# patch_libraries.sh – Bibliotheks-Patches

Die Eltako-Integration nutzt drei Python-Bibliotheken, die Home Assistant anhand der
`requirements` in `manifest.json` selbst installiert:

| Bibliothek | Version | Wofür |
|---|---|---|
| `eltako14bus` (Modul `eltakobus`) | 0.0.61 | RS485-Kommunikation mit dem FGW14-USB / Eltako-Bus |
| `esp2-gateway-adapter` | 0.2.15 | ESP3-Gateways wie USB300 (übersetzt ESP3 ↔ ESP2) |
| `enocean` | ≥ 0.60.1 | EEP-Definitionen |

Einige Fehler sitzen in diesen Bibliotheken und nicht in der Integration selbst. HACS
kann sie deshalb nicht mit ausliefern. Außerdem installiert HA die Bibliotheken bei
**jedem Core-Update neu**, wodurch manuelle Änderungen verloren gehen.
`patch_libraries.sh` wendet die Korrekturen automatisch wieder an.

## Was das Skript ändert

| Fix | Datei | Änderung | Warum |
|---|---|---|---|
| 1 | `eltakobus/serial.py` | `sleep(.00001)` → `sleep(0.01)` (sync + async) | Die Lese-Schleife pollt die serielle Schnittstelle alle 10 µs und erzeugt so **dauerhaft hohe CPU-Last**. 10 ms reichen für den Bus. |
| 2 | `esp2_gateway_adapter/esp3_serial_com.py` | `hasattr(packet, "response")`-Guard vor zwei `elif`-Zweigen | Pakete ohne `.response` lösen einen `AttributeError` aus. Danach ist der **USB300-Empfangsthread tot** und das Gateway empfängt nichts mehr. |
| 3 | `enocean/protocol/eep.py` | `XMLParsedAsHTMLWarning` wird unterdrückt | Rein kosmetisch, entfernt eine wiederkehrende Warnung im Log. |
| 4 | `eltakobus/serial.py` | `write_timeout=0.1` → `0.5` | Bei vielen Telegrammen kurz hintereinander (z. B. Lichtgruppen) laufen **Schreibvorgänge sonst in Timeouts**. |
| 5 | *optional, Standard aus* | Integrationsdateien aus `/config/eltako_patches/` wiederherstellen | Nur für Installationen **ohne HACS**. Siehe Warnung unten. |

Das Skript ist **idempotent**: Schon gepatchte Stellen erkennt es und überspringt sie.
Ist eine Zieldatei nicht vorhanden (z. B. weil eine Bibliothek in einer neuen Version
umgebaut wurde), meldet es einen Fehler, statt still „bereits gepatcht“ auszugeben.

> ⚠️ **Fix 5 und HACS:** Wer die Integration über HACS installiert, bekommt die gepatchten
> Integrationsdateien mit dem Release. Fix 5 muss dann **aus** bleiben (Standard). Sonst
> überschreibt das Skript nach jedem HACS-Update die neuen Dateien mit den alten
> Master-Kopien. Aktivieren nur bei manueller Installation:
> `RESTORE_INTEGRATION=1 bash /config/patch_libraries.sh`

## Voraussetzungen

- Home Assistant OS oder Supervised (getestet mit HAOS, HA Core 2026.x, Python 3.14)
- Die Eltako-Integration ist installiert, damit die drei Bibliotheken vorhanden sind
- Das Skript liegt unter `/config/patch_libraries.sh` und ist ausführbar:
  ```bash
  chmod +x /config/patch_libraries.sh
  ```
- Es läuft **innerhalb des Home-Assistant-Containers**. `shell_command` erfüllt das
  automatisch. Über das SSH-Add-on: `docker exec homeassistant bash /config/patch_libraries.sh`
  (das braucht das „Advanced SSH & Web Terminal“-Add-on mit deaktiviertem Protection Mode).
  Auf dem Proxmox- oder HAOS-Host findet `python3` die falschen site-packages.
- Verwendet werden nur `bash`, `python3`, `grep`, `sed`, `cmp` und `cp`. Alles davon ist
  im HA-Container vorhanden.

## Einrichtung: automatisch bei jedem HA-Start

`configuration.yaml`:
```yaml
shell_command:
  eltako_patch_libraries: "bash /config/patch_libraries.sh"
```

Automation:
```yaml
- id: eltako_patch_libraries_on_start
  alias: Eltako – Bibliotheks-Patches nach HA-Start
  triggers:
    - trigger: homeassistant
      event: start
  actions:
    - action: shell_command.eltako_patch_libraries
  mode: single
```

## Ablauf nach einem HA-Core-Update

1. HA startet mit den frisch installierten, **ungepatchten** Bibliotheken.
2. Die Automation führt das Skript aus, und es patcht die Dateien.
3. Python hat die Bibliotheken zu diesem Zeitpunkt schon geladen. Die Patches greifen
   deshalb erst **nach einem weiteren Neustart**:
   ```bash
   ha core restart
   ```
4. Ergebnis prüfen in `/config/patch_run.log`. Beim zweiten Lauf sollten alle Fixes
   „bereits gepatcht“ melden.

Bleibt die CPU-Last danach hoch, hilft ein kompletter Neustart der VM bzw. des Hosts.

## Log

Jeder Lauf überschreibt `/config/patch_run.log`. Ein anderer Pfad lässt sich über die
Variable `LOG_FILE` setzen.

```
=== Eltako Patches 2026-10-04 08:15:02 ===
site-packages: /usr/local/lib/python3.14/site-packages
✓ eltakobus serial.py Busy-Loop gepatcht
✓ esp2_gateway_adapter esp3_serial_com.py gepatcht
- enocean eep.py bereits gepatcht
✓ eltakobus serial.py write_timeout erhöht
- Fix 5 (Integration aus Master wiederherstellen) deaktiviert – Integration kommt über HACS
=== Patches abgeschlossen ===
>>> Es wurden Dateien geändert – sie greifen erst nach einem weiteren 'ha core restart'!
```

Exit-Code `0` bedeutet OK, `1` bedeutet, dass mindestens ein Patch nicht angewendet werden konnte.

## Langfristig

Sauberer wäre es, `eltako14bus` und `esp2-gateway-adapter` zu forken, die Fixes dort
einzubauen und in `manifest.json` per `requirements` auf die gepatchten Versionen zu
verweisen. Dann wäre dieses Skript überflüssig.
