"""Support for Eltako covers."""
from __future__ import annotations

from typing import Any
import math

from eltakobus.util import AddressExpression
from eltakobus.eep import *

from homeassistant import config_entries
from homeassistant.components.cover import CoverEntity, CoverEntityFeature, ATTR_POSITION, ATTR_TILT_POSITION
from homeassistant.const import CONF_DEVICE_CLASS, Platform, STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
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

    Position handling is telegram-based (no live interpolation): during travel
    the entity reports opening/closing, and the exact percentage is set when the
    actuator reports a stop / end position.

    Exception: moves to an END position (fully open / fully closed) get an
    optimistic end state. The actuator only sends its final telegram (0x50/0x70)
    once the sent runtime (incl. safety margin) has elapsed – i.e. several
    seconds after the cover physically hit the end stop. To avoid "closing"
    lingering, the end state is set optimistically once the expected travel time
    has passed. The real telegram still arrives afterwards and is authoritative.
    """

    # Minimum safety margin (seconds) added when driving to an end position, so
    # the cover reliably reaches the end stop even if the known position drifted.
    # The effective margin also scales with the remaining distance (see below).
    _END_STOP_MARGIN_S = 3

    # Additional margin as a fraction of the FULL travel time.
    _END_STOP_MARGIN_FRACTION = 0.10

    # Grace period (seconds) added to the expected travel time before the
    # optimistic end state is applied.
    _OPTIMISTIC_GRACE_S = 0.5

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

        self._movement_active: bool = False

        # Sequence number of the current movement. Every new command / final
        # telegram increments it, so a pending optimistic-end timer from an
        # older movement becomes a no-op (no cross-thread cancel needed).
        self._move_seq: int = 0

        # Measured travel times from actual device telegrams (in seconds).
        # NOTE: these are the durations of the last move that stopped at an
        # intermediate position – NOT full travel times.
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
                self._attr_current_cover_tilt_position = (old_tilt if old_tilt is not None else 100) if self._time_tilts else None

            elif latest_state.state == STATE_CLOSED:
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = True
                self._attr_current_cover_position = old_position if old_position is not None else 0
                self._attr_current_cover_tilt_position = (old_tilt if old_tilt is not None else 0) if self._time_tilts else None

            elif latest_state.state in (STATE_CLOSING, STATE_OPENING):
                # Reset stale movement state from before HA restart
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_current_cover_position = old_position
                self._attr_current_cover_tilt_position = old_tilt if self._time_tilts else None
                self._attr_is_closed = (old_position == 0) if old_position is not None else None

        except Exception as e:
            # Unknown state – but never leave the movement flags undefined.
            self._attr_current_cover_position = None
            self._attr_current_cover_tilt_position = None
            self._attr_is_opening = False
            self._attr_is_closing = False
            self._attr_is_closed = None

        self.schedule_update_ha_state()
        LOGGER.debug(f"[cover {self.dev_id}] value initially loaded: ["
                     + f"is_opening: {self.is_opening}, "
                     + f"is_closing: {self.is_closing}, "
                     + f"is_closed: {self.is_closed}, "
                     + f"current_position: {self._attr_current_cover_position}, "
                     + f"current_tilt_position: {self._attr_current_cover_tilt_position}, "
                     + f"state: {self.state}]")


    # ------------------------------------------------------------------
    # Optimistic end state
    # ------------------------------------------------------------------

    def _expected_travel_to_end(self, closing: bool) -> float | None:
        """Expected physical travel time (s) from the known position to the end stop."""
        full = self._time_closes if closing else self._time_opens
        if full is None:
            return None
        pos = self._attr_current_cover_position
        if pos is None:
            return float(full)
        remaining = pos if closing else (100 - pos)
        return max(0, remaining) / 100.0 * full

    def _schedule_optimistic_end(self, closing: bool) -> None:
        """Arm a timer that sets the end state once the cover should have arrived.

        Safe to call from any thread (command methods run in the executor).
        """
        self._move_seq += 1
        seq = self._move_seq

        travel = self._expected_travel_to_end(closing)
        if travel is None or self.hass is None:
            return
        delay = travel + self._OPTIMISTIC_GRACE_S

        @callback
        def _fire(_now) -> None:
            self._apply_optimistic_end(seq, closing)

        def _arm() -> None:
            async_call_later(self.hass, delay, _fire)

        self.hass.loop.call_soon_threadsafe(_arm)

    def _invalidate_optimistic_end(self) -> None:
        """Make any pending optimistic-end timer a no-op."""
        self._move_seq += 1

    @callback
    def _apply_optimistic_end(self, seq: int, closing: bool) -> None:
        if seq != self._move_seq:
            return  # a newer command or a real telegram superseded this movement
        if closing and not self._attr_is_closing:
            return
        if not closing and not self._attr_is_opening:
            return

        LOGGER.debug(f"[cover {self.dev_id}] optimistic end state: {'closed' if closing else 'open'}")
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = closing
        self._attr_current_cover_position = 0 if closing else 100
        if self._time_tilts:
            self._attr_current_cover_tilt_position = 0 if closing else 100
        # _movement_active stays True – the actuator is still running out its
        # margin; the final telegram (0x50/0x70) clears it.
        self.async_write_ha_state()


    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _open_runtime(self) -> int:
        """Runtime to send for an upward move to the top (see docstring above)."""
        if self._time_opens is None:
            return 255
        pos = self._attr_current_cover_position
        if pos is None:
            return self._time_opens + 1
        needed = max(0, 100 - pos) / 100.0 * self._time_opens
        margin = max(self._END_STOP_MARGIN_S, self._time_opens * self._END_STOP_MARGIN_FRACTION)
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
        self._schedule_optimistic_end(closing=False)

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
        self._schedule_optimistic_end(closing=True)

        self._attr_is_closing = True
        self._attr_is_opening = False
        self.schedule_update_ha_state()


    def set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        if self._time_closes is None or self._time_opens is None:
            return

        position = kwargs[ATTR_POSITION]
        current = self._attr_current_cover_position

        # End positions: identical to open/close (incl. optimistic end state).
        if position == 100:
            self.open_cover()
            return
        if position == 0:
            self.close_cover()
            return

        if current is None:
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

        address, _ = self._sender_id

        if self._sender_eep == H5_3F_7F:
            command = 0x01 if direction == "up" else 0x02
            msg = H5_3F_7F(time, command, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return

        self._movement_active = True
        # Intermediate target: no optimistic end, the time telegram sets the position.
        self._invalidate_optimistic_end()

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
        self._invalidate_optimistic_end()

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
                # Start telegram. If this movement was not started by HA (wall
                # button), arm the optimistic end as well.
                if not self._attr_is_closing:
                    self._schedule_optimistic_end(closing=True)
                self._movement_active = True
                self._attr_is_closing = True
                self._attr_is_opening = False
                self._attr_is_closed = False

            elif decoded.state == 0x50:  # fully closed
                self._invalidate_optimistic_end()
                self._movement_active = False
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = True
                self._attr_current_cover_position = 0
                if self._time_tilts:
                    self._attr_current_cover_tilt_position = 0

            elif decoded.state == 0x01:  # moving up
                if not self._attr_is_opening:
                    self._schedule_optimistic_end(closing=False)
                self._movement_active = True
                self._attr_is_opening = True
                self._attr_is_closing = False
                self._attr_is_closed = False

            elif decoded.state == 0x70:  # fully open
                self._invalidate_optimistic_end()
                self._movement_active = False
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = 100
                if self._time_tilts:
                    self._attr_current_cover_tilt_position = 100

            elif decoded.time is not None and decoded.direction is not None:
                # Final telegram: cover stopped (intermediate position or end of
                # sent runtime). Always clear the movement state here – even if
                # no travel times are configured.
                self._invalidate_optimistic_end()
                self._movement_active = False
                self._attr_is_opening = False
                self._attr_is_closing = False

                if self._time_closes is not None and self._time_opens is not None:
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
                        self._measured_time_opens = round(time_in_seconds, 1)
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
                        self._measured_time_closes = round(time_in_seconds, 1)

                    LOGGER.debug(f"[cover {self.dev_id}] Fahrt beendet nach {time_in_seconds:.1f}s "
                                 f"(Richtung {'auf' if decoded.direction == 0x01 else 'zu'})")

                if self._attr_current_cover_position is not None:
                    self._attr_is_closed = (self._attr_current_cover_position == 0)

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

        current_tilt = self._attr_current_cover_tilt_position
        if current_tilt is None:
            current_tilt = 0

        if tilt_position == current_tilt:
            return
        elif tilt_position > current_tilt:
            direction = "up"
            command = 0x01
            sleeptime = min((tilt_position - current_tilt) / 100.0 * self._time_tilts / 10.0, 255.0)
        else:
            direction = "down"
            command = 0x02
            sleeptime = min((current_tilt - tilt_position) / 100.0 * self._time_tilts / 10.0, 255.0)

        self._invalidate_optimistic_end()

        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_opening = (direction == "up")
            self._attr_is_closing = (direction == "down")
            self.schedule_update_ha_state()

        msg = H5_3F_7F(0, command, 1).encode_message(address)
        self.send_message(msg)
        await asyncio.sleep(sleeptime)
        msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
        self.send_message(msg)

        self._attr_current_cover_tilt_position = tilt_position
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.schedule_update_ha_state()