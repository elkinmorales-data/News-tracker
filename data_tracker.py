#!/usr/bin/env python3
"""
Data Science / Data Engineering Trends Tracker
-----------------------------------------------
Revisa fuentes RSS de data science, ML, data engineering una vez al día (8am
Colombia, vía GitHub Actions cron), detecta tendencias/ideas NUEVAS sobre:
  - Data Science & Machine Learning
  - Data Engineering & Arquitectura de datos
  - Analítica y visualización de datos
  - Nuevas herramientas, frameworks y técnicas
  - Casos de uso innovadores en la industria
Usa DeepSeek (API) para identificar las top 5 tendencias más novedosas del día.
Si hay al menos 1 tendencia nueva relevante, envía un correo. Si no, silencioso.

Estado persistente: seen_data_articles.json (se commitea de vuelta al repo
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

STATE_FILE = "seen_data_articles.json"
MAX_STATE_ITEMS = 500

RSS_FEEDS = {
    "KDnuggets": "https://www.kdnuggets.com/feed",
    "Towards Data Science": "https://medium.com/feed/towards-data-science",
    "Data Engineering Weekly": "https://www.dataengineeringweekly.com/feed.xml",
    "Analytics Vidhya": "https://www.analyticsvidhya.com/feed/",
    "Databricks Blog": "https://www.databricks.com/blog/feed",
    "Google AI Blog": "http://feeds.feedburner.com/GoogleAI",
    "Machine Learning Mastery": "https://machinelearningmastery.com/feed/",
    "PyTorch Blog": "https://pytorch.org/blog/feed.xml",
}

KEYWORDS = [
    "data science", "data scientist",
    "data engineering", "data engineer",
    "machine learning", "deep learning", "ml", "dl",
    "artificial intelligence", "ai", "generative ai", "genai",
    "llm", "large language model", "gpt", "transformer",
    "rag", "retrieval augmented generation",
    "vector database", "embeddings",
    "pipeline", "etl", "elt", "data pipeline",
    "data warehouse", "data lake", "lakehouse", "delta lake",
    "spark", "kafka", "airflow", "dbt", "snowflake", "databricks",
    "bigquery", "redshift", "clickhouse", "duckdb",
    "feature store", "mlops", "dataops", "data quality",
    "streaming", "real-time", "batch processing",
    "sql", "nosql", "graph database",
    "python", "pandas", "numpy", "scikit-learn", "pytorch", "tensorflow",
    "kubernetes", "docker", "infrastructure as code",
    "data governance", "data catalog", "data mesh", "data fabric",
    "analytics", "bi", "business intelligence", "visualization",
    "a/b testing", "experimentation", "causal inference",
    "time series", "forecast", "anomaly detection",
    "recommendation system", "nlp", "computer vision",
    "fine-tuning", "prompt engineering", "agent",
    "data modeling", "dimensional modeling", "star schema",
]

CRITERIA_MODERADO = """
Criterio de relevancia — Tendencias en Data Science / Data Engineering:
Marca como RELEVANTE si el artículo trata de:

1. NUEVA herramienta,框架 o tecnología en data science/engineering.
2. Técnica o metodología novedosa (ej: nueva arquitectura de LLM,
   nuevo approach de RAG, nueva técnica de fine-tuning).
3. Caso de uso innovador en la industria (ej: cómo Netflix usa X,
   cómo Spotify implementó Y).
4. Avance significativo en ML/DL (ej: nuevo modelo state-of-the-art,
   breakthrough en eficiencia).
5. Mejores prácticas actualizadas sobre herramientas del ecosistema
   (Spark, Airflow, dbt, Snowflake, etc.).
6. Tendencias emergentes: data mesh, data contracts, reverse ETL,
   feature stores, MLOps, data observability, etc.
7. Análisis comparativo relevante entre tecnologías (ej: ClickHouse
   vs DuckDB, Ray vs Spark).

NO es relevante si es:
- Tutorial básico para principiantes (ej: "qué es Python", "qué es SQL").
- Contenido puramente promocional o vendor spam sin sustancia técnica.
- Repetición de una tendencia ya cubierta antes en este tracker.
- Noticias de financiamiento/mergers sin innovación técnica real.
- Contenido clickbait sin profundidad técnica.
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

        for entry in feed.entries[:20]:
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
                "summary": summary[:800],
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "hash": h,
            })
    return candidates


