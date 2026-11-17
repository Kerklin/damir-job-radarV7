# Damir Job Radar v6

Automatic vacancy search for construction, infrastructure, WASH, procurement, project management and heritage roles, in Sarajevo, Bosnia and Herzegovina, the Western Balkans, Europe and UN duty stations worldwide. Every vacancy gets an explainable 0–100 fit score against Damir Hadžić's profile.

Three files. No API keys, no secrets, no database, no paid services.

```text
app.py
requirements.txt
README.md
```

## Sources (18 by default, 17 switched on)

| Group | Source | Address |
|---|---|---|
| International | UNDP, IOM | Oracle career sites |
| International | WFP, UNHCR | Workday career sites (UNHCR backup: UNjobs) |
| International | UNICEF | jobs.unicef.org (backup: UNjobs) |
| International | OSCE | vacancies.osce.org/latest-jobs |
| Sarajevo / BiH | UN in Bosnia and Herzegovina | bosniaherzegovina.un.org/en/jobs |
| Sarajevo / BiH | UNjobs: Bosnia and Herzegovina | unjobs.org/duty_stations/bosnia-and-herzegovina |
| Region | UNjobs: Serbia, UNjobs: Croatia | unjobs.org duty-station pages |
| BiH local | Klix Posao: Građevina | posao.klix.ba/kat/gradjevina |
| BiH local | MojPosao.ba: Građevinarstvo | mojposao.ba category search |
| Serbia local | Infostud: Građevinski inženjer; Građevina, geodezija | poslovi.infostud.com |
| Croatia local | MojPosao.hr | mojposao.hr |
| Europe | Arbeitnow, EuroBrussels | arbeitnow.com API, eurobrussels.com |
| Optional | UNjobs: WASH engineering | switched off by default |

Add more in the **Sources** tab: paste any job-board category or search-result page, careers page or RSS/Atom feed. Set **Country** for local boards that do not state the country of each job.

## What is new in v6

**More local and regional coverage**
- UN, OSCE and EU posts by duty station for Sarajevo/BiH, Serbia and Croatia.
- Local job boards in BiH, Serbia and Croatia, plus EuroBrussels for EU institutions and organizations.
- A universal reader for job-board listing pages and RSS/Atom feeds: it reads vacancy links, the company, place and closing date around each link, and structured job data when a page publishes it.
- Bosnian/Croatian/Serbian and German job titles and requirements are understood, with or without diacritics (građevinski = gradjevinski = gradevinski). Craft and support roles (radnik, majstor, monter, vozač, prodavač, call center…) are excluded.
- Local closing dates are read: "Prijava do 13. 12. 2026.", "Rok za prijavu", "Trajanje konkursa", "Ističe", "13.11. – 13.12.2026", "Bewerbungsfrist".
- Experience and language rules in local languages: "minimalno 5 godina radnog iskustva", "obavezno znanje njemačkog jezika" (required) vs "poželjno" (preferred, ignored). Vacancies written in German are flagged as needing German.
- New **Region** filter and **📍 BiH & region** tab. Local jobs outside BiH get a "check work permit" note.

**The same vacancy on several sites appears once**
- Example: UNHCR "WASH Engineer" from Workday and from UNjobs is merged. The official link is kept; missing closing date or text is filled from the other site; the Source column lists both sites.
- Merging is conservative: same organization, same title, same place and no conflicting closing date. Two postings on the same site are never merged.

**Fixes found in the V5 review**
| Issue | Effect in V5 | V6 |
|---|---|---|
| Backup used when the main site worked but had no relevant job | UNHCR could show "via fallback" with unrelated UNjobs listings | Backup only when the main site fails or no vacancy at all can be read from it |
| Any URL ending in `/search` or `/listing` treated as UNICEF-type site | Local boards would have been misread | That reader is limited to UNICEF and PageUp sites |
| Closing dates with a time zone were converted to UTC | A 23:59 (UTC-5) deadline showed one day later | Date kept as written |
| `01/12/2026` read month-first | 12 January instead of 1 December | Day-first unless impossible |
| "discloses 12 Nov 2026" read as a closing date | Rare false deadlines | Whole-word matching |
| UNjobs location for titles with grades and durations | Location included "NO-1, Temporary Position, 364 days" | Only place names kept |
| UNjobs page with no vacancies reported as a failure | False "Failed" status | Reported as OK, no matching vacancies |
| Connection sessions never closed; result store never pruned | Slow memory growth on long-running apps | Sessions closed after each source; results older than 48 hours removed |

## Update your repository

1. GitHub repository → **Add file → Upload files** → drag in the three files → **Commit changes**.
2. Streamlit redeploys automatically (or app menu → **Reboot app**).
3. Open **Sources** → **Run test** for each new source once.

New deployment: https://share.streamlit.io → **Create app** → repository, branch `main`, main file `app.py`, Python 3.12. No secrets.

Local run (Windows PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## Reading the Sources tab

| Status | Meaning |
|---|---|
| OK / OK (cached) | Working |
| OK (partial) | The site asked the app to slow down; some full texts skipped |
| OK (via fallback) | Main site refused or unreadable; backup page used |
| Failed, showing last results | Temporary problem; automatic retry shown |
| Failed: no vacancy links found (… may load its jobs with scripts) | The board builds its list with JavaScript, which this app cannot run. Use a different page of that board (category or search page), or Analyze vacancy |
| Failed: blocked by bot protection / access refused | The site refuses automated requests from the hosting server. Try running the app on your computer |

## Scoring (100 points)

Title 20 · competencies 25 · organization/sector 15 · experience 15 · education 10 · location/eligibility 5 · language 5 · contract 5. 85+ Excellent · 75+ Strong · 65+ Good · 50+ Possible.

- **Disqualified**: unrelated role family or explicit nationality restriction.
- **Review required**: strong match with a possible blocker: national post outside BiH, EU citizens only, internal/tier-restricted, licence, or a language you do not list.
- **Verify**: unknowns, never counted as matched.

## Limits

- Career sites and job boards can change their pages or block automated access at any time. A failing source never stops the others, and backups and last good results reduce the impact.
- Local boards are read from one category/search page each (the newest listings). Add further category pages as extra sources if needed.
- Not searched automatically: LinkedIn, Indeed, UN Careers (Inspira), UNOPS, EURES, EPSO. Paste those vacancies into **Analyze vacancy**.
- Backup and aggregator listings (UNjobs) are third-party pages. Always apply through the official vacancy.
- Streamlit Community Cloud keeps nothing between restarts: download your tracker and settings regularly.
