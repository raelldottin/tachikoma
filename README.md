<!---
This file is auto-generate by a github hook please modify README.template if you don't want to loose your work
-->
# raelldottin/tachikoma 1.0.0-31
[![Daily Automated Actions](https://github.com/raelldottin/tachikoma/actions/workflows/daily-run.yml/badge.svg?event=schedule)](https://github.com/raelldottin/tachikoma/actions/workflows/daily-run.yml)

Automate trivial tasks in Pixel Starships Mobile Starategy Sci-Fi MMORPG

# Requirements

`pip3 install xmltodict`

`pip3 install requests`

# Docs

It's super basic, one thing to note: `Device` class automatically saves generated device. Call `.reset()` method to cleanup saved data.

It also stores a token to relogin without credentials.

* One creates a device `device = Device(language='ru')`
* Then a client must be created `client = Client(device=device)`
* Use `client.login()` and `client.heartbeat()` to keep your session alive as a guest
* Use `client.login(email='supra@mail', password='1337')` and `client.heartbeat()` to keep authorized session alive
* Use `client.quickReload()` to re-authenticate your session
* Use `client.listActiveMarketplaceMessages()` to list all items you have available for sell in the marketplace
* Use `client.collectAllResources()` to collect all resources on your ship
* Use `client.collectDailyReward()` to collect the daily reward
---
