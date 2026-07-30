#!/usr/bin/env python3
"""PS4 DualShock 4 controller interface built on pygame's joystick API.

Axis/button indices below follow SDL2's default mapping for a Sony
"Wireless Controller" (DualShock 4) paired over Bluetooth on Linux. These
indices are driver-dependent and may differ on Windows/macOS or with a
different SDL/pygame version -- verify with the live telemetry view
(``python ps4_controller_test.py``) before relying on them for control.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import pygame

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AxisMap:
    """Joystick axis indices for a DS4 controller (SDL2 default mapping)."""

    left_x: int = 0
    left_y: int = 1
    l2_axis: int = 2
    right_x: int = 3
    right_y: int = 4
    r2_axis: int = 5


@dataclass(frozen=True)
class ButtonMap:
    """Joystick button indices for a DS4 controller (SDL2 default mapping)."""

    cross: int = 0
    circle: int = 1
    triangle: int = 2
    square: int = 3
    l1: int = 4
    r1: int = 5
    l2: int = 6
    r2: int = 7
    share: int = 8
    options: int = 9
    ps: int = 10
    l3: int = 11
    r3: int = 12


class PS4Controller:
    """Thin OOP wrapper around pygame's joystick API for a DS4 gamepad.

    Handles connection lifecycle, deadzone filtering, and exposes both a
    polling API (:meth:`snapshot`) and an event-callback API
    (:meth:`register_button_handler` / :meth:`register_axis_handler`).
    """

    def __init__(self, joystick_id: int = 0, deadzone: float = 0.12, dpad_hat: int = 0) -> None:
        self.joystick_id = joystick_id
        self.deadzone = deadzone
        self.dpad_hat = dpad_hat
        self.axes = AxisMap()
        self.buttons = ButtonMap()
        self.joystick: Optional[pygame.joystick.Joystick] = None
        self.button_handlers: Dict[int, Callable[[bool], None]] = {}
        self.axis_handlers: Dict[int, Callable[[float], None]] = {}

    def connect(self) -> None:
        """Initialize pygame's joystick subsystem and open the controller.

        Safe to call again after :meth:`close` (e.g. during a reconnect
        after a Bluetooth dropout).

        Raises:
            RuntimeError: No joystick is present, or ``joystick_id`` is
                out of range for the number of connected devices.
        """
        pygame.display.init()
        pygame.joystick.init()
        count = pygame.joystick.get_count()
        if count == 0:
            raise RuntimeError('No joystick detected. Pair the PS4 controller first.')
        if self.joystick_id >= count:
            raise RuntimeError(f'Joystick id {self.joystick_id} not available. Found {count} device(s).')
        self.joystick = pygame.joystick.Joystick(self.joystick_id)
        self.joystick.init()

    @property
    def is_connected(self) -> bool:
        """True if a joystick handle is open and still reachable by SDL."""
        return self.joystick is not None and pygame.joystick.get_count() > 0

    @property
    def name(self) -> str:
        return self.joystick.get_name() if self.joystick else 'Disconnected'

    def close(self) -> None:
        """Release the joystick handle and shut down pygame subsystems."""
        if self.joystick:
            try:
                self.joystick.quit()
            except pygame.error:
                pass
        self.joystick = None
        pygame.joystick.quit()
        pygame.display.quit()
        pygame.quit()

    def __enter__(self) -> 'PS4Controller':
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def register_button_handler(self, button_id: int, handler: Callable[[bool], None]) -> None:
        self.button_handlers[button_id] = handler

    def register_axis_handler(self, axis_id: int, handler: Callable[[float], None]) -> None:
        self.axis_handlers[axis_id] = handler

    def axis_value(self, axis_id: int) -> float:
        """Return the deadzone-filtered value of ``axis_id`` in [-1.0, 1.0]."""
        if not self.joystick:
            return 0.0
        axis_count = self.joystick.get_numaxes()
        if axis_id < 0 or axis_id >= axis_count:
            raise ValueError(f'Axis {axis_id} is not available. Controller exposes {axis_count} axes.')
        value = self.joystick.get_axis(axis_id)
        return 0.0 if abs(value) < self.deadzone else value

    def button_pressed(self, button_id: int) -> bool:
        return bool(self.joystick and self.joystick.get_button(button_id))

    def dpad(self) -> tuple[int, int]:
        """Return the D-pad state as an (x, y) hat tuple, e.g. (0, 1) = up."""
        if not self.joystick:
            return (0, 0)
        hat_count = self.joystick.get_numhats()
        if hat_count > self.dpad_hat:
            return self.joystick.get_hat(self.dpad_hat)
        return (0, 0)

    def snapshot(self) -> dict:
        """Poll and return the full controller state as a plain dict.

        Raises:
            pygame.error: If the underlying joystick device has been
                removed (e.g. Bluetooth dropout) since the last poll.
        """
        return {
            'left_stick': (self.axis_value(self.axes.left_x), self.axis_value(self.axes.left_y)),
            'right_stick': (self.axis_value(self.axes.right_x), self.axis_value(self.axes.right_y)),
            'triggers': {
                'l2': self.axis_value(self.axes.l2_axis),
                'r2': self.axis_value(self.axes.r2_axis),
            },
            'dpad': self.dpad(),
            'buttons': {
                'cross': self.button_pressed(self.buttons.cross),
                'circle': self.button_pressed(self.buttons.circle),
                'triangle': self.button_pressed(self.buttons.triangle),
                'square': self.button_pressed(self.buttons.square),
                'l1': self.button_pressed(self.buttons.l1),
                'r1': self.button_pressed(self.buttons.r1),
                'l2': self.button_pressed(self.buttons.l2),
                'r2': self.button_pressed(self.buttons.r2),
                'share': self.button_pressed(self.buttons.share),
                'options': self.button_pressed(self.buttons.options),
                'ps': self.button_pressed(self.buttons.ps),
                'l3': self.button_pressed(self.buttons.l3),
                'r3': self.button_pressed(self.buttons.r3),
            },
            'meta': {
                'axes': self.joystick.get_numaxes() if self.joystick else 0,
                'buttons': self.joystick.get_numbuttons() if self.joystick else 0,
                'hats': self.joystick.get_numhats() if self.joystick else 0,
            }
        }

    def process_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                handler = self.button_handlers.get(event.button)
                if handler:
                    handler(True)
            elif event.type == pygame.JOYBUTTONUP:
                handler = self.button_handlers.get(event.button)
                if handler:
                    handler(False)
            elif event.type == pygame.JOYAXISMOTION:
                handler = self.axis_handlers.get(event.axis)
                if handler:
                    value = 0.0 if abs(event.value) < self.deadzone else event.value
                    handler(value)

    def print_live_state(self, interval: float = 0.1) -> None:
        """Print a continuously updating single-line telemetry view.

        Intended as a manual diagnostic tool to verify axis/button mapping
        before wiring the controller into a control loop.
        """
        print(f'Connected to: {self.name}')
        if self.joystick:
            print(f'Axes: {self.joystick.get_numaxes()} | Buttons: {self.joystick.get_numbuttons()} | Hats: {self.joystick.get_numhats()}')
        print('Press Ctrl+C to stop. Move sticks or press buttons to test.\n')
        while True:
            try:
                pygame.event.pump()
                state = self.snapshot()
            except pygame.error as exc:
                logger.warning('Controller connection lost: %s', exc)
                break
            pressed = [k for k, v in state['buttons'].items() if v]
            line = (
                f"LX={state['left_stick'][0]: .2f} LY={state['left_stick'][1]: .2f} | "
                f"RX={state['right_stick'][0]: .2f} RY={state['right_stick'][1]: .2f} | "
                f"L2={state['triggers']['l2']: .2f} R2={state['triggers']['r2']: .2f} | "
                f"DPAD={state['dpad']} | "
                f"BTN={pressed if pressed else '[]'}"
            )
            print(line.ljust(160), end='\r', flush=True)
            time.sleep(interval)


def main() -> int:
    controller = PS4Controller()
    try:
        controller.connect()
        controller.print_live_state()
    except KeyboardInterrupt:
        print('\nStopped by user.')
        return 0
    except Exception:
        logger.exception('Script failed.')
        return 1
    finally:
        controller.close()


if __name__ == '__main__':
    raise SystemExit(main())
