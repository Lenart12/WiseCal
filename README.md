# WiseCal

WiseCal is a web application that automatically syncs your Wise TT school timetable to Google Calendar. It provides a simple web interface for configuration and runs periodic synchronization in the background.

## Features
- Syncs Wise TT timetables directly to Google Calendar
- Web-based configuration interface
- OAuth 2.0 authentication with Google
- Automatic background synchronization (every 15 minutes)
- Customizable event formatting per course and type (lectures/exercises)
- **Admin dashboard** for multi-user management and system monitoring
- **CSRF protection** for all state-changing operations
- Docker support for easy deployment

## Installation

### Using Docker (Recommended)
1. Clone this repository:
   ```bash
   git clone <repo-url>
   cd WiseCal
   ```

2. Set up environment variables in a `.env` file:
   ```bash
   OAUTH_CLIENT_SECRETS='{"web":{"client_id":"...","client_secret":"..."}}'
   FLASK_SECRET_KEY=your-secret-key
   TRUSTED_PROXY_COUNT=1
   ```

3. Build and run with Docker Compose:
   ```bash
   docker compose up -d
   ```

The application will be available at `http://localhost:5187`.

### Manual Installation
1. Clone this repository:
   ```bash
   git clone <repo-url>
   cd WiseCal
   ```

2. Install dependencies using uv:
   ```bash
   uv sync
   ```

3. Install Playwright browsers:
   ```bash
   uv run playwright install --with-deps chromium
   ```

4. Set up environment variables and run:
   ```bash
   export OAUTH_CLIENT_SECRETS='{"web":{"client_id":"...","client_secret":"..."}}'
   export FLASK_SECRET_KEY=your-secret-key
   uv run waitress-serve --call wisecal:create_app
   ```

## Configuration

### Google OAuth Setup
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google Calendar API
4. Create OAuth 2.0 credentials (Web application type)
5. Add your redirect URI (e.g., `http://localhost:5187/oauth2callback`)
6. Copy the client secrets JSON to `OAUTH_CLIENT_SECRETS` environment variable

### Finding Your Filter ID
1. Open [Wise TT](https://www.wise-tt.com) and navigate to your school's timetable
2. Select your desired groups/filters
3. Click the "Bookmark" icon
4. Copy the Filter ID from the URL

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `OAUTH_CLIENT_SECRETS` | Google OAuth 2.0 client secrets JSON | Yes |
| `FLASK_SECRET_KEY` | Secret key for Flask sessions | Yes |
| `WISECAL_ADMIN` | Comma-separated list of admin email addresses | No |
| `WISECAL_DATA_DIR` | Directory for storing user data (default: `./wc_data`) | No |
| `OAUTHLIB_INSECURE_TRANSPORT` | Set to `1` for development without HTTPS | No |
| `TRUSTED_PROXY_COUNT` | Number of reverse proxies to trust | No |

## Admin Features

WiseCal includes a comprehensive admin dashboard for managing multiple users and monitoring system health.

### Setting Up Admin Access
Add admin email addresses to the `WISECAL_ADMIN` environment variable (comma-separated for multiple admins):
```bash
WISECAL_ADMIN=admin@example.com,admin2@example.com
```

Admins will see an "Admin Dashboard" button on the home page after signing in with their admin email.

### Admin Dashboard Features

**System Health Monitoring:**
- Total registered users and configured calendars
- Active vs. disabled calendar count
- Total synced events across all users
- Unique timetables being monitored
- Disk usage statistics
- Last and next scheduler run times

**Per-User Management:**
- View all users with their status, last sync time, and event count
- Force sync for individual users
- Enable/disable calendars for specific users
- Identify stale users (no sync in 7+ days)

**Bulk Operations:**
- **Force Sync All Users** - Triggers immediate sync for all calendars
- **Enable All Calendars** - Enables sync for all users
- **Disable All Calendars** - Disables sync for all users
- **Re-download Timetables** - Clears cached timetables and forces fresh download
- **Clear All Sync State** - Resets sync state to trigger full re-sync

**Security:**
- All state-changing operations are protected with CSRF tokens
- Admin-only routes redirect non-admin users to home page
- Confirmation prompts for destructive bulk operations

### Accessing Admin Dashboard
1. Sign in with an email address listed in `WISECAL_ADMIN`
2. Click "🛡️ Admin Dashboard" button on the home page
3. View system stats, manage users, and perform bulk operations

## Usage
1. Open the web interface in your browser
2. Sign in with your Google account
3. Configure your timetable by providing:
   - Calendar name
   - School code (e.g., `um_feri`)
   - Filter ID from Wise TT
4. Customize event formatting (optional)
5. Save your configuration

The application will automatically sync your timetable to Google Calendar every 15 minutes.