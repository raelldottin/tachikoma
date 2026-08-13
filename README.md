<!---
This file is auto-generate by a github hook please modify README.template if you don't want to loose your work
-->
# //github.com/raelldottin/tachikoma 1.1.7-123
[![Daily Automated Actions](https://github.com/raelldottin/tachikoma/actions/workflows/daily-run.yml/badge.svg?event=schedule)](https://github.com/raelldottin/tachikoma/actions/workflows/daily-run.yml)

# Tachikoma - Pixel Starships Automation

This repository contains scripts and resources for automating tasks in the mobile game Pixel Starships. The project is built around a Python script `run.py` which interacts with various game elements to perform a series of automated tasks. It's designed to help players manage routine activities in the game more efficiently.

## Repository Contents

- `.dockerignore`, `.gitignore`: Ignore files.
- `.githooks/`: Custom Git hooks for the repository.
- `.github/`: GitHub Actions workflow `daily-run.yml` (runs 3×/day at 00:00, 08:00, 16:00 UTC).
- `.python-version`: Version-specific configurations.
- `LICENSE`: The license file.
- `README.md`: This readme file.
- `README.template`: Template for generating README files.
- `conf.py`, `index.rst`, `make.bat`: Sphinx documentation configurations.
- `pylintrc`: Configuration file for Python linter Pylint.
- `requirements.txt`: Required Python packages for the project.
- `run.py`: Main Python script to automate tasks in Pixel Starships.
- `sdk/`: Python package containing modules like `client`, `device`, `crew_leveling`, `security`, etc.
- `tests/`: Unit tests covering battle flow, security checksums, crew leveling, and PvP battles.

## run.py

`run.py` is the core script of this repository. It automates various tasks in Pixel Starships, such as resource collection, crew management, and more. The script provides a CLI for easy interaction and is configurable to either run as a guest or with user credentials.

### Key Features:

- Automated collection of resources and rewards.
- Crew and room upgrades management.
- Marketplace and messages handling.
- Optional email logging for monitoring script activities.
- End-to-end ship battle flow (CreateBattle9 → AcceptBattle5 → FinaliseBattle15).
- Crew stat formula integration from the Crew Planning and Training Guide.

### Battle Flow

The `--run-battle` flag triggers an end-to-end ship battle:
1. Ship HP check (must be 100%)
2. Rearm ship (restock ammo)
3. HeartBeat4 (keep session alive)
4. CreateBattle9 (initiate battle — **verified working** against live server)
5. AcceptBattle5 (accept the battle — **blocked**: native IL2CPP checksum)
6. FinaliseBattle15 (finalize battle — **blocked**: depends on AcceptBattle5)

### Crew Stat Formulas

Integrated from the *Crew Planning and Training Guide* by Raisinbunhk:
- `compute_final_stat(base, TP, equipment)` — final stat = base × (1 + TP/100) + equipment
- `compute_final_ability(base, TP, equipment%)` — final ability = base × (1 + TP/100) × (1 + equipment%)
- `compute_final_stamina(TP, equipment)` — final stamina = TP + equipment (additive)
- `get_tp_cap(rarity)` — training point caps: 3★=70, 4★=80, 5★=90, 6★=100, 7★=110
- `get_crew_level_cap(ship_level)` — level cap = ship_level × 4, max 40

### Ship Layout Analysis

Read-only layout evaluation — no HTTP mutations, no room-moving endpoints (PSS API doesn't expose them). The `analyzeShipLayout()` method fetches ship data and room designs, then evaluates:
- **Armor coverage**: percentage of critical rooms (reactors, weapons, shields) with adjacent armor
- **Repair proximity**: average distance from repair rooms to critical rooms
- **Weapon distribution**: left/right balance of weapons across the ship
- **Power balance**: reactor power generated vs estimated consumption
- **Overall defense score**: weighted average of the above (0-100)

Logs actionable recommendations: exposed critical rooms, missing repair/weapon/reactor rooms, weapon imbalance, bedroom capacity, pending upgrades. Runs automatically in the daily loop.

### Wiki Game Formulas

All 9 formulas from the [Pixel Starships Wiki](https://pixelstarships.fandom.com/wiki/Formulas) integrated as pure functions:

| # | Formula | Module | Key Functions |
|---|---------|--------|---------------|
| 1 | Room Reload | `sdk/game_formulas.py` | `room_reload_boosted`, `room_reload_powered`, `room_reload` |
| 2 | Crew Stat Buff | `sdk/crew_leveling.py` | `compute_final_stat`, `compute_final_ability`, `compute_final_stamina` |
| 3 | Escape | `sdk/game_formulas.py` | `escape_chance`, `escape_rate` |
| 4 | Dodge | `sdk/game_formulas.py` | `dodge_evasion` (post-July 2023 formula, capped at 80%) |
| 5 | Damage Reduction | `sdk/game_formulas.py` | `damage_reduction`, `effective_damage` |
| 6 | Fire Damage | `sdk/game_formulas.py` | `fire_damage_reduced`, `fire_crew_damage`, `fire_ap_damage` |
| 7 | Crew Stat by Level | `sdk/game_formulas.py` | `crew_stat_at_level` (ease_out / linear / ease_in) |
| 8 | Gas Draw Price | `sdk/game_formulas.py` | `gas_draw_price` (capped at 2M) |
| 9 | Trophy Gain/Loss | `sdk/game_formulas.py` | `trophy_gain` (capped 1-40) |

## Setup & Usage

1. Clone the repository to your local machine.
2. Install the required Python packages: `pip install -r requirements.txt`.
3. Run the script using Python: `python run.py`.

   Options:
   - `--auth-file`: Authentication string file for the game.
   - `--login-email`: Email for game login (password will be prompted or read from `--password-file`).
   - `--device-key`: Permanent device key (if not provided, generates new one).
   - `--smtp-email`: Email for SMTP (if email logging is desired).
   - `--smtp-password-file`: Path to file containing SMTP password.
   - `--recipient`: Recipient email for the log.
   - `--password-file`: Path to file containing game password (for CI).
   - `--run-battle`: Run end-to-end ship battle.

## Contributing

Contributions to enhance the script's functionality or efficiency are welcome. Feel free to fork the repository and submit pull requests.

## License

This project is licensed under the terms specified in the `LICENSE` file.

---

This README provides a basic overview of the repository. For more detailed information on specific components, please refer to the respective files or the source code comments.
