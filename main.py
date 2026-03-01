"""
Shoot Timer
===========
A simple shoot timer for ESP32 with ST7789 display.
Uses Boot Button (Pin 0) and Select Button (Pin 5) for menu navigation and control.
"""

import machine
from machine import Pin, PWM

# Note: For ESP32-C3/C6, esp32 module might not have wake_on_ext0. 
# We will use machine.Pin(..., wake=machine.Pin.WAKE_LOW) if so.
try:
    WAKE_LOW_FLAG = machine.Pin.WAKE_LOW
except:
    WAKE_LOW_FLAG = None

if hasattr(machine, 'I2S'):
    from machine import I2S
    HAS_I2S = True
else:
    HAS_I2S = False

from time import sleep, ticks_ms, ticks_diff, ticks_us
import random
import network
import struct
import st7789py as st7789
import tft_config
import vga2_bold_16x32 as font

# --- Configuration ---
BTN_SCROLL_PIN = 0  # Button 1 for SCROLL/NEXT/INCREMENT
BTN_SELECT_PIN = 5  # Button 2 for SELECT/START/STOP
BACKLIGHT_PIN = 22
BUZZER_PIN = 9      # User requested Pin 9

# I2S Microphone Setup
I2S_SCK_PIN = 2
I2S_WS_PIN = 3
I2S_SD_PIN = 4

# Acoustic Shot Detection Tuning
SHOT_TRANSIENT_RATIO = 8
SHOT_COOLDOWN_MS = 150
BACKGROUND_SAMPLES = 10

# Default Session Settings
settings = {
    "brightness": 32768,       # 50% PWM Duty
    "start_delay_mode": 0,     # 0=Immediate, 1-10=Fixed Seconds, 11=Random 1-10s
    "par_time": 0.0,           # 0 = Off, >0 = Par Time (seconds, 0.5s increments)
    "par_reps": 0,            # Number of Repetitions / Sessions
    "par_rest": 0,          # Rest time between string repetitions (seconds)
    "par_shots": 0,            # Expected shots per session (0 = Off)
    "mode": 0,                 # 0 = Live Fire, 1 = Dry Fire
    "sensitivity": 800000,     # Acoustic threshold
    "buzzer_vol": 50,          # 0-100% volume
    "theme": 0,                # 0 = Dark, 1 = Light
    "sleep_time": 5 ,          # 0 = Off (Disabled by default to prevent reboot loops)
    "bluetooth": 0,            # 0 = Off, 1 = On
    "rand_delay_min": 1,       # Minimum random delay in seconds
    "rand_delay_max": 5,       # Maximum random delay in seconds
}

def get_energy_threshold():
    # If mode is dry fire, drop threshold drastically (e.g., clicking sound)
    # If mode is live fire, strictly use configured sensitivity
    if settings["mode"] == 1:
        return 10000 # Very sensitive for dry fire clicks
    return settings["sensitivity"]

# Colors (Fallback/Constants if needed, but using functions dynamically below)
COLOR_BG = st7789.BLACK
COLOR_TEXT = st7789.WHITE

# Dynamic Theme Colors
def get_bg_color():
    return st7789.WHITE if settings["theme"] == 1 else st7789.BLACK

def get_text_color():
    return st7789.BLACK if settings["theme"] == 1 else st7789.WHITE
    
def get_accent_color():
    # Red for both themes (contrast is fine on both black and white)
    return st7789.RED

def get_ready_color():
    # Yellow on black, Blue on white
    return st7789.BLUE if settings["theme"] == 1 else st7789.YELLOW
    
def get_go_color():
    # Green on black, Dark Green on white (RGB565: 0x03E0)
    return 0x03E0 if settings["theme"] == 1 else st7789.GREEN
    
def get_sel_color():
    # Cyan on black, Blue on white
    return st7789.BLUE if settings["theme"] == 1 else st7789.CYAN

# Globals
app_state = "BOOT"
menu_idx = 0
current_menu = "MAIN"
shot_history = []
last_activity_time = ticks_ms()

tft = None
btn_scroll = None
btn_select = None
buzzer = None
audio_in = None
mic_buffer = None
mic_working = False

# Disable networking permanently to save power
try:
    network.WLAN(network.STA_IF).active(False)
    network.WLAN(network.AP_IF).active(False)
except: pass

try:
    tft = tft_config.config(tft_config.WIDE)
    tft.rotation(1)
    tft.fill(get_bg_color())
