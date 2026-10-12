import time
import json
import os
from datetime import datetime
import requests

# ---------------------------------------------------------
# 1. CONFIGURACIÓN Y CREDENCIALES (Leyendo desde Railway de forma segura)
# ---------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TELEGRAM_TOKEN = "8808039850:AAEXLka-1dtmfDrhRqS9MPb0aAJ0Ez9bzQA"
TELEGRAM_CHAT_ID = "5098093220"

DEPORTES = [
    "soccer_epl",             # Premier League
    "soccer_spain_la_liga",   # La Liga
    "basketball_nba",         # NBA
    "baseball_mlb",           # MLB
    "tennis_atp"              # ATP Tenis
]

REGIONS = "eu,us,uk"
MARKETS = "h2h,totals,spreads" 
ODDS_FORMAT = "decimal"

BOOKMAKERS_PERMITIDOS = [
    "pinnacle", "betfair_ex_eu", "betfair_sb_uk", "onexbet",
    "bet22", "marathonbet", "dafabet", "betcris", "matchbook"
]

BANKROLL_SIMULACION = 100.0
INTERVALO_MINUTOS = 5        

MARGEN_MINIMO_PORCENTAJE = 1.0  
TIEMPO_REPETICION_MINUTOS = 60  

CACHE_FILE = "surebets_cache.json"


# ---------------------------------------------------------
# 2. FUNCIONES DE MEMORIA Y MATEMÁTICAS
# ---------------------------------------------------------
def cargar_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def guardar_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"⚠️ Error guardando caché: {e}")

def calcular_surebet(cuotas):
    suma_implicita = sum(1 / c for c in cuotas)
    es_surebet = suma_implicita < 1
    margen = round((1 - suma_implicita) * 100, 2)
    return es_surebet, suma_implicita, margen

def calcular_stakes_redondeados(monto_total, cuotas, precision=1):
    suma_implicita = sum(1 / c for c in cuotas)
    stakes_exactos = [(monto_total / (c * suma_implicita)) for c in cuotas]
    stakes_redondeados = [round(s / precision) * precision for s in stakes_exactos]

    inversion_real = sum(stakes_redondeados)
    retornos = [s * c for s, c in zip(stakes_redondeados, cuotas)]
    retorno_minimo = min(retornos)

    beneficio_neto = retorno_minimo - inversion_real
    roi_real = (beneficio_neto / inversion_real) * 100 if inversion_real > 0 else 0

    return stakes_redondeados, inversion_real, beneficio_neto, roi_real

