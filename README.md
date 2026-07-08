# Oil / Geopolitics News Tracker

Monitorea noticias sobre el Estrecho de Ormuz, conflicto EEUU-Irán, tratados
internacionales y eventos que afecten el precio del petróleo. Corre cada 15
minutos vía GitHub Actions (gratis) y solo te envía un correo cuando detecta
un evento **nuevo y relevante** (   razonado por DeepSeek, no solo por keywords).

## Cómo funciona

1. Cada 15 min, GitHub Actions ejecuta `tracker.py`.
2. El script lee RSS de fuentes confiables (Reuters, Al Jazeera, OilPrice.com,
   CNBC Energy, EIA, AP).
3. Filtra por keywords (Ormuz, Irán, OPEP, sanciones, tregua, etc.) para no
   gastar tokens en ruido.
4. Los candidatos nuevos (no vistos antes, guardados en `seen_articles.json`)
   se envían a DeepSeek junto con el historial de lo ya reportado.
5. DeepSeek decide: ¿es genuinamente nuevo? ¿cumple el criterio de relevancia
   MODERADO? Si sí → te manda un correo con titular, dirección esperada del
   precio (alza/baja/incierto), resumen y fuentes.
6. El estado se guarda de vuelta en el repo para no repetir alertas.

## Setup (una sola vez, ~10 min)

### 1. Crea un repo en GitHub
Puede ser privado. Sube estos archivos (`tracker.py`, `requirements.txt`,
`.github/workflows/tracker.yml`, este README).

### 2. Consigue una API key de DeepSeek
En https://platform.deepseek.com → API Keys → crea una.
(Nota: esto es de pago por uso, muy barato para este volumen —
probablemente <$0.50 USD/mes corriendo cada 15 min, porque solo se llama
a DeepSeek cuando HAY candidatos nuevos tras el filtro de keywords).

### 3. Crea una "App Password" de Gmail
Tu Gmail normal NO funcionará por SMTP directo si tienes 2FA (recomendado
tenerlo). Pasos:
- Ve a https://myaccount.google.com/apppasswords
- Genera una contraseña de aplicación (16 caracteres)
- Guárdala, la usarás como `GMAIL_APP_PASSWORD`

### 4. Configura los Secrets en GitHub
En tu repo → Settings → Secrets and variables → Actions → New repository secret.
Crea estos 4:

| Nombre | Valor |
|---|---|
| `DEEPSEEK_API_KEY` | tu API key de DeepSeek |
| `GMAIL_USER` | el correo que ENVÍA (puede ser el mismo u otro Gmail) |
| `GMAIL_APP_PASSWORD` | la app password de 16 caracteres |
| `TO_EMAIL` | elkinstewarmanagement@gmail.com |

### 5. Activa el workflow
El cron ya está configurado a `*/15 * * * *` (cada 15 min, hora UTC).
GitHub a veces retrasa unos minutos los cron jobs en repos gratuitos (normal,
no es un bug tuyo).

Para probarlo manualmente: pestaña **Actions** → **Oil Tracker** →
**Run workflow**.

## Ajustar el criterio de relevancia

Edita el bloque `CRITERIA_MODERADO` en `tracker.py`. Ahí defines
explícitamente qué SÍ y qué NO dispara un correo. Puedes agregar/quitar
puntos según cómo veas que se comporta las primeras semanas.

## Ajustar fuentes RSS

Edita el diccionario `RSS_FEEDS`. Algunas notas:
- El feed de Reuters puede cambiar de URL ocasionalmente; si falla, revisa
  https://www.reuters.com/tools/rss o busca su feed vigente.
- El feed de AP usado es un mirror (`feedx.net`); si no funciona, reemplázalo
  por otro feed de AP o quítalo — Reuters + Al Jazeera + OilPrice ya cubren
  bien el tema.
- Puedes añadir Bloomberg, WSJ, FT si tienes suscripción con RSS habilitado.

## Costos estimados

- GitHub Actions: gratis (repos públicos ilimitado; privados con 2000
  min/mes gratis — este job tarda ~20-30 seg cada corrida, así que ~35
  corridas/día = sobra margen).
- DeepSeek API: solo se llama cuando hay candidatos nuevos tras filtro de
  keywords (no en cada corrida). Estimado muy bajo, unos pocos centavos al mes
  en escenarios de alta actividad noticiosa.
- Gmail SMTP: gratis.

## Limitaciones a tener en cuenta

- Depende de que las fuentes RSS estén disponibles y no cambien de URL.
- El filtro de keywords es en inglés/español mezclado; si una fuente usa
  otro idioma, podría no capturarse (puedes añadir más keywords).
- No es "tiempo real" en el sentido de milisegundos — es cada 15 min, que
  para un evento macro (cierre de Ormuz, guerra, tregua) es más que
  suficiente ya que el mercado también tarda en reaccionar.
