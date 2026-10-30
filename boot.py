# boot.py - runs first. Keeps every relay OFF while the rest of the program loads.
from machine import Pin

RELAY_PINS = (4, 15, 5, 17)   # must match DEVICES in main.py
RELAY_ACTIVE_LOW = True       # most relay modules switch on when the input goes LOW

for p in RELAY_PINS:
    Pin(p, Pin.OUT, value=1 if RELAY_ACTIVE_LOW else 0)