except Exception as e:
    print(f"TFT Init Failed: {e}")

try:
    backlight_pwm = PWM(Pin(BACKLIGHT_PIN))
    backlight_pwm.freq(1000)
    backlight_pwm.duty_u16(settings["brightness"])
except: pass

try:
    btn_scroll = Pin(BTN_SCROLL_PIN, Pin.IN, Pin.PULL_UP)
    btn_select = Pin(BTN_SELECT_PIN, Pin.IN, Pin.PULL_UP)
except Exception as e:
    print(f"Button Init Failed: {e}")

try:
    buzzer = Pin(BUZZER_PIN, Pin.OUT)
    buzzer.value(1) # Idle HIGH (OFF) for PNP transistor
    print("Buzzer init OK")
except:
    buzzer = None

if HAS_I2S:
    mic_buffer = bytearray(1024)
    try:
        audio_in = I2S(0, 
                   sck=Pin(I2S_SCK_PIN), 
                   ws=Pin(I2S_WS_PIN), 
                   sd=Pin(I2S_SD_PIN), 
                   mode=I2S.RX, bits=32, format=I2S.MONO, rate=16000, ibuf=4096)
        mic_working = True
    except:
        mic_working = False


# --- Helper Functions ---
def clear_screen():
    if tft: tft.fill(get_bg_color())

def draw_text(text, x, y, color=None):
    if color is None:
        color = get_text_color()
    if tft: tft.text(font, text, x, y, color, get_bg_color())

def beep(duration_ms=200):
    if buzzer and settings["buzzer_vol"] > 0:
        # Active buzzer with PNP transistor:
        # LOW (0) turns the transistor ON, HIGH (1) turns it OFF.
        # We ignore variable volume for active buzzers, just turn it on.
        buzzer.value(0) # Turn ON
        sleep(duration_ms / 1000)
        buzzer.value(1) # Turn OFF

def enter_deep_sleep():
    global tft, backlight_pwm, audio_in
    print("[SLEEP] Entering sleep sequence...")
    clear_screen()
    draw_text("SLEEPING...", 80, 100, get_accent_color())
    for _ in range(3):
        beep(100)
        sleep(0.1)
    sleep(0.5)
    
    # Turn off backlight and screen
    if tft: 
        print("[SLEEP] Turning off display...")
        tft.fill(st7789.BLACK)
    
    # Deinitialize peripherals to prevent crashes during lightsleep
    print("[SLEEP] Deinitializing PWM...")
    try:
        backlight_pwm.duty_u16(0)
        backlight_pwm.deinit()
    except Exception as e:
        print("[SLEEP] PWM deinit error:", e)

    if HAS_I2S and mic_working and audio_in:
        print("[SLEEP] Deinitializing I2S microphone...")
        try:
            audio_in.deinit()
        except Exception as e:
            print("[SLEEP] I2S deinit error:", e)

    print("[SLEEP] Disabling networking...")
    try:
        import network
        network.WLAN(network.STA_IF).active(False)
        network.WLAN(network.AP_IF).active(False)
    except Exception as e: 
        print("[SLEEP] Network disable error:", e)
    
    # To guarantee stability on ESP32-C6, we will use a "Soft Sleep". 
    # We leave the CPU awake but in a slow polling loop, which still saves power 
    # since we've disabled the high-drain peripherals (TFT, WiFi, I2S).
    print("[SLEEP] Entering Soft Sleep polling loop...")
    while True:
        # Check if button is pressed
        if btn_select.value() == 0:
            # Debounce
            sleep(0.05)
            if btn_select.value() == 0:
                print("[WAKE] Waking from Soft Sleep!")
                break
        
        # Slow polling loop to keep CPU usage minimal
        sleep(0.1)

    print("[WAKE] Waiting for button release...")
    while btn_select.value() == 0:
        sleep(0.05)
        
    print("[WAKE] Restoring display...")
    # When we wake up, restore display color and backlight
    if tft:
        try:
            # The ST7789 driver doesn't have a specific wake command in this library
            # So we re-init the whole config to ensure it turns back on properly
            global tft
            tft = tft_config.config(tft_config.WIDE)
            tft.rotation(1)
            tft.fill(get_bg_color())
        except Exception as e:
            print("[WAKE] Wake TFT error:", e)

    print("[WAKE] Restoring backlight PWM...")
    try:
        backlight_pwm = PWM(Pin(BACKLIGHT_PIN))
        backlight_pwm.freq(1000)
        backlight_pwm.duty_u16(settings["brightness"])
    except Exception as e:
        print("[WAKE] Wake PWM error:", e)

    print("[WAKE] Restoring I2S Microphone...")
    if HAS_I2S and mic_working:
        try:
            audio_in = I2S(0, 
                   sck=Pin(I2S_SCK_PIN), 
                   ws=Pin(I2S_WS_PIN), 
                   sd=Pin(I2S_SD_PIN), 
                   mode=I2S.RX, bits=32, format=I2S.MONO, rate=16000, ibuf=4096)
        except Exception as e:
            print("[WAKE] Wake I2S error:", e)
            
    print("[WAKE] Wake sequence complete.")

