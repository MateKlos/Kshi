from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import time
import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

EXCLUIR_DEPORTE = (
    "nfl",
    "ncaa football",
    "college football",
    "american football",
    "cfl",
    "rugby",
)

NO_ES_MONEYLINE = (
    "total",
    "over ",
    "under ",
    "spread",
    "handicap",
    "both teams to score",
    "btts",
    "clean sheet",
    "corner",
    "card",
    "goalscorer",
    "player",
    "first half",
    "1st half",
    "second half",
    "2nd half",
)

SI_ES_MONEYLINE = (
    "moneyline",
    "match winner",
    "game winner",
    "winner",
    "tie",
    "draw",
    " vs ",
    " vs.",
    " @ ",
)


def to_decimal(val) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return None


def get_json(path: str, params: dict | None = None, reintentos: int = 4) -> dict:
    url = f"{BASE_URL}{path}"
    for intento in range(reintentos):
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            espera = 1.5 * (intento + 1)
            print(f"[Rate limit 429] Esperando {espera:.1f}s...", flush=True)
            time.sleep(espera)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def get_volumen(market: dict) -> Decimal:
    return (
        to_decimal(market.get("volume_fp"))
        or to_decimal(market.get("volume"))
        or to_decimal(market.get("volume_24h_fp"))
        or to_decimal(market.get("volume_24h"))
        or Decimal("0")
    )


def get_prob_en_dolares(market: dict, side: str) -> Decimal | None:
    for suffix in (f"{side}_ask_dollars", f"{side}_ask", "last_price_dollars", "last_price"):
        raw = to_decimal(market.get(suffix))
        if raw is not None:
            prob = raw / Decimal("100") if raw > Decimal("1") else raw
            if Decimal("0") < prob <= Decimal("1"):
                return prob
    return None


def es_serie_futbol(series: dict) -> bool:
    cat = str(series.get("category") or "").lower()
    tags = " ".join(str(t).lower() for t in (series.get("tags") or []))
    texto = f"{cat} {tags} {str(series.get('title') or '').lower()} {str(series.get('ticker') or '').lower()}"
    if any(bad in texto for bad in EXCLUIR_DEPORTE):
        return False
    return "soccer" in texto or "football" in texto


def es_mercado_moneyline(market: dict, event_titulo: str = "") -> bool:
    texto = " ".join(
        str(x or "").lower()
        for x in (
            market.get("ticker"),
            market.get("title"),
            market.get("subtitle"),
            market.get("yes_sub_title"),
            market.get("no_sub_title"),
            event_titulo,
        )
    )
    if any(bad in texto for bad in NO_ES_MONEYLINE):
        return False
    return any(pista in texto for pista in SI_ES_MONEYLINE)


def obtener_series_futbol() -> dict[str, str]:
    """Devuelve un diccionario {series_ticker: titulo_competicion}."""
    series_list = []
    for params in ({"category": "Sports"}, {}):
        try:
            data = get_json("/series", params=params)
            candidatas = data.get("series") or []
            if candidatas:
                series_list = [s for s in candidatas if es_serie_futbol(s)]
                if series_list:
                    break
        except requests.HTTPError:
            continue

    resultado = {}
    for s in series_list:
        t = s.get("ticker")
        if t:
            resultado[t] = s.get("title") or t
    return resultado


