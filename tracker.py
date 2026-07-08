#!/usr/bin/env python3
"""
Oil / Geopolitics News Tracker
--------------------------------
Revisa fuentes RSS confiables cada 15 min (via GitHub Actions cron),
detecta noticias NUEVAS sobre:
  - Estrecho de Ormuz (cierre, bloqueo, amenazas)
  - Conflicto EEUU-Irán
  - Tratados/treguas internacionales que afecten el petróleo
  - Sanciones grandes, ataques a infraestructura energética
Usa DeepSeek (API) para razonar si la noticia es:
  a) genuinamente nueva (no ya vista/corroborada antes)
  b) lo suficientemente relevante como para mover el precio del barril
Si SI, envía un correo. Si no, no hace nada (silencioso).

Estado persistente: seen_articles.json (se commitea de vuelta al repo
por el workflow de GitHub Actions).
"""

import os
import json
import hashlib
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

import feedparser
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

STATE_FILE = "seen_articles.json"
MAX_STATE_ITEMS = 500  # para no dejar crecer el archivo infinito

# Fuentes RSS gratuitas y confiables, enfocadas en energía/geopolítica
RSS_FEEDS = {
    "Reuters - Energy": "https://www.reuters.com/arc/outboundfeeds/energy-rss/?outputType=xml",
    "Reuters - World": "https://www.reuters.com/arc/outboundfeeds/world-rss/?outputType=xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "OilPrice.com": "https://oilprice.com/rss/main",
    "CNBC Energy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19836768",
    "EIA - Petroleum": "https://www.eia.gov/rss/todayinenergy.xml",
    "AP Top News": "https://feedx.net/rss/ap.xml",  # mirror; ver README si falla
}

# Palabras clave para pre-filtrar antes de gastar tokens de la API
KEYWORDS = [
    "hormuz", "ormuz", "strait of hormuz",
    "iran", "irán", "irani",
    "oil price", "precio del petróleo", "crude", "barrel", "brent", "wti",
    "opec", "opep",
    "ceasefire", "tregua", "peace deal", "acuerdo de paz",
    "sanctions", "sanciones",
    "tanker", "petrolero", "shipping attack",
    "refinery attack", "ataque a refinería",
    "us military", "pentagon", "irgc", "revolutionary guard",
]

CRITERIA_MODERADO = """
Criterio de relevancia (nivel MODERADO):
Marca como RELEVANTE (debe enviarse correo) si la noticia trata de:
1. Cierre, bloqueo, o amenaza creíble de cierre del Estrecho de Ormuz.
2. Ataques militares directos entre EEUU e Irán (o proxies directos: IRGC,
   milicias respaldadas por Irán atacando activos de EEUU o viceversa).
3. Ataques a petroleros, infraestructura petrolera o refinerías en Medio Oriente.
4. Sanciones NUEVAS y significativas de EEUU/UE/ONU sobre exportaciones
   petroleras de Irán, Rusia, Venezuela, etc. (no renovaciones menores).
5. Anuncios de tregua, alto el fuego, o acuerdo de paz formal que
   razonablemente reduzca el riesgo geopolítico sobre el petróleo.
6. Decisiones grandes de la OPEP+ (recortes/aumentos de producción
   significativos, salida de un miembro, etc.)
7. Movimientos militares mayores (despliegue de portaaviones, movilización
   de tropas) directamente relacionados con Irán/Golfo Pérsico.

NO es relevante (no enviar correo) si es:
- Opinión/análisis especulativo sin hecho nuevo.
- Repetición de una noticia ya cubierta antes (aunque la redacción cambie).
- Fluctuaciones normales de precio sin causa geopolítica nueva.
- Declaraciones retóricas sin acción concreta (a menos que sea de muy alto
  nivel: presidente, líder supremo, secretario de Defensa/Estado).
- Sanciones menores, rutinarias o renovaciones automáticas.
"""

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = "deepseek-chat"

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
TO_EMAIL = os.environ.get("TO_EMAIL", "elkinstewarmanagement@gmail.com")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_hashes": [], "seen_summaries": []}


def save_state(state):
    # recortar para que no crezca infinito
    state["seen_hashes"] = state["seen_hashes"][-MAX_STATE_ITEMS:]
    state["seen_summaries"] = state["seen_summaries"][-200:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def article_hash(entry):
    key = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def matches_keywords(text):
    text_lower = text.lower()
    return any(kw in text_lower for kw in KEYWORDS)


def fetch_new_candidates(state):
    candidates = []
    for source_name, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] No se pudo leer {source_name}: {e}")
            continue

        for entry in feed.entries[:25]:
            title = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            combined = f"{title} {summary}"

            if not matches_keywords(combined):
                continue

            h = article_hash(entry)
            if h in state["seen_hashes"]:
                continue

            candidates.append({
                "source": source_name,
                "title": title,
                "summary": summary[:600],
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "hash": h,
            })
    return candidates


