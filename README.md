
# MT5 Order Manager
Medium article: Risk management and trailing stop with MT5 (with code)

### Install app on VPS
Get the latest Anaconda version from:
https://repo.anaconda.com/archive/

### Install dependencies:
pip install -r requirements.txt

## SSL
Register the IP address with a DNS provider:
https://freedns.afraid.org/subdomain/ – assign a URL to your IP address.

install winacme: https://www.win-acme.com/ 

open the port 80 in windows firewall: 

<img width="1053" height="328" alt="Capture d’écran 2026-05-30 à 19 53 14" src="https://github.com/user-attachments/assets/1b5e65bc-ee9d-407a-9a63-a9b532db486d" />

double check if the port is open:
`python -m http.server 80`

execute acm - manual, .., **pme files** 

update mt5_server.py with the correct file names:
```
    cert_file = 'xxxx.crabdance.com-crt.pem'
    key_file = 'xxxx.crabdance.com-key.pem'
```