def formatear_fecha(iso_string):
    try:
        dt = datetime.strptime(iso_string.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d/%m/%Y a las %H:%M")
    except:
        return iso_string


# ---------------------------------------------------------
# 3. MÓDULO DE NOTIFICACIONES
# ---------------------------------------------------------
def enviar_alerta_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": mensaje
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ Error al enviar a Telegram: {res.text}")
        else:
            print("✉️ ¡Alerta enviada con éxito a Telegram!")
    except Exception as e:
        print(f"⚠️ Excepción de conexión con Telegram: {e}")


# ---------------------------------------------------------
# 4. ESCÁNER PRINCIPAL CON VALIDACIONES RIGUROSAS
# ---------------------------------------------------------
def escanear_todos_los_deportes():
    ahora = time.time()
    surebets_notificadas = cargar_cache()
    
    claves_a_borrar = [
        k for k, timestamp in surebets_notificadas.items()
        if (ahora - float(timestamp)) > (TIEMPO_REPETICION_MINUTOS * 60)
    ]
    for k in claves_a_borrar:
        del surebets_notificadas[k]

    for sport in DEPORTES:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT
        }

        try:
            response = requests.get(url, params=params, timeout=15)
        except Exception as e:
            print(f"❌ Error de red ({sport}): {e}")
            continue

        if response.status_code != 200:
            continue

        eventos = response.json()
        print(f"🔍 Escaneando {len(eventos)} eventos en {sport}...")

        for evento in eventos:
            equipo_local = evento.get("home_team", "Local")
            equipo_visitante = evento.get("away_team", "Visitante")
            fecha_cruda = evento.get("commence_time", "")
            fecha_evento = formatear_fecha(fecha_cruda)

            bookmakers_datos = {}

            for bookmaker in evento.get("bookmakers", []):
                casa = bookmaker["title"]
                casa_key = bookmaker["key"].lower()

                if BOOKMAKERS_PERMITIDOS and casa_key not in BOOKMAKERS_PERMITIDOS:
                    continue

                if casa not in bookmakers_datos:
                    bookmakers_datos[casa] = {}

                for market in bookmaker.get("markets", []):
                    m_key = market["key"]
                    if m_key not in bookmakers_datos[casa]:
                        bookmakers_datos[casa][m_key] = {}

                    for outcome in market.get("outcomes", []):
                        nombre = outcome.get("name")
                        punto = outcome.get("point")
                        cuota = outcome.get("price")
                        
                        bookmakers_datos[casa][m_key][nombre] = {
                            "cuota": cuota,
                            "punto": punto
                        }

            mercados_globales = {}

            for casa, mercados in bookmakers_datos.items():
                for m_key, selecciones in mercados.items():
                    for nombre, info in selecciones.items():
                        punto = info["punto"]
                        cuota = info["cuota"]

                        if punto is not None:
                            clave_unica = f"{m_key}_{punto}"
                        else:
                            clave_unica = m_key

                        if clave_unica not in mercados_globales:
                            mercados_globales[clave_unica] = {}

                        if nombre not in mercados_globales[clave_unica] or cuota > mercados_globales[clave_unica][nombre]["cuota"]:
                            mercados_globales[clave_unica][nombre] = {
                                "cuota": cuota,
                                "casa": casa,
                                "punto": punto
                            }

            for clave_mercado, opciones in mercados_globales.items():
                if len(opciones) in [2, 3]:
                    cuotas_lista = [datos["cuota"] for datos in opciones.values()]
                    casas_lista = [datos["casa"] for datos in opciones.values()]
                    puntos_lista = [datos["punto"] for datos in opciones.values()]

                    # Validar casas distintas
                    if len(set(casas_lista)) == 1:
                        continue

                    # Validar hándicaps (spreads) estrictos
                    if "spreads" in clave_mercado.lower():
                        puntos_validos = [p for p in puntos_lista if p is not None]
                        if len(puntos_validos) == 2:
                            if sum(puntos_validos) != 0:
                                continue

                    es_surebet, suma_imp, margen_teorico = calcular_surebet(cuotas_lista)

                    if es_surebet and margen_teorico >= MARGEN_MINIMO_PORCENTAJE:
                        id_surebet = f"{sport}_{equipo_local}_{equipo_visitante}_{clave_mercado}"
                        
                        if id_surebet in surebets_notificadas:
                            continue

                        stakes, inversion, beneficio, roi = calcular_stakes_redondeados(
                            BANKROLL_SIMULACION, cuotas_lista
                        )

                        if beneficio > 0:
                            surebets_notificadas[id_surebet] = ahora
                            guardar_cache(surebets_notificadas)

                            mensaje = f"🚨 SUREBET DETECTADA ({sport.upper()} - {clave_mercado.upper()}) (+{margen_teorico}%)\n\n"
                            mensaje += f"🏆 Evento: {equipo_local} vs {equipo_visitante}\n"
                            mensaje += f"📅 Fecha: {fecha_evento}\n"
                            mensaje += f"📉 Inversa Total: {round(suma_imp, 4)}\n\n"

                            for i, (opcion, datos) in enumerate(opciones.items()):
                                punto_str = f" ({datos['punto']})" if datos['punto'] is not None else ""
                                mensaje += f"• {opcion}{punto_str}: {datos['cuota']} @ {datos['casa']}\n"
                                mensaje += f"  👉 Apostar: ${stakes[i]}\n"

                            mensaje += f"\n💵 Inversión Total: ${inversion}.00\n"
                            mensaje += f"📈 Ganancia Neta: ${round(beneficio, 2)} ({round(roi, 2)}% ROI)\n"

                            print(f"✨ Nueva Surebet real notificada: {equipo_local} vs {equipo_visitante} ({clave_mercado})")
                            enviar_alerta_telegram(mensaje)

        time.sleep(2)


# ---------------------------------------------------------
# 5. BUCLE DE EJECUCIÓN 24/7
# ---------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Bot en la nube operativo y listo.")
    print(f"🎯 Margen mínimo: {MARGEN_MINIMO_PORCENTAJE}% | Anti-duplicados: {TIEMPO_REPETICION_MINUTOS} min.\n--------------------------------------------------")

    while True:
        hora_actual = time.strftime("%H:%M:%S")
        print(f"\n[Ejecución {hora_actual}] Escaneando mercados...")
        
        escanear_todos_los_deportes()
        
        print(f"⏳ Esperando {INTERVALO_MINUTOS} minutos para la próxima revisión...")
        time.sleep(INTERVALO_MINUTOS * 60)