def obtener_grilla(
    min_vol_moneyline: Decimal = Decimal("10000"),
    max_prob: Decimal = Decimal("0.10"),
    max_paginas_mercados: int = 30,
) -> tuple[list[dict], int, int, int]:
    t0 = time.monotonic()
    series_futbol = obtener_series_futbol()
    print(f"Series de fútbol detectadas: {len(series_futbol)} ({time.monotonic() - t0:.1f}s)", flush=True)

    # Traemos mercados abiertos en lotes grandes y agrupamos por event_ticker
    mercados_por_evento: dict[str, list[dict]] = defaultdict(list)
    cursor = None
    paginas = 0

    while paginas < max_paginas_mercados:
        params = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        data = get_json("/markets", params=params)
        mercados = data.get("markets") or []
        paginas += 1

        for m in mercados:
            ev_ticker = m.get("event_ticker")
            if ev_ticker:
                mercados_por_evento[ev_ticker].append(m)

        print(
            f"Página {paginas} de mercados ({len(mercados)} items, {len(mercados_por_evento)} eventos acumulados) - {time.monotonic() - t0:.1f}s",
            flush=True,
        )

        cursor = data.get("cursor")
        if not cursor or not mercados:
            break

    # Filtramos eventos que pertenezcan a series de fútbol (o que tengan el mercado en una serie de fútbol)
    eventos_calificados = 0
    filas = []

    for ev_ticker, mercados in mercados_por_evento.items():
        primer_m = mercados[0]
        serie_ticker = primer_m.get("series_ticker") or ""

        # Verificamos si es de fútbol por series_ticker o por texto del mercado/evento
        es_futbol = serie_ticker in series_futbol
        if not es_futbol:
            texto_ev = f"{ev_ticker} {serie_ticker} {primer_m.get('title') or ''}".lower()
            if not any(bad in texto_ev for bad in EXCLUIR_DEPORTE) and (
                "soccer" in texto_ev or "football" in texto_ev
            ):
                es_futbol = True

        if not es_futbol:
            continue

        evento_titulo = primer_m.get("title") or ev_ticker
        competicion = series_futbol.get(serie_ticker) or serie_ticker or "Fútbol"

        # PASO 1: Volumen del moneyline del evento
        vols_ml = [get_volumen(m) for m in mercados if es_mercado_moneyline(m, evento_titulo)]
        vol_ml_evento = max(vols_ml) if vols_ml else Decimal("0")

        if vol_ml_evento <= min_vol_moneyline:
            continue

        eventos_calificados += 1

        # PASO 2: Todos los mercados del evento (sin filtro de volumen individual)
        for m in mercados:
            vol_mercado = get_volumen(m)
            mercado_nombre = m.get("title") or m.get("subtitle") or m.get("ticker", "")

            for side in ("yes", "no"):
                prob = get_prob_en_dolares(m, side)
                if prob is not None and prob <= max_prob:
                    filas.append(
                        {
                            "competicion": competicion,
                            "partido": evento_titulo,
                            "mercado": mercado_nombre,
                            "seleccion": m.get(f"{side}_sub_title") or side.upper(),
                            "lado": side.upper(),
                            "prob_pct": prob * Decimal("100"),
                            "precio_usd": prob,
                            "vol_ml": vol_ml_evento,
                            "vol_mercado": vol_mercado,
                            "ticker": m.get("ticker", ""),
                        }
                    )

    filas.sort(key=lambda x: (x["partido"], x["prob_pct"]))
    print(f"Listo en {time.monotonic() - t0:.1f}s.", flush=True)
    return filas, len(series_futbol), len(mercados_por_evento), eventos_calificados


def renderizar_markdown(
    filas: list[dict],
    total_series: int,
    total_eventos: int,
    eventos_calificados: int,
) -> str:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lineas = [
        "# Grilla Fútbol Kalshi (Eventos con Moneyline > 10,000 | Mercados <= 10%)",
        f"_Actualizado: {ahora} · Series: {total_series} · Eventos abiertos: {total_eventos} · Eventos con ML > 10k: {eventos_calificados}_",
        "",
    ]

    if not filas:
        lineas.append(
            "No se encontraron mercados <= 10% dentro de eventos de fútbol cuyo moneyline supere los 10,000 en este momento."
        )
        return "\n".join(lineas) + "\n"

    lineas.extend(
        [
            "| Competición | Partido | Mercado | Selección | Prob. / Precio | Vol. ML Evento | Vol. Mercado | Ticker |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )

    for f in filas:
        comp = str(f["competicion"]).replace("|", "/")
        partido = str(f["partido"]).replace("|", "/")
        mercado = str(f["mercado"]).replace("|", "/")
        sel = f"{str(f['seleccion']).replace('|', '/')} ({f['lado']})"
        prob_str = f"{f['prob_pct']:.1f}% (${f['precio_usd']:.2f})"
        vol_ml_str = f"{f['vol_ml']:,.0f}"
        vol_m_str = f"{f['vol_mercado']:,.0f}"
        lineas.append(
            f"| {comp} | {partido} | {mercado} | {sel} | {prob_str} | {vol_ml_str} | {vol_m_str} | `{f['ticker']}` |"
        )

    return "\n".join(lineas) + "\n"


def main():
    filas, total_series, total_eventos, eventos_calificados = obtener_grilla()
    md = renderizar_markdown(filas, total_series, total_eventos, eventos_calificados)
    Path("GRILLA.md").write_text(md, encoding="utf-8")
    print(md, flush=True)


if __name__ == "__main__":
    main()
