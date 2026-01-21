import dotenv
dotenv.load_dotenv()

import os
import flask
from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect
import yaml
import gcal
import json
import re
import logging
from functools import wraps

import google.oauth2.id_token
import google_auth_oauthlib.flow

from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import wise_tt
import wisecal_cron

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce apscheduler logging noise
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# This variable specifies the name of a file that contains the OAuth 2.0
# information for this application, including its client_id and client_secret.
CLIENT_SECRETS_JSON= json.loads(os.environ.get('OAUTH_CLIENT_SECRETS', '{}'))

# The OAuth 2.0 access scope allows for access to the
# authenticated user's account and requires requests to use an SSL connection.
SCOPES = gcal.SCOPES

app = flask.Flask(__name__)
# Apply ProxyFix to handle reverse proxy headers (X-Forwarded-For, X-Forwarded-Proto, etc.)
TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', '0'))
if TRUSTED_PROXY_COUNT > 0:
  logger.info(f"Applying ProxyFix with TRUSTED_PROXY_COUNT={TRUSTED_PROXY_COUNT}")
  app.wsgi_app = ProxyFix(app.wsgi_app,
                          x_for=TRUSTED_PROXY_COUNT,
                          x_proto=TRUSTED_PROXY_COUNT,
                          x_host=TRUSTED_PROXY_COUNT,
                          x_prefix=TRUSTED_PROXY_COUNT)
# Note: A secret key is included in the sample so that it works.
# If you use this code in your application, replace this with a truly secret
# key. See https://flask.palletsprojects.com/quickstart/#sessions.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'WiseCal-CHANGE-THIS')

# Initialize CSRF protection
csrf = CSRFProtect(app)

# Admin email list from environment variable
ADMIN_EMAILS = [email.strip() for email in os.environ.get('WISECAL_ADMIN', '').split(',') if email.strip()]

def is_admin():
    """Check if the current user is an admin."""
    email = flask.session.get('email')
    return email in ADMIN_EMAILS

