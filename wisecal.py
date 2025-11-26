import os
import flask
from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix
import yaml
import gcal
import json
import re
import logging

import google.oauth2.id_token
import google_auth_oauthlib.flow

from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
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


scheduler = BackgroundScheduler()
LJUBLJANA_TZ = ZoneInfo('Europe/Ljubljana')
last_check_time = None
last_update_time = None

def wisecal_sync_task():
    global last_check_time, last_update_time
    calendar_updated = wisecal_cron.main()
    last_check_time = datetime.now(LJUBLJANA_TZ)
    if calendar_updated:
        last_update_time = last_check_time

sync_job = scheduler.add_job(wisecal_sync_task, 'interval', minutes=15, max_instances=1)
logger.info("Starting background scheduler for calendar sync...")
scheduler.start()

@app.route('/')
def index():
  global last_check_time, last_update_time
  return flask.render_template('index.html',
                email=flask.session.get('email'),
                last_check_time=last_check_time,
                last_update_time=last_update_time,
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
      logger.error(f"Error downloading timetable for {email}: {e}")
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

  course_names = sorted(set([(slot.course, slot.course_abbr) for slot in slots]))
  courses = []
  for cn in course_names:
    courses.append({
      'name': cn[0],
      'id': cn[1],
      'pr_groups': sorted(set([g for slot in slots if slot.course_abbr == cn[1] and slot.ctype_abbr == 'PR' for g in slot.groups])),
      'rv_groups': sorted(set([g for slot in slots if slot.course_abbr == cn[1] and slot.ctype_abbr != 'PR' for g in slot.groups]))
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

  
@app.route('/stop')
def stop():
  email = flask.session.get('email')
  if not email:
    return flask.redirect('/')

  settings_fn = gcal.BASE_DATA_DIR / 'settings' / f"{email}.json"
  if not settings_fn.exists():
    return flask.render_template('error.html',
      message='Koledar ni nastavljen.',
      details='Za vaš račun ni bilo mogoče najti nastavitev koledarja.',
      help_tips=['Najprej nastavite koledar', 'Preverite, da ste prijavljeni s pravilnim računom'],
      back_url='/', back_text='Nazaj na začetek')
  settings = yaml.safe_load(open(settings_fn, 'r'))
  settings['calendar']['enabled'] = False
  with open(settings_fn, 'w') as fh:
    yaml.safe_dump(settings, fh)
  return flask.render_template('success.html', title='Sinhronizacija ustavljena', stopped=True)

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

def create_app():
  gcal.ensure_dirs()
  return app

if __name__ == '__main__':
  app.run(os.environ.get('HOST', 'localhost'), int(os.environ.get('PORT', 8080)))