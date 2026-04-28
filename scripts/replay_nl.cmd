@echo off
:: Replay a skill from a natural-language goal via the agent CLI.
::
:: Usage:
::   scripts\replay_nl.cmd "your goal" path\to\attachment.csv
::
:: Example:
::   scripts\replay_nl.cmd "Curate batch.csv into featured-row, comment 'demo'" tests\fixtures\batch.csv

if "%~1"=="" (
    echo Usage: replay_nl.cmd "goal" attachment_path
    exit /b 2
)
if "%~2"=="" (
    echo Usage: replay_nl.cmd "goal" attachment_path
    exit /b 2
)

py -m pilot.agent.cli do %1 --client groq --executor real --auto-approve --attach %2