def get_button_action():
    """
    Returns:
    0 = None
    1 = Scroll (Short Press Btn 1)
    2 = Select (Short Press Btn 2)
    3 = Back/Long Select (Long Press Btn 2) -> 400ms - 3000ms
    4 = Ultra Long Select (Extra Long Press Btn 2) -> 3000ms+
    """
    global last_activity_time
    
    if btn_scroll.value() == 0:
        last_activity_time = ticks_ms()
        sleep(0.05) # Debounce
        if btn_scroll.value() == 0:
            while btn_scroll.value() == 0: pass # Wait release
            return 1
            
    if btn_select.value() == 0:
        last_activity_time = ticks_ms()
        sleep(0.05) # Debounce
        if btn_select.value() == 0:
            start_t = ticks_ms()
            while btn_select.value() == 0: pass # Wait release
            duration = ticks_diff(ticks_ms(), start_t)
            if duration > 3000:
                return 4 # Ultra-Long press
            elif duration > 400:
                return 3 # Long press
            return 2 # Short press
    return 0


# --- Menu Definitions ---
MAIN_MENU = [] # Dynamically updated based on shot history. Starts completely empty.
# SETTINGS_MENU, SETUP_MENU, CALIB_MENU, SYS_MENU are primarily static bases
SETTINGS_MENU = ["Setup", "Calibrate", "System", "<- Back"]
SETUP_MENU_BASE = ["Start Delay", "Par Time", "Par Reps", "Par Rest", "Par Shots", "Mode", "<- Back"]
CALIB_MENU = ["Sensitivity", "Buzzer Vol", "<- Back"]
SYS_MENU = ["Brightness", "Theme", "Sleep Time", "Bluetooth", "<- Back"]

def draw_menu(items, selected_idx):
    clear_screen()
    
    is_main = (items == MAIN_MENU)
    
    if is_main:
        # User requested the screen to be blank on start. 
        # So we no longer draw "START" statically at the top.
        
        # Draw the actual menu items below
        y = 60
        x_offset = 30
        y_step = 40
        if len(items) > 0:
            for i, text in enumerate(items):
                color = get_sel_color() if i == selected_idx else get_text_color()
                prefix = ">" if i == selected_idx else " "
                draw_text(f"{prefix}{text}", x_offset, y, color)
                y += y_step
        else:
            draw_text("Press START", 72, 60, get_text_color())
            draw_text("to start !", 80, 100, get_text_color())

        # Delay Mode Indicator
        if settings["start_delay_mode"] == 0:
            delay_char = "Imm"
        elif settings["start_delay_mode"] <= 10:
            delay_char = "Del"
        else:
            delay_char = "Rnd"
        draw_text(delay_char, 5, 5, get_text_color())

        if settings.get("bluetooth", 0) == 1:
            draw_text("BL", 300, 5, get_text_color())

        """ if len(items) > 0:
            if settings["par_time"] > 0:
                draw_text(f"PAR: {settings['par_time']:.1f}s", 5, 140, get_ready_color()) """
            
        return # Main menu drawn, skip generic drawing
    
    # Generic drawing for submenus
    y = 5
    x_offset = 5
    y_step = 33
    
    start_idx = 0
    if selected_idx > 3:
        start_idx = selected_idx - 3
        
    end_idx = min(len(items), start_idx + 4)
    # Ensure we show 4 items if possible
    if end_idx - start_idx < 4 and len(items) >= 4:
        start_idx = len(items) - 4
        end_idx = len(items)

    for i in range(start_idx, end_idx):
        color = get_sel_color() if i == selected_idx else get_text_color()
        prefix = ">" if i == selected_idx else " "
        
        text = items[i]
        if text == "Start Delay":
            val = settings['start_delay_mode']
            if val == 0:
                text = "Delay: Immed"
            elif val <= 10:
                text = f"Delay: {val}s"
            else:
                text = "Delay: Random"
        elif text == "Rnd Min": text = f"Rnd Min: {settings['rand_delay_min']}s"
        elif text == "Rnd Max": text = f"Rnd Max: {settings['rand_delay_max']}s"
        elif text == "Par Time": text = f"Par Tm: {settings['par_time']:.1f}s"
        elif text == "Par Reps": text = f"Par Rp: {settings['par_reps']}"
        elif text == "Par Rest": text = f"Par Rt: {settings['par_rest']:.1f}s"
        elif text == "Par Shots": text = f"Par Sht:{settings['par_shots'] if settings['par_shots']>0 else 'Off'}"
        elif text == "Mode": text = f"Mode:{'Dry' if settings['mode']==1 else 'Live'}"
        elif text == "Bluetooth": text = f"BT:{'On' if settings.get('bluetooth', 0)==1 else 'Off'}"
        elif text == "Sensitivity": 
            mapped_val = int(settings['sensitivity'] / 100000)
            text = f"Sens: {mapped_val}"
        elif text == "Buzzer Vol": text = f"Buzz: {settings['buzzer_vol']}%"
        elif text == "Brightness": text = f"Bright:{int(settings['brightness']/65535*100)}%"
        elif text == "Theme": text = f"Theme:{'Light' if settings['theme'] == 1 else 'Dark'}"
        elif text == "Sleep Time": text = f"Sleep:{settings['sleep_time']}m" if settings['sleep_time'] > 0 else "Sleep:Off"
        
        draw_text(f"{prefix}{text}", x_offset, y, color)
        y += y_step

