from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Palabras de fútbol americano / rugby para descartar falsos positivos en categorías mixtas
EXCLUIR_DEPORTE = (
    "nfl",
    "ncaa football",
    "college football",
    "american football",
    "cfl",
    "rugby",
)

# Palabras típicas de mercados que NO son moneyline/resultado del partido
EXCLUIR_MERCADO = (
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
    "winner of",
    "champion",
    "relegat",
    "to advance",
)

PISTAS_MONEYLINE = (
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


def get_json(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def es_serie_futbol(series: dict) -> bool:
    cat = str(series.get("category") or "").lower()
    tags = " ".join(str(t).lower() for t in (series.get("tags") or []))
    texto = f"{cat} {tags} {str(series.get('title') or '').lower()} {str(series.get('ticker') or '').lower()}"

    if any(bad in texto for bad in EXCLUIR_DEPORTE):
        return False

    return "soccer" in texto or "football" in texto


def es_mercado_moneyline(market: dict, event: dict) -> bool:
    texto = " ".join(
        str(x or "").lower()
        for x in (
            market.get("ticker"),
            market.get("title"),
            market.get("subtitle"),
            market.get("yes_sub_title"),
            market.get("no_sub_title"),
            event.get("title"),
            event.get("sub_title"),
            event.get("category"),
        )
    )

    if any(bad in texto for bad in EXCLUIR_MERCADO):
        return False

    # Un partido moneyline suele tener "vs" / "@" o mención explícita a winner/moneyline/tie/draw
    return any(pista in texto for pista in PISTAS_MONEYLINE)


def get_prob_en_dolares(market: dict, side: str) -> Decimal | None:
    for suffix in (f"{side}_ask_dollars", f"{side}_ask", "last_price_dollars", "last_price"):
        raw = to_decimal(market.get(suffix))
        if raw is not None:
            prob = raw / Decimal("100") if raw > Decimal("1") else raw
            if Decimal("0") < prob <= Decimal("1"):
                return prob
    return None


def obtener_series_futbol() -> list[dict]:
    # Intentamos filtrar por categoría si la API lo soporta; si no, traemos todas y filtramos
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

    # Deduplicamos por ticker
    unicas = {}
    for s in series_list:
        t = s.get("ticker")
        if t:
            unicas[t] = s
    return list(unicas.values())


def obtener_grilla(
    min_volumen: Decimal = Decimal("10000"),
    max_prob: Decimal = Decimal("0.10"),
) -> tuple[list[dict], int, int]:
    series_futbol = obtener_series_futbol()
    eventos_vistos = 0
    filas = []

    for serie in series_futbol:
        series_ticker = serie["ticker"]
        serie_titulo = serie.get("title") or series_ticker

        cursor = None
        while True:
            params = {"status": "open", "series_ticker": series_ticker, "limit": 200}
            if cursor:
                params["cursor"] = cursor

            ev_data = get_json("/events", params=params)
            eventos = ev_data.get("events") or []
            eventos_vistos += len(eventos)

            for ev in eventos:
                event_ticker = ev.get("event_ticker")
                if not event_ticker:
                    continue

                m_data = get_json("/markets", params={"event_ticker": event_ticker, "limit": 200})
                for m in m_data.get("markets") or []:
                    if not es_mercado_moneyline(m, ev):
                        continue

                    vol = (
                        to_decimal(m.get("volume_fp"))
                        or to_decimal(m.get("volume"))
                        or to_decimal(m.get("volume_24h_fp"))
                        or to_decimal(m.get("volume_24h"))
                        or Decimal("0")
                    )
                    if vol <= min_volumen:
                        continue

                    for side in ("yes", "no"):
                        prob = get_prob_en_dolares(m, side)
                        if prob is not None and prob <= max_prob:
                            filas.append(
                                {
                                    "competicion": serie_titulo,
                                    "partido": ev.get("title") or m.get("title") or event_ticker,
                                    "seleccion": m.get(f"{side}_sub_title") or side.upper(),
                                    "lado": side.upper(),
                                    "prob_pct": prob * Decimal("100"),
                                    "precio_usd": prob,
                                    "volumen": vol,
                                    "ticker": m.get("ticker", ""),
                                }
                            )

            cursor = ev_data.get("cursor")
            if not cursor:
                break

    filas.sort(key=lambda x: (x["prob_pct"], -x["volumen"]))
    return filas, len(series_futbol), eventos_vistos


def renderizar_markdown(filas: list[dict], total_series: int, total_eventos: int) -> str:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lineas = [
        "# Grilla Fútbol Moneyline Kalshi (Volumen > 10,000 | Prob. <= 10%)",
        f"_Actualizado: {ahora} · Series de fútbol revisadas: {total_series} · Eventos abiertos revisados: {total_eventos}_",
        "",
    ]

    if not filas:
        lineas.append(
            "No se encontraron mercados *moneyline* de fútbol abiertos con volumen > 10,000 y probabilidad <= 10% en este momento."
        )
        return "\n".join(lineas) + "\n"

    lineas.extend(
        [
            "| Competición | Partido / Evento | Selección | Prob. / Precio | Volumen | Ticker |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )

    for f in filas:
        comp = str(f["competicion"]).replace("|", "/")
        partido = str(f["partido"]).replace("|", "/")
        sel = f"{str(f['seleccion']).replace('|', '/')} ({f['lado']})"
        prob_str = f"{f['prob_pct']:.1f}% (${f['precio_usd']:.2f})"
        vol_str = f"{f['volumen']:,.0f}"
        lineas.append(f"| {comp} | {partido} | {sel} | {prob_str} | {vol_str} | `{f['ticker']}` |")

    return "\n".join(lineas) + "\n"


def main():
    filas, total_series, total_eventos = obtener_grilla()
    md = renderizar_markdown(filas, total_series, total_eventos)
    Path("GRILLA.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
