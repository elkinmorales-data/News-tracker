#!/usr/bin/env python3
"""
Fed / Interest Rate News Tracker
----------------------------------
Revisa fuentes RSS de la FED y mercados financieros una vez al día (8am
Colombia, vía GitHub Actions cron), detecta noticias NUEVAS sobre:
  - Decisiones de tasas de interés de la FED
  - Actas de FOMC y cambios de lenguaje
  - Declaraciones de Powell y gobernadores
  - Datos macro (CPI, PCE, NFP) que cambien expectativas
  - Movimientos significativos en yields/tasas
Usa DeepSeek (API) para razonar si hay algo relevante y generar un
resumen diario consolidado.
Si hay algo relevante, envía UN correo con el resumen del día.
Si no, no hace nada (silencioso).

Estado persistente: seen_fed_articles.json (se commitea de vuelta al repo
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

STATE_FILE = "seen_fed_articles.json"
MAX_STATE_ITEMS = 500

# Fuentes RSS enfocadas en FED / tasas de interés / macro
RSS_FEEDS = {
    "Fed - Monetary Policy": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "Fed - Speeches": "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml",
    "CNBC Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Reuters Economy": "http://feeds.reuters.com/news/economy",
    "BLS (CPI/Empleo)": "https://www.bls.gov/feed/bls_latest.rss",
    "BEA (GDP/PCE)": "https://apps.bea.gov/rss/rss.xml",
}

# Palabras clave para pre-filtrar antes de gastar tokens de la API
KEYWORDS = [
    "federal reserve", "fed", "fomc", "interest rate",
    "rate hike", "rate cut", "rate hold", "rate pause",
    "monetary policy", "powell", "jerome powell",
    "treasury yield", "basis points", "hawkish", "dovish",
    "fed funds", "inflation", "cpi", "pce", "gdp",
    "rate decision", "dot plot", "yield curve",
    "fed chair", "federal open market",
    "employment", "nonfarm", "non-farm", "jobs report",
    "recession", "soft landing", "hard landing",
]

CRITERIA_MODERADO = """
Criterio de relevancia (nivel MODERADO) - Fed / Tasas de Interés:
Marca como RELEVANTE si la noticia trata de:
1. Decisión de tasas de interés de la FED (subida, bajada, mantenimiento).
2. Acta de FOMC (minutes) con cambios de lenguaje significativos
   (ej: de "restrictivo" a "restrictivo menos tiempo").
3. Declaraciones de Powell o gobernadores sobre DIRECCIÓN de tasas
   (no comentarios rutinarios).
4. Datos macro (CPI, PCE, NFP) que cambien significativamente las
   expectativas de tasas (desviación grande vs consenso).
5. Cambios grandes en yield curves o spread tasas largas/cortas.
6. Proyecciones dot plot con cambios vs expectativa del mercado.
7. Escenarios de recesión o soft landing que cambien la narrativa.

NO es relevante si es:
- Análisis especulativo sin dato nuevo.
- Repetición de postura ya conocida.
- Movimientos menores de mercado sin causa fundamental.
- Declaraciones genéricas sin señal concreta.
- Datos menores o revisiones rutinarias.
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


def ask_deepseek_for_daily_summary(candidates, state):
    """
    Envía todos los candidatos del día a DeepSeek para que genere un
    resumen diario consolidado. Devuelve dict con decisión.
    """
    if not candidates:
        return {"relevant": False}

    prior_context = "\n".join(f"- {s}" for s in state["seen_summaries"][-30:]) or "(sin historial previo)"

    articles_block = "\n\n".join(
        f"[{c['source']}] {c['title']}\nResumen: {c['summary']}\nLink: {c['link']}\nPublicado: {c['published']}"
        for c in candidates
    )

    prompt = f"""Eres un analista senior de macroeconomía y política monetaria de la FED.

{CRITERIA_MODERADO}

CONTEXTO DE NOTICIAS YA REPORTADAS ANTERIORMENTE (no repitas esto, es solo para
que sepas qué ya se cubrió y evites duplicados):
{prior_context}

NOTICIAS NUEVAS DETECTADAS HOY (candidatas, filtradas por keywords):
{articles_block}

Tu tarea:
1. Evalúa si ALGUNA de estas noticias es GENUINAMENTE RELEVANTE según el
   criterio de arriba.
2. Si NINGUNA califica, responde exactamente con este JSON (nada más):
{{"relevant": false}}
3. Si UNA O MÁS califican, consolida TODO en un resumen diario y responde
   con este JSON (nada más, sin markdown, sin backticks):
{{
  "relevant": true,
  "headline": "Resumen Diario: FED y Tasas de Interés - [fecha de hoy]",
  "impact_direction": "hawkish" | "dovish" | "incierto",
  "summary_es": "3-6 frases resumiendo los puntos clave del día sobre la FED/tasas. Incluye: qué pasó, qué significa, y cómo afecta las expectativas de tasas.",
  "key_points": ["punto clave 1", "punto clave 2", "punto clave 3"],
  "sources": ["lista de links de las noticias relevantes usadas"],
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
            "max_tokens": 1500,
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


def send_fed_email(decision):
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    subject = f"🏦 RESUMEN FED: {decision.get('headline', f'Tasas e Interés - {today}')}"

    direction = decision.get("impact_direction", "incierto")
    emoji = {"hawkish": "🔴", "dovish": "🟢", "incierto": "⚪"}.get(direction, "⚪")

    key_points_html = "".join(
        f"<li>{p}</li>" for p in decision.get("key_points", [])
    )

    sources_html = "".join(
        f"<li><a href='{s}'>{s}</a></li>" for s in decision.get("sources", [])
    )

    body_html = f"""
    <html><body style="font-family: Arial, sans-serif; line-height:1.5;">
      <h2>{emoji} {decision.get('headline','')}</h2>
      <p><b>Sentimiento general:</b> {direction.upper()}</p>
      <p>{decision.get('summary_es','')}</p>
      <p><b>Puntos clave:</b></p>
      <ul>{key_points_html}</ul>
      <p><b>Fuentes:</b></p>
      <ul>{sources_html}</ul>
      <hr>
      <p style="font-size:12px;color:#888;">
        Generado automáticamente por tu Fed Tracker · {datetime.now(timezone.utc).isoformat()}
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

    for c in candidates:
        state["seen_hashes"].append(c["hash"])

    if not candidates:
        save_state(state)
        print("[INFO] Nada nuevo hoy. Fin del ciclo.")
        return

    if not DEEPSEEK_API_KEY:
        print("[ERROR] Falta DEEPSEEK_API_KEY, no se puede razonar sobre relevancia.")
        save_state(state)
        return

    decision = ask_deepseek_for_daily_summary(candidates, state)

    if decision.get("relevant"):
        if GMAIL_USER and GMAIL_APP_PASSWORD:
            send_fed_email(decision)
        else:
            print("[WARN] Decisión relevante pero faltan credenciales de email.")
        summary = decision.get("new_summary_for_memory")
        if summary:
            state["seen_summaries"].append(summary)
    else:
        print("[INFO] DeepSeek determinó que no hay nada suficientemente relevante hoy.")

    save_state(state)


if __name__ == "__main__":
    main()
