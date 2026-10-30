# ESP32 Room Controller

My EEE mini project. An ESP32 running MicroPython that switches four relays (a light, a fan and two spares), turns the light on when a PIR sees someone, and counts how many hours each appliance has been running.

I didn't want to build yet another board that just flips a relay. A fan that has been on for 3000 hours should tell you something, so every channel keeps its own hour counter and shows "SERVICE DUE" once it passes a limit you set.

## What it does

The light comes on when the PIR sees movement and goes off after a timeout. The timeout is 10 to 60 minutes and each channel has its own. If you switch something by hand, that channel drops to manual, otherwise the PIR would just undo what you did.

There's a 1.3" OLED and a rotary encoder on the board. The screen shows the device name, whether it's on, its hours and the time left before it switches off. Turn the knob to change device, click for the menu, hold to go back.

The ESP32 also runs its own WiFi hotspot called RoomControl and serves a web page at 192.168.4.1. It has the same controls as the knob, plus a bar chart of energy per device, a graph of power over the last hour, a log of the last ten events, and a form to give it your home WiFi. I wrote the web server myself (about 60 lines) because I didn't want to squeeze a big library into the ESP32's memory.

Energy is an estimate. There's no current sensor. You enter the wattage of each appliance on the page and it works out kWh from the hours it has been on. A fan on low speed will read too high.

There's also an MQ-7 gas sensor. It warms up for 3 minutes, takes whatever it reads as the clean-air baseline, and from then on shows the reading as a multiple of that baseline. At 1.5x it turns the fan on, and it turns it off again below 1.25x. It's not calibrated and the heater runs at a steady 5V instead of the cycle the datasheet wants. Treat it as a ventilation demo, not as a CO alarm.

The Telegram bot is optional and it's the newest part, so it's the least tested.

## <img width="1200" height="1600" alt="WhatsApp Image 2026-10-30 at 08 56 27" src="https://github.com/user-attachments/assets/f1666c3c-b5de-4014-8d9f-f0006ca3b495" />

## Parts

- ESP32 DevKit
- 4-channel 5V relay module (active-low)
- HC-SR501 PIR
- MQ-7 module
- 1.3" I2C OLED, SH1106 (not SSD1306, I found that out the hard way, the picture comes out shifted)
- KY-040 rotary encoder
- a 10k and a 20k resistor



```
Relays 1-4        GPIO 4, 15, 5, 17
PIR               GPIO 33
MQ-7 AO           GPIO 34, through the divider below
OLED SDA / SCL    GPIO 21 / 22
Encoder CLK / DT  GPIO 18 / 19
Encoder button    GPIO 25
```

The MQ-7 can put out up to 5V, so AO goes through 10k in series and 20k to ground, and the ESP32 reads the point in between. Pin 34 is on ADC1, which still works when WiFi is on.

Relay modules are usually active-low, which means the pins have to be high to keep the relays off. `boot.py` does that first thing, before anything else loads, so nothing clicks on at power-up.

## Putting it on the board

1. Flash MicroPython on the ESP32.
2. Copy `boot.py`, `main.py` and `tgbot.py` to the root of the board.
3. Copy the files in `lib/` to `/lib` on the board.
4. Reset it, join the RoomControl hotspot (the password is `AP_PASS` in `main.py`, change it) and open `http://192.168.4.1`.
5. To use your home WiFi, type the name and password into the page. It won't rejoin by itself after a restart. Open the hotspot page and press Connect Saved. I did it this way so the board is always reachable, even if the home WiFi is down.

## Telegram

1. Message @BotFather, send `/newbot` and copy the token.
2. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. The number after `"chat":{"id":` is your chat ID. It's not the number in the token.
3. Put both in `main.py` (`TG_TOKEN`, `TG_CHAT_ID`). Don't push them to GitHub.

Commands: `/status`, `/on 1`, `/off fan`, `/auto 1`, `/manual 1`, `/co`, `/log`. It only listens to your chat ID, and it only works while the board is on a WiFi network that has internet.

## Things to know

- The web page has no login. Anyone on the hotspot can switch your appliances, so change the password.
- Your home WiFi password is saved as plain text in `wifi.json` on the board.
- The relay side is mains. Test with LEDs first, use a fuse and a proper enclosure, and don't touch it while it's plugged in. The MQ-7 heater also gets hot.
- It's a prototype. It hasn't been tested against any electrical safety standard.

## Credits

`lib/sh1106.py` is robert-hh's SH1106 driver. `lib/rotary.py` and `lib/rotary_irq_esp.py` are from Mike Teachman's micropython-rotary. Both are MIT licensed and I've kept their headers. The rest is mine.
