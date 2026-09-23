import icalendar
import hashlib
import datetime
import base64
import re
import urllib.request

def generate_course_slug(course_name):
    """
    Generate a URL-friendly slug from course name.
    Example: "Spletne tehnologije" → "spletne-tehnologije"
    """
    # Convert to lowercase
    slug = course_name.lower()
    
    # Replace Slovenian and common diacritics with ASCII equivalents
    char_map = {
        'č': 'c', 'ć': 'c',
        'š': 's',
        'ž': 'z',
        'đ': 'd',
        'ä': 'a', 'á': 'a', 'à': 'a',
        'ö': 'o', 'ó': 'o', 'ò': 'o',
        'ü': 'u', 'ú': 'u', 'ù': 'u',
        'é': 'e', 'è': 'e', 'ë': 'e',
    }
    for old, new in char_map.items():
        slug = slug.replace(old, new)
    
    # Replace spaces and underscores with hyphens
    slug = slug.replace(' ', '-').replace('_', '-')
    
    # Remove any non-alphanumeric characters except hyphens
    slug = ''.join(c for c in slug if c.isalnum() or c == '-')
    
    # Remove consecutive hyphens
    while '--' in slug:
        slug = slug.replace('--', '-')
    
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    
    return slug

def timetable_id(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]

def download_ical(url, download_path):
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    if not data.startswith(b'BEGIN:VCALENDAR'):
        raise ValueError(f"Povezava {url} ne vrne koledarja v ICS formatu.")
    with open(download_path, 'wb') as fh:
        fh.write(data)
    return download_path

class WiseSlot:
    course = "" # Course name - e.g., "Spletne tehnologije"
    course_abbr = ""  # Course abbreviation - e.g., "ST"
    course_slug = ""  # Course slug - e.g., "spletne-tehnologije"
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
        cf = f.get(self.course_slug, {}).get(fsel, {})
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

CTYPE_MAP = {
    'PR': 'Predavanje',
    'SV': 'Seminarske vaje',
    'LV': 'Laboratorijske vaje',
    'SE': 'Seminar',
    'RV': 'Računalniške vaje'
}

def get_slots(ical_path):
    cal = icalendar.Calendar.from_ical(open(ical_path, 'rb').read())
    events = []

    for component in cal.walk('VEVENT'):
        slot = WiseSlot()
        summary = str(component.get('SUMMARY')).strip()
        # SUMMARY format: "COURSE NAME (TYPE)", TYPE may carry a suffix, e.g. "RV-NA DALJAVO"
        match = re.match(r'^(.*?)\s*\(([^()]+)\)$', summary)
        if match:
            slot.course = match.group(1).capitalize()
            ctype_base, _, ctype_suffix = match.group(2).strip().partition('-')
            slot.ctype_abbr = ctype_base.strip()
            slot.ctype = CTYPE_MAP.get(slot.ctype_abbr, slot.ctype_abbr)
            if ctype_suffix.strip():
                slot.ctype += f" ({ctype_suffix.strip().lower()})"
        else:
            slot.course = summary.capitalize()
            slot.ctype_abbr = 'UN'
            slot.ctype = 'Neznano'

        abbr_ignore = ['in']
        slot.course_abbr = "".join([word[0] for word in slot.course.split(" ") if word and word.lower() not in abbr_ignore]).upper()
        slot.course_slug = generate_course_slug(slot.course)
        slot.location = str(component.get('LOCATION', ''))

        # DESCRIPTION format: "Predavatelji: A, B\nSkupine: X, Y"
        fields = {}
        for line in str(component.get('DESCRIPTION', '')).splitlines():
            key, _, value = line.partition(':')
            fields[key.strip()] = value.strip()
        slot.lecturer = ", ".join(l.strip().title() for l in fields.get('Predavatelji', '').split(',') if l.strip())
        slot.groups = [g.strip() for g in fields.get('Skupine', '').split(',') if g.strip()]

        slot.start_time = component.get('DTSTART').dt
        slot.end_time = component.get('DTEND').dt

        duration = slot.end_time - slot.start_time
        if duration.total_seconds() <= 0 or duration.total_seconds() > 8 * 3600:
            print(f"Warning: Invalid duration for event '{slot.course}' from {slot.start_time} to {slot.end_time} - skipping.")
            continue

        events.append(slot)
    return events
