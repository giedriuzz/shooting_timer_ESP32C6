"""Generic ESP32 320x240

"""

from machine import Pin, SPI
import st7789py as st7789

TFA = 40
BFA = 40
WIDE = 1
TALL = 0
SCROLL = 0      # orientation for scroll.py
FEATHERS = 1    # orientation for feathers.py

def config(rotation=0):
    """
    Configures and returns an instance of the ST7789 display driver.

    Args:
        rotation (int): The rotation of the display (default: 0).

    Returns:
        ST7789: An instance of the ST7789 display driver.
    """

    return st7789.ST7789(
        SPI(1, baudrate=40000000, sck=Pin(7), mosi=Pin(6), miso=None),
        172,
        320,
        reset=Pin(21, Pin.OUT),
        cs=Pin(14, Pin.OUT),
        dc=Pin(15, Pin.OUT),
        backlight=Pin(22, Pin.OUT),
        rotation=rotation)