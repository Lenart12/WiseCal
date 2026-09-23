import gcal
import wise_tt
import yaml
import filecmp
import logging
import copy
import time
from google.auth.exceptions import RefreshError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 1000
MAX_ATTEMPTS = 5

def error_status(exception):
    return getattr(getattr(exception, 'resp', None), 'status', None)

def is_transient(exception):
    status = error_status(exception)
    if status in (429, 500, 502, 503, 504):
        return True
    reasons = [d.get('reason') for d in (getattr(exception, 'error_details', None) or []) if isinstance(d, dict)]
    return status == 403 and any(r in ('rateLimitExceeded', 'userRateLimitExceeded') for r in reasons)

def execute_batched(service, requests):
    """Execute {event_id: request_factory} in batches, retrying transient errors with backoff.
    Returns {event_id: exception} for requests that ultimately failed."""
    pending = dict(requests)
    errors = {}
    for attempt in range(MAX_ATTEMPTS):
        if attempt > 0:
            logger.info(f"Retrying {len(pending)} requests after transient errors (attempt {attempt + 1}/{MAX_ATTEMPTS})")
            time.sleep(2 ** attempt)
        retry = {}
        def callback(event_id, _, exception):
            if exception is None:
                errors.pop(event_id, None)
                return
            errors[event_id] = exception
            if is_transient(exception):
                retry[event_id] = pending[event_id]
        ids = list(pending)
        for i in range(0, len(ids), BATCH_SIZE):
            batch = service.new_batch_http_request(callback=callback)
            for event_id in ids[i:i + BATCH_SIZE]:
                batch.add(pending[event_id](), request_id=event_id)
            batch.execute()
        pending = retry
        if not pending:
            break
    return errors

def log_errors(action, errors):
    for event_id, exception in errors.items():
        logger.error(f"Failed to {action} event {event_id}: {getattr(exception, 'content', exception)}")

def sync_slots(slots, settings):
    owner = settings['calendar']['owner']
    synced_slots = set(gcal.load_synced_event_ids(owner))
    format_settings = settings['format']
    slots_fmt = [slot.to_gcal(format_settings) for slot in slots]
    slots_fmt = [slot for slot in slots_fmt if slot is not None]
    new_ids = set([slot['id'] for slot in slots_fmt])

    synced = []
    to_insert = []
    to_delete = []

    for slot in slots_fmt:
        if slot['id'] in synced_slots:
            synced.append(slot['id'])
        else:
            to_insert.append(slot)
    for slot_id in synced_slots:
        if slot_id not in new_ids:
            to_delete.append(slot_id)

    if len(to_insert) == 0 and len(to_delete) == 0:
        logger.debug(f"No changes to sync for {owner}")
        return

    logger.info(f"Syncing for {owner}: {len(to_insert)} to insert, {len(to_delete)} to delete, {len(synced)} unchanged")

    try:
        service = gcal.get_cal_service(owner)
    except RefreshError as e:
        logger.error(f"Failed to refresh credentials for {owner}: {e}")
        gcal.set_calendar_enabled(owner, False)
        return

    cal_id = gcal.get_cal_id(owner)
    if not cal_id:
        cal_id = gcal.create_calendar(owner, settings['calendar']['title'])
        logger.info(f"Created new calendar for {owner}: {cal_id}")

    insert_errors = execute_batched(service, {
        slot['id']: lambda slot=slot: service.events().insert(calendarId=cal_id, body=slot)
        for slot in to_insert
    })

    # 409 on insert means the event already exists - update it instead
    conflicts = {event_id for event_id, e in insert_errors.items() if error_status(e) == 409}
    if conflicts:
        logger.info(f"Updating {len(conflicts)} existing events for {owner}")
        update_errors = execute_batched(service, {
            slot['id']: lambda slot=slot: service.events().update(calendarId=cal_id, eventId=slot['id'], body=slot)
            for slot in to_insert if slot['id'] in conflicts
        })
        for event_id in conflicts:
            insert_errors.pop(event_id)
        insert_errors.update(update_errors)

    delete_errors = execute_batched(service, {
        event_id: lambda event_id=event_id: service.events().delete(calendarId=cal_id, eventId=event_id)
        for event_id in to_delete
    })
    # 404 on delete is fine - event is already gone
    delete_errors = {event_id: e for event_id, e in delete_errors.items() if error_status(e) != 404}

    log_errors('insert', insert_errors)
    log_errors('delete', delete_errors)

    inserted_ids = [slot['id'] for slot in to_insert if slot['id'] not in insert_errors]
    deleted_ids = [event_id for event_id in to_delete if event_id not in delete_errors]

    # Events that failed to delete stay tracked so they are retried on the next sync
    final_synced_ids = set(synced) | set(inserted_ids) | set(delete_errors)
    gcal.save_synced_event_ids(owner, list(final_synced_ids))
    gcal.set_last_update_time(owner)

    if insert_errors or delete_errors:
        logger.warning(f"Sync completed for {owner} with errors: {len(insert_errors)} insert failures, {len(delete_errors)} delete failures")
        
        # Check if calendar might be gone
        if gcal.check_calendar_exists(owner, cal_id) is False:
            logger.error(f"Calendar {cal_id} for {owner} no longer exists. Disabling calendar sync.")
            gcal.set_calendar_enabled(owner, False)
            gcal.delete_calendar_id(owner)
        else:
            gcal.set_force_sync(owner, True)
    else:
        logger.info(f"Sync completed for {owner}: {len(inserted_ids)} inserted, {len(deleted_ids)} deleted")

