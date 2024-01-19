<!---
This file is auto-generate by a github hook please modify README.template if you don't want to loose your work
-->
# raelldottin/tachikoma 1.1.7-125
[![Hourly Automated Actions](https://github.com/raelldottin/tachikoma/actions/workflows/hourly-run.yml/badge.svg?event=schedule)](https://github.com/raelldottin/tachikoma/actions/workflows/hourly-run.yml)

# Tachikoma - Pixel Starships Automation

This repository contains scripts and resources for automating tasks in the mobile game Pixel Starships. The project is built around a Python script `run.py` which interacts with various game elements to perform a series of automated tasks. It's designed to help players manage routine activities in the game more efficiently.

## Repository Contents

- `.dockerignore`, `.gitignore`: Ignore files.
- `.githooks/`: Custom Git hooks for the repository.
- `.github/`: GitHub-specific configurations, including Actions workflow `hourly-run.yml`.
- `.python-version`: Version-specific configurations.
- `.ruff_cache/`: Cache directory for Python linter Ruff.
- `LICENSE`: The license file.
- `README.md`: This readme file.
- `README.template`: Template for generating README files.
- `conf.py`, `index.rst`, `make.bat`: Sphinx documentation configurations.
- `pylintrc`: Configuration file for Python linter Pylint.
- `requirements.txt`: Required Python packages for the project.
- `run.py`: Main Python script to automate tasks in Pixel Starships.
- `sdk/`: Python package containing modules like `client`, `device`, etc., used by `run.py`.

## run.py

`run.py` is the core script of this repository. It automates various tasks in Pixel Starships, such as resource collection, crew management, and more. The script provides a CLI for easy interaction and is configurable to either run as a guest or with user credentials.

### Key Features:

- Automated collection of resources and rewards.
- Crew and room upgrades management.
- Marketplace and messages handling.
- Optional email logging for monitoring script activities.

## Setup & Usage

1. Clone the repository to your local machine.
2. Install the required Python packages: `pip install -r requirements.txt`.
3. Run the script using Python: `python run.py`.

   Options:
   - `-a` / `--auth`: Authentication string for the game.
   - `-e` / `--email`: Email for SMTP (if email logging is desired).
   - `-p` / `--password`: Password for SMTP.
   - `-r` / `--recipient`: Recipient email for the log.

## Contributing

Contributions to enhance the script's functionality or efficiency are welcome. Feel free to fork the repository and submit pull requests.

## License

This project is licensed under the terms specified in the `LICENSE` file.

---

This README provides a basic overview of the repository. For more detailed information on specific components, please refer to the respective files or the source code comments.

---
