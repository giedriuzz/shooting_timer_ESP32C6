"""
Shoot Timer
===========
A simple shoot timer for ESP32 with ST7789 display.
Uses Boot Button (Pin 0) and Select Button (Pin 5) for menu navigation and control.
"""

import machine
from machine import Pin, PWM

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
BUZZER_PIN = 32     # Moved from 5 to 32 to free up Button 2

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
    "par_reps": 1,             # Number of Repetitions / Sessions
    "par_rest": 3.0,           # Rest time between string repetitions (seconds)
    "par_shots": 0,            # Expected shots per session (0 = Off)
    "mode": 0,                 # 0 = Live Fire, 1 = Dry Fire
    "sensitivity": 800000,     # Acoustic threshold
    "buzzer_vol": 50,          # 0-100% volume
}

def get_energy_threshold():
    # If mode is dry fire, drop threshold drastically (e.g., clicking sound)
    # If mode is live fire, strictly use configured sensitivity
    if settings["mode"] == 1:
        return 10000 # Very sensitive for dry fire clicks
    return settings["sensitivity"]

# Colors
COLOR_BG = st7789.BLACK
COLOR_TEXT = st7789.WHITE
COLOR_ACCENT = st7789.RED
COLOR_READY = st7789.YELLOW
COLOR_GO = st7789.GREEN
COLOR_SEL = st7789.CYAN

# Globals
app_state = "BOOT"
menu_idx = 0
current_menu = "MAIN"
shot_history = []

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
    tft.fill(COLOR_BG)
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
    buzzer = PWM(Pin(BUZZER_PIN))
    buzzer.duty_u16(0)
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
    if tft: tft.fill(COLOR_BG)

def draw_text(text, x, y, color=COLOR_TEXT):
    if tft: tft.text(font, text, x, y, color, COLOR_BG)

def beep(duration_ms=200, freq=3000):
    if buzzer and settings["buzzer_vol"] > 0:
        duty = int((settings["buzzer_vol"] / 100) * 65535)
        buzzer.freq(freq)
        buzzer.duty_u16(duty)
        sleep(duration_ms / 1000)
        buzzer.duty_u16(0)

def get_button_action():
    """
    Returns:
    0 = None
    1 = Scroll (Short Press Btn 1)
    2 = Select (Short Press Btn 2)
    3 = Back/Long Select (Long Press Btn 2)
    """
    if btn_scroll.value() == 0:
        sleep(0.05) # Debounce
        if btn_scroll.value() == 0:
            while btn_scroll.value() == 0: pass # Wait release
            return 1
            
    if btn_select.value() == 0:
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
MAIN_MENU = ["START", "Setup", "Calibrate", "System"]
SETUP_MENU = ["Delay Min", "Delay Max", "Par Time", "Par Reps", "Par Rest", "Par Shots", "Mode", "<- Back"]
CALIB_MENU = ["Sensitivity", "Buzzer Vol", "<- Back"]
SYS_MENU = ["Brightness", "<- Back"]

def draw_menu(items, selected_idx):
    clear_screen()
    y = 5
    
    start_idx = 0
    if selected_idx > 3:
        start_idx = selected_idx - 3
        
    end_idx = min(len(items), start_idx + 4)
    # Ensure we show 4 items if possible
    if end_idx - start_idx < 4 and len(items) >= 4:
        start_idx = len(items) - 4
        end_idx = len(items)

    for i in range(start_idx, end_idx):
        color = COLOR_SEL if i == selected_idx else COLOR_TEXT
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
        
        draw_text(f"{prefix}{text}", 5, y, color)
        y += 33

