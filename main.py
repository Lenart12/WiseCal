import json
from playwright.sync_api import sync_playwright
import dotenv
import argparse
import pathlib
import subprocess
import openai
import os
import discord_webhook
import time

def download_ical(api_url, timetable, download_path):
    with sync_playwright() as p:
        # print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        url = f"{api_url}/wtt_{timetable['schoolcode']}/index.jsp?filterId={timetable['filterId']}"
        page.goto(url)
        # print(f"Navigated to {url}")
        page.click('a[title="Izvoz celotnega urnika v ICS formatu  "]')
        # print("Clicked on iCal export link")
        with page.expect_download() as download_info:
            pass  # The click already initiated the download
            # print("Waiting for download to start...")
        download = download_info.value
        download.save_as(download_path)
        # print(f"Downloaded iCal file to {download_path}")
        browser.close()
    return download_path

def get_changes(diff_output):
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Ti si pomočnik, ki analizira spremembe v urnikih. "
                    "Tvoja naloga je, da iz diff primerjave dveh ICS datotek (stare in nove različice urnika) "
                    "ustvariš seznam sprememb v JSON formatu. "
                    "Ne dodajaj nobenih dodatnih komentarjev ali besedila."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Analiziraj naslednji diff ICS datotek in vrni seznam sprememb kot JSON array.\n"
                    "Vsaka sprememba mora imeti ključ:\n"
                    "- 'predmet' : ime predmeta (npr. IZBRANI ALGORITMI)\n"
                    "- 'tip_dogodka' : tip dogodka (npr. PR (Predavanja))\n"
                    "- 'sprememba' : opis spremembe (npr. Termin se je premaknil iz srede, 8. 10. 2025, 13:00–15:00 na torek, 7. 10. 2025, 13:00–15:00.)\n\n"
                    "Napotki:\n"
                    "- Opis spremembe naj bo kratek in v berljivem formatu primeren za obvestilo študentom. Opisa nikoli ne podaj z dvopičjem (npr. NE 'Dodano: Nov termin' ampak 'Dodan je nov termin').\n"
                    "- Tip dogodka izlušči iz opisa predmeta (DESCRIPTION), običajno je to kratica kot RV, PR, LV. in ga podaj kot KRATICA (Opis) npr. PR (Predavanja)\n"
                    "- Seznam vseh kratic: PR, PR 1, itd. = predavanja; SV, SV 1, itd. = seminarske vaje; LV, LV 1, itd. = laboratorijske vaje; SE, SE 1, itd. = seminar; RV, RV 1, itd. = računalniške vaje"
                    "- Če je isti dogodek nekje odstranjen in nato dodan na drugem mestu, zabeleži to kot ena sprememba istega dogodka (v primeru, da se sploh razlikujeta lokacija ali termin).\n"
                    "- Če je termin dogodka večkrat spremenjen na isto novo vrednost, zabeleži samo eno spremembo (npr. od zdaj naprej se bo dogodek redno pojavljal ob novem terminu).\n"
                    "Diff vsebina:\n" + diff_output
                ),
            },
        ],
    )

    json_text = response.choices[0].message.content
    json_content = json.loads(json_text)
    if not isinstance(json_content, list):
        raise ValueError("Expected a JSON array from the model response.")
    events = []
    for change in json_content:
        if not isinstance(change, dict):
            print("Skipping invalid change entry (not a dict):", change)
            raise ValueError("Each change entry must be a JSON object.")
        if not all(key in change for key in ("predmet", "tip_dogodka", "sprememba")):
            print("Skipping invalid change entry (missing keys):", change)
            raise ValueError("Each change entry must contain all required keys.")
        events.append({
            "predmet": change["predmet"],
            "tip_dogodka": change["tip_dogodka"],
            "sprememba": change["sprememba"],
        })
    return events