def require_admin(f):
    """Decorator to require admin authentication for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_admin():
            flask.flash('Access denied. Admin privileges required.', 'error')
            return flask.redirect('/')
        return f(*args, **kwargs)
    return decorated_function


scheduler = BackgroundScheduler()
LJUBLJANA_TZ = ZoneInfo('Europe/Ljubljana')
last_check_time = None

def wisecal_sync_task():
    global last_check_time
    calendar_updated = wisecal_cron.main()
    last_check_time = datetime.now(LJUBLJANA_TZ)

sync_job = scheduler.add_job(wisecal_sync_task, 'interval', minutes=15, max_instances=1)
logger.info("Starting background scheduler for calendar sync...")
scheduler.start()

@app.route('/')
def index():
  global last_check_time
  email = flask.session.get('email')

  if not email:
    has_settings = False
    calendar_enabled = False
    last_update_time = None
  else:
    try:
      calendar_enabled = gcal.get_calendar_enabled(email)
      has_settings = True
      last_update_time = gcal.get_last_update_time(email)
    except FileNotFoundError:
      has_settings = False
      calendar_enabled = False

  return flask.render_template('index.html',
                email=email,
                last_check_time=last_check_time,
                last_update_time=last_update_time,
                has_settings=has_settings,
                calendar_enabled=calendar_enabled,
                is_admin=is_admin()
                )

@app.route('/authorize')
def authorize():
  # Create flow instance to manage the OAuth 2.0 Authorization Grant Flow steps.
  flow = google_auth_oauthlib.flow.Flow.from_client_config(
    CLIENT_SECRETS_JSON, scopes=SCOPES)

  # The URI created here must exactly match one of the authorized redirect URIs
  # for the OAuth 2.0 client, which you configured in the API Console. If this
  # value doesn't match an authorized URI, you will get a 'redirect_uri_mismatch'
  # error.
  flow.redirect_uri = flask.url_for('oauth2callback', _external=True)

  prompt = 'consent' if flask.request.args.get('prompt') == 'consent' else 'select_account'

  authorization_url, state = flow.authorization_url(
      # Enable offline access so that you can refresh an access token without
      # re-prompting the user for permission. Recommended for web server apps.
      access_type='offline',
      # Enable incremental authorization. Recommended as a best practice.
      include_granted_scopes='true',
      prompt=prompt)

  # Store the state so the callback can verify the auth server response.
  flask.session['state'] = state

  return flask.redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
  # Specify the state when creating the flow in the callback so that it can
  # verified in the authorization server response.
  state = flask.session['state']
  flow = google_auth_oauthlib.flow.Flow.from_client_config(
    CLIENT_SECRETS_JSON, scopes=SCOPES, state=state)
  flow.redirect_uri = flask.url_for('oauth2callback', _external=True)


  # Use the authorization server's response to fetch the OAuth 2.0 tokens.
  authorization_response = flask.request.url
  def access_denied():
    logger.warning("OAuth callback: user denied access")
    return flask.render_template('error.html',
      message='Dostop zavrnjen.',
      details='Niste dovolili dostopa do vašega Google Koledarja.',
      help_tips=['Če želite uporabljati WiseCal, morate dovoliti dostop do vašega Google Koledarja.', 'Lahko poskusite znova in tokrat dovolite dostop.'],
      back_url='/', back_text='Nazaj na začetek')

  if 'error' in flask.request.args:
    if flask.request.args.get('error') == 'access_denied':
      return access_denied()
    else:
      logger.warning(f"OAuth callback: error received - {flask.request.args.get('error')}")
      return flask.render_template('error.html',
        message='Napaka pri avtentikaciji.',
        details=f"Prejeto sporočilo o napaki: {flask.request.args.get('error')}",
        help_tips=['Poskusite znova čez nekaj minut.', 'Če se napaka ponovi, kontaktirajte podporo.'],
        back_url='/', back_text='Nazaj na začetek')
    
  scopes = flask.request.args.get('scope', '').split(' ')
  for s in SCOPES:
    if s not in scopes:
      logger.warning(f"OAuth callback: scope {s} not granted")
      return access_denied()

  flow.fetch_token(authorization_response=authorization_response)
  
  # Check if all required scopes were granted
  missing_scopes = [s for s in SCOPES if s not in flow.credentials.scopes]
  if missing_scopes:
    for s in missing_scopes:
      logger.warning(f"OAuth callback: scope {s} not granted")
    # Redirect to authorize with consent prompt to get missing scopes
    return flask.redirect(flask.url_for('authorize', prompt='consent'))
  
  
  decoded = google.oauth2.id_token.verify_oauth2_token(
      flow.credentials.id_token,
      google.auth.transport.requests.Request(),
      flow.credentials.client_id
  )
  flask.session['email'] = decoded['email']
  logger.info(f"User logged in: {decoded['email']}")
  cred_fn = gcal.BASE_DATA_DIR / 'credentials' / f"{decoded['email']}.json"
  if flow.credentials.refresh_token is not None:
    with open(cred_fn, 'w') as fh:
      fh.write(flow.credentials.to_json())
    logger.info(f"Saved credentials for: {decoded['email']}")
  else:
    if not cred_fn.exists():
      logger.warning(f"OAuth callback: no refresh token and no existing credentials for {decoded['email']}")
      # Redirect to authorize with consent prompt to get refresh token
      return flask.redirect(flask.url_for('authorize', prompt='consent'))
  return flask.redirect('/')

@app.route('/logout')
def logout():
  email = flask.session.get('email')
  logger.info(f"User logged out: {email}")
  flask.session.clear()
  return flask.redirect('/')

@app.route('/setup')
def setup():
  email = flask.session.get('email')
  if not email:
    return flask.redirect('/')
  
  # Check if user already has configured calendar
  existing_settings = None
  settings_fn = gcal.BASE_DATA_DIR / 'settings' / f"{email}.yaml"
  if settings_fn.exists():
    try:
      existing_settings = yaml.safe_load(open(settings_fn, 'r'))
    except:
      pass
  
  return flask.render_template('setup.html', existing_settings=existing_settings)

@app.route('/configure', methods=['GET', 'POST'])
def configure():
  email = flask.session.get('email')
  if not email:
    return flask.redirect('/')
  
  params = request.args if flask.request.method == 'GET' else request.form
  title = params.get('title')
  schoolcode = params.get('schoolcode')
  filterId = params.get('filterId')

  if not title or not re.match(r'^[A-Za-z0-9 _-]{1,100}$', title):
    return flask.render_template('error.html',
      message='Ime koledarja ni veljavno.',
      details='Ime lahko vsebuje samo črke, številke, presledke, podčrtaje in vezaje (1-100 znakov).',
      back_url='/setup', back_text='Nazaj na nastavitve')
  if not schoolcode or not re.match(r'^[a-z_]{1,20}$', schoolcode):
    return flask.render_template('error.html',
      message='Šifra šole ni veljavna.',
      details='Šifra šole lahko vsebuje samo male črke in podčrtaje (npr. um_feri).',
      back_url='/setup', back_text='Nazaj na nastavitve')
  if not filterId or not re.match(r'^[\d,;]{1,40}$', filterId):
    return flask.render_template('error.html',
      message='Filter ID ni veljaven.',
      details='Filter ID lahko vsebuje samo številke, vejice in podpičja.',
      help_tips=['Odpri WiseTT urnik', 'Izberi želene skupine', 'Klikni na ikono "Bookmark"', 'Kopiraj Filter ID iz URL-ja'],
      back_url='/setup', back_text='Nazaj na nastavitve')

  if flask.request.method == 'POST':
    form = flask.request.form
    courses = flask.session.get('courses', [])
    if len(courses) == 0:
      return flask.render_template('error.html',
        message='Seja je potekla.',
        details='Vaša seja je potekla ali pa niso bili najdeni nobeni predmeti.',
        help_tips=['Poskusite znova z novimi nastavitvami'],
        back_url='/setup', back_text='Nazaj na nastavitve')
    settings = {
      'calendar': {
        'enabled': True,
        'owner': email,
        'title': title,
        'force_sync': True,
        'timetable': {
          'schoolcode': schoolcode,
          'filterId': filterId
        }
      },
      'format': {}
    }
    for course in ['DEFAULT'] + courses:
      for ctype in ['PR', 'VAJE']:
        def v(key):
          v = form.get(f'course/{course}/{ctype}/{key}')
          if v is None:
            return
          v = v.replace('\\n', '\n').strip()
          if v == '':
            return
          v = v.replace('EMPTY', '').strip()
          settings['format'].setdefault(course, {}).setdefault(ctype, {})[key] = v
        def i(key):
          v = form.get(f'course/{course}/{ctype}/{key}')
          if v is None:
            return
          if not v.lstrip('-').isdigit():
            return
          i = int(v)
          if i == 0:
            return
          settings['format'].setdefault(course, {}).setdefault(ctype, {})[key] = i
        def l(key):
          list_str = f'course/{course}/{ctype}/{key}/'
          items = [i.split('/')[-1].strip() for i in form.keys() if i.startswith(list_str) and form.get(i) == 'on']
          if len(items) == 0:
            return
          settings['format'].setdefault(course, {}).setdefault(ctype, {})[key] = items
        i('color')
        v('title')
        v('location')
        v('description')
        l('exclude_groups')
        i('start_offset')
        i('end_offset')

    settings_fn = gcal.BASE_DATA_DIR / 'settings' / f"{email}.yaml"
    with open(settings_fn, 'w') as fh:
      yaml.safe_dump(settings, fh)
    logger.info(f"Configuration saved for {email}: {title} ({schoolcode}, {filterId})")
    sync_job.modify(next_run_time=datetime.now())
    logger.info(f"Scheduled immediate sync because of new configuration for {email}")
    return flask.render_template('success.html', title=title)

  cal_fn = gcal.BASE_DATA_DIR / 'calendars' / f"{schoolcode}_{filterId}.ics"

  if not cal_fn.exists():
    try:
      logger.info(f"Downloading timetable for {email}: {schoolcode}, {filterId}")
      wise_tt.download_ical(
          {'schoolcode': schoolcode, 'filterId': filterId},
          cal_fn
      )
    except Exception as e:
      logger.error(f"Error downloading timetable for {email}: {str(e).splitlines()[0].strip()}")
      return flask.render_template('error.html',
        message='Napaka pri prenosu urnika.',
        details=str(e),
        help_tips=['Preverite, da je šifra šole pravilna', 'Preverite, da je Filter ID pravilen', 'Poskusite znova čez nekaj minut'],
        back_url='/setup', back_text='Nazaj na nastavitve')

  try:
    slots = wise_tt.get_slots(cal_fn)
    logger.info(f"Loaded {len(slots)} slots for {email}")
  except Exception as e:
    return flask.render_template('error.html',
      message='Napaka pri branju urnika.',
      details=str(e),
      help_tips=['Preverite, da je šifra šole pravilna (npr. um_feri)', 'Preverite, da je Filter ID pravilen', 'Prepričajte se, da ima urnik aktivne termine'],
      back_url='/setup', back_text='Nazaj na nastavitve')

  if len(slots) == 0:
    return flask.render_template('error.html',
      message='V urniku ni najdenih terminov.',
      details='Za podane podatke ni bilo mogoče najti nobenega termina.',
      help_tips=['Preverite, da je šifra šole pravilna (npr. um_feri)', 'Preverite, da je Filter ID pravilen', 'Prepričajte se, da ima urnik aktivne termine'],
      back_url='/setup', back_text='Nazaj na nastavitve')

  pr_groups = sorted(set([g for slot in slots if slot.ctype_abbr == 'PR' for g in slot.groups]))
  rv_groups = sorted(set([g for slot in slots if slot.ctype_abbr != 'PR' for g in slot.groups]))

  course_names = sorted(set([(slot.course, slot.course_slug) for slot in slots]))
  courses = []
  for cn in course_names:
    courses.append({
      'name': cn[0],
      'id': cn[1],
      'pr_groups': sorted(set([g for slot in slots if slot.course_slug == cn[1] and slot.ctype_abbr == 'PR' for g in slot.groups])),
      'rv_groups': sorted(set([g for slot in slots if slot.course_slug == cn[1] and slot.ctype_abbr != 'PR' for g in slot.groups]))
    })

  flask.session['courses'] = [c[1] for c in course_names]

  # Load existing settings for prefilling form if available
  existing_format = {}
  settings_fn = gcal.BASE_DATA_DIR / 'settings' / f"{email}.yaml"
  if settings_fn.exists():
    try:
      existing_settings = yaml.safe_load(open(settings_fn, 'r'))
      existing_format = existing_settings.get('format', {})
    except:
      pass

  return flask.render_template('configure.html',
                               title=title,
                               schoolcode=schoolcode,
                               filterId=filterId,
                               pr_groups=pr_groups,
                               rv_groups=rv_groups,
                               courses=courses,
                               existing_format=existing_format)

  
@app.route('/sync/<start_stop>', methods=['POST'])
def toggle_sync(start_stop):
  email = flask.session.get('email')
  if not email:
    return flask.redirect('/')
  
  if start_stop == 'start':
    enabled = True
    title = "Sinhronizacija vklopljena"
  elif start_stop == 'stop':
    enabled = False
    title = "Sinhronizacija ustavljena"
  else:
    return flask.render_template('error.html',
      message='Neveljavna zahteva.',
      details='Zahteva mora biti bodisi "start" ali "stop".',
      back_url='/', back_text='Nazaj na začetek')

  try:
    gcal.set_calendar_enabled(email, enabled)
  except FileNotFoundError:
    return flask.render_template('error.html',
      message='Koledar ni nastavljen.',
      details='Za vaš račun ni bilo mogoče najti nastavitev koledarja.',
      help_tips=['Najprej nastavite koledar', 'Preverite, da ste prijavljeni s pravilnim računom'],
      back_url='/', back_text='Nazaj na začetek')

  return flask.render_template('success.html', title=title, stopped=not enabled)

# ==================== ADMIN ROUTES ====================

@app.route('/admin')
@require_admin
def admin_dashboard():
  """Admin dashboard with system health and user management."""
  global last_check_time
  
  settings_dir = gcal.BASE_DATA_DIR / 'settings'
  credentials_dir = gcal.BASE_DATA_DIR / 'credentials'
  synced_dir = gcal.BASE_DATA_DIR / 'synced_events'
  calendars_dir = gcal.BASE_DATA_DIR / 'calendars'
  
  # Collect user data
  users = []
  total_events = 0
  active_count = 0
  disabled_count = 0
  
  for settings_fn in sorted(settings_dir.glob('*.yaml')):
    email = settings_fn.stem
    try:
      settings = yaml.safe_load(open(settings_fn, 'r'))
      calendar_config = settings.get('calendar', {})
      enabled = calendar_config.get('enabled', False)
      
      # Count events for this user
      synced_file = synced_dir / f"{email}.txt"
      event_count = 0
      if synced_file.exists():
        event_count = len(open(synced_file).readlines())
      total_events += event_count
      
      # Get last update time
      last_update = gcal.get_last_update_time(email)
      
      # Check if stale (no update in 7+ days)
      is_stale = False
      if last_update is None or last_update < datetime.now(LJUBLJANA_TZ) - timedelta(days=7):
        is_stale = True
      
      if enabled:
        active_count += 1
      else:
        disabled_count += 1
      
      users.append({
        'email': email,
        'enabled': enabled,
        'title': calendar_config.get('title', 'N/A'),
        'schoolcode': calendar_config.get('timetable', {}).get('schoolcode', 'N/A'),
        'filterId': calendar_config.get('timetable', {}).get('filterId', 'N/A'),
        'event_count': event_count,
        'last_update': last_update,
        'is_stale': is_stale,
        'force_sync': calendar_config.get('force_sync', False)
      })
    except Exception as e:
      logger.error(f"Error loading settings for {email}: {e}")
      users.append({
        'email': email,
        'enabled': False,
        'title': 'ERROR',
        'schoolcode': 'N/A',
        'filterId': 'N/A',
        'event_count': 0,
        'last_update': None,
        'is_stale': True,
        'force_sync': False
      })
  
  # System health stats
  total_registered = len(list(credentials_dir.glob('*.json')))
  total_configured = len(users)
  unique_timetables = len([f for f in calendars_dir.glob('*.ics') if not f.name.endswith('.new.ics')])
  
  # Calculate disk usage
  try:
    disk_usage_bytes = sum(f.stat().st_size for f in gcal.BASE_DATA_DIR.rglob('*') if f.is_file())
    disk_usage_mb = disk_usage_bytes / (1024 * 1024)
  except Exception:
    disk_usage_mb = 0
  
  # Scheduler info
  next_run_time = sync_job.next_run_time if sync_job else None
  
  # Timetable info with detailed data
  timetables = []
  for cal_file in sorted(calendars_dir.glob('*.ics')):
    if not cal_file.name.endswith('.new.ics'):
      size_kb = cal_file.stat().st_size / 1024
      
      # Parse filename to extract schoolcode and filterId
      # Format: {schoolcode}_{filterId}.ics
      # schoolcode can contain _, filterId only contains digits, commas, and semicolons
      # So we split from the right to find the filterId
      filename_stem = cal_file.stem
      parts = filename_stem.rsplit('_', 1)
      if len(parts) == 2 and re.match(r'^[\d,;]+$', parts[1]):
        schoolcode = parts[0]
        filterId = parts[1]
      else:
        schoolcode = filename_stem
        filterId = 'N/A'
      
      # Count slots in the timetable
      slot_count = 0
      try:
        slots = wise_tt.get_slots(cal_file)
        slot_count = len(slots)
      except Exception:
        slot_count = 0
      
      # Find users using this timetable
      users_using = []
      for user_data in users:
        if user_data['schoolcode'] == schoolcode and user_data['filterId'] == filterId:
          users_using.append(user_data['email'])
      
      # Get last modified time
      last_modified = datetime.fromtimestamp(cal_file.stat().st_mtime, LJUBLJANA_TZ)
      
      timetables.append({
        'filename': cal_file.name,
        'schoolcode': schoolcode,
        'filterId': filterId,
        'size_kb': size_kb,
        'slot_count': slot_count,
        'users_using': users_using,
        'last_modified': last_modified
      })
  
  return flask.render_template('admin_dashboard.html',
                               users=users,
                               total_registered=total_registered,
                               total_configured=total_configured,
                               active_count=active_count,
                               disabled_count=disabled_count,
                               total_events=total_events,
                               unique_timetables=unique_timetables,
                               disk_usage_mb=disk_usage_mb,
                               last_check_time=last_check_time,
                               next_run_time=next_run_time,
                               timetables=timetables)

@app.route('/admin/user/<email>')
@require_admin
def admin_user_detail(email):
  """View detailed information about a specific user."""
  settings_fn = gcal.BASE_DATA_DIR / 'settings' / f"{email}.yaml"
  
  if not settings_fn.exists():
    flask.flash(f'User {email} not found', 'error')
    return flask.redirect('/admin')
  
  settings = yaml.safe_load(open(settings_fn, 'r'))
  
  # Get synced event count
  synced_file = gcal.BASE_DATA_DIR / 'synced_events' / f"{email}.txt"
  event_count = 0
  if synced_file.exists():
    event_count = len(open(synced_file).readlines())
  
  last_update = gcal.get_last_update_time(email)
  
  return flask.render_template('admin_user_detail.html',
                               email=email,
                               settings=settings,
                               settings_yaml=yaml.safe_dump(settings, default_flow_style=False),
                               event_count=event_count,
                               last_update=last_update)

@app.route('/admin/force-sync/<email>', methods=['POST'])
@require_admin
def admin_force_sync_user(email):
  """Force sync for a specific user."""
  settings_fn = gcal.BASE_DATA_DIR / 'settings' / f"{email}.yaml"
  
  if not settings_fn.exists():
    flask.flash(f'User {email} not found', 'error')
    return flask.redirect('/admin')
  
  try:
    settings = yaml.safe_load(open(settings_fn, 'r'))
    settings.setdefault('calendar', {})['force_sync'] = True
    with open(settings_fn, 'w') as f:
      yaml.safe_dump(settings, f)
    
    # Trigger immediate scheduler run
    sync_job.modify(next_run_time=datetime.now())
    
    logger.info(f"Admin forced sync for user: {email}")
    flask.flash(f'Force sync triggered for {email}', 'success')
  except Exception as e:
    logger.error(f"Error forcing sync for {email}: {e}")
    flask.flash(f'Error: {str(e)}', 'error')
  
  return flask.redirect('/admin')

@app.route('/admin/disable/<email>', methods=['POST'])
@require_admin
def admin_disable_user(email):
  """Disable calendar sync for a user."""
  try:
    gcal.set_calendar_enabled(email, False)
    logger.info(f"Admin disabled calendar for user: {email}")
    flask.flash(f'Calendar disabled for {email}', 'success')
  except FileNotFoundError:
    flask.flash(f'User {email} not found', 'error')
  except Exception as e:
    logger.error(f"Error disabling calendar for {email}: {e}")
    flask.flash(f'Error: {str(e)}', 'error')
  
  return flask.redirect('/admin')

@app.route('/admin/enable/<email>', methods=['POST'])
@require_admin
def admin_enable_user(email):
  """Enable calendar sync for a user."""
  try:
    gcal.set_calendar_enabled(email, True)
    logger.info(f"Admin enabled calendar for user: {email}")
    flask.flash(f'Calendar enabled for {email}', 'success')
  except FileNotFoundError:
    flask.flash(f'User {email} not found', 'error')
  except Exception as e:
    logger.error(f"Error enabling calendar for {email}: {e}")
    flask.flash(f'Error: {str(e)}', 'error')
  
  return flask.redirect('/admin')

# ==================== BULK OPERATIONS ====================

@app.route('/admin/bulk/force_sync_all', methods=['POST'])
@require_admin
def admin_bulk_force_sync_all():
  """Force sync for all users."""
  settings_dir = gcal.BASE_DATA_DIR / 'settings'
  success_count = 0
  errors = []
  
  for settings_fn in settings_dir.glob('*.yaml'):
    email = settings_fn.stem
    try:
      settings = yaml.safe_load(open(settings_fn, 'r'))
      settings.setdefault('calendar', {})['force_sync'] = True
      with open(settings_fn, 'w') as f:
        yaml.safe_dump(settings, f)
      success_count += 1
    except Exception as e:
      errors.append(f"{email}: {str(e)}")
  
  # Trigger immediate scheduler run
  if success_count > 0:
    sync_job.modify(next_run_time=datetime.now())
  
  logger.info(f"Admin forced sync for all users: {success_count} successful, {len(errors)} errors")
  
  if errors:
    flask.flash(f'Force sync set for {success_count} users with {len(errors)} errors', 'warning')
  else:
    flask.flash(f'Force sync triggered for all {success_count} users', 'success')
  
  return flask.redirect('/admin')

@app.route('/admin/bulk/disable_all', methods=['POST'])
@require_admin
def admin_bulk_disable_all():
  """Disable all calendars."""
  settings_dir = gcal.BASE_DATA_DIR / 'settings'
  success_count = 0
  errors = []
  
  for settings_fn in settings_dir.glob('*.yaml'):
    email = settings_fn.stem
    try:
      gcal.set_calendar_enabled(email, False)
      success_count += 1
    except Exception as e:
      errors.append(f"{email}: {str(e)}")
  
  logger.info(f"Admin disabled all calendars: {success_count} successful, {len(errors)} errors")
  
  if errors:
    flask.flash(f'Disabled {success_count} calendars with {len(errors)} errors', 'warning')
  else:
    flask.flash(f'All {success_count} calendars disabled', 'success')
  
  return flask.redirect('/admin')

@app.route('/admin/bulk/enable_all', methods=['POST'])
@require_admin
def admin_bulk_enable_all():
  """Enable all calendars."""
  settings_dir = gcal.BASE_DATA_DIR / 'settings'
  success_count = 0
  errors = []
  
  for settings_fn in settings_dir.glob('*.yaml'):
    email = settings_fn.stem
    try:
      gcal.set_calendar_enabled(email, True)
      success_count += 1
    except Exception as e:
      errors.append(f"{email}: {str(e)}")
  
  logger.info(f"Admin enabled all calendars: {success_count} successful, {len(errors)} errors")
  
  if errors:
    flask.flash(f'Enabled {success_count} calendars with {len(errors)} errors', 'warning')
  else:
    flask.flash(f'All {success_count} calendars enabled', 'success')
  
  return flask.redirect('/admin')

@app.route('/admin/bulk/redownload_timetables', methods=['POST'])
@require_admin
def admin_bulk_redownload_timetables():
  """Delete all cached timetables to force re-download."""
  calendars_dir = gcal.BASE_DATA_DIR / 'calendars'
  success_count = 0
  errors = []
  
  for cal_file in calendars_dir.glob('*.ics'):
    if not cal_file.name.endswith('.new.ics'):
      try:
        cal_file.unlink()
        success_count += 1
      except Exception as e:
        errors.append(f"{cal_file.name}: {str(e)}")
  
  # Trigger immediate sync to re-download
  if success_count > 0:
    sync_job.modify(next_run_time=datetime.now())
  
  logger.info(f"Admin deleted timetables: {success_count} successful, {len(errors)} errors")
  
  if errors:
    flask.flash(f'Deleted {success_count} timetables with {len(errors)} errors', 'warning')
  else:
    flask.flash(f'All {success_count} timetables deleted and sync triggered', 'success')
  
  return flask.redirect('/admin')

@app.route('/admin/bulk/clear_sync_state', methods=['POST'])
@require_admin
def admin_bulk_clear_sync_state():
  """Clear sync state for all users."""
  synced_dir = gcal.BASE_DATA_DIR / 'synced_events'
  success_count = 0
  errors = []
  
  for synced_file in synced_dir.glob('*.txt'):
    try:
      synced_file.unlink()
      success_count += 1
    except Exception as e:
      errors.append(f"{synced_file.name}: {str(e)}")
  
  logger.info(f"Admin cleared sync state: {success_count} files deleted, {len(errors)} errors")
  
  if errors:
    flask.flash(f'Cleared {success_count} sync state files with {len(errors)} errors', 'warning')
  else:
    flask.flash(f'All {success_count} sync state files cleared', 'success')
  
  return flask.redirect('/admin')

@app.route('/admin/migrate-keys', methods=['POST'])
@require_admin
def admin_migrate_keys():
  """Migrate configuration keys from abbreviations to slugs."""
  settings_dir = gcal.BASE_DATA_DIR / 'settings'
  calendars_dir = gcal.BASE_DATA_DIR / 'calendars'
  
  success_count = 0
  errors = []
  migration_details = []
  
  for settings_fn in settings_dir.glob('*.yaml'):
    email = settings_fn.stem
    try:
      # Load settings
      settings = yaml.safe_load(open(settings_fn, 'r'))
      calendar_config = settings.get('calendar', {})
      format_config = settings.get('format', {})
      
      if not format_config:
        continue
      
      # Get timetable info
      schoolcode = calendar_config.get('timetable', {}).get('schoolcode')
      filterId = calendar_config.get('timetable', {}).get('filterId')
      
      if not schoolcode or not filterId:
        errors.append(f"{email}: Missing timetable configuration")
        continue
      
      # Find and load timetable file
      cal_filename = f"{schoolcode}_{filterId}.ics"
      cal_path = calendars_dir / cal_filename
      
      if not cal_path.exists():
        errors.append(f"{email}: Timetable file not found: {cal_filename}")
        continue
      
      # Parse slots to get course names and slugs
      slots = wise_tt.get_slots(str(cal_path))
      
      # Create mapping from abbreviation to slug
      abbr_to_slug = {}
      for slot in slots:
        if slot.course_abbr not in abbr_to_slug:
          abbr_to_slug[slot.course_abbr] = slot.course_slug
      
      # Create backup
      backup_fn = settings_fn.with_suffix('.yaml.backup')
      with open(backup_fn, 'w') as f:
        yaml.dump(settings, f, allow_unicode=True)
      
      # Migrate format keys
      new_format = {}
      keys_migrated = []
      
      # Keep DEFAULT
      if 'DEFAULT' in format_config:
        new_format['DEFAULT'] = format_config['DEFAULT']
      
      # Migrate course-specific keys
      for key, value in format_config.items():
        if key == 'DEFAULT':
          continue
        
        if key in abbr_to_slug:
          new_key = abbr_to_slug[key]
          new_format[new_key] = value
          keys_migrated.append(f"{key} → {new_key}")
        # else: orphaned key, not migrating (will be removed)
      
      # Update settings
      settings['format'] = new_format
      
      # Save migrated settings
      with open(settings_fn, 'w') as f:
        yaml.dump(settings, f, allow_unicode=True)
      
      success_count += 1
      if keys_migrated:
        migration_details.append({
          'email': email,
          'keys': keys_migrated
        })
      
      logger.info(f"Migrated keys for {email}: {', '.join(keys_migrated) if keys_migrated else 'no keys to migrate'}")
      
    except Exception as e:
      errors.append(f"{email}: {str(e)}")
      logger.error(f"Error migrating keys for {email}: {e}")
  
  logger.info(f"Admin migrated configuration keys: {success_count} successful, {len(errors)} errors")
  
  if errors:
    flask.flash(f'Migrated {success_count} user settings with {len(errors)} errors', 'warning')
    for error in errors[:5]:  # Show first 5 errors
      flask.flash(error, 'error')
  else:
    flask.flash(f'Successfully migrated configuration keys for {success_count} users', 'success')
  
  if migration_details:
    flask.flash(f'Migrated {sum(len(d["keys"]) for d in migration_details)} total keys', 'info')
  
  return flask.redirect('/admin')

# ==================== END ADMIN ROUTES ====================

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

def create_app():
  gcal.ensure_dirs()
  return app

if __name__ == '__main__':
  app.run(os.environ.get('HOST', 'localhost'), int(os.environ.get('PORT', 8080)))