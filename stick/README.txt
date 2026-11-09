EO1BIAN - A NEW LIFE FOR YOUR ELECTRIC OBJECTS EO1
====================================================

1. Put this stick in the EO1 (micro-USB, with an OTG adapter if needed) and plug in the power.
   After about 15 seconds you see the EO1bian picture, then setup instructions with QR codes.
2. Turn off mobile data on your phone, scan the QR codes on the screen and choose your Wi-Fi.
3. On the control page that opens on your phone, choose Install (recommended): EO1bian is copied
   to the EO1's own memory (about 10 minutes). Then remove the stick, unplug the power, plug it in.
   Afterwards the stick is free again: copy eo-art/ to your computer first (your old art, if any).

Nothing here can brick the EO1 (its boot chip is never written): it always starts from a USB stick
first, so this stick brings it back at any time. Android stays on the EO1, and any Electric Objects art still on it is saved to eo-art/ here.

Wi-Fi without the phone (optional): fill in wifi_ssid and wifi_password in eo1.txt before step 1.
SSH (optional): put your public key in ssh/authorized_keys, or use "Connect a computer" later.
Problems: the logs/ folder on this stick has the start-up and install logs.

More: https://github.com/eo1bian/eo1bian
