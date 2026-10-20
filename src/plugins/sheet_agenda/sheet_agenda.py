import csv
import io
import logging
import os
import re
from datetime import date, datetime, time, timedelta

import pytz
import requests

from plugins.base_plugin.base_plugin import PLUGINS_DIR
from plugins.calendar.calendar import Calendar
from plugins.calendar.constants import FONT_SIZES

logger = logging.getLogger(__name__)

SHEETS_BASE_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}"
CALENDAR_RENDER_DIR = os.path.join(PLUGINS_DIR, "calendar", "render")

# Couleurs contrastées, lisibles sur écran e-paper, attribuées aux catégories
CATEGORY_PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#000000", "#8c564b", "#e6b800"]

MONTHS = {
    "janv": 1, "jan": 1, "févr": 2, "fevr": 2, "fév": 2, "fev": 2, "feb": 2, "mars": 3, "mar": 3,
    "avr": 4, "apr": 4, "mai": 5, "may": 5, "juin": 6, "jun": 6, "juil": 7, "jul": 7,
    "août": 8, "aout": 8, "aug": 8, "sept": 9, "sep": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}

DATE_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
DATE_NUMERIC_RE = re.compile(r"(?<![\d:h])(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}|\d{2}))?(?![\d:h])")
DATE_TEXT_RE = re.compile(r"(\d{1,2})(?:er)?\s+([a-zéû]+)\.?(?:\s+(\d{4}))?", re.IGNORECASE)
TIME_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:h|:)\s*(\d{2})?", re.IGNORECASE)

WEEKDAYS = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
SLOT_RE = re.compile(r"^\s*(a\.?\s?m\.?|p\.?\s?m\.?|matin|apr[eè]s[- ]?midi|aprem)\s*$", re.IGNORECASE)

# (mot-clé du statut, fond, texte) : reprend les couleurs des listes déroulantes de la feuille
STATUS_STYLES = [
    ("télétrav", "#d4ecc4", "#245a17"), ("teletrav", "#d4ecc4", "#245a17"),
    ("bureau", "#c6e2f7", "#0b4f8a"),
    ("congé", "#1155cc", "#ffffff"), ("conge", "#1155cc", "#ffffff"),
    ("absent", "#f9cbad", "#7a3410"),
    ("esat", "#e6d4f5", "#5b2382"),
    ("récup", "#7b3f00", "#ffffff"), ("recup", "#7b3f00", "#ffffff"),
    ("remplir", "#c9d9e0", "#2f4f5f"),
]
FALLBACK_STATUS_STYLES = [("#fde8a8", "#5c4500"), ("#f4c7c3", "#7a1f16"), ("#d9d9d9", "#000000")]

# nombre maximum d'onglets téléchargés pour trouver celui de la semaine
MAX_TABS_SCANNED = 20


