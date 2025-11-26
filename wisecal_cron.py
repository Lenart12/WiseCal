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
        logger.info(f"No changes to sync for {owner}")
        return

    logger.info(f"Syncing for {owner}: {len(to_insert)} to insert, {len(to_delete)} to delete, {len(synced)} unchanged")

    service = gcal.get_cal_service(owner)
    cal_id = gcal.get_cal_id(owner)
    if not cal_id:
        cal_id = gcal.create_calendar(owner, settings['calendar']['title'])
        logger.info(f"Created new calendar for {owner}: {cal_id}")

    BATCH_SIZE = 50
    for i in range(0, len(to_insert), BATCH_SIZE):
        batch = service.new_batch_http_request()
        for slot in to_insert[i:i+BATCH_SIZE]:
            batch.add(service.events().insert(calendarId=cal_id, body=slot))
        batch.execute()
    for i in range(0, len(to_delete), BATCH_SIZE):
        batch = service.new_batch_http_request()
        for slot_id in to_delete[i:i+BATCH_SIZE]:
            batch.add(service.events().delete(calendarId=cal_id, eventId=slot_id))
        batch.execute()

    gcal.save_synced_event_ids(owner, synced + [slot['id'] for slot in to_insert])
    logger.info(f"Sync completed for {owner}")

def main():
    logger.info("Starting WiseCal cron job")
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
    logger.info(f"Found {total_users} enabled calendars to sync")
    
    for schoolcode in jobs:
        for filterId in jobs[schoolcode]:
            tt_filename = schoolcode + "_" + filterId
            logger.info(f"Downloading timetable: {schoolcode}, {filterId}")
            new_tt = wise_tt.download_ical(
                {'schoolcode': schoolcode, 'filterId': filterId},
                gcal.BASE_DATA_DIR / 'calendars' / f"{tt_filename}.new.ics"
            )
            old_tt = gcal.BASE_DATA_DIR / 'calendars' / f"{tt_filename}.ics"

            has_force_sync = any(settings.get('calendar', {}).get('force_sync', False) for settings in jobs[schoolcode][filterId])
            is_same = old_tt.exists() and filecmp.cmp(old_tt, new_tt)
            # If the old and new files are the same, delete the new one and continue
            if not has_force_sync and is_same:
                new_tt.unlink()
                logger.info(f"No changes in timetable: {schoolcode}, {filterId}")
                continue

            slots = wise_tt.get_slots(new_tt)
            logger.info(f"Timetable changed: {schoolcode}, {filterId} - {len(slots)} slots")
            for settings in jobs[schoolcode][filterId]:
                if is_same and not settings.get('calendar', {}).get('force_sync', False):
                    logger.info(f"Skipping sync for {settings['calendar']['owner']} as there are no changes")
                    continue
                try:
                    sync_slots(slots, settings)
                except Exception as e:
                    logger.error(f"Error syncing slots for {settings['calendar']['owner']}: {e}")

            new_tt.rename(old_tt)
    
    logger.info("WiseCal cron job completed")
            

if __name__ == '__main__':
    main()