# --- State Machine App ---
def main():
    global app_state, menu_idx, current_menu, shot_history, last_activity_time
    
    last_activity_time = ticks_ms()

    # Boot Sequence
    clear_screen()
    draw_text("G electronic", 70, 70, get_accent_color()) # [ ] Sureguliuoti užrašo G-electronic lygiavimą
    sleep(3)
    beep(100)
    
    app_state = "MENU"
    menu_idx = 0
    current_menu = "MAIN"

    while True:
        if app_state == "MENU":
            if current_menu == "MAIN": 
                global MAIN_MENU
                if len(shot_history) > 0:
                    MAIN_MENU = ["Review"]
                else:
                    MAIN_MENU = []
                menu_list = MAIN_MENU
                if menu_idx >= len(menu_list): menu_idx = 0
            elif current_menu == "SETTINGS": menu_list = SETTINGS_MENU
            elif current_menu == "SETUP": 
                # Rebuild setup menu dynamically based on Random mode
                menu_list = SETUP_MENU_BASE.copy()
                if settings["start_delay_mode"] > 10:
                    menu_list.insert(1, "Rnd Min")
                    menu_list.insert(2, "Rnd Max")
                if menu_idx >= len(menu_list): menu_idx = 0
            elif current_menu == "CALIB": menu_list = CALIB_MENU
            elif current_menu == "REV_OPT":
                menu_list = ["Splits", "Clear"]
                if settings.get("bluetooth", 0) == 1:
                    menu_list.append("Send BT")
                menu_list.append("<- Back")
            else: menu_list = SYS_MENU
                
            draw_menu(menu_list, menu_idx)
            
            while app_state == "MENU":
                # Check for inactivity Deep Sleep if enabled
                if settings["sleep_time"] > 0:
                    sleep_ms = settings["sleep_time"] * 60 * 1000
                    if ticks_diff(ticks_ms(), last_activity_time) >= sleep_ms:
                        enter_deep_sleep()
                        
                        # Truly woke up. Wait for them to release the button if they were holding it
                        while btn_select.value() == 0:
                            sleep(0.05)
                            
                        # Show menu again
                        last_activity_time = ticks_ms()
                        draw_menu(menu_list, menu_idx)
                    
                act = get_button_action()
                if act == 1: # SCROLL
                    if len(menu_list) > 0:
                        menu_idx = (menu_idx + 1) % len(menu_list)
                    draw_menu(menu_list, menu_idx)
                
                elif act == 2: # SHORT SELECT
                    if len(menu_list) == 0:
                        selectedSTR = ""
                    else:
                        selectedSTR = menu_list[menu_idx]
                    
                    if current_menu == "MAIN":
                        # In the completely revamped MAIN menu, Short Select ALWAYS starts the timer
                        app_state = "STANDBY"
                    
                    elif current_menu == "SETTINGS":
                        if "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0; break
                    
                    elif current_menu == "REV_OPT":
                        if "Clear" in selectedSTR:
                            shot_history.clear()
                            current_menu = "MAIN"
                            menu_idx = 0
                            break
                        elif "Send BT" in selectedSTR:
                            # Placeholder for Bluetooth logic
                            draw_text("Sending...", 80, 80, get_accent_color())
                            sleep(1)
                            draw_menu(menu_list, menu_idx)
                        elif "Back" in selectedSTR:
                            current_menu = "MAIN"; menu_idx = 0; break
                    
                    elif current_menu == "SETUP":
                        if "Start Delay" in selectedSTR:
                            settings["start_delay_mode"] = (settings["start_delay_mode"] + 1) % 12
                            # If we switch to/from random mode, break to re-render the list immediately
                            if settings["start_delay_mode"] == 11 or settings["start_delay_mode"] == 0:
                                break
                        elif "Rnd Min" in selectedSTR:
                            settings["rand_delay_min"] += 1
                            if settings["rand_delay_min"] >= settings["rand_delay_max"]:
                                settings["rand_delay_min"] = 1
                        elif "Rnd Max" in selectedSTR:
                            settings["rand_delay_max"] += 1
                            if settings["rand_delay_max"] > 15:
                                settings["rand_delay_max"] = settings["rand_delay_min"] + 1
                        elif "Par Time" in selectedSTR:
                            settings["par_time"] += 0.5
                            if settings["par_time"] > 30.0: settings["par_time"] = 0.0
                        elif "Par Reps" in selectedSTR:
                            settings["par_reps"] += 1
                            if settings["par_reps"] > 10: settings["par_reps"] = 1
                        elif "Par Rest" in selectedSTR:
                            settings["par_rest"] += 0.5
                            if settings["par_rest"] > 15.0: settings["par_rest"] = 1.0
                        elif "Par Shots" in selectedSTR:
                            settings["par_shots"] += 1
                            if settings["par_shots"] > 20: settings["par_shots"] = 0
                        elif "Mode" in selectedSTR:
                            settings["mode"] = 1 if settings["mode"] == 0 else 0
                        elif "Back" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0; break
                        draw_menu(menu_list, menu_idx)

                    elif current_menu == "CALIB":
                        if "Sensitivity" in selectedSTR:
                            val = int(settings["sensitivity"] / 100000)
                            val = (val + 1) % 16
                            if val == 0: val = 1
                            settings["sensitivity"] = val * 100000
                        elif "Buzzer Vol" in selectedSTR:
                            settings["buzzer_vol"] = (settings["buzzer_vol"] + 10) % 110
                        elif "Back" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0; break
                        draw_menu(menu_list, menu_idx)

                    elif current_menu == "SYS":
                        if "Brightness" in selectedSTR:
                            cur_pct = int(settings["brightness"] / 65535 * 100)
                            cur_pct = (cur_pct + 10) % 110
                            if cur_pct == 0: cur_pct = 10
                            settings["brightness"] = int((cur_pct / 100) * 65535)
                            try:
                                backlight_pwm.duty_u16(settings["brightness"])
                            except: pass
                        elif "Theme" in selectedSTR:
                            settings["theme"] = 1 if settings["theme"] == 0 else 0
                        elif "Sleep Time" in selectedSTR:
                            settings["sleep_time"] += 1
                            if settings["sleep_time"] > 60: settings["sleep_time"] = 0
                        elif "Bluetooth" in selectedSTR:
                            settings["bluetooth"] = 1 if settings.get("bluetooth", 0) == 0 else 0
                        elif "Back" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0; break
                        draw_menu(menu_list, menu_idx)
                        
                elif act == 3: # LONG SELECT
                    if current_menu == "MAIN": 
                        if len(menu_list) > 0:
                            # In the MAIN menu, Long Select enters the currently highlighted sub-menu (Review)
                            selectedSTR = menu_list[menu_idx]
                            if "Review" in selectedSTR:
                                current_menu = "REV_OPT"; menu_idx = 0; break
                    elif current_menu == "SETTINGS": 
                        selectedSTR = menu_list[menu_idx]
                        if "Setup" in selectedSTR: current_menu = "SETUP"; menu_idx = 0; break
                        elif "Calibrate" in selectedSTR: current_menu = "CALIB"; menu_idx = 0; break
                        elif "System" in selectedSTR: current_menu = "SYS"; menu_idx = 0; break
                        elif "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0; break
                    elif current_menu == "REV_OPT":
                        selectedSTR = menu_list[menu_idx]
                        if "Splits" in selectedSTR: app_state = "REVIEW"; break
                        elif "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0; break
                    else: 
                        current_menu = "SETTINGS"; menu_idx = 0; break

                elif act == 4: # ULTRA LONG SELECT (>3s)
                    if current_menu == "MAIN":
                        # Directly enter SETTINGS from anywhere on the MAIN menu
                        current_menu = "SETTINGS"; menu_idx = 0; break
                        
                if app_state != "MENU": break
                
                sleep(0.05)

        elif app_state == "STANDBY":
            clear_screen()
            draw_text("STANDBY...", 80, 100, get_ready_color())
            
            # Determine actual delay based on start_delay_mode
            d_mode = settings["start_delay_mode"]
            if d_mode == 0:
                delay_ms = 0
            elif d_mode <= 10:
                delay_ms = d_mode * 1000
            else:
                # Seed heavily entropy-based microsecond timing of the user's button press
                random.seed(ticks_us())
                delay_ms = random.randint(settings["rand_delay_min"] * 1000, settings["rand_delay_max"] * 1000)
            
            # Cancellable wait loop
            start_wait = ticks_ms()
            cancelled = False
            while ticks_diff(ticks_ms(), start_wait) < delay_ms:
                if get_button_action() != 0: 
                    cancelled = True
                    break
                sleep(0.05)
                
            if cancelled:
                app_state = "MENU"
            else:
                beep(800)
                app_state = "RUNNING"
                
        elif app_state == "RUNNING":
            
            # Ensure buttons are released before starting the run loop
            # Otherwise, the button press that started the session might immediately stop it
            while btn_scroll.value() == 0 or btn_select.value() == 0:
                sleep(0.01)
                
            shot_history = []
            
            # Drain mic buffer before start
            if mic_working:
                for _ in range(5): audio_in.readinto(mic_buffer)

            par_time_ms = int(settings["par_time"] * 1000)
            par_shots_req = settings["par_shots"]
            
            running = True
            
            # If par_reps is 0 but we started, we should at least run 1 infinite session.
            reps_to_run = max(1, settings["par_reps"])
            
            # Loop through the configured number of repetitions (sessions)
            for rep in range(reps_to_run):
                if not running: break
                
                # If not the first rep, apply the Rest Delay and start beep
                # Only apply rest if we are actually doing multiple configured reps
                if rep > 0 and settings["par_reps"] > 1:
                    clear_screen()
                    draw_text(f"REST: {settings['par_rest']}s", 20, 40, get_ready_color())
                    draw_text(f"Next: Rep {rep+1}", 20, 80, get_text_color())
                    
                    start_rest = ticks_ms()
                    rest_ms = int(settings["par_rest"] * 1000)
                    while ticks_diff(ticks_ms(), start_rest) < rest_ms:
                        if get_button_action() != 0: 
                            running = False
                            break
                        sleep(0.05)
                        
                    if not running: break
                    beep(1000) # GO beep again for the next rep!
                
                # --- START OF ACTIVE REP ---
                clear_screen()
                
                # Only show REP text if par settings are active
                if settings["par_time"] > 0 or settings["par_reps"] > 0 or settings["par_shots"] > 0:
                    rep_str = f"REP {rep+1}/{settings['par_reps']}"
                    # 8-9 chars -> 128-144px width. Let's say 128px. Center = (320-128)/2 = 96
                    rep_w = len(rep_str) * 16
                    rep_x = (320 - rep_w) // 2
                    draw_text(rep_str, rep_x, 5, get_ready_color())
                
                start_time = ticks_ms()
                shot_count = 0
                last_shot_time = 0
                last_display_time = -1 # Used to track when we should redraw the timer
                
                bg_noise_level = 0
                bg_history = [0] * BACKGROUND_SAMPLES
                bg_idx = 0
                
                par_beeped = False
                rep_finished = False

                while running and not rep_finished:
                    current_time = ticks_diff(ticks_ms(), start_time)
                    
                    # --- Live Timer Display on LCD ---
                    # We only update the display every 10ms (1/100th second) to prevent extreme flickering
                    display_val = current_time // 10 
                    if display_val != last_display_time:
                        last_display_time = display_val
                        time_str = f"{current_time/1000:.2f}"
                        # Center the live timer display (approx. 5 chars -> 80px)
                        timer_x = (320 - (len(time_str) * 16)) // 2
                        
                        # We only clear the specific rect where the timer draws to minimize flicker
                        # 40px down, 100 wide, 35 high
                        tft.fill_rect((320 - 120) // 2, 40, 120, 35, get_bg_color())
                        draw_text(time_str, timer_x, 40, get_text_color())

                    # Stop via ANY Button Press
                    act = get_button_action()
                    if act != 0:
                        running = False
                        break
                        
                    # Check Par Time Beep
                    if par_time_ms > 0 and current_time >= par_time_ms and not par_beeped:
                        # Only beep Par Time if we aren't about to finish the entire session.
                        # If this is the last rep, the double beep at the end will signify completion.
                        if rep < reps_to_run - 1:
                            beep(300)
                        par_beeped = True
                        
                        # If Par Shots is OFF (0), reaching Par Time completes the Session string
                        if par_shots_req == 0:
                            rep_finished = True
                            sleep(1.0) # Let the beep finish so they see the last time before clearing

                    # Detection Logic
                    max_val = 0
                    avg_energy = 0
                    samples_read = 0
                    
                    if mic_working:
                        num_bytes = audio_in.readinto(mic_buffer)
                        if num_bytes > 0:
                            total_energy = 0
                            for i in range(0, num_bytes, 4):
                                val = struct.unpack('<i', mic_buffer[i:i+4])[0]
                                if val < 0: val = -val
                                val = val >> 8 
                                if val > max_val: max_val = val
                                total_energy += val
                                samples_read += 1
                            
                            if samples_read > 0:
                                avg_energy = total_energy // samples_read

                            if max_val < get_energy_threshold() // 2:
                                bg_history[bg_idx] = avg_energy
                                bg_idx = (bg_idx + 1) % BACKGROUND_SAMPLES
                                bg_noise_level = sum(bg_history) // BACKGROUND_SAMPLES

                    time_since_last = ticks_diff(current_time, last_shot_time) if shot_count > 0 else 99999
                    
                    is_loud_enough = max_val > get_energy_threshold()
                    is_transient_spike = avg_energy > (bg_noise_level * SHOT_TRANSIENT_RATIO) or bg_noise_level == 0
                    is_cooled_down = time_since_last > SHOT_COOLDOWN_MS
                    
                    mic_shot_detected = is_loud_enough and is_transient_spike and is_cooled_down
                    
                    if mic_shot_detected:
                       shot_count += 1
                       
                       split_time = ticks_diff(current_time, last_shot_time) if shot_count > 1 else 0
                       last_shot_time = current_time
                       
                       global_shot_count = len(shot_history) + 1
                       shot_history.append((global_shot_count, shot_count, rep + 1, last_shot_time, split_time))
                       
                       msg = f"#{shot_count}: {last_shot_time/1000:.2f}s"
                       split_str = f"Sp: {split_time/1000:.2f}s" if shot_count > 1 else ""
                       print(f"Shot detected! Time: {last_shot_time/1000:.2f}s | Rep {rep+1} | Peak: {max_val} | Thresh: {get_energy_threshold()}")
                       
                       # Center calculations for live shot info
                       msg_w = len(msg) * 16
                       msg_x = (320 - msg_w) // 2
                       
                       spl_w = len(split_str) * 16
                       spl_x = (320 - spl_w) // 2
                       
                       # Clear the specific drawing area before drawing new text
                       tft.fill_rect(0, 80, 320, 80, get_bg_color())
                       
                       tft.text(font, msg, msg_x, 80, get_text_color(), get_bg_color())
                       if split_str:
                           tft.text(font, split_str, spl_x, 120, get_accent_color(), get_bg_color())
                       
                       if mic_working:
                           for _ in range(3): audio_in.readinto(mic_buffer) 
                           
                       # If we have reached the expected shots for this Par session, end it early!
                       if par_shots_req > 0 and shot_count >= par_shots_req:
                           rep_finished = True
                           sleep(1.0) # Show the time for a second before going to rest
                    
                    sleep(0.01)

            if running:
                # Session completed normally
                beep(200)
                sleep(0.2)
                beep(200)

            app_state = "REVIEW"

        elif app_state == "REVIEW":
            if len(shot_history) > 0:
                clear_screen()
                
                # We separate out the review grouping by REP
                # Form a dictionary or list of sub-lists grouped by Rep
                reps_data = {}
                for shot in shot_history:
                    # shot = (global_shot_count, rep_shot_count, rep_num, total_time, split_time)
                    r_num = shot[2]
                    if r_num not in reps_data:
                        reps_data[r_num] = []
                    reps_data[r_num].append(shot)
                
                sorted_reps = sorted(list(reps_data.keys()))
                
                # "--- RESULTS ---"
                draw_text("--- RESULTS ---", 40, 0, get_ready_color())
                
                total_shots_all = len(shot_history)
                draw_text(f"Total Reps: {len(sorted_reps)}", 10, 45, get_ready_color())
                draw_text(f"Total Shots: {total_shots_all}", 10, 85, get_text_color())
                     
                draw_text("SEL:Exit  SCR:Review", 8, 125, get_ready_color())
                
                while True:
                    act = get_button_action()
                    if act != 0: break
                    sleep(0.05)
                
                if act == 2 or act == 3: 
                    app_state = "MENU"
                    continue
                
                # User hit SCROLL, start reviewing rep by rep, shot by shot
                rep_list_idx = 0
                while rep_list_idx < len(sorted_reps):
                    current_rep_num = sorted_reps[rep_list_idx]
                    rep_shots = reps_data[current_rep_num]
                    
                    rep_total_time = rep_shots[-1][3] if rep_shots else 0
                    
                    # Show Rep Summary first
                    clear_screen()
                    draw_text(f"--- REP {current_rep_num} ---", 40, 5, get_ready_color())
                    draw_text(f"Shots: {len(rep_shots)}", 10, 45, get_text_color())
                    draw_text(f"Time: {rep_total_time/1000:.2f}s", 10, 85, get_go_color())
                    draw_text("SCR:Splits SEL:Skip", 8, 125, get_text_color())
                    
                    skip_rep_splits = False
                    while True:
                        act = get_button_action()
                        if act == 1: 
                            skip_rep_splits = True
                            break # Skip this rep's splits, go to next rep summary
                        elif act == 2 or act == 3:
                            break # Go view splits
                        sleep(0.05)
                    
                    if skip_rep_splits:
                        rep_list_idx += 1
                        if rep_list_idx >= len(sorted_reps):
                            app_state = "MENU"
                        continue
                    
                    # Scroll through the specific splits for this Rep
                    shot_idx = 0
                    exit_review = False
                    while shot_idx < len(rep_shots):
                        clear_screen()
                        s_global_count, s_count, s_rep, s_total, s_split = rep_shots[shot_idx]
                        
                        shot_text = f"Rep {s_rep} - #{s_count}/{len(rep_shots)}"
                        shot_w = len(shot_text) * 16
                        shot_x = (320 - shot_w) // 2
                        draw_text(shot_text, shot_x, 5, get_ready_color())
                        
                        draw_text(f"Tm: {s_total/1000:.2f}s", 10, 45, get_text_color())
                        
                        if s_count > 1:
                            draw_text(f"St. {s_count-1}-{s_count}: Sp. {s_split/1000:.2f}s", 10, 85, get_ready_color())
                        else:
                            draw_text("Sp. Shot: ----", 10, 85, get_accent_color())
                            
                        draw_text("SCR:Bk SEL:Nx/Ex", 16, 125, get_text_color())
                            
                        while True:
                            act = get_button_action()
                            if act == 1: # Next split
                                shot_idx += 1
                                break
                            elif act == 2: # Back
                                if shot_idx > 0:
                                    shot_idx -= 1
                                break
                            elif act == 3: # Exit review entirely
                                exit_review = True
                                break
                            sleep(0.05)
                            
                        if exit_review:
                            break
                    
                    if exit_review:
                         app_state = "MENU"
                         break
                    
                    # Reached end of splits for this Rep, go to next Rep
                    rep_list_idx += 1
                    
                app_state = "MENU"
            else:
                draw_text("STOPPED", 100, 200, get_accent_color())
                sleep(2)
                app_state = "MENU"
                
        sleep(0.1)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print("CRASHED:", e)
        # Prevent immediately dropping to REPL without showing something if possible
        try:
            draw_text("ERROR", 10, 10, get_accent_color())
        except: pass
