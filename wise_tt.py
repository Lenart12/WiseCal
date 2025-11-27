from playwright.sync_api import sync_playwright
import icalendar
import hashlib
import datetime
import base64

WTT_API_URL = "https://www.wise-tt.com"

def download_ical(timetable, download_path):
    with sync_playwright() as p:
        # print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        url = f"{WTT_API_URL}/wtt_{timetable['schoolcode']}/index.jsp?filterId={timetable['filterId']}"
        response = page.goto(url)
        if not response or not response.ok:
            raise ValueError(f"Napaka pri nalaganju {url}, status: {response.status if response else 'no response'}")
        if page.locator('a[title="Izvoz celotnega urnika v ICS formatu  "]').count() == 0:
            raise ValueError(f"Urnik na {url} nima aktivnih terminov.")
        # print(f"Navigated to {url}")
        page.click('a[title="Izvoz celotnega urnika v ICS formatu  "]', timeout=3000)
        # print("Clicked on iCal export link")
        with page.expect_download(timeout=5000) as download_info:
            pass  # The click already initiated the download
            # print("Waiting for download to start...")
        download = download_info.value
        download.save_as(download_path)
        # print(f"Downloaded iCal file to {download_path}")
        browser.close()
    return download_path        

class WiseSlot:
    course = "" # Course name - e.g., "Spletne tehnologije"
    course_abbr = ""  # Course abbreviation - e.g., "ST"
    ctype = ""  # Course type - e.g., "Predavanje", "Računalniške vaje", "Seminarska vaje"
    ctype_abbr = ""  # Course type abbreviation - e.g., "PR", "RV", "SV"
    groups = []  # List of groups for this course type - e.g., "MAG 1 RIT", "MAG 1 RIT RV 5"
    location = ""  # Location of the session
    lecturer = ""  # Lecturer's name
    start_time = None  # Start time as datetime object
    end_time = None    # End time as datetime object

    _hash = None # Cached hash value

    def _fmt_self(self, fmt):
        return fmt.format(
            course=self.course,
            course_abbr=self.course_abbr,
            ctype=self.ctype,
            ctype_abbr=self.ctype_abbr,
            groups=", ".join(self.groups),
            location=self.location,
            lecturer=self.lecturer,
            start_time=self.start_time,
            end_time=self.end_time
        )
    
    def to_gcal(self, f):
        fsel = 'PR' if self.ctype_abbr == 'PR' else 'VAJE'
        df = f.get('DEFAULT', {}).get(fsel, {})
        cf = f.get(self.course_abbr, {}).get(fsel, {})
        def v(key, default):
            return cf.get(key, df.get(key, default))
        color = v('color', None)
        if color is None:
            b0 = hashlib.md5(self.course_abbr.encode('utf-8')).digest()[0]
            color = (b0 % 11) + 1  # Google Calendar colors are 1-11
        title_fmt = v('title', "{course} {ctype_abbr}")
        location_fmt = v('location', "{location}")
        description_fmt = v('description', "{course} {ctype} by {lecturer} for groups: {groups}")
        start_offset = v('start_offset', None)
        end_offset = v('end_offset', None)
        exclude_groups = df.get('exclude_groups', []) + cf.get('exclude_groups', [])
        filtered_groups = [g for g in self.groups if g not in exclude_groups]
        if len(filtered_groups) == 0:
            return None
        title = self._fmt_self(title_fmt)
        location = self._fmt_self(location_fmt)
        description = self._fmt_self(description_fmt)
        start_time = self.start_time
        end_time = self.end_time
        if start_offset is not None:
            start_time += datetime.timedelta(minutes=start_offset)
        if end_offset is not None:
            end_time += datetime.timedelta(minutes=end_offset)

        hash_input = f"{title}|{location}|{description}|{start_time.isoformat()}|{end_time.isoformat()}|{color}"
        md5_input = hashlib.md5(hash_input.encode('utf-8')).digest()
        
        return {
            'id': base64.b32hexencode(md5_input).decode('utf-8').rstrip('=').lower(),
            'summary': title,
            'location': location,
            'description': description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'Europe/Ljubljana',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'Europe/Ljubljana',
            },
            'colorId': color,
        }

