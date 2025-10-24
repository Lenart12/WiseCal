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
  [-d <storage_dir>] \
  [-a <api_url>]
```

### Cron Job Example
To run the script every 30 minutes, add the following line to your crontab (edit with `crontab -e`):
```cron
*/30 * * * * /root/.local/bin/uv run --project /path/to/WiseDiff /path/to/WiseDiff/main.py -t "schoolcode/filterId" -n "Timetable name" -d /path/to/storage
```

Make sure to replace `/path/to/WiseDiff` and `/path/to/storage` with the actual paths on your system, ensure that the `uv` command is accessible, and that the script has the necessary permissions to run.

It is recommended to run the script first manually to ensure everything is set up correctly before scheduling it with cron.


### Arguments
- `-t`, `--timetable` (required): Timetable in the format `schoolcode/filterId`
- `-n`, `--timetable-name` (required): Human-readable name for the timetable
- `-d`, `--storage-dir`: Directory to store ICS files (default: current directory)
- `-a`, `--api-url`: Wise TT API URL (default: https://www.wise-tt.com)

### Example
```bash
uv run main.py -t "um_feri/0;389,569;0;0;" -n "FERI RIT MAG 1. letnik""
```

## Environment Variables
Store these values in a `.env` file or export as an environment variable.

- `OPENAI_API_KEY`: Your OpenAI API key (required for change summarization)
- `DISCORD_WEBHOOK_URL`: Discord webhook URL for sending notifications (required)