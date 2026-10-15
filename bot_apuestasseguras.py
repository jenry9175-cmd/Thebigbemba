import time
import json
import os
from collections import Counter
from datetime import datetime, timezone, timedelta
import asyncio
import aiohttp

try:
    from zoneinfo import ZoneInfo
    ZONA_LOCAL = ZoneInfo("America/Caracas")
except Exception:
    # Venezuela usa UTC-4 fijo (sin horario de verano)
    ZONA_LOCAL = timezone(timedelta(hours=-4))

# ---------------------------------------------------------
# 1. CONFIGURACIÓN Y CREDENCIALES (OPTIMIZADA ANTI-FANTASMAS)
# ---------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8808039850:AAEXLka-1dtmfDrhRqS9MPb0aAJ0Ez9bzQA")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5098093220")

# Definimos los deportes a monitorear
DEPORTES = [
    "soccer_epl",             # Fútbol (Premier League)
    "basketball_nba"          # Baloncesto (NBA)
]

# Se mantiene por compatibilidad con la API. Como abajo se envía también el
# parámetro "bookmakers", la API le da prioridad y el coste se calcula sobre
# tus 4 casas (hasta 10 casas cuentan como 1 región), no sobre 2 regiones.
REGIONS = "eu,us"
MARKETS = "h2h"               # Mercado directo para agilizar respuestas
ODDS_FORMAT = "decimal"

# Casas de apuestas estables y operables (Filtradas para evitar desajustes fantasmas)
BOOKMAKERS_PERMITIDOS = [
    "pinnacle",         # Pinnacle (La referencia global de cuotas reales)
    "onexbet",          # 1xBet (Muy activa y accesible en Venezuela)
    "marathonbet",      # Marathon Bet
    "betonlineag"       # BetOnline (Sólida con cripto)
]

BANKROLL_SIMULACION = 100.0
# Cada escaneo cuesta 2 créditos (1 por deporte). Con 1 minuto se gastan unos
# 2.880 créditos al día (~86.000 al mes), así que necesitas un plan de pago.
# Con el plan gratuito (500 créditos/mes) súbelo a 180.
INTERVALO_MINUTOS = 1

# FILTRO ANTIFANTASMAS CLAVE: Margen mínimo más exigente (2.0%)
MARGEN_MINIMO_PORCENTAJE = 2.0
TIEMPO_REPETICION_MINUTOS = 120

# FILTRO DE FRESCURA: se descartan las cuotas cuyo "last_update" tenga más
# de estos minutos al momento de recibirlas. Ajústalo viendo el resumen que
# imprime el bot en cada ciclo (línea "📊 Resumen"): si casi todo se descarta
# como "cuotas_viejas", sube el valor; si salen candidatas dudosas, bájalo.
MAX_EDAD_CUOTA_MINUTOS = 5

CACHE_FILE = "surebets_cache.json"


# ---------------------------------------------------------
# 2. FUNCIONES DE MEMORIA Y MATEMÁTICAS
# ---------------------------------------------------------
def cargar_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def guardar_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"⚠️ Error guardando caché: {e}")

def parse_iso(iso_string):
    """Convierte '2026-10-15T18:30:00Z' en un datetime con zona UTC."""
    return datetime.fromisoformat(iso_string.replace("Z", "+00:00"))

def formatear_fecha(iso_string):
    """Muestra la fecha en hora de Venezuela (antes se mostraba en UTC)."""
    try:
        return parse_iso(iso_string).astimezone(ZONA_LOCAL).strftime("%d/%m/%Y a las %H:%M")
    except Exception:
        return iso_string

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

def nombres_esperados(sport, m_key, local, visitante):
    """
    Resultados que DEBE tener un mercado completo. Si faltan (por ejemplo, sin
    el empate en fútbol), la suma de probabilidades sería engañosamente baja y
    se detectaría una surebet falsa.
    """
    if m_key == "h2h":
        nombres = {local, visitante}
        if sport.startswith("soccer"):
            nombres.add("Draw")      # fútbol = 3 resultados
        return nombres
    if m_key == "totals":
        return {"Over", "Under"}
    if m_key == "spreads":
        return {local, visitante}
    return None                      # mercado desconocido: no se evalúa


