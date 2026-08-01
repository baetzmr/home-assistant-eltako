"""Support for Eltako covers."""
from __future__ import annotations

from typing import Any
import math

from eltakobus.util import AddressExpression
from eltakobus.eep import *

from homeassistant import config_entries
from homeassistant.components.cover import CoverEntity, CoverEntityFeature, ATTR_POSITION, ATTR_TILT_POSITION
from homeassistant.const import CONF_DEVICE_CLASS, Platform, STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.restore_state import RestoreEntity

from .device import *
from . import config_helpers
from .config_helpers import DeviceConf
from .gateway import EnOceanGateway
from .const import (
    CONF_SENDER,
    CONF_TIME_CLOSES,
    CONF_TIME_OPENS,
    CONF_TIME_TILTS,
    CONF_FAST_STATUS_CHANGE,
    DOMAIN,
    MANUFACTURER,
    LOGGER,
)
from . import get_gateway_from_hass, get_device_config_for_gateway
import asyncio

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Eltako cover platform."""
    gateway: EnOceanGateway = get_gateway_from_hass(hass, config_entry)
    config: ConfigType = get_device_config_for_gateway(hass, config_entry, gateway)

    entities: list[EltakoEntity] = []

    platform = Platform.COVER
    if platform in config:
        for entity_config in config[platform]:

            try:
                dev_conf = DeviceConf(entity_config, [CONF_DEVICE_CLASS, CONF_TIME_CLOSES, CONF_TIME_OPENS, CONF_TIME_TILTS])
                sender_config = config_helpers.get_device_conf(entity_config, CONF_SENDER)

                entities.append(EltakoCover(platform, gateway, dev_conf.id, dev_conf.name, dev_conf.eep,
                                            sender_config.id, sender_config.eep,
                                            dev_conf.get(CONF_DEVICE_CLASS), dev_conf.get(CONF_TIME_CLOSES), dev_conf.get(CONF_TIME_OPENS), dev_conf.get(CONF_TIME_TILTS)))

            except Exception as e:
                LOGGER.warning("[%s] Could not load configuration", platform)
                LOGGER.critical(e, exc_info=True)


    validate_actuators_dev_and_sender_id(entities)
    log_entities_to_be_added(entities, platform)
    async_add_entities(entities)

class EltakoCover(EltakoEntity, CoverEntity, RestoreEntity):
    """Representation of an Eltako cover device.

    Position handling is deliberately telegram-based (no live interpolation):
    during travel the entity only reports opening/closing, and the exact
    percentage is set when the actuator reports a stop / end position.
    """

    # Minimum safety margin (seconds) added when driving to an end position, so
    # the cover reliably reaches the end stop even if the known position drifted.
    # The effective margin also scales with the remaining distance (see below).
    # Overshooting into the end stop is harmless (that is what a full "open" did
    # anyway); the total sent time is bounded to full_time + this margin so the
    # motor never stalls into the end stop longer than that.
    _END_STOP_MARGIN_S = 3

    # Additional margin as a fraction of the FULL travel time, so the end stop
    # is reached even if the known position drifted by up to ~this fraction
    # (e.g. 0.10 covers ~10 percentage points of drift). Scales with cover speed.
    _END_STOP_MARGIN_FRACTION = 0.10

    def __init__(self, platform:str, gateway: EnOceanGateway, dev_id: AddressExpression, dev_name: str, dev_eep: EEP, sender_id: AddressExpression, sender_eep: EEP, device_class: str, time_closes, time_opens, time_tilts):
        """Initialize the Eltako cover device."""
        super().__init__(platform, gateway, dev_id, dev_name, dev_eep)
        self._sender_id = sender_id
        self._sender_eep = sender_eep

        self._attr_device_class = device_class
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = None  # means undefined state
        self._attr_current_cover_position = None
        self._attr_current_cover_tilt_position = None
        self._time_closes = time_closes
        self._time_opens = time_opens
        self._time_tilts = time_tilts

        # True only while a movement is actively in progress. Informational –
        # set when a command is sent or a movement telegram arrives, cleared on
        # a final telegram (time/direction, 0x50, 0x70).
        self._movement_active: bool = False

        # Measured travel times from actual device telegrams (in seconds).
        # Updated after a movement that stops at an intermediate position.
        self._measured_time_closes: float | None = None
        self._measured_time_opens: float | None = None

        self._attr_supported_features = (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP)

        if time_tilts is not None:
            self._attr_supported_features |= CoverEntityFeature.SET_TILT_POSITION

        if time_closes is not None and time_opens is not None:
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION


    @property
    def extra_state_attributes(self) -> dict:
        """Return measured travel times as entity attributes."""
        return {
            "measured_time_closes": self._measured_time_closes,
            "measured_time_opens": self._measured_time_opens,
            "configured_time_closes": self._time_closes,
            "configured_time_opens": self._time_opens,
        }

    def load_value_initially(self, latest_state: State):
        try:
            old_position = latest_state.attributes.get('current_position')
            old_tilt = latest_state.attributes.get('current_tilt_position')

            if latest_state.state == STATE_OPEN:
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = old_position if old_position is not None else 100
                self._attr_current_cover_tilt_position = old_tilt if old_tilt is not None else 100

            elif latest_state.state == STATE_CLOSED:
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = True
                self._attr_current_cover_position = old_position if old_position is not None else 0
                self._attr_current_cover_tilt_position = old_tilt if old_tilt is not None else 0

            elif latest_state.state == STATE_CLOSING:
                # Reset stale closing state from before HA restart
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = old_position
                self._attr_current_cover_tilt_position = old_tilt

            elif latest_state.state == STATE_OPENING:
                # Reset stale opening state from before HA restart
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = old_position
                self._attr_current_cover_tilt_position = old_tilt

        except Exception as e:
            self._attr_current_cover_position = None
            self._attr_current_cover_tilt_position = None
            self._attr_is_opening = None
            self._attr_is_closing = None
            self._attr_is_closed = None

        self.schedule_update_ha_state()
        LOGGER.debug(f"[cover {self.dev_id}] value initially loaded: ["
                     + f"is_opening: {self.is_opening}, "
                     + f"is_closing: {self.is_closing}, "
                     + f"is_closed: {self.is_closed}, "
                     + f"current_position: {self._attr_current_cover_position}, "
                     + f"current_tilt_position: {self._attr_current_cover_tilt_position}, "
                     + f"state: {self.state}]")


    def _open_runtime(self) -> int:
        """Runtime to send for an upward move to the top.

        When the current position is known, send the time needed to reach the
        top plus a safety margin, instead of the full configured time – so the
        actuator reports "fully open" shortly after physically arriving rather
        than at the end of a full-length command. The margin scales with the
        remaining distance (and never drops below _END_STOP_MARGIN_S) so the end
        stop is reached reliably on fast and slow covers alike. The total is
        bounded to full_time + _END_STOP_MARGIN_S to limit end-stop stall.
        Falls back to the full time when the position/time is unknown.
        """
        if self._time_opens is None:
            return 255
        pos = self._attr_current_cover_position
        if pos is None:
            return self._time_opens + 1
        needed = max(0, 100 - pos) / 100.0 * self._time_opens
        margin = max(self._END_STOP_MARGIN_S, self._time_opens * self._END_STOP_MARGIN_FRACTION)
        # Cap at full time + 1: that is the longest any move to the top can need
        # (from the very bottom), so it always reaches the end stop, and it is
        # never more than the old behaviour sent.
        return max(1, min(math.ceil(needed + margin), self._time_opens + 1))

    def _close_runtime(self) -> int:
        """Runtime to send for a downward move to the bottom (see _open_runtime)."""
        if self._time_closes is None:
            return 255
        pos = self._attr_current_cover_position
        if pos is None:
            return self._time_closes + 1
        needed = max(0, pos) / 100.0 * self._time_closes
        margin = max(self._END_STOP_MARGIN_S, self._time_closes * self._END_STOP_MARGIN_FRACTION)
        return max(1, min(math.ceil(needed + margin), self._time_closes + 1))

    def open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        time = self._open_runtime()

        address, _ = self._sender_id

        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(time, 0x01, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return

        self._movement_active = True

        # Show "opening" immediately. The exact position is only updated when the
        # actuator reports an end/stop telegram (no live interpolation by design).
        self._attr_is_opening = True
        self._attr_is_closing = False
        self.schedule_update_ha_state()


    def close_cover(self, **kwargs: Any) -> None:
        """Close cover."""
        time = self._close_runtime()

        address, _ = self._sender_id

        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(time, 0x02, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return

        self._movement_active = True

        # Show "closing" immediately (see open_cover).
        self._attr_is_closing = True
        self._attr_is_opening = False
        self.schedule_update_ha_state()


    def set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position.

        Sends a timed movement command; the actuator stops itself and reports the
        reached position via a time/direction telegram (handled in value_changed),
        which is when the percentage updates.
        """
        if self._time_closes is None or self._time_opens is None:
            return

        address, _ = self._sender_id
        position = kwargs[ATTR_POSITION]
        current = self._attr_current_cover_position

        if position == 100:
            direction = "up"
            time = self._open_runtime()
        elif position == 0:
            direction = "down"
            time = self._close_runtime()
        elif current is None:
            # Current position unknown (e.g. no telegram since startup) – a precise
            # partial move is impossible without a reference. Do a full run toward
            # the target so the end-stop telegram (0x50/0x70) re-establishes a
            # known position instead of crashing on a None comparison.
            LOGGER.debug("[cover %s] set_cover_position: current position unknown, running fully", self.dev_id)
            if position >= 50:
                self.open_cover()
            else:
                self.close_cover()
            return
        elif position == current:
            return
        elif position > current:
            direction = "up"
            time = max(1, min(int(((position - current) / 100.0) * self._time_opens), 255))
        else:  # position < current
            direction = "down"
            time = max(1, min(int(((current - position) / 100.0) * self._time_closes), 255))

        if self._sender_eep == H5_3F_7F:
            if direction == "up":
                command = 0x01
            else:  # down
                command = 0x02

            msg = H5_3F_7F(time, command, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return

        self._movement_active = True

        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_opening = (direction == "up")
            self._attr_is_closing = (direction == "down")
            self.schedule_update_ha_state()


    def stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        address, _ = self._sender_id

        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
            self.send_message(msg)

        self._movement_active = False

        # Clear movement immediately. The precise stop position is set a moment
        # later when the actuator's time/direction telegram arrives (value_changed).
        self._attr_is_closing = False
        self._attr_is_opening = False
        self.schedule_update_ha_state()


    def value_changed(self, msg):
        """Update the internal state of the cover."""
        try:
            decoded = self.dev_eep.decode_message(msg)
        except Exception as e:
            LOGGER.warning("Could not decode message: %s", str(e))
            return

        if self.dev_eep in [G5_3F_7F]:
            LOGGER.debug(f"[cover {self.dev_id}] G5_3F_7F - {decoded.__dict__}")

            if decoded.state == 0x02:  # moving down
                # Movement telegram – reflect it whether triggered by HA or a
                # hardware wall button. No position change during travel.
                self._movement_active = True
                self._attr_is_closing = True
                self._attr_is_opening = False
                self._attr_is_closed = False

            elif decoded.state == 0x50:  # fully closed
                self._movement_active = False
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = True
                self._attr_current_cover_position = 0
                self._attr_current_cover_tilt_position = 0

            elif decoded.state == 0x01:  # moving up
                # See 0x02 above – reflect hardware-button movements too.
                self._movement_active = True
                self._attr_is_opening = True
                self._attr_is_closing = False
                self._attr_is_closed = False

            elif decoded.state == 0x70:  # fully open
                self._movement_active = False
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = 100
                self._attr_current_cover_tilt_position = 100

            elif decoded.time is not None and decoded.direction is not None and self._time_closes is not None and self._time_opens is not None:
                # Cover stopped at an intermediate position → NOW the precise
                # percentage is computed from the reported travel time.
                self._movement_active = False

                time_in_seconds = decoded.time / 10.0

                if decoded.direction == 0x01:  # up
                    if self._attr_current_cover_position is None:
                        self._attr_current_cover_position = 0
                    if self._time_opens:
                        self._attr_current_cover_position = min(
                            self._attr_current_cover_position + int(time_in_seconds / self._time_opens * 100.0), 100
                        )
                    if self._time_tilts:
                        if self._attr_current_cover_tilt_position is None:
                            self._attr_current_cover_tilt_position = 0
                        self._attr_current_cover_tilt_position = min(
                            self._attr_current_cover_tilt_position + int(decoded.time / self._time_tilts * 100.0), 100
                        )
                else:  # down
                    if self._attr_current_cover_position is None:
                        self._attr_current_cover_position = 100
                    if self._time_closes:
                        self._attr_current_cover_position = max(
                            self._attr_current_cover_position - int(time_in_seconds / self._time_closes * 100.0), 0
                        )
                    if self._time_tilts:
                        if self._attr_current_cover_tilt_position is None:
                            self._attr_current_cover_tilt_position = 100
                        self._attr_current_cover_tilt_position = max(
                            self._attr_current_cover_tilt_position - int(decoded.time / self._time_tilts * 100.0), 0
                        )

                self._attr_is_closed = (self._attr_current_cover_position == 0)
                self._attr_is_opening = False
                self._attr_is_closing = False

                # Store measured travel time for this direction
                if decoded.direction == 0x01:  # up
                    self._measured_time_opens = round(time_in_seconds, 1)
                    LOGGER.info(f"[cover {self.dev_id}] Gemessene Öffnungszeit: {time_in_seconds:.1f}s "
                                f"→ Empfehlung: time_opens: {int(time_in_seconds)} "
                                f"(aktuell konfiguriert: {self._time_opens})")
                else:  # down
                    self._measured_time_closes = round(time_in_seconds, 1)
                    LOGGER.info(f"[cover {self.dev_id}] Gemessene Schließzeit: {time_in_seconds:.1f}s "
                                f"→ Empfehlung: time_closes: {int(time_in_seconds)} "
                                f"(aktuell konfiguriert: {self._time_closes})")

            LOGGER.debug(f"[cover {self.dev_id}] state: {self.state}, opening: {self.is_opening}, closing: {self.is_closing}, closed: {self.is_closed}, position: {self._attr_current_cover_position}, movement_active: {self._movement_active}")

            self.schedule_update_ha_state()


    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the cover tilt (slats) to a specific position."""
        if self._time_tilts is None:
            return
        if self._sender_eep != H5_3F_7F:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return

        address, _ = self._sender_id
        tilt_position = kwargs[ATTR_TILT_POSITION]

        # Baseline when the current tilt is still undefined (no telegram yet).
        current_tilt = self._attr_current_cover_tilt_position
        if current_tilt is None:
            current_tilt = 0

        if tilt_position == current_tilt:
            return
        elif tilt_position > current_tilt:
            direction = "up"
            command = 0x01
            sleeptime = min((tilt_position - current_tilt) / 100.0 * self._time_tilts / 10.0, 255.0)
        else:  # tilt_position < current_tilt
            direction = "down"
            command = 0x02
            sleeptime = min((current_tilt - tilt_position) / 100.0 * self._time_tilts / 10.0, 255.0)

        # Reflect the tilt movement immediately if fast status change is enabled.
        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_opening = (direction == "up")
            self._attr_is_closing = (direction == "down")
            self.schedule_update_ha_state()

        # Perform the tilt: start moving, wait the computed time, then stop.
        msg = H5_3F_7F(0, command, 1).encode_message(address)
        self.send_message(msg)
        await asyncio.sleep(sleeptime)
        msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
        self.send_message(msg)

        # Movement finished: store the new tilt position and clear movement flags
        # so the entity does not stay stuck on "opening"/"closing".
        self._attr_current_cover_tilt_position = tilt_position
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.schedule_update_ha_state()