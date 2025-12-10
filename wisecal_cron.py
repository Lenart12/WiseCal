import gcal
import wise_tt
import yaml
import filecmp
import logging
import copy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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

    service = gcal.get_cal_service(owner)
    cal_id = gcal.get_cal_id(owner)
    if not cal_id:
        cal_id = gcal.create_calendar(owner, settings['calendar']['title'])
        logger.info(f"Created new calendar for {owner}: {cal_id}")

    # Track successfully processed events
    inserted_ids = []
    deleted_ids = []
    insert_errors = []
    delete_errors = []

    def make_insert_callback(slot_id):
        def callback(_, __, exception):
            if exception is not None:
                insert_errors.append((slot_id, exception))
                logger.error(f"Failed to insert event {slot_id}: {exception}")
            else:
                inserted_ids.append(slot_id)
        return callback

    def make_delete_callback(slot_id):
        def callback(_, __, exception):
            if exception is not None:
                # 404 errors on delete are okay - event already gone
                if hasattr(exception, 'resp') and exception.resp.status == 404:
                    deleted_ids.append(slot_id)
                else:
                    delete_errors.append((slot_id, exception))
                    logger.error(f"Failed to delete event {slot_id}: {exception}")
            else:
                deleted_ids.append(slot_id)
        return callback

    BATCH_SIZE = 1000
    insert_idx = 0
    delete_idx = 0
    while insert_idx < len(to_insert) or delete_idx < len(to_delete):
        batch = service.new_batch_http_request()
        batch_count = 0

        # Add inserts to batch
        while insert_idx < len(to_insert) and batch_count < BATCH_SIZE:
            slot = to_insert[insert_idx]
            batch.add(
                service.events().insert(calendarId=cal_id, body=slot),
                callback=make_insert_callback(slot['id'])
            )
            insert_idx += 1
            batch_count += 1

        # Add deletes to batch
        while delete_idx < len(to_delete) and batch_count < BATCH_SIZE:
            slot_id = to_delete[delete_idx]
            batch.add(
                service.events().delete(calendarId=cal_id, eventId=slot_id),
                callback=make_delete_callback(slot_id)
            )
            delete_idx += 1
            batch_count += 1

        batch.execute()

    # Update synced IDs: keep synced + successfully inserted - successfully deleted
    final_synced_ids = set(synced) | set(inserted_ids)
    final_synced_ids -= set(deleted_ids)
    gcal.save_synced_event_ids(owner, list(final_synced_ids))

    if insert_errors or delete_errors:
        logger.warning(f"Sync completed for {owner} with errors: {len(insert_errors)} insert failures, {len(delete_errors)} delete failures")
        
        # Check if calendar might be gone
        if gcal.check_calendar_exists(owner, cal_id) is False:
            logger.error(f"Calendar {cal_id} for {owner} no longer exists. Disabling calendar sync.")
            gcal.set_calendar_enabled(owner, False)
            gcal.delete_calendar_id(owner)
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
            schoolcode = settings['calendar'].get('timetable', {}).get('schoolcode')
            filterId = settings['calendar'].get('timetable', {}).get('filterId')
            if not schoolcode or not filterId:
                logger.warning(f"Skipping settings file {settings_fn} due to missing schoolcode or filterId")
                continue
            jobs.setdefault(schoolcode, {}).setdefault(filterId, []).append(settings)
            # Reset force_sync after use
            if settings['calendar'].get('force_sync', False):
                logger.info(f"Force sync enabled for {settings['calendar']['owner']}")
                new_settings = copy.deepcopy(settings)
                new_settings['calendar']['force_sync'] = False
                with open(settings_fn, 'w') as f:
                    yaml.safe_dump(new_settings, f)
            
    
    total_users = sum(len(users) for sc in jobs.values() for users in sc.values())
    logger.debug(f"Found {total_users} enabled calendars to sync")
    
    calendar_updated = False
    for schoolcode in jobs:
        for filterId in jobs[schoolcode]:
            tt_filename = schoolcode + "_" + filterId
            logger.debug(f"Downloading timetable: {schoolcode}, {filterId}")
            try:
                new_tt = wise_tt.download_ical(
                    {'schoolcode': schoolcode, 'filterId': filterId},
                    gcal.BASE_DATA_DIR / 'calendars' / f"{tt_filename}.new.ics"
                )
            except Exception as e:
                logger.error(f"Failed to download timetable for {schoolcode}, {filterId}: {str(e).splitlines()[0].strip()}")
                continue
            old_tt = gcal.BASE_DATA_DIR / 'calendars' / f"{tt_filename}.ics"

            has_force_sync = any(settings.get('calendar', {}).get('force_sync', False) for settings in jobs[schoolcode][filterId])
            is_same = old_tt.exists() and filecmp.cmp(old_tt, new_tt)
            # If the old and new files are the same, delete the new one and continue
            if not has_force_sync and is_same:
                new_tt.unlink()
                logger.debug(f"No changes in timetable: {schoolcode}, {filterId}")
                continue

            slots = wise_tt.get_slots(new_tt)
            logger.info(f"Timetable changed: {schoolcode}, {filterId} - {len(slots)} slots")
            for settings in jobs[schoolcode][filterId]:
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
