

# MT5 Trade dashboard
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

open the port 80 in windows firewall (used by acme to check the domain): 

<img width="1053" height="328" alt="Capture d’écran 2026-05-30 à 19 53 14" src="https://github.com/user-attachments/assets/1b5e65bc-ee9d-407a-9a63-a9b532db486d" />

double check if the port is open:
`python -m http.server 80`

execute acm - manual, .., **pme files** 

update mt5_server.py with the correct file names:
```
    cert_file = 'xxxx.crabdance.com-crt.pem'
    key_file = 'xxxx.crabdance.com-key.pem'
```
open the port 8443 in windows firewall.

## Main app

using anaconda console, `python server_mt5.py`

open the browser using full url, ex: `demodashboard.crabdance.com:8443` 
default credentials: admin/xxxxxx

by default, mt5 does not allow any trading operation from python, if you want to be able to change the stop, create
order, you can activate the option ( at your own risk) with: Tools/Option/Expert Advisor/allow algorithmic trading

<img width="400" height="262" alt="Capture d’écran 2026-06-01 à 10 32 53" src="https://github.com/user-attachments/assets/bc97753b-1830-4443-8658-41002d16dafa" />
