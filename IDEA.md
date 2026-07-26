## The core idea

Your **Tachikoma repository is an autonomous account caretaker for Pixel Starships**. It replaces the repetitive maintenance involved in opening the game, checking resources, collecting rewards, managing crew, and starting upgrades.

The intended workflow is:

> Authenticate → inspect the ship and account → decide what maintenance is appropriate → perform those actions → send a report.

The README describes it as a Python-based system for automating routine Pixel Starships activities, centered on `run.py` and an internal SDK. 

## How it works

### 1. It behaves like a lightweight game client

The `sdk/client.py` layer communicates directly with the Pixel Starships API. It identifies itself using Unity/mobile-client headers, maintains an HTTP session, handles retries and rate limiting, parses the game’s XML responses, and refreshes authorization when necessary.  

The `Device` class creates and persists a virtual device identity along with its refresh token. This lets the automation return later and access the same account without requiring an interactive login every time.  

### 2. It performs a daily maintenance sequence

Once authenticated, `run.py` loads current game data and then executes a defined series of chores:

- Collect flying Starbux.
- Claim task and daily rewards.
- Inspect crew.
- Upgrade research and rooms.
- Read marketplace messages and account messages.
- Manage training.
- Calculate resource totals.
- Upgrade eligible characters.

That entire sequence is visible in the main execution loop. 

### 3. It contains strategy, not merely API wrappers

The more interesting part of the repository is the decision logic. For crew training, for example, Tachikoma categorizes characters by role, checks their current room, training percentage, fatigue, and elapsed time, and then selects an appropriate tier of training. It also warns when a crew member should be moved to another room to continue developing the desired statistics.  

It can finish an existing training session, compare the resulting statistics, select the next training design, and start another session automatically. 

This makes Tachikoma an early **rules-based game agent**: it observes state and applies your encoded priorities.

### 4. It is designed to run unattended

The GitHub Actions workflow runs the program daily on an Ubuntu runner. It installs the Python dependencies and processes five separate authentication strings stored as GitHub secrets.  

Afterward, the script can email its captured log so you can monitor what it collected, upgraded, or failed to complete without opening GitHub or the game.  

## The idea in one sentence

**Tachikoma is a scheduled, headless Pixel Starships operations agent that maintains several accounts by interacting with the game API, applying your progression rules, and reporting what it did.**

The name also fits the concept: an independent little machine carrying out assigned work while operating with a limited form of decision-making.
