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

from time import sleep, ticks_ms, ticks_diff
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
    "delay_min": 2,            # Random start min (seconds)
    "delay_max": 6,            # Random start max (seconds)
    "par_time": 0.0,           # 0 = Off, >0 = Par Time (seconds, 0.5s increments)
    "par_reps": 0,            # Number of Repetitions / Sessions
    "par_rest": 0,          # Rest time between string repetitions (seconds)
    "par_shots": 0,            # Expected shots per session (0 = Off)
    "mode": 0,                 # 0 = Live Fire, 1 = Dry Fire
    "sensitivity": 800000,     # Acoustic threshold
    "buzzer_vol": 50,          # 0-100% volume
    "theme": 0,                # 0 = Dark, 1 = Light
    "sleep_time": 5,           # Inactivity timeout to Deep Sleep (minutes, 0 = Off)
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

# --- Hardware Setup ---
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
    global tft, backlight_pwm
    clear_screen()
    draw_text("SLEEPING...", 80, 100, get_accent_color())
    for _ in range(3):
        beep(100)
        sleep(0.1)
    sleep(0.5)
    
    # Turn off backlight and screen
    if tft: tft.fill(st7789.BLACK)
    try:
        backlight_pwm.duty_u16(0)
    except: pass
    
    # Configure wake up from Select Button (Pin 5) pulling LOW
    wake_pin = Pin(BTN_SELECT_PIN, Pin.IN, Pin.PULL_UP)
    
    # Generic machine API (ESP32-C3, ESP32-C6, etc)
    if WAKE_LOW_FLAG is not None:
         wake_pin.irq(handler=lambda t: None, trigger=Pin.IRQ_FALLING, wake=machine.SLEEP)
            
    # Enter Light Sleep (behaves like deep sleep on C6 but wakes cleanly from GPIO)
    machine.lightsleep()
    
    # Disable the IRQ immediately so it doesn't fire repeatedly
    if WAKE_LOW_FLAG is not None:
         wake_pin.irq(handler=None, wake=0)
    # When we wake up, restore display color and backlight
    if tft:
        try:
            tft.fill(get_bg_color())
        except Exception as e:
            print("Wake TFT error:", e)

    try:
        backlight_pwm = PWM(Pin(BACKLIGHT_PIN))
        backlight_pwm.freq(1000)
        backlight_pwm.duty_u16(settings["brightness"])
    except Exception as e:
        print("Wake PWM error:", e)

