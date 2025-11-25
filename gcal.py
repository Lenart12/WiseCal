import os
import pathlib
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DATA_DIR = pathlib.Path(os.getenv('WISECAL_DATA_DIR', './wc_data'))

SCOPES = ['openid',
          'https://www.googleapis.com/auth/userinfo.email',
          'https://www.googleapis.com/auth/calendar.app.created']

def ensure_dirs():
    (BASE_DATA_DIR / 'credentials').mkdir(parents=True, exist_ok=True)
    (BASE_DATA_DIR / 'cal_ids').mkdir(parents=True, exist_ok=True)
    (BASE_DATA_DIR / 'synced_events').mkdir(parents=True, exist_ok=True)
    (BASE_DATA_DIR / 'settings').mkdir(parents=True, exist_ok=True)
    (BASE_DATA_DIR / 'calendars').mkdir(parents=True, exist_ok=True)

def get_cal_service(user: str):
    creds_fn = BASE_DATA_DIR / 'credentials' / f'{user}.json'
    if not creds_fn.exists():
        raise FileNotFoundError(f'No credentials file found for user {user} at {creds_fn}')
    creds = Credentials.from_authorized_user_file(str(creds_fn), scopes=SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise ValueError(f'Credentials for user {user} are invalid and cannot be refreshed.')
        with open(creds_fn, 'w') as token:
            token.write(creds.to_json())
    
    service = build('calendar', 'v3', credentials=creds)
    return service

def get_cal_id(user: str) -> str:
    cal_id_fn = BASE_DATA_DIR / 'cal_ids' / f'{user}.txt'
    if not cal_id_fn.exists():
        return None
    with open(cal_id_fn, 'r') as fh:
        cal_id = fh.read().strip()
    return cal_id

def create_calendar(user: str, name: str):
    service = get_cal_service(user)
    calendar = {
        'summary': name,
        'timeZone': 'Europe/Ljubljana'
    }
    created_cal = service.calendars().insert(body=calendar).execute()
    cal_id = created_cal['id']
    cal_id_fn = BASE_DATA_DIR / 'cal_ids' / f'{user}.txt'
    with open(cal_id_fn, 'w') as fh:
        fh.write(cal_id)
    return cal_id

def load_synced_event_ids(user: str) -> list[str]:
    synced_events_fn = BASE_DATA_DIR / 'synced_events' / f'{user}.txt'
    if not synced_events_fn.exists():
        return []
    with open(synced_events_fn, 'r') as fh:
        event_ids = [line.strip() for line in fh if line.strip()]
    return event_ids

def save_synced_event_ids(user: str, events: list[str]):
    synced_events_fn = BASE_DATA_DIR / 'synced_events' / f'{user}.txt'
    with open(synced_events_fn, 'w') as fh:
        for event_id in events:
            fh.write(f'{event_id}\n')