def ask_deepseek_to_reason(candidates, state):
    """
    Envía los candidatos + resumen de lo ya visto a DeepSeek para que decida
    si hay algo NUEVO y REALMENTE relevante. Devuelve dict con decisión.
    """
    if not candidates:
        return {"relevant": False}

    prior_context = "\n".join(f"- {s}" for s in state["seen_summaries"][-30:]) or "(sin historial previo)"

    articles_block = "\n\n".join(
        f"[{c['source']}] {c['title']}\nResumen: {c['summary']}\nLink: {c['link']}\nPublicado: {c['published']}"
        for c in candidates
    )

    prompt = f"""Eres un analista senior de riesgo geopolítico y mercado petrolero.

{CRITERIA_MODERADO}

CONTEXTO DE NOTICIAS YA REPORTADAS ANTERIORMENTE (no repitas esto, es solo para
que sepas qué ya se cubrió y evites duplicados o "info vieja disfrazada"):
{prior_context}

NOTICIAS NUEVAS DETECTADAS EN ESTA RONDA (candidatas, filtradas por keywords):
{articles_block}

Tu tarea:
1. Evalúa si alguna de estas noticias es GENUINAMENTE NUEVA (no es solo
   una reformulación de algo ya reportado arriba) Y cumple el criterio de
   relevancia MODERADO.
2. Si NINGUNA califica, responde exactamente con este JSON (nada más):
{{"relevant": false}}
3. Si UNA O MÁS califican, responde con este JSON (nada más, sin markdown,
   sin backticks):
{{
  "relevant": true,
  "headline": "Titular corto y directo en español para el asunto del correo",
  "impact_direction": "alza" | "baja" | "incierto",
  "summary_es": "2-4 frases explicando qué pasó y por qué afecta el precio del petróleo",
  "sources": ["lista de links de las noticias usadas"],
  "new_summary_for_memory": "1 frase resumen para guardar en el historial y evitar duplicados futuros"
}}

Responde SOLO con el JSON, sin texto adicional."""

    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    text = text.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("[WARN] No se pudo parsear respuesta de DeepSeek:", text)
        return {"relevant": False}


def send_email(decision):
    subject = f"🛢️ ALERTA PETRÓLEO: {decision.get('headline', 'Evento relevante detectado')}"

    direction = decision.get("impact_direction", "incierto")
    emoji = {"alza": "📈", "baja": "📉", "incierto": "❓"}.get(direction, "❓")

    sources_html = "".join(
        f"<li><a href='{s}'>{s}</a></li>" for s in decision.get("sources", [])
    )

    body_html = f"""
    <html><body style="font-family: Arial, sans-serif; line-height:1.5;">
      <h2>{emoji} {decision.get('headline','')}</h2>
      <p><b>Dirección esperada del precio:</b> {direction.upper()}</p>
      <p>{decision.get('summary_es','')}</p>
      <p><b>Fuentes:</b></p>
      <ul>{sources_html}</ul>
      <hr>
      <p style="font-size:12px;color:#888;">
        Generado automáticamente por tu Oil Tracker · {datetime.now(timezone.utc).isoformat()}
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(body_html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

    print(f"[OK] Email enviado: {subject}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    candidates = fetch_new_candidates(state)

    print(f"[INFO] {len(candidates)} candidatos nuevos tras filtro de keywords.")

    # Marcar como vistos de una vez (aunque no sean relevantes, ya los "leímos")
    for c in candidates:
        state["seen_hashes"].append(c["hash"])

    if not candidates:
        save_state(state)
        print("[INFO] Nada nuevo. Fin del ciclo.")
        return

    if not DEEPSEEK_API_KEY:
        print("[ERROR] Falta DEEPSEEK_API_KEY, no se puede razonar sobre relevancia.")
        save_state(state)
        return

    decision = ask_deepseek_to_reason(candidates, state)

    if decision.get("relevant"):
        if GMAIL_USER and GMAIL_APP_PASSWORD:
            send_email(decision)
        else:
            print("[WARN] Decisión relevante pero faltan credenciales de email.")
        summary = decision.get("new_summary_for_memory")
        if summary:
            state["seen_summaries"].append(summary)
    else:
        print("[INFO] DeepSeek determinó que no hay nada suficientemente relevante/nuevo.")

    save_state(state)


if __name__ == "__main__":
    main()