def get_button_action():
    """
    Returns:
    0 = None
    1 = Scroll (Short Press Btn 1)
    2 = Select (Short Press Btn 2)
    3 = Back/Long Select (Long Press Btn 2)
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
            if duration > 800:
                return 3 # Long press
            return 2 # Short press
    return 0


# --- Menu Definitions ---
MAIN_MENU = ["START", "Settings"]
SETTINGS_MENU = ["Setup", "Calibrate", "System", "<- Back"]
SETUP_MENU = ["Delay Min", "Delay Max", "Par Time", "Par Reps", "Par Rest", "Par Shots", "Mode", "<- Back"]
CALIB_MENU = ["Sensitivity", "Buzzer Vol", "<- Back"]
SYS_MENU = ["Brightness", "Theme", "Sleep Time", "<- Back"]

def draw_menu(items, selected_idx):
    clear_screen()
    
    is_main = (items == MAIN_MENU)
    
    if is_main:
        # [ ] MENU ALIGNMENT: Adjust START_X, START_Y to center the START text
        # Screen is 320x172 (landscape). Font is 16x32. ">START" = 96px, so X = 112
        START_X = 112 
        START_Y = 40
        
        # [ ] MENU ALIGNMENT: Adjust SETTINGS_X, SETTINGS_Y to set spacing from START
        # ">Settings" = 144px, so X = 88
        SETTINGS_X = 88
        SETTINGS_Y = 100
        
        for i, text in enumerate(items):
            color = get_sel_color() if i == selected_idx else get_text_color()
            prefix = ">" if i == selected_idx else " "
            
            if text == "START":
                draw_text(f"{prefix}{text}", START_X, START_Y, color)
            elif text == "Settings":
                draw_text(f"{prefix}{text}", SETTINGS_X, SETTINGS_Y, color)

        if settings["par_time"] > 0:
            draw_text(f"PAR: {settings['par_time']:.1f}s", 5, 205, get_ready_color())
            
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
        if text == "Delay Min": text = f"St.Min: {settings['delay_min']}s"
        elif text == "Delay Max": text = f"St.Max: {settings['delay_max']}s"
        elif text == "Par Time": text = f"Par Tm: {settings['par_time']:.1f}s"
        elif text == "Par Reps": text = f"Par Rp: {settings['par_reps']}"
        elif text == "Par Rest": text = f"Par Rt: {settings['par_rest']:.1f}s"
        elif text == "Par Shots": text = f"Par Sht:{settings['par_shots'] if settings['par_shots']>0 else 'Off'}"
        elif text == "Mode": text = f"Mode:{'Dry' if settings['mode']==1 else 'Live'}"
        elif text == "Sensitivity": 
            mapped_val = int(settings['sensitivity'] / 100000)
            text = f"Sens: {mapped_val}"
        elif text == "Buzzer Vol": text = f"Buzz: {settings['buzzer_vol']}%"
        elif text == "Brightness": text = f"Bright:{int(settings['brightness']/65535*100)}%"
        elif text == "Theme": text = f"Theme:{'Light' if settings['theme'] == 1 else 'Dark'}"
        elif text == "Sleep Time": text = f"Sleep:{settings['sleep_time']}m" if settings['sleep_time'] > 0 else "Sleep:Off"
        
        draw_text(f"{prefix}{text}", x_offset, y, color)
        y += y_step

    if is_main and settings["par_time"] > 0:
        draw_text(f"PAR: {settings['par_time']:.1f}s", 5, 205, get_ready_color())

# --- State Machine App ---
def main():
    global app_state, menu_idx, current_menu, shot_history, last_activity_time
    
    last_activity_time = ticks_ms()

    # Boot Sequence
    clear_screen()
    draw_text("G electronic", 70, 70, get_accent_color()) # [ ] Sureguliuoti užrašo G-electronic lygiavimą
    sleep(3)
    clear_screen()
    draw_text("Shooting timer", 50, 70, get_ready_color()) # [ ] Sureguliuoti užrašo "Shooting timer" lygiavimą
    sleep(2)
    beep(100)
    
    app_state = "MENU"
    menu_idx = 0
    current_menu = "MAIN"

    while True:
        if app_state == "MENU":
            if current_menu == "MAIN": menu_list = MAIN_MENU
            elif current_menu == "SETTINGS": menu_list = SETTINGS_MENU
            elif current_menu == "SETUP": menu_list = SETUP_MENU
            elif current_menu == "CALIB": menu_list = CALIB_MENU
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
                    menu_idx = (menu_idx + 1) % len(menu_list)
                    draw_menu(menu_list, menu_idx)
                
                elif act == 3: # LONG SELECT -> Back to main
                    if current_menu == "MAIN": pass
                    elif current_menu == "SETTINGS": 
                        current_menu = "MAIN"; menu_idx = 0; draw_menu(MAIN_MENU, menu_idx)
                    else: 
                        current_menu = "SETTINGS"; menu_idx = 0; draw_menu(SETTINGS_MENU, menu_idx)
                    
                elif act == 2: # SHORT SELECT -> Edit / Enter
                    selectedSTR = menu_list[menu_idx]
                    
                    if current_menu == "MAIN":
                        if "START" in selectedSTR: app_state = "STANDBY"
                        elif "Settings" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0
                    
                    elif current_menu == "SETTINGS":
                        if "Setup" in selectedSTR: current_menu = "SETUP"; menu_idx = 0
                        elif "Calibrate" in selectedSTR: current_menu = "CALIB"; menu_idx = 0
                        elif "System" in selectedSTR: current_menu = "SYS"; menu_idx = 0
                        elif "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0
                    
                    elif current_menu == "SETUP":
                        if "Back" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0
                        elif "Delay Min" in selectedSTR: 
                            settings["delay_min"] = (settings["delay_min"] % 10) + 1
                            if settings["delay_max"] < settings["delay_min"]: 
                                settings["delay_max"] = settings["delay_min"]
                        elif "Delay Max" in selectedSTR: 
                            settings["delay_max"] = (settings["delay_max"] % 10) + 1
                            if settings["delay_max"] < settings["delay_min"]:
                                settings["delay_max"] = settings["delay_min"]
                        elif "Par Time" in selectedSTR: 
                            settings["par_time"] += 0.5
                            if settings["par_time"] > 30.0: settings["par_time"] = 0.0
                        elif "Par Reps" in selectedSTR: settings["par_reps"] = (settings["par_reps"] % 10) + 1
                        elif "Par Rest" in selectedSTR: 
                            settings["par_rest"] += 0.5
                            if settings["par_rest"] > 30.0: settings["par_rest"] = 1.0
                        elif "Par Shots" in selectedSTR: settings["par_shots"] = (settings["par_shots"] + 1) % 20
                        elif "Mode" in selectedSTR: settings["mode"] = 1 if settings["mode"] == 0 else 0
                        
                    elif current_menu == "CALIB":
                        if "Back" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0
                        elif "Sensitivity" in selectedSTR: 
                            # Range 1 to 20 (mapped to 100k -> 2M)
                            mapped = int(settings["sensitivity"] / 100000)
                            mapped = (mapped % 20) + 1
                            settings["sensitivity"] = mapped * 100000
                        elif "Buzzer Vol" in selectedSTR: settings["buzzer_vol"] = (settings["buzzer_vol"] + 25) % 125
                        
                    elif current_menu == "SYS":
                        if "Back" in selectedSTR: current_menu = "SETTINGS"; menu_idx = 0
                        elif "Brightness" in selectedSTR: 
                            pct = int(settings["brightness"]/65535*100)
                            pct = (pct + 25) % 125
                            if pct == 0: pct = 10
                            settings["brightness"] = int((pct / 100) * 65535)
                            try:
                                backlight_pwm.duty_u16(settings["brightness"])
                            except: pass
                        elif "Theme" in selectedSTR:
                            settings["theme"] = 1 if settings["theme"] == 0 else 0
                        elif "Sleep Time" in selectedSTR:
                            # 0 (Off), 1, 2, 5, 10, 15, 30
                            times = [0, 1, 2, 5, 10, 15, 30]
                            try:
                                curr_idx = times.index(settings["sleep_time"])
                            except:
                                curr_idx = 3 # default 5
                            settings["sleep_time"] = times[(curr_idx + 1) % len(times)]
                            
                    if app_state == "MENU": # Redraw menu after changes
                        if current_menu == "MAIN": menu_list = MAIN_MENU
                        elif current_menu == "SETTINGS": menu_list = SETTINGS_MENU
                        elif current_menu == "SETUP": menu_list = SETUP_MENU
                        elif current_menu == "CALIB": menu_list = CALIB_MENU
                        else: menu_list = SYS_MENU
                        draw_menu(menu_list, menu_idx)
                
                sleep(0.05)

        elif app_state == "STANDBY":
            clear_screen()
            draw_text("STANDBY...", 80, 100, get_ready_color())
            
            d_min = min(settings["delay_min"], settings["delay_max"])
            d_max = max(settings["delay_min"], settings["delay_max"])
            delay_ms = random.randint(d_min * 1000, d_max * 1000)
            
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
                
                # Calculate total time (time of the last shot)
                s_tot_count, _, _, s_tot_time, _ = shot_history[-1]
                
                # "--- RESULTS ---" (15 chars) = 240px. Center = 40.
                draw_text("--- RESULTS ---", 40, 0, get_ready_color())
                
                if settings["par_time"] > 0 or settings["par_reps"] > 0 or settings["par_shots"] > 0:
                     max_rep = shot_history[-1][2]
                     draw_text(f"Reps: {max_rep}", 10, 45, get_ready_color())
                     draw_text(f"Shots: {s_tot_count}", 10, 85, get_text_color())
                else:
                     draw_text(f"Shots: {s_tot_count}", 10, 45, get_text_color())
                     draw_text(f"Total: {s_tot_time/1000:.2f}s", 10, 85, get_go_color())
                     
                # "SEL:Exit  SCR:Split" (19 chars) = 304px. Center X = 8, Bottom Y = 136
                draw_text("SEL:Exit  SCR:Split", 8, 136, get_ready_color())
                
                # Wait for interaction
                while True:
                    act = get_button_action()
                    if act != 0: break
                    sleep(0.05)
                
                # Exit if Select (2) or Long Select (3) was pressed
                if act == 2 or act == 3: 
                    app_state = "MENU"
                    continue
                
                # User hit SCROLL, start reviewing
                idx = 0
                while True:
                    clear_screen()
                    s_global_count, s_count, s_rep, s_total, s_split = shot_history[idx]
                    
                    # 1. Center "Shot N/Total" on top
                    shot_text = f"Shot {s_global_count}/{len(shot_history)}"
                    # E.g. "Shot 1/5" is 8 chars = 128px. Center = (320-128)/2 = 96
                    shot_w = len(shot_text) * 16
                    shot_x = (320 - shot_w) // 2
                    draw_text(shot_text, shot_x, 5, get_ready_color())
                    
                    if settings["par_time"] > 0 or settings["par_reps"] > 0 or settings["par_shots"] > 0:
                        rep_total = 0
                        for shot in reversed(shot_history):
                            if shot[2] == s_rep:
                                rep_total = shot[3]
                                break
                        draw_text(f"Rep: {s_rep} Tot: {rep_total/1000:.2f}s", 10, 35, get_ready_color())
                    
                    # 2. Show Total time in first line
                    draw_text(f"Tm: {s_total/1000:.2f}s", 10, 65, get_text_color())
                    
                    # 3. Show split shot segment in second line, and split time in third line
                    if s_count > 1:
                        draw_text(f"Sp. Shot: {s_count-1}-{s_count}", 10, 95, get_text_color())
                        draw_text(f"Sp.tm: {s_split/1000:.2f}s", 10, 125, get_accent_color())
                    else:
                        draw_text("Sp. Shot: --", 10, 95, get_text_color())
                        draw_text("Sp.tm: ----", 10, 125, get_accent_color())
                    
                    # Bottom labels
                    # There are 18 characters here. Center is 16.
                    draw_text("SCR:Nx SEL:Bk / Ex", 16, 150, get_bg_color())
                        
                    while True:
                        act = get_button_action()
                        if act == 1: # Next (Scroll button)
                            idx = (idx + 1)
                            if idx >= len(shot_history):
                                # Instead of exiting immediately, go to a post-review menu
                                post_review_menu = ["Send", "Back", "Exit"]
                                p_idx = 0
                                draw_menu(post_review_menu, p_idx)
                                
                                while True:
                                    p_act = get_button_action()
                                    if p_act == 1: # Scroll
                                        p_idx = (p_idx + 1) % len(post_review_menu)
                                        draw_menu(post_review_menu, p_idx)
                                    elif p_act == 2 or p_act == 3: # Select
                                        p_sel = post_review_menu[p_idx]
                                        if p_sel == "Send":
                                            # Placeholder for send logic
                                            clear_screen()
                                            draw_text("Sending...", 80, 80, get_ready_color())
                                            sleep(1)
                                            draw_menu(post_review_menu, p_idx)
                                        elif p_sel == "Back":
                                            # Go back to viewing the last shot
                                            idx = len(shot_history) - 1
                                            break
                                        elif p_sel == "Exit":
                                            app_state = "MENU"
                                            break
                                    sleep(0.05)
                            break
                        elif act == 2: # Back (Short Select)
                            if idx > 0:
                                idx -= 1
                            break
                        elif act == 3: # Exit (Long Select)
                            app_state = "MENU"
                            break
                        sleep(0.05)
                        
                    if app_state == "MENU": break
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