def main():
    logger.debug("Starting WiseCal cron job")
    gcal.ensure_dirs()
    settings_dir = gcal.BASE_DATA_DIR / 'settings'
    jobs = {}
    for settings_fn in settings_dir.glob('*.yaml'):
        settings = yaml.safe_load(open(settings_fn, 'r'))
        if settings.get('calendar', {}).get('enabled', False):
            url = settings['calendar'].get('timetable', {}).get('url')
            if not url:
                logger.warning(f"Skipping settings file {settings_fn} due to missing timetable url")
                continue
            jobs.setdefault(url, []).append(settings)
            # Reset force_sync after use
            if settings['calendar'].get('force_sync', False):
                logger.info(f"Force sync enabled for {settings['calendar']['owner']}")
                new_settings = copy.deepcopy(settings)
                new_settings['calendar']['force_sync'] = False
                with open(settings_fn, 'w') as f:
                    yaml.safe_dump(new_settings, f)
            
    
    total_users = sum(len(users) for users in jobs.values())
    logger.debug(f"Found {total_users} enabled calendars to sync")
    
    calendar_updated = False
    for url, users in jobs.items():
        tt_filename = wise_tt.timetable_id(url)
        logger.debug(f"Downloading timetable: {url}")
        try:
            new_tt = wise_tt.download_ical(url, gcal.BASE_DATA_DIR / 'calendars' / f"{tt_filename}.new.ics")
        except Exception as e:
            logger.error(f"Failed to download timetable {url}: {str(e).splitlines()[0].strip()}")
            continue
        old_tt = gcal.BASE_DATA_DIR / 'calendars' / f"{tt_filename}.ics"

        has_force_sync = any(settings.get('calendar', {}).get('force_sync', False) for settings in users)
        is_same = old_tt.exists() and filecmp.cmp(old_tt, new_tt)
        # If the old and new files are the same, delete the new one and continue
        if not has_force_sync and is_same:
            new_tt.unlink()
            logger.debug(f"No changes in timetable: {url}")
            continue

        slots = wise_tt.get_slots(new_tt)
        logger.info(f"Timetable changed: {url} - {len(slots)} slots")
        for settings in users:
            if is_same and not settings.get('calendar', {}).get('force_sync', False):
                logger.debug(f"Skipping sync for {settings['calendar']['owner']} as there are no changes")
                continue
            try:
                sync_slots(slots, settings)
                calendar_updated = True
            except Exception as e:
                logger.error(f"Error syncing slots for {settings['calendar']['owner']}: {e}")

        new_tt.rename(old_tt)
    
    logger.debug("WiseCal cron job completed")
    return calendar_updated

if __name__ == '__main__':
    main()