def get_slots(ical_path):
    cal = icalendar.Calendar.from_ical(open(ical_path, 'rb').read())
    events = []

    def fallback_event(component):
        slot = WiseSlot()
        slot.course = str(component.get('SUMMARY')).capitalize() + " (Fallback)"
        slot.location = str(component.get('LOCATION'))
        slot.start_time = component.get('DTSTART').dt
        slot.end_time = component.get('DTEND').dt
        slot.ctype = "Unknown"
        slot.ctype_abbr = "UN"
        slot.lecturer = "Unknown"
        slot.groups = []
        return slot

    for component in cal.walk():
        if component.name == "VEVENT":
            slot = WiseSlot()
            slot.course = str(component.get('SUMMARY')).capitalize()
            dparts = str(component.get('DESCRIPTION')).split(", ")
            if len(dparts) < 4:
                print(f"Warning: DESCRIPTION field does not have enough parts: '{component.get('DESCRIPTION')}'")
                events.append(fallback_event(component))
                continue
            if slot.course != dparts[0].capitalize():
                print(f"Warning: SUMMARY and DESCRIPTION course names do not match: '{slot.course}' != '{dparts[0].capitalize()}'")
                events.append(fallback_event(component))
                continue
            abbr_ignore = ['in']
            slot.course_abbr = "".join([word[0] for word in slot.course.split(" ") if word and word.lower() not in abbr_ignore]).upper()
            slot.ctype_abbr = dparts[1]
            ctype_map = {
                'PR': 'Predavanje',
                'SV': 'Seminarske vaje',
                'LV': 'Laboratorijske vaje',
                'SE': 'Seminar',
                'RV': 'Računalniške vaje'
            }
            slot.ctype = ctype_map.get(slot.ctype_abbr, slot.ctype_abbr)
            slot.location = str(component.get('LOCATION'))

            lecturers = []
            groups = []
            groups_started = False
            lecutrers_and_groups = dparts[2:]

            # We need to heuristically separate lecturers and groups from the remaining parts
            # Assumptions:
            # - The first part is always a lecturer
            # - The last part is always a group
            # - After the first part, once we start seeing groups, all subsequent parts are groups
            # - Groups often contain digits or specific keywords
            for i, part in enumerate(lecutrers_and_groups):
                # First part is always lecturer
                if i == 0:
                    lecturers.append(part.title())
                    # print(f"First part, adding lecturer: {part.title()}")
                    continue
                # Last part is always group or groups started
                if i == len(lecutrers_and_groups) - 1 or groups_started:
                    groups.append(part)
                    # print(f"Adding group: {part}")
                    continue

                # Now decide based on content
                units = part.replace('.', '').lower().split(' ')
                # If we find a digit, we assume it's a group
                if any(char.isdigit() for char in part):
                    groups_started = True
                    groups.append(part)
                    # print(f"Found digit, adding group: {part}")
                    continue

                # Check for common lecturer indicators
                lecturer_indicators = ['dr', 'prof', 'doc', 'asist', 'demonstrator']
                if any(unit == indicator for unit in units for indicator in lecturer_indicators):
                    lecturers.append(part.title())
                    # print(f"Found lecturer indicator, adding lecturer: {part.title()}")
                    continue

                # Check for common group indicators
                group_indicators = ['sk', 'erasmus', 'rv', 'vs', 'un', 'mag', 'izb']
                if any(unit == indicator for unit in units for indicator in group_indicators):
                    groups_started = True
                    groups.append(part)
                    # print(f"Found group indicator, adding group: {part}")
                    continue

                # If there's only one word, assume it's a group
                if len(units) == 1:
                    groups_started = True
                    groups.append(part)
                    # print(f"Single unit, assuming group, adding: {part}")
                    continue

                # Default to lecturer if none of the above matched
                lecturers.append(part.title())
                # print(f"Defaulting to lecturer, adding: {part.title()}")

            slot.lecturer = ", ".join(lecturers)
            slot.groups = [group.strip() for group in groups]

            slot.start_time = component.get('DTSTART').dt
            slot.end_time = component.get('DTEND').dt
            events.append(slot)
    return events

def get_session_filters(slots):
    filters = set()
    for slot in slots:
        for group in slot.groups:
            filters.add((slot.course, slot.ctype, group))
    return sorted(list(filters), key=lambda x: (x[0], x[1], x[2]))



import yaml
import json
if __name__ == "__main__":
    slots = get_slots('calendar(2).ics')
    # for slot in slots:
    #     print(f"{slot.course} ({slot.ctype}) by {slot.lecturer} at {slot.location} from {slot.start_time} to {slot.end_time}, Groups: {', '.join(slot.groups)} Hash: {slot.hash()}")
    # filters = get_session_filters(slots)
    # for f in filters:
    #     print(f"Course: {f[0]}, Type: {f[1]}, Group: {f[2]}")
    groups = set([g for slot in slots for g in slot.groups])
    print("All groups:")
    for g in sorted(list(groups)):
        print(f" - {g}")
    # settings = yaml.safe_load(open('settings.yaml', 'r', encoding='utf-8'))
    # slots_fmt = [slot.to_gcal(settings['format']) for slot in slots]
    # slots_fmt = [slot for slot in slots_fmt if slot is not None]
    # for slot in slots_fmt:
    #     print(json.dumps(slot, indent=2, ensure_ascii=False))