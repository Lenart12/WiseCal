# WiseDiff

WiseDiff is a Python script for monitoring changes in school timetables (ICS files) and sending notifications about detected changes to a Discord channel via webhook. It is designed for use with Wise TT timetables and provides automated change detection and reporting.

## Features
- Downloads the latest timetable from Wise TT
- Compares new and old ICS files to detect changes
- Uses OpenAI to summarize changes in a student-friendly format
- Sends notifications to Discord via webhook

## Installation
1. Clone this repository:
   ```bash
   git clone <repo-url>
   cd urnik
   ```
2. Create and activate a virtual environment (recommended):
   ```bash
   uv sync
   ```

   ```
3. Install Playwright browsers:
   ```bash
   playwright install
   ```
4. Set up environment variables and run the script in a cron job or scheduler.

## Usage
Run the script from the command line:
```bash
uv run main.py \
  -t <schoolcode/filterId> \
  -n "Timetable Name" \
  -w <discord_webhook_url> \
  [-d <storage_dir>] \
  [-a <api_url>]
```

### Arguments
- `-t`, `--timetable` (required): Timetable in the format `schoolcode/filterId`
- `-n`, `--timetable-name` (required): Human-readable name for the timetable
- `-w`, `--webhook-url` (required): Discord webhook URL
- `-d`, `--storage-dir`: Directory to store ICS files (default: current directory)
- `-a`, `--api-url`: Wise TT API URL (default: https://www.wise-tt.com)

### Example
```bash
uv run main.py -t "um_feri/0;389,569;0;0;" -n "FERI RIT MAG 1. letnik" -w "https://discord.com/api/webhooks/..."
```

## Environment Variables
- `OPENAI_API_KEY`: Your OpenAI API key (required for change summarization)
  - Store this in a `.env` file or export as an environment variable.