def ask_deepseek_for_trends(candidates, state):
    if not candidates:
        return {"relevant": False}

    prior_context = "\n".join(f"- {s}" for s in state["seen_summaries"][-40:]) or "(sin historial previo)"

    articles_block = "\n\n".join(
        f"[{c['source']}] {c['title']}\nResumen: {c['summary']}\nLink: {c['link']}\nPublicado: {c['published']}"
        for c in candidates
    )

    prompt = f"""Eres un analista senior de tendencias en data science, data engineering y tecnología de datos.

{CRITERIA_MODERADO}

CONTEXTO DE TENDENCIAS YA REPORTADAS ANTERIORMENTE (NO repitas estas ideas,
son tendencias que ya se han enviado antes y deben evitarse):
{prior_context}

ARTÍCULOS NUEVOS DETECTADOS HOY (candidatos, filtrados por keywords):
{articles_block}

Tu tarea:
1. Analiza estos artículos e identifica las tendencias/ideas MÁS NOVEDOSAS
   que NO estén ya cubiertas en el historial de arriba.
2. Si NINGUNA tendencia es genuinamente nueva o relevante, responde:
   {{"relevant": false}}
3. Si HAY tendencias nuevas, selecciona MÁXIMO 5 (las mejores) y responde:
   {{
     "relevant": true,
     "headline": "Tendencias en Data Science/Engineering - [fecha de hoy]",
     "trends": [
       {{
         "title": "Nombre corto de la tendencia",
         "summary": "2-3 frases explicando de qué se trata, por qué es relevante y su impacto potencial",
         "source": "link del artículo original",
         "category": "data-science" | "data-engineering" | "ml-ai" | "tools" | "industry"
       }}
     ],
     "sources": ["lista de links de los artículos usados"],
     "new_summary_for_memory": "Lista separada por | de las tendencias clave cubiertas hoy para evitar duplicados futuros: tendencia 1 | tendencia 2 | tendencia 3"
   }}

IMPORTANTE:
- Máximo 5 trends. Si solo 1 o 2 son valiosos, devuelve solo esos.
- Cada trend debe ser UNA IDEA DISTINTA y NUEVA comparada con el historial.
- La categoria puede ser: "data-science", "data-engineering", "ml-ai", "tools", "industry".
- Responde SOLO con el JSON, sin markdown, sin backticks, sin texto adicional."""

    resp = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    if not resp.ok:
        print(f"[ERROR] DeepSeek respondió {resp.status_code}: {resp.text[:500]}")
        return {"relevant": False}
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
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    subject = f"📊 DATA TRENDS: {decision.get('headline', f'Tendencias - {today}')}"

    trends = decision.get("trends", [])
    category_emoji = {
        "data-science": "🔬",
        "data-engineering": "⚙️",
        "ml-ai": "🤖",
        "tools": "🛠️",
        "industry": "🏢",
    }

    trends_html = ""
    for t in trends:
        emoji = category_emoji.get(t.get("category", ""), "📌")
        trends_html += f"""
        <div style="background:#f6f8fa;border-radius:6px;padding:12px;margin-bottom:12px;">
          <h4 style="margin:0 0 6px 0;">{emoji} {t['title']}</h4>
          <p style="margin:0;color:#444;">{t['summary']}</p>
          <p style="margin:4px 0 0 0;font-size:12px;">
            <a href="{t.get('source','')}">Ver artículo original →</a>
          </p>
        </div>"""

    body_html = f"""
    <html><body style="font-family: Arial, sans-serif; line-height:1.5;">
      <h2>📊 Tendencias del día en Data Science & Engineering</h2>
      <p style="color:#666;">{today} · {len(trends)} tendencias identificadas</p>
      {trends_html}
      <hr>
      <p style="font-size:12px;color:#888;">
        Generado automáticamente por tu Data Trends Tracker · {datetime.now(timezone.utc).isoformat()}
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
        print("[INFO] No hay artículos nuevos hoy. Sin correo.")
        return

    if not DEEPSEEK_API_KEY:
        print("[ERROR] Falta DEEPSEEK_API_KEY, no se puede analizar tendencias.")
        save_state(state)
        return

    decision = ask_deepseek_for_trends(candidates, state)

    if decision.get("relevant"):
        if GMAIL_USER and GMAIL_APP_PASSWORD:
            send_email(decision)
        else:
            print("[WARN] Tendencias detectadas pero faltan credenciales de email.")
        summary = decision.get("new_summary_for_memory")
        if summary:
            state["seen_summaries"].append(summary)
    else:
        print("[INFO] DeepSeek determinó que no hay tendencias nuevas hoy.")

    save_state(state)


if __name__ == "__main__":
    main()
