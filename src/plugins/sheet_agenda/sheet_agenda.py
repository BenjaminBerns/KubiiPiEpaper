import csv
import io
from datetime import datetime, timedelta

import pytz
import requests

from plugins.base_plugin.base_plugin import BasePlugin

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
DEMI_JOURNEES = ["am", "pm", "matin", "après-midi", "aprem"]

# mot cherché dans la case (sans accent) : (couleur du fond, couleur du texte)
COULEURS = {
    "teletrav": ("#d4ecc4", "#245a17"),
    "bureau": ("#c6e2f7", "#0b4f8a"),
    "conge": ("#1155cc", "#ffffff"),
    "absent": ("#f9cbad", "#7a3410"),
    "esat": ("#e6d4f5", "#5b2382"),
    "recup": ("#7b3f00", "#ffffff"),
    "remplir": ("#c9d9e0", "#2f4f5f"),
}


def case(ligne, colonne):
    """Texte d'une case, ou "" si la ligne est plus courte."""
    return ligne[colonne].strip() if colonne < len(ligne) else ""


def est_entete(ligne):
    """Une ligne d'en-tête contient au moins 3 jours de la semaine."""
    return sum(texte.strip().lower() in JOURS for texte in ligne) >= 3


def statut(texte):
    """Texte de la case avec ses couleurs."""
    mot = texte.lower().replace("é", "e")
    for cle, (fond, couleur) in COULEURS.items():
        if cle in mot:
            return {"texte": texte, "fond": fond, "couleur": couleur}
    return {"texte": texte, "fond": "#d9d9d9", "couleur": "#000000"}


class SheetAgenda(BasePlugin):

    def generate_image(self, settings, device_config):

        lien = settings.get("sheetUrl", "")
        if "/d/" not in lien:
            raise RuntimeError("Lien invalide.")
        # lien du type https://docs.google.com/spreadsheets/d/<id_feuille>/edit?gid=<onglet>#gid=<onglet>
        # l'identifiant est entre "/d/" et le "/" suivant
        id_feuille = lien.split("/d/")[1].split("/")[0]
        # le numéro d'onglet est après "gid=", jusqu'au prochain "&" ou "#" (premier onglet = "0")
        onglet = lien.split("gid=")[1].split("&")[0].split("#")[0] if "gid=" in lien else "0"
        lignes = self.lire_feuille(id_feuille, onglet, device_config)

        fuseau = pytz.timezone(device_config.get_config("timezone", default="Europe/Paris"))
        aujourdhui = datetime.now(fuseau).date()
        lundi = aujourdhui - timedelta(days=aujourdhui.weekday())
        dates_semaine = [(lundi + timedelta(days=i)).strftime("%d/%m") for i in range(7)]

        entetes = [i for i, ligne in enumerate(lignes) if est_entete(ligne)]
        if not entetes:
            raise RuntimeError("Aucune ligne avec Lundi, Mardi, Mercredi… dans la feuille.")
        debut = entetes[0]
        for i in entetes:
            titre = case(lignes[i], 0) or (case(lignes[i - 1], 0) if i > 0 else "")
            if any(date in titre for date in dates_semaine):
                debut = i
                break
        entete = lignes[debut]
        titre = case(entete, 0) or (case(lignes[debut - 1], 0) if debut > 0 else "") or f"Semaine du {dates_semaine[0]}"

        jours = []
        for colonne in range(1, len(entete)):
            nom = entete[colonne].strip()
            if nom.lower() in JOURS:
                date = lundi + timedelta(days=JOURS.index(nom.lower()))
                jours.append({"nom": nom, "date": date.strftime("%d/%m"), "aujourdhui": date == aujourdhui, "colonnes": [colonne]})
            elif nom:
                break
            elif jours:
                jours[-1]["colonnes"].append(colonne)

        personnes = []
        for ligne in lignes[debut + (2 if demi else 1):]:
            if est_entete(ligne) or (personnes and not any(texte.strip() for texte in ligne)):
                break
            if case(ligne, 0):
                statuts = {colonne: statut(case(ligne, colonne)) for jour in jours for colonne in jour["colonnes"]}
                personnes.append({"nom": case(ligne, 0), "statuts": statuts})

        taille = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            taille = taille[::-1]
        donnees = {"titre": titre, "jours": jours, "demi": demi, "personnes": personnes}
        image = self.render_image(taille, "presence.html", "presence.css", donnees)
        if not image:
            raise RuntimeError("La capture de l'écran a échoué, voir les logs.")
        return image

    def lire_feuille(self, id_feuille, onglet, device_config):
        """Renvoie la feuille sous forme de liste de lignes (chaque ligne = liste de textes)."""
        fichier_cle = device_config.load_env_key("GOOGLE_SERVICE_ACCOUNT_FILE")

        # sans compte de service : la feuille doit être partagée « avec le lien »
        if not fichier_cle:
            url = f"https://docs.google.com/spreadsheets/d/{id_feuille}/export?format=csv&gid={onglet}"
            reponse = requests.get(url, timeout=30)
            if reponse.status_code != 200 or "accounts.google.com" in reponse.url:
                raise RuntimeError("Impossible de lire la feuille : est-elle partagée à « Tous les utilisateurs disposant du lien » ?")
            reponse.encoding = "utf-8"
            return list(csv.reader(io.StringIO(reponse.text)))

        # avec compte de service : la feuille doit être partagée avec son adresse e-mail
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        compte = service_account.Credentials.from_service_account_file(
            fichier_cle, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        session = AuthorizedSession(compte)
        api = f"https://sheets.googleapis.com/v4/spreadsheets/{id_feuille}"
        reponse = session.get(api, params={"fields": "sheets.properties"}, timeout=30)
        if reponse.status_code != 200:
            raise RuntimeError(f"Accès refusé : partagez la feuille en lecture avec {compte.service_account_email}")
        noms = {str(o["properties"]["sheetId"]): o["properties"]["title"] for o in reponse.json()["sheets"]}
        if onglet not in noms:
            raise RuntimeError("L'onglet du lien n'existe pas dans la feuille.")
        plage = requests.utils.quote(f"'{noms[onglet]}'", safe="")
        return session.get(f"{api}/values/{plage}", timeout=30).json().get("values", [])