# --- State Machine App ---
def main():
    global app_state, menu_idx, current_menu, shot_history
    
    # Boot Sequence
    clear_screen()
    draw_text("G electronic", 70, 70, COLOR_ACCENT) # [ ] Sureguliuoti užrašo G-electronic lygiavimą
    sleep(3)
    clear_screen()
    draw_text("Shooting timer", 50, 70, COLOR_READY) # [ ] Sureguliuoti užrašo "Shooting timer" lygiavimą
    sleep(2)
    
    app_state = "MENU"
    menu_idx = 0
    current_menu = "MAIN"

    while True:
        if app_state == "MENU":
            menu_list = MAIN_MENU if current_menu == "MAIN" else SETUP_MENU if current_menu == "SETUP" else CALIB_MENU if current_menu == "CALIB" else SYS_MENU
            draw_menu(menu_list, menu_idx)
            
            while app_state == "MENU":
                act = get_button_action()
                if act == 1: # SCROLL
                    menu_idx = (menu_idx + 1) % len(menu_list)
                    draw_menu(menu_list, menu_idx)
                
                elif act == 3: # LONG SELECT -> Back to main
                    if current_menu == "MAIN": pass
                    else: current_menu = "MAIN"; menu_idx = 0; draw_menu(MAIN_MENU, menu_idx)
                    
                elif act == 2: # SHORT SELECT -> Edit / Enter
                    selectedSTR = menu_list[menu_idx]
                    
                    if current_menu == "MAIN":
                        if "START" in selectedSTR: app_state = "STANDBY"
                        elif "Setup" in selectedSTR: current_menu = "SETUP"; menu_idx = 0
                        elif "Calibrate" in selectedSTR: current_menu = "CALIB"; menu_idx = 0
                        elif "System" in selectedSTR: current_menu = "SYS"; menu_idx = 0
                        elif "System" in selectedSTR: current_menu = "SYS"; menu_idx = 0
                        elif "Exit" in selectedSTR: app_state = "IDLE"
                    
                    elif current_menu == "SETUP":
                        if "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0
                        elif "Delay Min" in selectedSTR: settings["delay_min"] = (settings["delay_min"] % 10) + 1
                        elif "Delay Max" in selectedSTR: settings["delay_max"] = (settings["delay_max"] % 10) + 1
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
                        if "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0
                        elif "Sensitivity" in selectedSTR: 
                            # Range 1 to 20 (mapped to 100k -> 2M)
                            mapped = int(settings["sensitivity"] / 100000)
                            mapped = (mapped % 20) + 1
                            settings["sensitivity"] = mapped * 100000
                        elif "Buzzer Vol" in selectedSTR: settings["buzzer_vol"] = (settings["buzzer_vol"] + 25) % 125
                        
                    elif current_menu == "SYS":
                        if "Back" in selectedSTR: current_menu = "MAIN"; menu_idx = 0
                        elif "Brightness" in selectedSTR: 
                            pct = int(settings["brightness"]/65535*100)
                            pct = (pct + 25) % 125
                            if pct == 0: pct = 10
                            settings["brightness"] = int((pct / 100) * 65535)
                            try:
                                backlight_pwm.duty_u16(settings["brightness"])
                            except: pass
                            
                    if app_state == "MENU": # Redraw menu after changes
                        menu_list = MAIN_MENU if current_menu == "MAIN" else SETUP_MENU if current_menu == "SETUP" else CALIB_MENU if current_menu == "CALIB" else SYS_MENU
                        draw_menu(menu_list, menu_idx)
                
                sleep(0.05)

        elif app_state == "STANDBY":
            clear_screen()
            draw_text("STANDBY...", 80, 100, COLOR_READY)
            
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
                app_state = "IDLE"
            else:
                beep(300, 3000)
                app_state = "RUNNING"
                
        elif app_state == "RUNNING":
            
            shot_history = []
            
            # Drain mic buffer before start
            if mic_working:
                for _ in range(5): audio_in.readinto(mic_buffer)

            par_time_ms = int(settings["par_time"] * 1000)
            par_shots_req = settings["par_shots"]
            
            running = True
            
            # Loop through the configured number of repetitions (sessions)
            for rep in range(settings["par_reps"]):
                if not running: break
                
                # If not the first rep, apply the Rest Delay and start beep
                if rep > 0:
                    clear_screen()
                    draw_text(f"REST: {settings['par_rest']}s", 20, 40, COLOR_READY)
                    draw_text(f"Next: Rep {rep+1}", 20, 80, COLOR_TEXT)
                    
                    start_rest = ticks_ms()
                    rest_ms = int(settings["par_rest"] * 1000)
                    while ticks_diff(ticks_ms(), start_rest) < rest_ms:
                        if get_button_action() != 0: 
                            running = False
                            break
                        sleep(0.05)
                        
                    if not running: break
                    beep(300, 3000) # GO beep again for the next rep!
                
                # --- START OF ACTIVE REP ---
                clear_screen()
                rep_str = f"REP {rep+1}/{settings['par_reps']}"
                draw_text(rep_str, 5, 5, COLOR_READY)
                draw_text("GO, GO, GO!", 30, 45, COLOR_GO)
                
                start_time = ticks_ms()
                shot_count = 0
                last_shot_time = 0
                
                bg_noise_level = 0
                bg_history = [0] * BACKGROUND_SAMPLES
                bg_idx = 0
                
                par_beeped = False
                rep_finished = False

                while running and not rep_finished:
                    current_time = ticks_diff(ticks_ms(), start_time)
                    
                    # Stop via ANY Button Press
                    act = get_button_action()
                    if act != 0:
                        running = False
                        break
                        
                    # Check Par Time Beep
                    if par_time_ms > 0 and current_time >= par_time_ms and not par_beeped:
                        beep(500, 3000)
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
                       shot_history.append((global_shot_count, last_shot_time, split_time))
                       
                       msg = f"#{shot_count}: {last_shot_time/1000:.2f}s   "
                       split_str = f"Split: {split_time/1000:.2f}s   " if shot_count > 1 else "                "
                       print(f"Shot! Rep {rep+1} | {msg.strip()} | Peak: {max_val} | Thresh: {get_energy_threshold()}")
                       
                       # Clear the specific drawing area before drawing new text
                       tft.fill_rect(0, 80, 240, 60, COLOR_BG)
                       
                       tft.text(font, msg, 20, 80, COLOR_TEXT, COLOR_BG)
                       tft.text(font, split_str, 20, 115, COLOR_ACCENT, COLOR_BG)
                       
                       if mic_working:
                           for _ in range(3): audio_in.readinto(mic_buffer) 
                           
                       # If we have reached the expected shots for this Par session, end it early!
                       if par_shots_req > 0 and shot_count >= par_shots_req:
                           rep_finished = True
                           sleep(1.0) # Show the time for a second before going to rest
                    
                    sleep(0.01)

            app_state = "REVIEW"

        elif app_state == "REVIEW":
            if len(shot_history) > 0:
                clear_screen()
                
                # Calculate total time (time of the last shot)
                s_tot_count, s_tot_time, _ = shot_history[-1]
                
                draw_text("-- RESULTS --", 20, 5, COLOR_READY)
                draw_text(f"Shots: {s_tot_count}", 10, 45, COLOR_TEXT)
                draw_text(f"Total: {s_tot_time/1000:.2f}s", 10, 80, COLOR_GO)
                draw_text("SEL:Exit SCR:Split", 5, 115, COLOR_READY)
                
                # Wait for interaction
                while True:
                    act = get_button_action()
                    if act != 0: break
                    sleep(0.05)
                
                if act == 2 or act == 3: # User wants to exit instead of viewing
                    app_state = "MENU"
                    continue
                
                # User hit SCROLL, start reviewing
                idx = 0
                while True:
                    clear_screen()
                    s_count, s_total, s_split = shot_history[idx]
                    
                    draw_text(f"Shot {s_count}/{len(shot_history)}", 40, 40, COLOR_READY)
                    draw_text(f"Time: {s_total/1000:.2f}s", 20, 100, COLOR_TEXT)
                    if s_count > 1:
                        draw_text(f"Split: {s_split/1000:.2f}s", 20, 140, COLOR_ACCENT)
                    else:
                        draw_text("Split: ----", 20, 140, COLOR_ACCENT)
                    
                    draw_text("SCROLL=Next", 10, 200, COLOR_BG)
                    draw_text("SEL=Exit", 180, 200, COLOR_ACCENT)
                        
                    while True:
                        act = get_button_action()
                        if act == 1: # Next
                            idx = (idx + 1)
                            if idx >= len(shot_history):
                                app_state = "MENU"
                            break
                        elif act == 2 or act == 3: # Exit
                            app_state = "MENU"
                            break
                        sleep(0.05)
                        
                    if app_state == "MENU": break
            else:
                draw_text("STOPPED", 100, 200, COLOR_ACCENT)
                sleep(2)
                app_state = "MENU"
                
        sleep(0.1)

if __name__ == '__main__':
    main()