# ---------------------------------------------------------
# 3. MÓDULO DE NOTIFICACIONES ASÍNCRONO
# ---------------------------------------------------------
async def enviar_alerta_telegram(session, mensaje):
    """Devuelve True solo si Telegram confirmó el envío."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.post(url, json=payload, timeout=timeout) as res:
            if res.status != 200:
                texto = await res.text()
                print(f"⚠️ Error al enviar a Telegram: {texto}")
                return False
            print("✉️ ¡Alerta enviada con éxito a Telegram!")
            return True
    except Exception as e:
        print(f"⚠️ Excepción de conexión con Telegram: {e}")
        return False


# ---------------------------------------------------------
# 4. PETICIÓN INDIVIDUAL ASÍNCRONA A LA API
# ---------------------------------------------------------
async def obtener_odds_deporte(session, sport):
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "bookmakers": ",".join(BOOKMAKERS_PERMITIDOS),
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, params=params, timeout=timeout) as response:
            restantes = response.headers.get("x-requests-remaining", "?")
            usadas = response.headers.get("x-requests-used", "?")
            ultima = response.headers.get("x-requests-last", "?")
            print(f"💳 {sport}: coste {ultima} | usados {usadas} | restantes {restantes}")

            if response.status == 200:
                data = await response.json()
                return sport, data

            cuerpo = (await response.text())[:200]
            if response.status == 401:
                print(f"⚠️ {sport}: API key inválida o sin créditos (401). {cuerpo}")
            elif response.status == 429:
                print(f"⚠️ {sport}: límite de peticiones alcanzado (429). {cuerpo}")
            else:
                print(f"⚠️ Error API en {sport}: Estado {response.status}. {cuerpo}")
            return sport, []
    except Exception as e:
        print(f"❌ Error de red ({sport}): {e}")
        return sport, []


# ---------------------------------------------------------
# 5. LÓGICA DE DETECCIÓN (separada para poder probarla sin red)
# ---------------------------------------------------------
def recolectar_mercados(evento, ahora_dt, stats):
    """
    Agrupa, por mercado, la MEJOR cuota fresca de cada resultado entre todas
    las casas permitidas. Las cuotas viejas se descartan ANTES de comparar,
    para que una cuota alta y desactualizada no gane a una fresca.
    """
    local = evento.get("home_team", "Local")
    visitante = evento.get("away_team", "Visitante")
    mercados_globales = {}

    for bookmaker in evento.get("bookmakers", []):
        casa = bookmaker.get("title", bookmaker.get("key", "?"))
        casa_key = str(bookmaker.get("key", "")).lower()

        if BOOKMAKERS_PERMITIDOS and casa_key not in BOOKMAKERS_PERMITIDOS:
            continue

        for market in bookmaker.get("markets", []):
            m_key = market.get("key")

            # --- Filtro de frescura ---
            marca_tiempo = market.get("last_update") or bookmaker.get("last_update")
            try:
                edad_min = (ahora_dt - parse_iso(marca_tiempo)).total_seconds() / 60
            except Exception:
                stats["sin_timestamp"] += 1
                continue
            if edad_min > MAX_EDAD_CUOTA_MINUTOS:
                stats["cuotas_viejas"] += 1
                continue

            for outcome in market.get("outcomes", []):
                nombre = outcome.get("name")
                punto = outcome.get("point")
                cuota = outcome.get("price")

                # Una cuota decimal válida es un número mayor que 1
                if not isinstance(cuota, (int, float)) or cuota <= 1:
                    continue

                # --- Clave del mercado ---
                if m_key == "spreads":
                    # Se agrupa por el hándicap del equipo LOCAL: "Local -1.5" y
                    # "Visitante +1.5" caen en la misma clave y se comparan.
                    if punto is None or nombre not in (local, visitante):
                        continue
                    punto_local = punto if nombre == local else -punto
                    clave_unica = f"spreads_{round(punto_local, 2)}"
                elif punto is not None:
                    clave_unica = f"{m_key}_{punto}"
                else:
                    clave_unica = m_key

                mercado = mercados_globales.setdefault(
                    clave_unica, {"m_key": m_key, "opciones": {}}
                )
                actual = mercado["opciones"].get(nombre)
                if actual is None or cuota > actual["cuota"]:
                    mercado["opciones"][nombre] = {
                        "cuota": cuota,
                        "casa": casa,
                        "punto": punto,
                        "edad_min": edad_min
                    }

    return mercados_globales


def construir_mensaje(sport, clave_mercado, local, visitante, fecha_evento,
                      suma_imp, margen, opciones, stakes, inversion, beneficio, roi):
    mensaje = f"🚨 SUREBET CANDIDATA ({sport.upper()} - {clave_mercado.upper()}) (+{margen}%)\n"
    mensaje += "⚠️ Verifica las cuotas en cada casa antes de apostar.\n\n"
    mensaje += f"🏆 Evento: {local} vs {visitante}\n"
    mensaje += f"📅 Fecha (hora de Venezuela): {fecha_evento}\n"
    mensaje += f"📉 Inversa Total: {round(suma_imp, 4)}\n\n"

    for i, (opcion, datos) in enumerate(opciones.items()):
        punto_str = f" ({datos['punto']})" if datos["punto"] is not None else ""
        mensaje += f"• {opcion}{punto_str}: {datos['cuota']} @ {datos['casa']}\n"
        mensaje += f"  🕒 Cuota de hace {datos['edad_min']:.1f} min\n"
        mensaje += f"  👉 Apostar: ${stakes[i]:.2f}\n"

    mensaje += f"\n💵 Inversión Total: ${inversion:.2f}\n"
    mensaje += f"📈 Ganancia Neta: ${round(beneficio, 2)} ({round(roi, 2)}% ROI)\n"
    return mensaje


def buscar_surebets_evento(sport, evento, ahora_dt, stats):
    """Devuelve la lista de surebets candidatas de un evento (puede estar vacía)."""
    stats["eventos"] += 1

    # --- Se descartan partidos ya iniciados o en vivo ---
    inicio = parse_iso(evento["commence_time"])
    if inicio <= ahora_dt:
        stats["en_vivo"] += 1
        return []

    local = evento.get("home_team", "Local")
    visitante = evento.get("away_team", "Visitante")
    fecha_evento = formatear_fecha(evento.get("commence_time", ""))

    candidatas = []
    mercados = recolectar_mercados(evento, ahora_dt, stats)

    for clave_mercado, mercado in mercados.items():
        opciones = mercado["opciones"]

        # --- El mercado debe estar COMPLETO (3 resultados en fútbol, 2 en NBA) ---
        esperados = nombres_esperados(sport, mercado["m_key"], local, visitante)
        if esperados is None or set(opciones.keys()) != esperados:
            stats["mercado_incompleto"] += 1
            continue

        cuotas_lista = [d["cuota"] for d in opciones.values()]
        casas_lista = [d["casa"] for d in opciones.values()]

        # Todas las patas en la misma casa no es arbitraje entre casas
        if len(set(casas_lista)) == 1:
            continue

        es_surebet, suma_imp, margen_teorico = calcular_surebet(cuotas_lista)

        # Aquí se aplica el filtro estricto del margen mínimo
        if not (es_surebet and margen_teorico >= MARGEN_MINIMO_PORCENTAJE):
            continue

        stakes, inversion, beneficio, roi = calcular_stakes_redondeados(
            BANKROLL_SIMULACION, cuotas_lista
        )
        if beneficio <= 0:
            continue

        candidatas.append({
            "id": f"{sport}_{local}_{visitante}_{clave_mercado}",
            "titulo": f"{local} vs {visitante}",
            "margen": margen_teorico,
            "mensaje": construir_mensaje(
                sport, clave_mercado, local, visitante, fecha_evento,
                suma_imp, margen_teorico, opciones, stakes, inversion, beneficio, roi
            )
        })

    return candidatas


# ---------------------------------------------------------
# 6. ESCÁNER PRINCIPAL CONCURRENTE (EN PARALELO)
# ---------------------------------------------------------
async def escanear_todos_los_deportes():
    ahora = time.time()
    surebets_notificadas = cargar_cache()

    claves_a_borrar = [
        k for k, timestamp in surebets_notificadas.items()
        if (ahora - float(timestamp)) > (TIEMPO_REPETICION_MINUTOS * 60)
    ]
    for k in claves_a_borrar:
        del surebets_notificadas[k]

    stats = Counter()

    async with aiohttp.ClientSession() as session:
        tareas = [obtener_odds_deporte(session, sport) for sport in DEPORTES]
        resultados = await asyncio.gather(*tareas)

        # Hora de referencia para medir la edad de las cuotas recién recibidas
        ahora_dt = datetime.now(timezone.utc)

        for sport, eventos in resultados:
            if not eventos:
                continue

            print(f"🔍 Procesando {len(eventos)} eventos en {sport} (antifantasmas)...")

            for evento in eventos:
                # Un evento con datos raros ya no aborta todo el escaneo
                try:
                    candidatas = buscar_surebets_evento(sport, evento, ahora_dt, stats)
                except Exception as e:
                    stats["errores_evento"] += 1
                    print(f"⚠️ Error procesando un evento de {sport}: {e}")
                    continue

                for c in candidatas:
                    if c["id"] in surebets_notificadas:
                        continue

                    stats["candidatas"] += 1
                    print(f"✨ Surebet candidata: {c['titulo']} (+{c['margen']}%)")

                    # Solo se marca como notificada si Telegram confirmó el envío
                    if await enviar_alerta_telegram(session, c["mensaje"]):
                        surebets_notificadas[c["id"]] = ahora
                        guardar_cache(surebets_notificadas)

    guardar_cache(surebets_notificadas)  # persiste también la limpieza de claves viejas

    print(
        "📊 Resumen: "
        f"eventos={stats['eventos']} | en_vivo_omitidos={stats['en_vivo']} | "
        f"cuotas_viejas={stats['cuotas_viejas']} | sin_timestamp={stats['sin_timestamp']} | "
        f"mercados_incompletos={stats['mercado_incompleto']} | "
        f"candidatas_nuevas={stats['candidatas']} | errores={stats['errores_evento']}"
    )


# ---------------------------------------------------------
# 7. BUCLE DE EJECUCIÓN 24/7 ASÍNCRONO
# ---------------------------------------------------------
async def main():
    if not ODDS_API_KEY:
        raise SystemExit("❌ Falta la variable de entorno ODDS_API_KEY.")

    print("🤖 Bot antifantasmas operativo.")
    print(
        f"🎯 Margen mínimo exigido: {MARGEN_MINIMO_PORCENTAJE}% | "
        f"Casas: {', '.join(BOOKMAKERS_PERMITIDOS)} | "
        f"Edad máx. de cuota: {MAX_EDAD_CUOTA_MINUTOS} min\n"
        "--------------------------------------------------"
    )

    while True:
        hora_actual = time.strftime("%H:%M:%S")
        print(f"\n[Ejecución {hora_actual}] Escaneando mercados limpios...")

        try:
            await escanear_todos_los_deportes()
        except Exception as e:
            print(f"⚠️ Error en el ciclo: {e}")

        print(f"⏳ Esperando {INTERVALO_MINUTOS} minutos para la próxima revisión...")
        await asyncio.sleep(INTERVALO_MINUTOS * 60)

if __name__ == "__main__":
    asyncio.run(main())