class SheetAgenda(Calendar):
    """Affiche un emploi du temps géré dans une Google Sheet.

    Trois modes :
      - "presence" : planning de présence (une personne par ligne, une colonne par jour
                     ou demi-journée, un statut par cellule) redessiné pour l'écran.
      - "agenda"   : les lignes de la feuille (date, début, fin, titre) sont converties
                     en événements et affichées en vue semaine, comme le plugin Calendar.
      - "capture"  : la feuille est rendue telle quelle (couleurs, fusions, mise en forme),
                     éventuellement limitée à la semaine en cours.
    """

    def __init__(self, config, **dependencies):
        super().__init__(config, **dependencies)
        # réutilise le template et le css du plugin Calendar pour le mode agenda
        self.env.loader.searchpath.append(CALENDAR_RENDER_DIR)

    def generate_image(self, settings, device_config):
        sheet_url = (settings.get("sheetUrl") or "").strip()
        if not sheet_url:
            raise RuntimeError("L'URL de la Google Sheet est requise.")
        sheet_id, gid = self.parse_sheet_url(sheet_url)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        timezone = device_config.get_config("timezone", default="Europe/Paris")
        tz = pytz.timezone(timezone)
        current_dt = datetime.now(tz)
        week_start, week_end = self.get_week_range(current_dt.date(), settings)

        sheet_name = (settings.get("sheetName") or "").strip()
        if sheet_name:
            gid = self.resolve_gid(sheet_id, self.format_sheet_name(sheet_name, current_dt))
        elif settings.get("autoTab") == "true":
            gid = self.find_week_tab(sheet_id, week_start, week_end) or gid

        display_mode = settings.get("displayMode", "presence")
        if display_mode == "presence":
            image = self.render_presence(sheet_id, gid, settings, dimensions, current_dt, week_start, week_end)
        elif display_mode == "capture":
            image = self.render_capture(sheet_id, gid, settings, dimensions, current_dt, week_start, week_end)
        else:
            image = self.render_agenda(sheet_id, gid, settings, dimensions, device_config, timezone, current_dt, week_start, week_end)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

    # ---------------------------------------------------------------- presence

    def render_presence(self, sheet_id, gid, settings, dimensions, current_dt, week_start, week_end):
        planning = self.parse_presence(self.fetch_csv(sheet_id, gid), week_start, week_end)
        today = current_dt.date()

        days = planning["days"]
        for day in days:
            day["date"] = week_start + timedelta(days=(day["weekday"] - week_start.weekday()) % 7)
            day["today"] = day["date"] == today
        if settings.get("presenceView") == "today" and any(day["today"] for day in days):
            days = [day for day in days if day["today"]]

        columns = [slot["column"] for day in days for slot in day["slots"]]
        people = [
            {"name": person["name"], "statuses": [self.status_style(person["cells"].get(col, ""), settings) for col in columns]}
            for person in planning["people"]
        ]

        template_params = {
            "title": planning["title"] or self.week_title(week_start, week_end),
            "days": days,
            "has_slots": any(slot["label"] for day in days for slot in day["slots"]),
            "people": people,
            "plugin_settings": settings,
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal")),
        }
        return self.render_image(dimensions, "presence.html", "presence.css", template_params)

    def parse_presence(self, rows, week_start, week_end):
        """Lit un planning de présence : une ligne d'en-tête avec les jours de la semaine
        (cellules fusionnées = colonnes vides à droite), une ligne optionnelle de
        demi-journées (A.M / P.M), puis une ligne par personne."""
        header_indexes = [i for i, row in enumerate(rows) if sum(self.weekday_of(cell) is not None for cell in row) >= 3]
        if not header_indexes:
            raise RuntimeError("Planning introuvable : aucune ligne avec les jours de la semaine (Lundi, Mardi…).")

        # plusieurs semaines dans le même onglet : on prend le bloc de la semaine en cours
        header_index = header_indexes[0]
        for index in header_indexes:
            dates = [self.parse_date(cell, week_start.year) for cell in rows[index][:1] + (rows[index - 1][:1] if index else [])]
            if any(d and week_start <= d < week_end for d in dates):
                header_index = index
                break
        header = rows[header_index]

        days = []
        for col in range(1, len(header)):
            weekday = self.weekday_of(header[col])
            if weekday is not None:
                days.append({"name": header[col].strip(), "weekday": weekday, "slots": []})
            elif header[col].strip():
                break
            if days:
                days[-1]["slots"].append({"column": col, "label": ""})

        first_person = header_index + 1
        if first_person < len(rows) and any(SLOT_RE.match(cell) for cell in rows[first_person]):
            for day in days:
                for slot in day["slots"]:
                    slot["label"] = self.cell(rows[first_person], slot["column"])
            first_person += 1
        # une colonne fusionnée sans libellé de demi-journée n'est qu'un débordement
        for day in days:
            labelled = [slot for slot in day["slots"] if slot["label"]]
            day["slots"] = labelled or day["slots"][:1]

        people = []
        next_headers = [i for i in header_indexes if i > header_index]
        last_row = next_headers[0] if next_headers else len(rows)
        for row in rows[first_person:last_row]:
            name = self.cell(row, 0)
            if not name:
                if people and not any(cell.strip() for cell in row):
                    break
                continue
            people.append({"name": name, "cells": {col: cell.strip() for col, cell in enumerate(row)}})

        title = self.cell(header, 0) or (self.cell(rows[header_index - 1], 0) if header_index else "")
        return {"title": title, "days": days, "people": people}

    def status_style(self, status, settings):
        key = status.lower()
        custom = {}
        for line in re.split(r"[;\n]", settings.get("statusColors") or ""):
            if "=" in line:
                label, color = line.split("=", 1)
                custom[label.strip().lower()] = color.strip()
        for label, color in custom.items():
            if label and label in key:
                return {"text": status, "background": color, "color": self.get_contrast_color(color)}
        for keyword, background, color in STATUS_STYLES:
            if keyword in key:
                return {"text": status, "background": background, "color": color}
        if not status:
            return {"text": "", "background": "transparent", "color": "inherit"}
        background, color = FALLBACK_STATUS_STYLES[sum(map(ord, key)) % len(FALLBACK_STATUS_STYLES)]
        return {"text": status, "background": background, "color": color}

    @staticmethod
    def weekday_of(text):
        return WEEKDAYS.get((text or "").strip().lower())

    def find_week_tab(self, sheet_id, week_start, week_end):
        """Cherche l'onglet de la semaine en cours : d'abord dans le nom des onglets,
        puis dans les premières lignes de chaque onglet (ex : « Semaine du 28/09 au 02/10 »)."""
        tabs = self.list_tabs(sheet_id)
        for name, gid in tabs:
            day = self.parse_date(name, week_start.year)
            if day and week_start <= day < week_end:
                return gid

        # les onglets récents sont en général au début ou à la fin du classeur
        half = MAX_TABS_SCANNED // 2
        candidates = tabs if len(tabs) <= MAX_TABS_SCANNED else tabs[:half] + tabs[-half:]
        for name, gid in candidates:
            for row in self.fetch_csv(sheet_id, gid)[:3]:
                days = (self.parse_date(cell, week_start.year) for cell in row)
                if any(d and week_start <= d < week_end for d in days):
                    logger.info(f"Onglet de la semaine trouvé : {name}")
                    return gid
        logger.warning("Aucun onglet ne correspond à la semaine en cours, utilisation de l'onglet du lien")
        return None

    # ------------------------------------------------------------------ agenda

    def render_agenda(self, sheet_id, gid, settings, dimensions, device_config, timezone, current_dt, week_start, week_end):
        rows = self.fetch_csv(sheet_id, gid)
        events = self.parse_events(rows, settings, week_start, week_end)
        if not events:
            logger.warning("Aucun événement trouvé dans la Google Sheet pour la semaine en cours")

        template_params = {
            "view": "timeGridWeek",
            "events": events,
            "current_dt": current_dt.replace(minute=0, second=0, microsecond=0).isoformat(),
            "timezone": timezone,
            "plugin_settings": settings,
            "time_format": device_config.get_config("time_format", default="24h"),
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal")),
        }
        css_file = os.path.join(CALENDAR_RENDER_DIR, "calendar.css")
        return self.render_image(dimensions, "calendar.html", css_file, template_params)

    def parse_events(self, rows, settings, week_start, week_end):
        date_col = self.column_index(settings.get("dateColumn") or "A")
        start_col = self.column_index(settings.get("startColumn") or "B")
        end_col = self.column_index(settings.get("endColumn"))
        title_col = self.column_index(settings.get("titleColumn") or "C")
        color_col = self.column_index(settings.get("colorColumn"))
        default_color = settings.get("eventColor") or "#000000"

        events = []
        categories = {}
        current_day = None
        for row in rows:
            # une date vide reprend la date de la ligne précédente (cellules fusionnées)
            day = self.parse_date(self.cell(row, date_col), week_start.year)
            if day:
                current_day = day
            title = self.cell(row, title_col)
            if not current_day or not title or not (week_start <= current_day < week_end):
                continue

            times = self.parse_times(self.cell(row, start_col))
            if end_col is not None:
                times += self.parse_times(self.cell(row, end_col))

            color = default_color
            category = self.cell(row, color_col)
            if category.startswith("#"):
                color = category
            elif category:
                color = categories.setdefault(category, CATEGORY_PALETTE[len(categories) % len(CATEGORY_PALETTE)])

            event = {
                "title": title,
                "backgroundColor": color,
                "textColor": self.get_contrast_color(color),
            }
            if times:
                start = datetime.combine(current_day, times[0])
                end = datetime.combine(current_day, times[1]) if len(times) > 1 else start + timedelta(hours=1)
                event.update({"start": start.isoformat(), "end": end.isoformat(), "allDay": False})
            else:
                event.update({"start": current_day.isoformat(), "allDay": True})
            events.append(event)
        return events

    # ----------------------------------------------------------------- capture

    def render_capture(self, sheet_id, gid, settings, dimensions, current_dt, week_start, week_end):
        params = {"headers": "false", "gid": gid}
        capture_range = (settings.get("captureRange") or "").strip()
        if capture_range:
            params["range"] = capture_range
        html = self.fetch(f"{SHEETS_BASE_URL.format(sheet_id=sheet_id)}/htmlview/sheet", params).text

        table_match = re.search(r'<table class="waffle.*?</table>', html, re.S)
        if not table_match:
            raise RuntimeError("Impossible de lire la feuille. Est-elle partagée en lecture avec le lien ?")
        table = table_match.group(0)
        styles = "\n".join(re.findall(r"\.ritz \.waffle[^{]*\{[^}]*\}", html))

        keep_rows = None
        if settings.get("autoWeek") == "true":
            keep_rows = self.find_week_rows(self.fetch_csv(sheet_id, gid), settings, week_start, week_end)
            if keep_rows is None:
                logger.warning("Semaine en cours introuvable dans la feuille, affichage complet")
        table = self.filter_table_rows(table, keep_rows)

        template_params = {
            "table": table,
            "sheet_styles": styles,
            "title": self.week_title(week_start, week_end),
            "plugin_settings": settings,
        }
        return self.render_image(dimensions, "sheet_capture.html", "sheet_capture.css", template_params)

    def find_week_rows(self, rows, settings, week_start, week_end):
        """Retourne les index de lignes (0 = ligne 1) à afficher : les lignes d'en-tête
        puis le bloc qui commence à la première date de la semaine en cours et se termine
        avant la première date postérieure à la semaine."""
        header_rows = int(settings.get("headerRows") or 1)
        start = end = None
        for index, row in enumerate(rows):
            days = [d for d in (self.parse_date(cell, week_start.year) for cell in row) if d]
            if start is None:
                if any(week_start <= d < week_end for d in days):
                    start = index
            elif days and all(d >= week_end for d in days):
                end = index
                break
        if start is None:
            return None
        if end is None:
            end = len(rows)
        return set(range(min(header_rows, start))) | set(range(start, end))

    @staticmethod
    def filter_table_rows(table, keep_rows):
        def keep(match):
            row = match.group(0)
            if "freezebar-horizontal-handle" in row:
                return ""
            row_id = re.search(r'<th[^>]*id="\d+R(\d+)"', row)
            if keep_rows is None or row_id is None:
                return row
            return row if int(row_id.group(1)) in keep_rows else ""

        return re.sub(r"<tr[^>]*>.*?</tr>", keep, table, flags=re.S)

    # ---------------------------------------------------------- google sheets

    @staticmethod
    def parse_sheet_url(url):
        id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        if not id_match:
            raise RuntimeError("URL de Google Sheet invalide.")
        gid_match = re.search(r"[#?&]gid=(\d+)", url)
        return id_match.group(1), gid_match.group(1) if gid_match else "0"

    def list_tabs(self, sheet_id):
        """Retourne la liste (nom, gid) des onglets, dans l'ordre du classeur."""
        html = self.fetch(f"{SHEETS_BASE_URL.format(sheet_id=sheet_id)}/htmlview").text
        tabs = []
        for name, gid in re.findall(r'items\.push\(\{name: "(.*?)", pageUrl: ".*?", gid: "(\d+)"', html):
            name = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name).replace("\\/", "/")
            tabs.append((name.strip(), gid))
        return tabs

    def resolve_gid(self, sheet_id, sheet_name):
        for name, gid in self.list_tabs(sheet_id):
            if name.lower() == sheet_name.lower():
                return gid
        raise RuntimeError(f"Onglet '{sheet_name}' introuvable dans la Google Sheet.")

    def fetch_csv(self, sheet_id, gid):
        response = self.fetch(f"{SHEETS_BASE_URL.format(sheet_id=sheet_id)}/export", {"format": "csv", "gid": gid})
        response.encoding = "utf-8"
        return list(csv.reader(io.StringIO(response.text)))

    @staticmethod
    def fetch(url, params=None):
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code in (401, 403):
                raise RuntimeError("accès refusé")
            response.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Impossible de récupérer la Google Sheet (partagée avec le lien ?) : {e}")
        if "accounts.google.com" in response.url:
            raise RuntimeError("La Google Sheet n'est pas accessible : partagez-la en lecture à toute personne disposant du lien.")
        return response

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def format_sheet_name(sheet_name, current_dt):
        iso_year, iso_week, _ = current_dt.isocalendar()
        try:
            return sheet_name.format(semaine=iso_week, annee=iso_year)
        except (KeyError, IndexError, ValueError):
            return sheet_name

    @staticmethod
    def get_week_range(today, settings):
        week_start_day = int(settings.get("weekStartDay", 1))
        python_week_start = (week_start_day - 1) % 7
        start = today - timedelta(days=(today.weekday() - python_week_start) % 7)
        return start, start + timedelta(days=7)

    @staticmethod
    def week_title(week_start, week_end):
        last_day = week_end - timedelta(days=1)
        return f"Semaine {week_start.isocalendar()[1]} · {week_start.strftime('%d/%m')} – {last_day.strftime('%d/%m/%Y')}"

    @staticmethod
    def column_index(letters):
        letters = (letters or "").strip().upper()
        if not letters:
            return None
        if letters.isdigit():
            return int(letters) - 1
        index = 0
        for char in letters:
            index = index * 26 + (ord(char) - ord("A") + 1)
        return index - 1

    @staticmethod
    def cell(row, index):
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    @staticmethod
    def parse_date(text, default_year):
        if not text:
            return None
        try:
            match = DATE_ISO_RE.search(text)
            if match:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            match = DATE_NUMERIC_RE.search(text)
            if match:
                year = match.group(3)
                year = int(year) + 2000 if year and len(year) == 2 else int(year or default_year)
                return date(year, int(match.group(2)), int(match.group(1)))
            for match in DATE_TEXT_RE.finditer(text):
                month = match.group(2).lower()
                for prefix, number in MONTHS.items():
                    if month.startswith(prefix):
                        return date(int(match.group(3) or default_year), number, int(match.group(1)))
        except ValueError:
            pass
        return None

    @staticmethod
    def parse_times(text):
        times = []
        for match in TIME_RE.finditer(text or ""):
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
            if hour < 24 and minute < 60:
                times.append(time(hour, minute))
        return times
