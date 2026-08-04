#!/usr/bin/env python3
"""Arcade-drive control of a two-motor tank chassis via PS4 controller.

Hardware: two BTS7960 H-bridge modules (one per side) driven from a
Raspberry Pi in ``GPIO.BOARD`` pin-numbering mode. See readme.md for the
full pin map and wiring notes.

Safety features:
- Motor commands are slew-rate limited (see ``DriveConfig.slew_rate_per_sec``)
  so the H-bridges never see an instantaneous full-reverse command, which
  would otherwise draw a large current spike and stress the gearbox.
- If the controller disconnects (e.g. Bluetooth dropout) both motors are
  stopped immediately and the script polls for reconnection rather than
  continuing to drive with a stale command.
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass

import pygame
import RPi.GPIO as GPIO
from ps4_controller_test import PS4Controller

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MotorPins:
    """Physical (BOARD) pin numbers wired to one BTS7960 module."""

    rpwm: int
    lpwm: int
    ren: int
    len_: int


@dataclass
class DriveConfig:
    """Tunable driving parameters, centralized for easy field adjustment."""

    max_speed: float = 0.85
    """Speed cap in normal mode (0-1), applied unless L1/R1 override it."""
    boost_speed: float = 1.0
    """Speed cap while R1 (full speed) is held."""
    slow_speed: float = 0.45
    """Speed cap while L1 (slow mode) is held."""
    precision_speed: float = 0.35
    """Fixed speed used for D-pad precision nudges."""
    turn_gain: float = 0.75
    """Scales right-stick turn input relative to throttle."""
    expo_factor: float = 0.35
    """Cubic expo blend (0=linear, 1=full cubic) for finer low-speed control."""
    controller_deadzone: float = 0.10
    loop_hz: float = 20.0
    """Target control-loop update rate."""
    slew_rate_per_sec: float = 3.0
    """Max change in normalized motor speed per second (protects the
    H-bridges/gearbox from instantaneous reversal current spikes)."""
    reconnect_retry_sec: float = 1.0


class BTS7960Motor:
    """Drives one BTS7960 H-bridge module via two PWM channels.

    RPWM/LPWM each drive one direction; only one is active at a time so the
    module never sees both high sides enabled simultaneously. R_EN/L_EN are
    held HIGH permanently since this design doesn't need a hardware
    disable/current-sense path.
    """

    def __init__(self, pins: MotorPins, pwm_frequency: int = 1000) -> None:
        self.pins = pins
        self.pwm_frequency = pwm_frequency
        self.rpwm = None
        self.lpwm = None

    def setup(self) -> None:
        """Configure GPIO pins and start both PWM channels at 0% duty.

        Raises:
            RuntimeError: If the pins are already in use or GPIO setup
                otherwise fails (wrapped for a clearer field-debug message).
        """
        try:
            GPIO.setup(self.pins.rpwm, GPIO.OUT)
            GPIO.setup(self.pins.lpwm, GPIO.OUT)
            GPIO.setup(self.pins.ren, GPIO.OUT)
            GPIO.setup(self.pins.len_, GPIO.OUT)

            GPIO.output(self.pins.ren, GPIO.HIGH)
            GPIO.output(self.pins.len_, GPIO.HIGH)

            self.rpwm = GPIO.PWM(self.pins.rpwm, self.pwm_frequency)
            self.lpwm = GPIO.PWM(self.pins.lpwm, self.pwm_frequency)
            self.rpwm.start(0)
            self.lpwm.start(0)
        except Exception as exc:
            raise RuntimeError(f'Failed to initialize motor on pins {self.pins}: {exc}') from exc

    def set_speed(self, value: float) -> None:
        """Drive the motor at ``value`` in [-1.0, 1.0] (negative = reverse)."""
        if self.rpwm is None or self.lpwm is None:
            raise RuntimeError('Motor not initialized.')

        value = max(-1.0, min(1.0, value))
        duty = abs(value) * 100.0

        if value > 0:
            self.lpwm.ChangeDutyCycle(0)
            self.rpwm.ChangeDutyCycle(duty)
        elif value < 0:
            self.rpwm.ChangeDutyCycle(0)
            self.lpwm.ChangeDutyCycle(duty)
        else:
            self.rpwm.ChangeDutyCycle(0)
            self.lpwm.ChangeDutyCycle(0)

    def stop(self) -> None:
        if self.rpwm:
            self.rpwm.ChangeDutyCycle(0)
        if self.lpwm:
            self.lpwm.ChangeDutyCycle(0)

    def cleanup(self) -> None:
        self.stop()
        if self.rpwm:
            self.rpwm.stop()
        if self.lpwm:
            self.lpwm.stop()


class TankDrive:
    """Combines two motors and a PS4 controller into an arcade-drive tank.

    Control scheme:
    - Left stick Y: throttle (forward/reverse)
    - Right stick X: turn
    - L1 / R1: slow mode / full-speed override
    - D-pad: fixed low-speed nudges (precise positioning)
    - PS button: exit
    """

    def __init__(self, config: DriveConfig | None = None) -> None:
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)

        self.left_motor = BTS7960Motor(
            MotorPins(rpwm=12, lpwm=35, ren=16, len_=18)
        )
        self.right_motor = BTS7960Motor(
            MotorPins(rpwm=32, lpwm=33, ren=22, len_=36)
        )
        self.config = config or DriveConfig()
        self.controller = PS4Controller(deadzone=self.config.controller_deadzone)
        self.running = True
        # Last commanded speeds, used as the starting point for slew-rate limiting.
        self._current_left = 0.0
        self._current_right = 0.0

    def setup(self) -> None:
        self.left_motor.setup()
        self.right_motor.setup()
        self.controller.connect()

    def expo(self, value: float) -> float:
        """Blend linear/cubic response for finer control near center stick."""
        factor = self.config.expo_factor
        return (1 - factor) * value + factor * (value ** 3)

    def mix_arcade(self, throttle: float, turn: float) -> tuple[float, float]:
        """Combine throttle/turn into independent left/right motor commands."""
        left = throttle + turn
        right = throttle - turn
        scale = max(1.0, abs(left), abs(right))
        return left / scale, right / scale

    def _slew(self, current: float, target: float, dt: float) -> float:
        """Ramp ``current`` toward ``target`` at most ``slew_rate_per_sec``.

        Prevents commanding an instantaneous full-forward-to-full-reverse
        change, which would otherwise force the H-bridge through a large,
        near-instant current swing and shock-load the gearbox.
        """
        max_delta = self.config.slew_rate_per_sec * dt
        delta = max(-max_delta, min(max_delta, target - current))
        return current + delta

    def compute_targets(self, state: dict) -> tuple[float, float, float, float]:
        """Compute (throttle, turn, target_left, target_right) from controller state."""
        throttle = self.expo(-state['left_stick'][1])
        turn = self.expo(state['right_stick'][0]) * self.config.turn_gain

        if state['buttons']['r1']:
            speed_limit = self.config.boost_speed
        elif state['buttons']['l1']:
            speed_limit = self.config.slow_speed
        else:
            speed_limit = self.config.max_speed

        left, right = self.mix_arcade(throttle, turn)
        left *= speed_limit
        right *= speed_limit

        precision = self.config.precision_speed
        if state['dpad'] == (0, 1):
            left = right = precision
        elif state['dpad'] == (0, -1):
            left = right = -precision
        elif state['dpad'] == (-1, 0):
            left, right = -precision, precision
        elif state['dpad'] == (1, 0):
            left, right = precision, -precision

        return throttle, turn, left, right

    def apply_drive(self, state: dict, dt: float) -> tuple[float, float, float, float]:
        """Compute targets from controller state, slew-limit, and drive motors."""
        throttle, turn, target_left, target_right = self.compute_targets(state)

        self._current_left = self._slew(self._current_left, target_left, dt)
        self._current_right = self._slew(self._current_right, target_right, dt)

        self.left_motor.set_speed(self._current_left)
        self.right_motor.set_speed(self._current_right)
        return throttle, turn, self._current_left, self._current_right

    def stop_all(self) -> None:
        self.left_motor.stop()
        self.right_motor.stop()
        self._current_left = 0.0
        self._current_right = 0.0

    def _reconnect(self) -> None:
        """Block, retrying at a fixed interval, until the controller is back."""
        while self.running:
            try:
                self.controller.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup before retry
                pass
            try:
                self.controller.connect()
                logger.info('Controller reconnected: %s', self.controller.name)
                return
            except RuntimeError:
                time.sleep(self.config.reconnect_retry_sec)

    def run(self) -> None:
        print('Tank control started.')
        print('Left stick Y = throttle | Right stick X = turn')
        print('L1 = slow mode | R1 = full speed | PS = exit')
        print('D-pad = low-speed precise movement')
        print('Press Ctrl+C to stop.\n')

        loop_period = 1.0 / self.config.loop_hz
        last_time = time.monotonic()
        try:
            while self.running:
                loop_start = time.monotonic()
                # Clamp dt so a long reconnect pause never bypasses slew limiting.
                dt = min(loop_start - last_time, loop_period)
                last_time = loop_start

                try:
                    pygame.event.pump()
                    if pygame.joystick.get_count() == 0:
                        raise pygame.error('Controller disconnected')
                    state = self.controller.snapshot()
                except pygame.error as exc:
                    print()
                    logger.warning('Controller connection lost (%s). Stopping motors and reconnecting...', exc)
                    self.stop_all()
                    self._reconnect()
                    continue

                if state['buttons']['ps']:
                    print('\nPS button pressed. Exiting.')
                    break

                throttle, turn, left, right = self.apply_drive(state, dt)
                pressed = [k for k, v in state['buttons'].items() if v]
                line = (
                    f'THR={throttle: .2f} TURN={turn: .2f} | '
                    f'LEFT={left: .2f} RIGHT={right: .2f} | '
                    f'BTN={pressed if pressed else []}'
                )
                print(line.ljust(160), end='\r', flush=True)

                elapsed = time.monotonic() - loop_start
                time.sleep(max(0.0, loop_period - elapsed))
        finally:
            self.stop_all()

    def cleanup(self) -> None:
        self.stop_all()
        self.left_motor.cleanup()
        self.right_motor.cleanup()
        self.controller.close()
        GPIO.cleanup()


def main() -> int:
    tank = TankDrive()

    def _handle_signal(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        tank.setup()
        tank.run()
        return 0
    except KeyboardInterrupt:
        print('\nStopped by user.')
        return 0
    except Exception:
        logger.exception('Fatal error.')
        return 1
    finally:
        tank.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
