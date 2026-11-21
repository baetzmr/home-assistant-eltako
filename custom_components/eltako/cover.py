"""Support for Eltako covers."""
from __future__ import annotations

from typing import Any
import asyncio
import math
import threading

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


class _PendingStart:
    """A movement command that still waits for the actuator's start telegram."""

    __slots__ = ("seq", "closing", "end_move", "msg", "attempt")

    def __init__(self, seq: int, closing: bool, end_move: bool, msg, attempt: int):
        self.seq = seq
        self.closing = closing
        self.end_move = end_move
        self.msg = msg
        self.attempt = attempt


class EltakoCover(EltakoEntity, CoverEntity, RestoreEntity):
    """Representation of an Eltako cover device (FSB14 / FSB61 / FSB71 ...).

    Position handling is telegram-based (no live interpolation): during travel
    the entity reports opening/closing, and the exact percentage is set when the
    actuator reports a stop / end position.

    Start confirmation
    ------------------
    Every movement command sent by HA must be confirmed by the actuator with its
    start telegram (RPS 0x01 = moving up / 0x02 = moving down) within
    _START_CONFIRM_TIMEOUT_S. Without confirmation:
      * moves to an END position are re-sent up to _START_CONFIRM_RETRIES times
        (safe: a longer runtime only keeps the relay on at the end stop),
      * moves to an INTERMEDIATE position are NOT re-sent (could overshoot if
        only the confirmation got lost) – the state is reverted instead,
      * if still unconfirmed, the movement state is reverted and a warning is
        logged. HA never claims a movement the actuator did not acknowledge.

    Optimistic end state
    --------------------
    Moves to an END position (fully open / fully closed) get an optimistic end
    state. The actuator only sends its final telegram (0x50/0x70) once the sent
    runtime (incl. safety margin) has elapsed – i.e. several seconds after the
    cover physically hit the end stop. To avoid "closing" lingering, the end
    state is set optimistically once the expected travel time has passed.
    The optimistic timer is armed ONLY after the actuator confirmed the start.
    The real telegram still arrives afterwards and is authoritative.

    Threading
    ---------
    The sync command methods run in the executor, value_changed() runs in the
    executor (dispatcher), timer callbacks run in the event loop. All shared
    movement bookkeeping (_move_seq, _pending_start, _tilt_active) is guarded
    by _lock; timers are always armed in the event loop.
    """

    # Enable/disable the start confirmation watchdog. Requires the actuator to
    # send start telegrams (0x01/0x02) – check with logger "eltako: debug".
    _START_CONFIRM_ENABLED = True

    # Time (s) within which the actuator must confirm a command with 0x01/0x02.
    _START_CONFIRM_TIMEOUT_S = 3.0

    # Number of re-sends for unconfirmed END-position commands.
    _START_CONFIRM_RETRIES = 1

    # Minimum safety margin (seconds) added when driving to an end position, so
    # the cover reliably reaches the end stop even if the known position drifted.
    # The effective margin also scales with the full travel time (see below).
    _END_STOP_MARGIN_S = 3

    # Additional margin as a fraction of the FULL travel time.
    _END_STOP_MARGIN_FRACTION = 0.10

    # Grace period (seconds) added to the expected travel time before the
    # optimistic end state is applied.
    _OPTIMISTIC_GRACE_S = 0.5

    # Max runtime that fits into the seconds-based command telegram.
    _MAX_RUNTIME_S = 255

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

        # Guards _move_seq, _pending_start and _tilt_active (accessed from the
        # executor and from the event loop).
        self._lock = threading.RLock()

        # Sequence number of the current movement. Every new command / final
        # telegram increments it, so pending timers from an older movement
        # become no-ops (no cross-thread cancel needed).
        self._move_seq: int = 0

        # Command that still waits for the actuator's start telegram.
        self._pending_start: _PendingStart | None = None

        # True while a tilt (slat) movement runs – it must not arm the
        # optimistic end state for a full open/close.
        self._tilt_active: bool = False

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

        except Exception:
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
    # Timer helper
    # ------------------------------------------------------------------

    def _call_later_in_loop(self, delay: float, action) -> None:
        """Run `action()` in the event loop after `delay` seconds.

        Safe to call from any thread.
        """
        if self.hass is None:
            return

        @callback
        def _fire(_now) -> None:
            action()

        def _arm() -> None:
            async_call_later(self.hass, delay, _fire)

        self.hass.loop.call_soon_threadsafe(_arm)


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

        Must be called with the current movement already registered (it takes a
        new sequence number, invalidating older timers). Safe from any thread.
        """
        with self._lock:
            self._move_seq += 1
            seq = self._move_seq

        travel = self._expected_travel_to_end(closing)
        if travel is None:
            return
        self._call_later_in_loop(travel + self._OPTIMISTIC_GRACE_S,
                                 lambda: self._apply_optimistic_end(seq, closing))

    def _invalidate_timers(self) -> None:
        """Make any pending optimistic-end / confirmation timer a no-op."""
        with self._lock:
            self._move_seq += 1

    @callback
    def _apply_optimistic_end(self, seq: int, closing: bool) -> None:
        with self._lock:
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
    # Start confirmation
    # ------------------------------------------------------------------

    def _send_move(self, msg, closing: bool, end_move: bool, attempt: int = 0) -> None:
        """Send a movement command and – if enabled – watch for the start telegram.

        Safe to call from any thread.
        """
        if not self._START_CONFIRM_ENABLED:
            with self._lock:
                self._tilt_active = False
                self._pending_start = None
            self._invalidate_timers()
            self.send_message(msg)
            if end_move:
                self._schedule_optimistic_end(closing)
            return

        # Register BEFORE sending, so a very fast start telegram finds it.
        with self._lock:
            self._tilt_active = False
            self._move_seq += 1
            seq = self._move_seq
            self._pending_start = _PendingStart(seq, closing, end_move, msg, attempt)

        self.send_message(msg)
        self._call_later_in_loop(self._START_CONFIRM_TIMEOUT_S,
                                 lambda: self._check_start_confirmation(seq))

    def _take_pending(self) -> _PendingStart | None:
        """Remove and return the pending command (actuator reacted)."""
        with self._lock:
            pending = self._pending_start
            self._pending_start = None
            return pending

    @callback
    def _check_start_confirmation(self, seq: int) -> None:
        """Event loop: runs _START_CONFIRM_TIMEOUT_S after a command was sent."""
        with self._lock:
            pending = self._pending_start
            if pending is None or pending.seq != seq:
                return  # confirmed, or superseded by a newer command
            retry = pending.end_move and pending.attempt < self._START_CONFIRM_RETRIES
            if not retry:
                self._pending_start = None
                self._move_seq += 1  # drop anything belonging to this movement

        if retry:
            LOGGER.warning("[cover %s] No start confirmation from actuator - re-sending command (attempt %d)",
                           self.dev_id, pending.attempt + 2)
            # send_message must not block the loop -> executor
            self.hass.async_add_executor_job(
                self._send_move, pending.msg, pending.closing, pending.end_move, pending.attempt + 1)
            return

        LOGGER.warning("[cover %s] Actuator did not confirm the %s command - state not changed",
                       self.dev_id, "close" if pending.closing else "open")
        self._movement_active = False
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.async_write_ha_state()


    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _end_runtime(self, full_time, remaining_percent: float) -> int:
        """Runtime (s) to send for a move to an end position.

        Needed time for the remaining distance plus a safety margin; never
        shorter than needed, never longer than full travel time + margin.
        A longer runtime is harmless: the motor's end switch stops the cover,
        the actuator just keeps the relay on until the runtime has elapsed.
        """
        if full_time is None:
            return self._MAX_RUNTIME_S
        margin = max(self._END_STOP_MARGIN_S, full_time * self._END_STOP_MARGIN_FRACTION)
        needed = max(0.0, remaining_percent) / 100.0 * full_time
        return max(1, min(math.ceil(needed + margin), math.ceil(full_time + margin), self._MAX_RUNTIME_S))

    def _open_runtime(self) -> int:
        """Runtime to send for an upward move to the top."""
        pos = self._attr_current_cover_position
        return self._end_runtime(self._time_opens, 100 if pos is None else 100 - pos)

    def _close_runtime(self) -> int:
        """Runtime to send for a downward move to the bottom."""
        pos = self._attr_current_cover_position
        return self._end_runtime(self._time_closes, 100 if pos is None else pos)

    def _sender_supported(self) -> bool:
        if self._sender_eep == H5_3F_7F:
            return True
        LOGGER.warning("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
        return False

    def open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        if not self._sender_supported():
            return
        address, _ = self._sender_id
        msg = H5_3F_7F(self._open_runtime(), 0x01, 1).encode_message(address)

        self._movement_active = True
        self._attr_is_opening = True
        self._attr_is_closing = False
        self._send_move(msg, closing=False, end_move=True)
        self.schedule_update_ha_state()


    def close_cover(self, **kwargs: Any) -> None:
        """Close cover."""
        if not self._sender_supported():
            return
        address, _ = self._sender_id
        msg = H5_3F_7F(self._close_runtime(), 0x02, 1).encode_message(address)

        self._movement_active = True
        self._attr_is_closing = True
        self._attr_is_opening = False
        self._send_move(msg, closing=True, end_move=True)
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
            time = max(1, min(round(((position - current) / 100.0) * self._time_opens), self._MAX_RUNTIME_S))
        else:  # position < current
            direction = "down"
            time = max(1, min(round(((current - position) / 100.0) * self._time_closes), self._MAX_RUNTIME_S))

        if not self._sender_supported():
            return
        address, _ = self._sender_id
        command = 0x01 if direction == "up" else 0x02
        msg = H5_3F_7F(time, command, 1).encode_message(address)

        self._movement_active = True
        # Intermediate target: no optimistic end, the time telegram sets the position.
        self._send_move(msg, closing=(direction == "down"), end_move=False)

        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_opening = (direction == "up")
            self._attr_is_closing = (direction == "down")
            self.schedule_update_ha_state()


    def stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        address, _ = self._sender_id

        with self._lock:
            self._pending_start = None
        self._invalidate_timers()

        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
            self.send_message(msg)

        self._movement_active = False
        self._attr_is_closing = False
        self._attr_is_opening = False
        self.schedule_update_ha_state()


    # ------------------------------------------------------------------
    # Incoming telegrams
    # ------------------------------------------------------------------

    @staticmethod
    def _is_own_command_echo(msg) -> bool:
        """True for a shutter COMMAND telegram (H5_3F_7F) instead of a status.

        Commands carry the runtime in whole seconds (DB0 bit1 = 0, e.g. 0x08),
        the actuator's stop/status telegram uses DB0 = 0x0A (bit1 = 1). If the
        sender ID equals the device ID, our own command may come back via the
        event bus and must not be mistaken for a stop telegram.
        """
        try:
            return msg.org == 0x07 and len(msg.data) >= 4 and not (msg.data[3] & 0x02)
        except Exception:
            return False

    def _handle_start_telegram(self, closing: bool) -> None:
        """Actuator reports: movement started (0x01 up / 0x02 down)."""
        pending = self._take_pending()
        moving_in_direction = self._attr_is_closing if closing else self._attr_is_opening
        with self._lock:
            tilt_active = self._tilt_active

        if tilt_active:
            arm = False                     # slat movement – never a full run
        elif pending is not None and pending.closing == closing:
            arm = pending.end_move          # our command confirmed
            if pending.attempt > 0:
                LOGGER.info("[cover %s] Command confirmed after re-send", self.dev_id)
        else:
            arm = not moving_in_direction   # started externally (wall button)

        if arm:
            self._schedule_optimistic_end(closing=closing)

        self._movement_active = True
        self._attr_is_closing = closing
        self._attr_is_opening = not closing
        self._attr_is_closed = False

    def _handle_final_telegram(self) -> None:
        """Actuator reports end of movement (end position or stop telegram)."""
        with self._lock:
            self._pending_start = None
            self._tilt_active = False
            self._move_seq += 1  # invalidates optimistic / confirmation timers
        self._movement_active = False
        self._attr_is_opening = False
        self._attr_is_closing = False

    def value_changed(self, msg):
        """Update the internal state of the cover."""
        if self.dev_eep not in [G5_3F_7F]:
            return

        if self._is_own_command_echo(msg):
            return

        try:
            decoded = self.dev_eep.decode_message(msg)
        except Exception as e:
            LOGGER.warning("Could not decode message: %s", str(e))
            return

        LOGGER.debug(f"[cover {self.dev_id}] G5_3F_7F - {decoded.__dict__}")

        if decoded.state == 0x02:  # moving down
            self._handle_start_telegram(closing=True)

        elif decoded.state == 0x01:  # moving up
            self._handle_start_telegram(closing=False)

        elif decoded.state == 0x50:  # fully closed
            self._handle_final_telegram()
            self._attr_is_closed = True
            self._attr_current_cover_position = 0
            if self._time_tilts:
                self._attr_current_cover_tilt_position = 0

        elif decoded.state == 0x70:  # fully open
            self._handle_final_telegram()
            self._attr_is_closed = False
            self._attr_current_cover_position = 100
            if self._time_tilts:
                self._attr_current_cover_tilt_position = 100

        elif decoded.time is not None and decoded.direction is not None:
            # Final telegram: cover stopped (intermediate position or end of
            # sent runtime). Always clear the movement state here – even if
            # no travel times are configured.
            self._handle_final_telegram()

            if self._time_closes is not None and self._time_opens is not None:
                time_in_seconds = decoded.time / 10.0

                if decoded.direction == 0x01:  # up
                    if self._attr_current_cover_position is None:
                        self._attr_current_cover_position = 0
                    if self._time_opens:
                        self._attr_current_cover_position = min(
                            self._attr_current_cover_position + round(time_in_seconds / self._time_opens * 100.0), 100
                        )
                    if self._time_tilts:
                        if self._attr_current_cover_tilt_position is None:
                            self._attr_current_cover_tilt_position = 0
                        self._attr_current_cover_tilt_position = min(
                            self._attr_current_cover_tilt_position + round(decoded.time / self._time_tilts * 100.0), 100
                        )
                    self._measured_time_opens = round(time_in_seconds, 1)
                else:  # down
                    if self._attr_current_cover_position is None:
                        self._attr_current_cover_position = 100
                    if self._time_closes:
                        self._attr_current_cover_position = max(
                            self._attr_current_cover_position - round(time_in_seconds / self._time_closes * 100.0), 0
                        )
                    if self._time_tilts:
                        if self._attr_current_cover_tilt_position is None:
                            self._attr_current_cover_tilt_position = 100
                        self._attr_current_cover_tilt_position = max(
                            self._attr_current_cover_tilt_position - round(decoded.time / self._time_tilts * 100.0), 0
                        )
                    self._measured_time_closes = round(time_in_seconds, 1)

                LOGGER.debug(f"[cover {self.dev_id}] movement finished after {time_in_seconds:.1f}s "
                             f"(direction {'up' if decoded.direction == 0x01 else 'down'})")

            if self._attr_current_cover_position is not None:
                self._attr_is_closed = (self._attr_current_cover_position == 0)

        else:
            return  # unknown telegram – no state change

        LOGGER.debug(f"[cover {self.dev_id}] state: {self.state}, opening: {self.is_opening}, closing: {self.is_closing}, closed: {self.is_closed}, position: {self._attr_current_cover_position}, movement_active: {self._movement_active}")

        self.schedule_update_ha_state()


    # ------------------------------------------------------------------
    # Tilt
    # ------------------------------------------------------------------

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the cover tilt (slats) to a specific position."""
        if self._time_tilts is None:
            return
        if self._sender_eep != H5_3F_7F:
            LOGGER.warning("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
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

        with self._lock:
            self._pending_start = None
            self._tilt_active = True
            self._move_seq += 1

        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_opening = (direction == "up")
            self._attr_is_closing = (direction == "down")
            self.async_write_ha_state()

        msg = H5_3F_7F(0, command, 1).encode_message(address)
        self.send_message(msg)
        await asyncio.sleep(sleeptime)
        msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
        self.send_message(msg)

        self._attr_current_cover_tilt_position = tilt_position
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.async_write_ha_state()

        # Normally the actuator's stop telegram clears the tilt flag. If it
        # never arrives, clear it anyway so later wall-button runs are handled.
        with self._lock:
            tilt_seq = self._move_seq

        def _clear_tilt() -> None:
            with self._lock:
                if self._move_seq == tilt_seq:
                    self._tilt_active = False

        self._call_later_in_loop(self._START_CONFIRM_TIMEOUT_S + 2.0, _clear_tilt)