def changes_webhook(webhook_url, changes, tt_name, tt_api_url, timetable):
    webhook = discord_webhook.DiscordWebhook(url=webhook_url)
    if len(changes) == 0:
        return
    
    tt_url = f"{tt_api_url}/wtt_{timetable['schoolcode']}/index.jsp?filterId={timetable['filterId']}"

    if len(changes) > 25:
        embed = discord_webhook.DiscordEmbed(
            title="Preveč sprememb za prikaz",
            description=f"Zaznanih je bilo {len(changes)} sprememb. Prosimo, preverite urnik neposredno.",
            color='f70237',
        )
        embed.set_footer(text="Sprememba urnika")
        embed.set_url(tt_url)
        webhook.add_embed(embed)
        webhook.execute()
        return

    embed = discord_webhook.DiscordEmbed(
        title=f"Spremembe v urniku za {tt_name}",
        description=f"Zaznanih je bilo toliko sprememb v urniku: {len(changes)}.",
        color='6384a3',
    )
    embed.set_url(tt_url)
    embed.set_timestamp()

    for change in changes:
        embed.add_embed_field(
            name=f"{change['predmet']} - {change['tip_dogodka']}",
            value=change['sprememba'],
            inline=False
        )

    webhook.add_embed(embed)
    webhook.execute()

def main():
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(description="Wise TT calendar diff checker")
    parser.add_argument("-d", "--storage-dir", type=pathlib.Path, default=pathlib.Path("."), help="Directory to store data files")
    parser.add_argument("-a", "--api-url", type=str, default="https://www.wise-tt.com", help="Wise TT API URL")
    def parse_timetable(value):
        parts = value.split("/")
        if len(parts) != 2:
            raise argparse.ArgumentTypeError(
                'Timetable must be in the format "schoolcode/filterId"'
            )
        return {
            "schoolcode": parts[0],
            "filterId": parts[1],
        }
    parser.add_argument(
        "-t", "--timetable",
        type=parse_timetable,
        required=True,
        help='Timetable in the format "schoolcode/filterId"'
    )
    parser.add_argument(
        "-n", "--timetable-name", type=str, required=True, help="Human-readable name of the timetable (e.g., 'FERI RIT MAG 1. letnik')"
    )

    if "OPENAI_API_KEY" not in os.environ:
        parser.error("OPENAI_API_KEY environment variable is not set.")
    elif not os.getenv("DISCORD_WEBHOOK_URL"):
        parser.error("DISCORD_WEBHOOK_URL environment variable is not set.")

    args = parser.parse_args()

    tt_filename = args.timetable["schoolcode"] + "_" + args.timetable["filterId"]
    new_tt = download_ical(args.api_url, args.timetable, args.storage_dir / f"{tt_filename}.new.ics")
    old_tt = args.storage_dir / f"{tt_filename}.ics"

    if old_tt.exists():
        # print(f"Comparing {new_tt} with {old_tt}")
        diff = subprocess.run(
            ["diff", "-u7", str(old_tt), str(new_tt)],
            capture_output=True, text=True
        )
        if diff.returncode == 0:
            print("No changes detected.")
            new_tt.unlink()  # Remove the new file if no changes
        elif diff.returncode == 1:
            diff_output = diff.stdout

            diff_dir = args.storage_dir / 'diffs'
            diff_dir.mkdir(exist_ok=True)
            timestamp = int(time.time())
            diff_file = diff_dir / f"{tt_filename}_{timestamp}.diff"
            diff_file.write_text(diff_output)

            # print("Changes detected:")
            # print(diff.stdout)
            changes = get_changes(diff_output)
            # print("Detected changes:")
            # print(changes)
            changes_webhook(os.getenv("DISCORD_WEBHOOK_URL"), changes, args.timetable_name, args.api_url, args.timetable)
            new_tt.rename(old_tt)  # Update the old file with the new one
        else:
            print("Error during diff operation:")
            print(diff.stderr)
    else:
        # print(f"No existing timetable found. Saving new timetable as {old_tt}")
        new_tt.rename(old_tt)

if __name__ == "__main__":
    main()
