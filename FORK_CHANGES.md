# Änderungen gegenüber dem Original (grimmpp)

Dieser Fork basiert auf **[grimmpp/home-assistant-eltako](https://github.com/grimmpp/home-assistant-eltako)**,
Stand Tag **`v1.5.10-bugfix-covers`** (Commit `58505a9`, Dezember 2025). Der `main`-Branch
des Originals enthält bereits Inhalte der Version 2.0-alpha, die mit diesem Stand nicht kompatibel sind.

- **Branch:** `baetzmr-patches`
- **Kompletter Diff:** [v1.5.10-bugfix-covers...baetzmr:baetzmr-patches](https://github.com/grimmpp/home-assistant-eltako/compare/v1.5.10-bugfix-covers...baetzmr:baetzmr-patches)
- **Umfang:** 13 Integrationsdateien, davon eine neu, rund +680 / −260 Zeilen (ohne reine Whitespace-Änderungen)
- **Domain bleibt `eltako`:** Eine bestehende YAML-Konfiguration, Config-Entries und Entities werden 1:1 übernommen.

Getestet im Dauerbetrieb mit HA Core 2026.x (Python 3.14), einem **FGW14-USB** (RS485-Bus)
und einem **USB300** (EnOcean-Funk) parallel.

---

## Inhalt

1. [Neue Geräte](#1-neue-geräte)
2. [Rollläden / Jalousien (cover.py)](#2-rollläden--jalousien-coverpy)
3. [Gateway: Stabilität und Thread-Sicherheit](#3-gateway-stabilität-und-thread-sicherheit)
4. [Adressbezogene Event-Verteilung](#4-adressbezogene-event-verteilung)
5. [Kompatibilität mit aktuellen HA-Versionen](#5-kompatibilität-mit-aktuellen-ha-versionen)
6. [Kleinere Fehlerbehebungen](#6-kleinere-fehlerbehebungen)
7. [Bibliotheks-Patches (patch_libraries.sh)](#7-bibliotheks-patches-patch_librariessh)
8. [Verhaltensänderungen, die man kennen sollte](#8-verhaltensänderungen-die-man-kennen-sollte)
9. [Bekannte offene Punkte](#9-bekannte-offene-punkte)
10. [Übersicht nach Datei](#10-übersicht-nach-datei)

---

## 1. Neue Geräte

### AFRISO ASD20 Funk-Rauchmelder (EEP F6-05-02) – *neu*

`eltakobus` kennt das Profil F6-05-02 nicht. Die neue Datei `eep_smoke.py` registriert es als
Ableitung von F6-02-01, denn mechanisch ist es ein RPS-Telegramm. Danach funktioniert
`EEP.find('F6-05-02')`. In `schema.py` ist F6-05-02 als erlaubtes Binary-Sensor-EEP eingetragen.

Pro Rauchmelder entstehen **drei Binary-Sensoren**:

| Entity | device_class | Verhalten |
|---|---|---|
| `<Name> Alarm` | `smoke` | Datenbyte `0x10` schaltet ein. Aus geht der Sensor bei `0x00` oder automatisch nach **30 s** ohne erneutes Alarm-Telegramm. |
| `<Name> Batterie schwach` | `battery` | `0x30` schaltet ein, `0x00` schaltet aus |
| `<Name> Online` | `connectivity` | Jedes Telegramm schaltet ein. Aus geht der Sensor nach **25 h** ohne Telegramm (der Melder sendet regelmäßig ein Lebenszeichen). |

```yaml
binary_sensor:
  - id: ff-xx-xx-xx       # Funk-ID des Rauchmelders
    eep: F6-05-02
    name: Rauchmelder Flur
```

### Piotek-Tracker / Präsenzmelder (EEP A5-07-01) – *neu*

Für A5-07-01-Geräte entstehen jetzt **zwei Entities**:

| Entity | Verhalten |
|---|---|
| `<Name> Anwesenheit` | Ein 4BS-Telegramm (org `0x07`) schaltet ein. Aus geht der Sensor nach **90 s** ohne weiteres Telegramm. Der Timer wird bei jedem Telegramm neu gestartet. |
| `<Name> Button` | Reagiert nur auf RPS-Telegramme (org `0x05`): `0x70` = gedrückt, `0x00` = losgelassen |

Beide Entities verarbeiten Telegramme thread-sicher im HA-Event-Loop (`hass.add_job`).

> ⚠️ Diese Weiche gilt für **alle** Geräte mit `eep: A5-07-01`, siehe [Abschnitt 8](#8-verhaltensänderungen-die-man-kennen-sollte).

---

## 2. Rollläden / Jalousien (cover.py)

Die Cover-Logik wurde für FSB14/FSB61-Aktoren weitgehend neu geschrieben.

**Telegrammbasierte Position statt Interpolation:** Während der Fahrt zeigt die Entity nur
„öffnet“ bzw. „schließt“. Die genaue Prozentposition kommt erst mit dem Stopp-Telegramm des
Aktors (Laufzeit und Richtung) bzw. mit `0x50` (zu) oder `0x70` (auf). Eine Live-Interpolation
wurde getestet und wegen zu großer Ungenauigkeit verworfen.

**Optimistischer Endzustand:** Der Aktor meldet die Endlage erst, wenn die gesendete Laufzeit
inklusive Sicherheitsaufschlag abgelaufen ist, also einige Sekunden nach dem Erreichen des
Anschlags. Damit „schließt“ nicht so lange stehen bleibt, setzt die Integration den Endzustand
optimistisch, sobald die erwartete Fahrzeit plus 0,5 s vorbei ist. Das reale Telegramm kommt
danach trotzdem und hat das letzte Wort. Ein neuer Befehl, ein Stopp oder ein echtes
Telegramm macht einen noch laufenden Timer wirkungslos. Dafür sorgt eine Sequenznummer, die
ohne Thread-übergreifendes Abbrechen auskommt. Das funktioniert auch bei Fahrten, die über
einen Wandtaster gestartet wurden.

**Dynamische Laufzeit:** Statt immer `time_opens + 1` wird nur die Restfahrzeit von der
aktuellen Position bis zum Anschlag gesendet, plus ein Sicherheitsaufschlag von mindestens 3 s
bzw. 10 % der Gesamtlaufzeit. Die Obergrenze bleibt `time_opens + 1`.

**Weitere Änderungen:**
- Die Zielpositionen 0 % und 100 % laufen über `open_cover` / `close_cover`, inklusive
  optimistischem Endzustand.
- Ist die Position unbekannt, fährt der Rollladen ganz auf oder zu, statt mit `None` zu rechnen
  (vorher ein `TypeError`).
- Das Stopp-Telegramm setzt den Fahrzustand immer zurück, auch wenn keine Laufzeiten konfiguriert sind.
- Beim Wiederherstellen nach einem Neustart bleibt die gespeicherte Position erhalten. Vorher
  wurde sie hart auf 0 oder 100 gesetzt. Ein beim Neustart hängengebliebenes „öffnet/schließt“
  wird zurückgesetzt.
- Neigung (Tilt): `set_cover_tilt_position` ist jetzt async und nutzt `asyncio.sleep` statt
  `time.sleep`. Vorher wurde ein Executor-Thread blockiert. Die Tilt-Position wird nur noch
  gesetzt, wenn `time_tilts` konfiguriert ist.
- Neue Attribute: `measured_time_opens` / `measured_time_closes` (die Fahrzeit der letzten
  Fahrt laut Aktor-Telegramm) sowie `configured_time_opens` / `configured_time_closes`.
- `open_cover`, `close_cover` und `stop_cover` setzen den Fahrzustand jetzt **immer**, nicht
  nur bei `fast_status_change: true`.

---

## 3. Gateway: Stabilität und Thread-Sicherheit

**Kein Absturz des Empfangs-Threads mehr:** `_callback_receive_message_from_serial_bus` ist
komplett in `try/except` gekapselt. Telegramme ohne Adresse (ACKs, Base-ID-Antworten und
andere reine `ESP2Message`-Objekte) werden per `getattr(message, "address", None)` übersprungen.
Vorher brachte ein solches Telegramm mit einem `AttributeError` den USB300-Lesethread zum
Absturz, und das Gateway war bis zum Neustart taub.

**Thread-sicherer Zugriff auf den Event-Loop:** Die Callbacks der seriellen Schnittstelle
laufen in einem eigenen Thread. Alle Aufrufe in den HA-Event-Loop laufen jetzt über
`asyncio.run_coroutine_threadsafe(...)` bzw. `loop.call_soon_threadsafe(...)` statt
`hass.create_task(...)`. Das betrifft Senden, Nachrichtenzähler, „letzte Nachricht“ und den
Verbindungsstatus. Neuere HA-Versionen werten `create_task` aus fremden Threads als Fehler.

**Nicht blockierender Start und Entladen:**
- Der Bus wird nicht mehr im Konstruktor initialisiert, sondern in `async_setup()` über
  `hass.async_add_executor_job(self._init_bus)`. Das Öffnen der seriellen Schnittstelle
  blockiert den Event-Loop nicht mehr.
- `unload()` ruft kein `bus.join()` mehr auf. Das blockierte den Event-Loop beim Entladen
  und beim Herunterfahren.
- `async_unload_entry` entlädt zuerst sauber alle Plattformen (`async_unload_platforms`) und
  stoppt erst danach das Gateway.
- Der Reconnect-Button führt `gateway.reconnect()` im Executor aus.

**Senden robuster:** Ein Fehler beim Senden verwirft nur das betroffene Telegramm und loggt
es. `msg.serialize().hex()` wird nur berechnet, wenn Debug-Logging aktiv ist.

---

## 4. Adressbezogene Event-Verteilung

Im Original ging **jedes** empfangene Telegramm per Dispatcher an **alle** Entities eines
Gateways, und jede Entity prüfte selbst, ob die Adresse zu ihr passt. Bei großen
Installationen sind das tausende Callbacks pro Minute.

Jetzt verteilt das Gateway Telegramme auf ein adressbezogenes Signal
(`<signal>.<adresse>`). Jede Entity registriert sich nur für ihre eigenen
`listen_to_addresses`. Die Adressprüfung in `_message_received_callback` entfällt.

Zusätzlich: `should_poll = False` für alle Eltako-Entities. Die Integration ist rein
push-basiert, das Polling war wirkungslos und kostete nur Rechenzeit.

Betroffen sind `gateway.py`, `device.py` und `climate.py` (Klima-Callback unterscheidet jetzt
sauber zwischen Aktor und Thermostat).

---

## 5. Kompatibilität mit aktuellen HA-Versionen

- **`via_device` → `via_device_id`:** HA meldet `via_device=(DOMAIN, identifier)` als
  veraltet (wird ab 2027.8 entfernt). Das Gateway merkt sich jetzt seine eigene Device-ID aus
  `async_get_or_create` (neue Property `gateway.device_id`). Die Geräte verweisen per
  `via_device_id` darauf. Bei den Gateway-eigenen Entities (Verbindungsstatus, Zähler,
  Info-Felder, Reconnect-Button) wurde der Selbstverweis entfernt.
- **`LOGGER.warn` → `LOGGER.warning`** in allen Dateien (`warn` ist veraltet). Dabei wurden
  einige kaputte Log-Meldungen repariert, z. B. f-Strings ohne `f` oder `%s` ohne Argument.
- **Klima-Controller:** Der Update-Task wird nicht mehr im Konstruktor mit
  `asyncio.ensure_future` gestartet, sondern in `async_added_to_hass`. Beim Entfernen der
  Entity wird er sauber abgebrochen (`async_will_remove_from_hass`).
- **Nachrichtenzähler (`Received Messages per Session`):** StateClass `TOTAL_INCREASING` →
  `MEASUREMENT`, ungültige Unit-Felder wurden entfernt. Der Zähler beginnt pro Sitzung bei 0,
  was mit `TOTAL_INCREASING` zu falschen Statistiken führte.

---

## 6. Kleinere Fehlerbehebungen

- **`config_helpers.py`:** Veränderliche Standardargumente (`=[]`) durch `None` ersetzt.
  `general_settings` wird jetzt auf eine Kopie der Standardwerte gemergt, statt sie zu ersetzen
  oder zu verändern. Fehlende Schlüssel in der Konfiguration führen so nicht mehr zu einem `KeyError`.
- **Validierung:** Gateway-eigene Entities (Reconnect-Button, Info-Sensoren) werden nicht mehr
  als Aktoren validiert. Das hatte falsche Warnungen „wrong sender/device id“ erzeugt.
  `dev_id_validation_by_transmitter` akzeptiert bei Transceivern jede ID.
- **Licht:** Fehlt beim Wiederherstellen der Helligkeitswert, wird bei eingeschaltetem Licht
  255 angenommen. Ein Fehler beim Wiederherstellen bricht den Start der Entity nicht mehr ab.
  Dimmer-Telegramme mit unerwartetem `org` werden ignoriert, statt Fehler auszulösen.
- **Binary-Sensor:** Ein Fehler beim Debug-Logging (`json.dumps` auf nicht serialisierbare
  Werte) verwirft kein erfolgreich dekodiertes Telegramm mehr.
- **Verbindungsstatus-Sensor:** Registriert keinen unnötigen Bus-Listener mehr und stellt nur
  den letzten Zustand wieder her.

---

## 7. Bibliotheks-Patches (patch_libraries.sh)

Einige Fehler liegen nicht in der Integration, sondern in den von HA installierten
Bibliotheken `eltakobus`, `esp2_gateway_adapter` und `enocean`. HACS kann sie nicht
ausliefern, und HA setzt sie bei jedem Core-Update zurück. Das Skript
[`scripts/patch_libraries.sh`](scripts/patch_libraries.sh) spielt sie automatisch wieder ein:

| Fix | Bibliothek | Wirkung |
|---|---|---|
| 1 | eltakobus | Behebt den **Busy-Loop**, der dauerhaft hohe CPU-Last erzeugt (10 µs → 10 ms Sleep) |
| 2 | esp2_gateway_adapter | `hasattr`-Guard: **USB300-Empfangsthread stürzt nicht mehr ab** |
| 3 | enocean | Unterdrückt die `XMLParsedAsHTMLWarning` (kosmetisch) |
| 4 | eltakobus | `write_timeout` 0,1 s → 0,5 s, **weniger Sende-Timeouts** |
| 5 | – | Optional, nur ohne HACS: stellt Integrationsdateien aus Master-Kopien wieder her (Standard: aus) |

Einrichtung, Voraussetzungen und Ablauf nach einem Core-Update: **[scripts/README.md](scripts/README.md)**

> **Empfehlung:** Ohne Fix 1 und Fix 2 laufen USB300-Gateways instabil, und die CPU-Last ist
> deutlich erhöht. Das Skript sollte deshalb bei jeder Installation dieses Forks eingerichtet werden.

---

## 8. Verhaltensänderungen, die man kennen sollte

- **A5-07-01 wird immer als Piotek-Tracker behandelt.** Jedes Gerät mit `eep: A5-07-01` bekommt
  die zwei Entities „Anwesenheit“ (90-s-Auto-Off) und „Button“ statt des bisherigen einzelnen
  Occupancy-Sensors. Für andere Präsenzmelder mit diesem EEP passt das unter Umständen nicht.
  Die Entity-IDs ändern sich, und die alte Entity wird verwaist.
- **Cover-Fahrzustand unabhängig von `fast_status_change`:** Auf/Zu/Stopp setzen „öffnet“ bzw.
  „schließt“ sofort, auch bei `fast_status_change: false`. Nur Zwischenpositionen und Tilt
  richten sich weiter nach der Einstellung.
- **Rauchmelder-Entities** werden auf Deutsch benannt („Alarm“, „Batterie schwach“, „Online“),
  ebenso die Tracker-Entities.
- Einige neue Log-Meldungen sind auf Deutsch.

---

## 9. Bekannte offene Punkte

- **Shutdown-Race:** Trifft beim Herunterfahren von HA noch ein spätes Telegramm vom USB300
  ein, kann im Log `RuntimeError: Event loop is closed` erscheinen. Das ist funktional
  harmlos. Die Abhilfe (`loop.is_closed()`-Guard im Empfangs-Callback und in
  `_fire_connection_state_changed_event`) ist noch **nicht** enthalten.
- **FWS61-Wetterstation:** Gelegentlich erscheint „Could not decode message“ in mehreren
  Meldungen kurz hintereinander. Das Telegramm wird übersprungen, sonst hat es keine Auswirkungen.
- **FSB14-Endlage:** Der Aktor sendet das Endlagen-Telegramm etwa 4 s nach Erreichen des
  Anschlags. Das ist Hardware-Verhalten. Der optimistische Endzustand überbrückt diese Zeit.
- Die Bibliotheks-Patches sollten langfristig in Forks der Bibliotheken wandern, siehe
  [scripts/README.md](scripts/README.md#langfristig).

---

## 10. Übersicht nach Datei

| Datei | Änderung |
|---|---|
| `eep_smoke.py` | **neu**: registriert EEP F6-05-02 |
| `binary_sensor.py` | ASD20-Rauchmelder, Piotek-Tracker, robustes Debug-Logging, `via_device` entfernt |
| `cover.py` | telegrammbasierte Position, optimistischer Endzustand, dynamische Laufzeit, async Tilt, Mess-Attribute |
| `gateway.py` | Empfangs-Thread-Schutz, adressbezogenes Dispatching, thread-sichere Loop-Aufrufe, nicht blockierender Start und Unload, `device_id` |
| `device.py` | adressbezogene Listener, `should_poll=False`, `via_device_id` |
| `sensor.py` | Validierung ohne Gateway-Entities, StateClass Nachrichtenzähler, `via_device` entfernt |
| `climate.py` | Update-Task im HA-Lebenszyklus, Aktor/Thermostat-Unterscheidung |
| `light.py` | Helligkeit beim Wiederherstellen, unerwartetes `org` ignorieren, `warning` |
| `button.py` | Reconnect im Executor, kein Bus-Listener, keine Validierung |
| `eltako_integration_init.py` | Plattformen sauber entladen, Log-Meldungen repariert |
| `config_helpers.py` | veränderliche Standardargumente, Merge der `general_settings` |
| `schema.py` | F6-05-02 als Binary-Sensor-EEP |
| `switch.py` | `warn` → `warning` |
| `manifest.json`, `hacs.json` | eigene Version und Repo-URLs für HACS |
