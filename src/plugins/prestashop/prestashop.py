from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image, ImageDraw
from utils.app_utils import get_font
import requests
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class PrestaShopDashboard(BasePlugin):
    
    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            
        width, height = dimensions
        
        ps_url = settings.get("ps_url", "").rstrip("/")
        ps_key = settings.get("ps_key", "")
        ps_exclude_b2b = settings.get("ps_exclude_b2b") == 'on' or settings.get("ps_exclude_b2b") is True
        
        data = self.fetch_data(ps_url, ps_key, ps_exclude_b2b)
        
        image = Image.new("RGBA", dimensions, (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        
        font_huge = get_font("Jost", int(min(width, height) * 0.14), "bold")
        font_large = get_font("Jost", int(min(width, height) * 0.10), "bold")
        font_medium = get_font("Jost", int(min(width, height) * 0.08), "normal")
        font_small = get_font("Jost", int(min(width, height) * 0.06), "normal")
        
        text_color = (0, 0, 0, 255)
        
        if "error" in data:
            draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 255))
            draw.text((10, 20), "ERREUR API", font=font_large, fill=(255, 255, 255, 255))
            draw.text((10, 50), "Vérifiez vos paramètres", font=font_medium, fill=(255, 255, 255, 255))
            draw.text((10, 95), data["error"][:45], font=font_small, fill=(255, 255, 255, 255))
            return image

        margin_x = int(width * 0.04)
        margin_y = int(height * 0.04)
        
        ca_text = f"{data['revenue_today']}€"
        evo_text = f" ({data['revenue_evolution']})"
        
        # Ligne 1: Chiffre d'affaires
        draw.text((margin_x, margin_y), ca_text, font=font_huge, fill=text_color)
        bbox = draw.textbbox((margin_x, margin_y), ca_text, font=font_huge)
        draw.text((bbox[2], margin_y + (bbox[3]-bbox[1])*0.3), evo_text, font=font_medium, fill=text_color) 
        
        # Ligne 2: Commandes
        draw.text((margin_x, int(height * 0.3)), f"À expédier : {data['orders_to_ship']}", font=font_large, fill=text_color)

        # Ligne de séparation
        mid_y = int(height * 0.55)
        draw.line((0, mid_y, width, mid_y), fill=text_color, width=2)

        # Zone Secondaire (Bas)
        bottom_start = mid_y + int(height * 0.05)
        spacing = int(height * 0.12)

        draw.text((margin_x, bottom_start), f"Panier moyen : {data['average_cart']}€", font=font_medium, fill=text_color)
        draw.text((margin_x, bottom_start + spacing), f"Paniers actifs (30m) : {data['active_carts']}", font=font_small, fill=text_color)
        draw.text((margin_x, bottom_start + spacing * 2), f"Nouveaux clients : {data['new_customers']}", font=font_small, fill=text_color)

        return image

    def fetch_data(self, ps_url, ps_key, ps_exclude_b2b=True):
        if not ps_url or not ps_key:
            return {"error": "Paramètres API PrestaShop manquants."}
            
        try:
            auth = (ps_key, '')
            now = datetime.now()
            
            # Format dates for API
            today_start = now.strftime("%Y-%m-%d 00:00:00")
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            yesterday_start = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
            yesterday_now = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            thirty_mins_ago = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

            def get_api(endpoint, params):
                params['io_format'] = 'JSON'
                response = requests.get(f"{ps_url}/api/{endpoint}", auth=auth, params=params, timeout=10)
                response.raise_for_status()
                return response.json()

            # 1. Commandes à expédier (état 2 ou 3)
            orders_ship_2 = get_api("orders", {"display": "[id]", "filter[current_state]": "[2]"}).get("orders", [])
            orders_ship_3 = get_api("orders", {"display": "[id]", "filter[current_state]": "[3]"}).get("orders", [])
            orders_to_ship = len(orders_ship_2) + len(orders_ship_3)

            # 2 & 3. CA et Panier moyen du jour
            today_orders_res = get_api("orders", {
                "display": "[total_paid_tax_incl,id_address_invoice]", 
                "filter[valid]": "[1]",
                "filter[date_add]": f"[{today_start},{now_str}]",
                "date": "1"
            }).get("orders", [])
            
            revenue_today = sum(float(o["total_paid_tax_incl"]) for o in today_orders_res)
            
            # Filtre des commandes d'Entreprises pour le panier moyen
            valid_cart_orders = today_orders_res
            if ps_exclude_b2b and valid_cart_orders:
                address_ids = list(set([o['id_address_invoice'] for o in valid_cart_orders if 'id_address_invoice' in o]))
                if address_ids:
                    # Requête groupée des adresses de facturation
                    ids_str = "|".join(str(aid) for aid in address_ids)
                    addresses_info = get_api("addresses", {
                        "display": "[id,company]",
                        "filter[id]": f"[{ids_str}]"
                    }).get("addresses", [])
                    
                    # On repère les adresses qui ont un nom d'entreprise
                    address_companies = {str(a['id']): str(a.get('company', '') or '').strip() for a in addresses_info}
                    
                    # On exclut de la moyenne toute commande dont l'adresse de facturation contient une entreprise
                    valid_cart_orders = [o for o in valid_cart_orders if not address_companies.get(str(o.get('id_address_invoice')))]
            
            average_cart = (sum(float(o["total_paid_tax_incl"]) for o in valid_cart_orders) / len(valid_cart_orders)) if valid_cart_orders else 0

            # 4. Evolution CA (par rapport à hier même heure)
            yesterday_orders_res = get_api("orders", {
                "display": "[total_paid_tax_incl]", 
                "filter[valid]": "[1]",
                "filter[date_add]": f"[{yesterday_start},{yesterday_now}]",
                "date": "1"
            }).get("orders", [])
            
            revenue_yesterday = sum(float(o["total_paid_tax_incl"]) for o in yesterday_orders_res)
            
            revenue_evolution = 0
            if revenue_yesterday > 0:
                revenue_evolution = ((revenue_today - revenue_yesterday) / revenue_yesterday) * 100
            elif revenue_today > 0:
                revenue_evolution = 100
            evolution_str = f"+{round(revenue_evolution, 1)}%" if revenue_evolution >= 0 else f"{round(revenue_evolution, 1)}%"

            # 5. Paniers actifs
            carts_res = get_api("carts", {
                "display": "[id]",
                "filter[date_upd]": f"[{thirty_mins_ago},{now_str}]",
                "date": "1"
            }).get("carts", [])
            active_carts = len(carts_res)

            # 6. Nouveaux clients
            customers_res = get_api("customers", {
                "display": "[id]",
                "filter[date_add]": f"[{today_start},{now_str}]",
                "date": "1"
            }).get("customers", [])
            new_customers = len(customers_res)

            return {
                "orders_to_ship": orders_to_ship,
                "revenue_today": round(revenue_today, 2),
                "revenue_evolution": evolution_str,
                "average_cart": round(average_cart, 2),
                "active_carts": active_carts,
                "new_customers": new_customers
            }
            
        except Exception as e:
            logger.error(f"Prestashop API Error: {e}")
            return {"error": str(e)}
