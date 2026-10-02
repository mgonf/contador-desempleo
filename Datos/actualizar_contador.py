# /// script
# requires-python = ">=3.10"
# dependencies = ["pytrends>=4.9.2", "pandas>=2.0", "statsmodels", "scipy", "openpyxl", "requests"]
# ///
r"""
Actualización mensual del contador diario de desempleo en España.

Uso (en la carpeta del proyecto, con el entorno .venv):
    .\.venv\Scripts\python.exe actualizar_contador.py

Pasos:
  1. Google Trends: refresca la mensual 2004-hoy y la mensual del periodo P
     de «paro» y «desempleo», y descarga la diaria de los meses nuevos
     (más el mes anterior y el actual, que pueden estar incompletos).
     Construye la diaria de Kostopoulos, Meyer y Uhr (2020).
  2. Eurostat: descarga la tasa de paro y los parados de España (une_rt_m,
     sin desestacionalizar). Si la API falla, usa el Excel indicado con
     --eurostat-xlsx (exportación de Eurostat como la que ya tienes).
  3. Recalcula el contador y la evaluación fuera de muestra.
  4. Genera contador_desempleo.html a partir de plantilla_contador.html.

Después, sube contador_desempleo.html al chat para publicarlo en el mismo
enlace: https://claude.ai/artifact/HYQodTzCjXfjrn3AkLvMcr

Opciones:
  --sin-descarga        no consulta Google Trends (usa los CSV existentes)
  --eurostat-xlsx RUTA  lee Eurostat de un Excel en lugar de la API
"""

import argparse
import calendar
import json
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats

TERMINOS = ["paro", "desempleo"]
GEO = "ES"
INICIO_P = date(2010, 1, 1)   # inicio de la serie diaria
INICIO_MUESTRA = "2005-01"     # primera observación de la regresión mensual
MESES_CALENTAMIENTO = 12      # meses iniciales de diaria reservados para el factor de escala
COVID = ("2020-03", "2021-06")
VENTANA = 28                   # días de la media móvil
PAUSA, REINTENTOS = 15, 6
HOY = date.today()
LOG = Path("gt_log.txt")


def log(msg):
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(linea + "\n")


# ----------------------------------------------------------------------------
# 1. GOOGLE TRENDS
# ----------------------------------------------------------------------------
def consulta(pt, termino, timeframe):
    for intento in range(1, REINTENTOS + 1):
        try:
            pt.build_payload([termino], cat=0, timeframe=timeframe, geo=GEO)
            df = pt.interest_over_time()
            if df.empty:
                raise ValueError("respuesta vacía")
            time.sleep(PAUSA)
            out = df[[termino]].astype(float).rename(columns={termino: "valor"})
            out["parcial"] = df["isPartial"].astype(bool) if "isPartial" in df else False
            return out
        except Exception as e:
            espera = PAUSA * 2 ** intento
            log(f"  fallo {termino} {timeframe} ({type(e).__name__}); reintento {intento} en {espera} s")
            time.sleep(espera)
    raise RuntimeError(f"No se pudo descargar {termino} {timeframe}. Relanza más tarde.")


def descarga_trends():
    from pytrends.request import TrendReq
    pt = TrendReq(hl="es-ES", tz=-60)
    for termino in TERMINOS:
        s = termino.replace(" ", "_")
        log(f"[{termino}] mensual 2004-hoy")
        consulta(pt, termino, f"2004-01-01 {HOY:%Y-%m-%d}").to_csv(f"gt2_mensual2004_{s}.csv")
        log(f"[{termino}] mensual del periodo P")
        mens = consulta(pt, termino, f"{INICIO_P:%Y-%m-%d} {HOY:%Y-%m-%d}")
        mens.index = mens.index.to_period("M")
        mens.to_csv(f"gt2_mensualP_{s}.csv")

        fc = Path(f"gt_cache_{s}.csv")
        cache = pd.read_csv(fc) if fc.exists() else pd.DataFrame(columns=["date", "valor", "parcial", "mes"])
        hechos = set(cache["mes"])
        actual = pd.Period(HOY, "M")
        refrescar = {str(actual), str(actual - 1)}
        m = pd.Period(INICIO_P, "M")
        while m <= actual:
            if str(m) not in hechos or str(m) in refrescar:
                ultimo = min(date(m.year, m.month, calendar.monthrange(m.year, m.month)[1]), HOY)
                tf = f"{m.year}-{m.month:02d}-01 {ultimo:%Y-%m-%d}"
                log(f"[{termino}] diaria {tf}")
                d = consulta(pt, termino, tf).reset_index()
                d["date"] = d["date"].dt.strftime("%Y-%m-%d")
                d["mes"] = str(m)
                cache = pd.concat([cache[cache["mes"] != str(m)], d], ignore_index=True)
                cache.to_csv(fc, index=False)
            m += 1


def construye_kost(termino):
    s = termino.replace(" ", "_")
    mens = pd.read_csv(f"gt2_mensualP_{s}.csv", index_col=0)["valor"]
    mens.index = pd.PeriodIndex(mens.index, freq="M")
    cache = pd.read_csv(f"gt_cache_{s}.csv", parse_dates=["date"]).sort_values("date")
    peso = cache["mes"].map(lambda m: mens[pd.Period(m, "M")] / 100)
    kost = pd.DataFrame({"date": cache["date"].values, "diaria_mes": cache["valor"].values,
                         "peso_mensual": peso.values, "kost": (cache["valor"] * peso).values,
                         "parcial": cache["parcial"].values}).set_index("date")
    kost.to_csv(f"gt2_kost_{s}.csv")


# ----------------------------------------------------------------------------
# 2. EUROSTAT
# ----------------------------------------------------------------------------
def eurostat_api():
    import requests
    url = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_m"
    series = {}
    for ajuste, suf in [("NSA", ""), ("SA", "_sa")]:
        for unidad, nombre in [("PC_ACT", "tasa"), ("THS_PER", "parados")]:
            r = requests.get(url, params={"geo": GEO, "s_adj": ajuste, "age": "TOTAL", "sex": "T",
                                          "unit": unidad, "format": "JSON", "lang": "EN"}, timeout=60)
            r.raise_for_status()
            js = r.json()
            idx = js["dimension"]["time"]["category"]["index"]
            vals = js["value"]
            s = {pd.Period(lab.replace("M", "-"), "M"): vals.get(str(pos)) for lab, pos in idx.items()}
            series[nombre + suf] = pd.Series(s, dtype=float).sort_index()
    return pd.DataFrame(series)


def eurostat_xlsx(ruta):
    x = pd.ExcelFile(ruta)
    series = {}
    for hoja in x.sheet_names:
        d = pd.read_excel(x, hoja, header=None)
        c0 = d[0].astype(str)
        if not (c0 == "TIME").any() or not (c0 == "Spain").any():
            continue
        meta = " ".join(str(v) for v in d.iloc[:12].values.ravel())
        if "Unadjusted" in meta:
            suf = ""
        elif "Seasonally adjusted" in meta:
            suf = "_sa"
        else:
            continue
        unidad = "tasa" if "Percentage" in meta else "parados" if "Thousand" in meta else None
        unidad = unidad + suf if unidad else None
        tr, sr = d.index[c0 == "TIME"][0], d.index[c0 == "Spain"][0]
        s = {}
        for c in d.columns[1:]:
            t, v = d.iloc[tr, c], d.iloc[sr, c]
            if isinstance(t, str) and "-" in t:
                s[pd.Period(t, "M")] = pd.to_numeric(v, errors="coerce")
        s = pd.Series(s, dtype=float)
        if unidad and s.notna().any():
            series[unidad] = s
    return pd.DataFrame(series)


# ----------------------------------------------------------------------------
# 3. MODELO
# ----------------------------------------------------------------------------
def carga_gt(termino):
    s = termino.replace(" ", "_")
    M = pd.read_csv(f"gt2_mensual2004_{s}.csv", index_col=0)["valor"]
    M.index = pd.to_datetime(M.index).to_period("M")
    d = pd.read_csv(f"gt2_kost_{s}.csv", parse_dates=["date"]).set_index("date")
    per = d.index.to_period("M")
    # Cociente mensual índice mensual / media de la diaria de Kostopoulos. El factor
    # de escala del mes m es la mediana de los cocientes de los meses anteriores a m.
    ratio = M.reindex(per.unique()) / d["kost"].groupby(per).mean()
    media = d["diaria_mes"] / d["diaria_mes"].groupby(per).transform("mean") * M.reindex(per).values
    return np.log(M), {"kost": d["kost"], "media": media}, ratio


def cw_p(f_bench, f_modelo, y):
    """Test de Clark y West (2007) para modelos anidados, una cola."""
    f_bench, f_modelo, y = map(np.asarray, (f_bench, f_modelo, y))
    f = (y - f_bench) ** 2 - ((y - f_modelo) ** 2 - (f_bench - f_modelo) ** 2)
    n = len(f)
    x = f - f.mean()
    v = x @ x / n
    for l in range(1, 4):
        v += 2 * (1 - l / 4) * (x[l:] @ x[:-l]) / n
    return float(1 - stats.t.cdf(f.mean() / np.sqrt(v / n), n - 1))


class Modelo:
    def __init__(self, u, G):
        self.u, self.G = u, G
        self.y = u.diff(12)
        self.cache = {}

    def coef(self, K):
        if K not in self.cache:
            X = pd.DataFrame({"c": 1.0, "y1": self.y.shift(1), "y2": self.y.shift(2)})
            if self.G is not None:
                X["g"] = self.G.diff(12)
            d = pd.concat([self.y.rename("y"), X], axis=1).dropna().loc[INICIO_MUESTRA:K]
            self.cache[K] = sm.OLS(d["y"], d.drop(columns="y")).fit().params
        return self.cache[K]

    def paso(self, uh, p, j, g_actual=None):
        v = uh[j - 12] + p["c"] + p["y1"] * (uh[j - 1] - uh[j - 13]) + p["y2"] * (uh[j - 2] - uh[j - 14])
        if self.G is not None:
            g = g_actual if g_actual is not None else self.G[j]
            v += p["g"] * (g - self.G[j - 12])
        return v

    def nowcast(self, m, K, g_actual=None):
        """Estimación del mes m conociendo u hasta K; los meses intermedios
        se estiman con el índice mensual completo."""
        p = self.coef(K)
        uh = {t: self.u[t] for t in self.u.loc[:K].index[-30:]}
        for j in pd.period_range(K + 1, m - 1, freq="M"):
            uh[j] = self.paso(uh, p, j)
        uh[m] = self.paso(uh, p, m, g_actual)
        return uh


def prevision_multi(u, Gs, valores_dia, meses):
    """Estimación de fin de mes con varios regresores de Google (dos pasos, u conocida hasta m-2).
    Gs: lista de logs mensuales; valores_dia: funciones m -> log del índice diario de 28 días."""
    y = u.diff(12)
    out = []
    for m in meses:
        K = m - 2
        X = pd.DataFrame({"c": 1.0, "y1": y.shift(1), "y2": y.shift(2)})
        for i, g in enumerate(Gs):
            X[f"g{i}"] = g.diff(12)
        d = pd.concat([y.rename("y"), X], axis=1).dropna().loc[INICIO_MUESTRA:K]
        p = sm.OLS(d["y"], d.drop(columns="y")).fit().params
        uh = {t: u[t] for t in u.loc[:K].index[-30:]}
        for j in (m - 1, m):
            v = uh[j - 12] + p["c"] + p["y1"] * (uh[j - 1] - uh[j - 13]) + p["y2"] * (uh[j - 2] - uh[j - 14])
            for i, g in enumerate(Gs):
                gj = g[j] if j < m else valores_dia[i](m)
                v += p[f"g{i}"] * (gj - g[j - 12])
            uh[j] = v
        out.append(uh[m])
    return pd.Series(out, index=meses)


def calcula(eu):
    u = eu["tasa"].dropna()
    lf = (eu["parados"] / eu["tasa"] * 100).dropna()
    LAST = u.index[-1]
    cov = lambda idx: ~((idx >= pd.Period(COVID[0], "M")) & (idx <= pd.Period(COVID[1], "M")))
    rmse = lambda e: float(np.sqrt(np.mean(np.asarray(e) ** 2)))

    # El contador y la evaluación empiezan tras un año de diaria (para el factor de escala recursivo)
    primer_dia = min(pd.read_csv(f"gt2_kost_{t.replace(' ', '_')}.csv", usecols=["date"])["date"].min() for t in TERMINOS)
    inicio = pd.Period(primer_dia, "M") + MESES_CALENTAMIENTO
    INICIO_CONTADOR = inicio.start_time.strftime("%Y-%m-%d")
    meses_ev = pd.period_range(inicio, LAST, freq="M")
    bench = Modelo(u, None)
    fb = pd.Series({m: bench.nowcast(m, m - 2)[m] for m in meses_ev})
    eb = fb - u.reindex(fb.index)
    cb = cov(eb.index)

    def lf_mes(m, K):
        return lf[m] if m in lf.index else lf[m - 12] * lf[K] / lf[K - 12]

    ref_mes = {}

    def referencia(m):
        if m not in ref_mes:
            ref_mes[m] = bench.nowcast(m, min(m - 2, LAST))[m]
        return ref_mes[m]

    series, evaluacion = {}, {"bench": [rmse(eb), rmse(eb[cb])]}
    gdia = {}
    info = {"inicio_eval": str(meses_ev[0]), "inicio_diaria": str(pd.Period(primer_dia, "M")), "n_eval": len(meses_ev), "n_eval_sc": int(cb.sum()),
            "pico": {}, "k": {}, "rango_ratio": {}}
    GT = {}
    for termino in TERMINOS:
        G, diarias, ratio = carga_gt(termino)
        GT[termino] = (G, diarias, ratio)
        info["pico"][termino] = diarias["kost"].idxmax().strftime("%Y-%m-%d")
        info["k"][termino] = round(float(ratio.dropna().median()), 3)
        rc = ratio.dropna().iloc[:-1]  # sin el mes en curso, que puede estar incompleto
        info["rango_ratio"][termino] = [round(float(rc.min()), 2), round(float(rc.max()), 2)]
        mod = Modelo(u, G)
        for metodo, s in diarias.items():
            roll = s.rolling(VENTANA, min_periods=VENTANA).mean().loc[INICIO_CONTADOR:]
            filas = []
            for dd, val in roll.items():
                m = pd.Period(dd, "M")
                K = min(m - 2, LAST)
                if metodo == "kost":
                    val = val * ratio.loc[:m - 1].median()
                uh = mod.nowcast(m, K, np.log(val))
                filas.append((dd, m, uh[m - 1], uh[m], lf_mes(m, K), lf_mes(m - 1, K), val, referencia(m)))
            R = pd.DataFrame(filas, columns=["date", "mes", "u_prev", "u", "lf", "lf_prev", "m28", "ref"]).set_index("date")
            if metodo == "kost":
                gdia[termino] = {"idx": [round(float(x), 3) for x in s.reindex(R.index)],
                                 "m28": [round(float(x), 3) for x in R["m28"]]}
            fin = R[R["mes"] <= LAST].groupby("mes").tail(1).set_index("mes")
            e = (fin["u"] - u.reindex(fin.index)).reindex(eb.index)
            clave = f"{termino}|{metodo}"
            yv = u.reindex(fb.index)
            fg = yv + e
            evaluacion[clave] = [rmse(e), rmse(e[cb]), cw_p(fb, fg, yv), cw_p(fb[cb], fg[cb], yv[cb])]
            ult = R.iloc[-1]
            series[clave] = {
                "u": [round(float(x), 3) for x in R["u"]],
                "p": [round(float(a * b / 100), 1) for a, b in zip(R["u"], R["lf"])],
                "lf": [round(float(x), 1) for x in R["lf"]],
                "rmse": round(rmse(e[cb]), 3),
                "comp": [round(float(a - b), 3) for a, b in zip(R["u"], R["ref"])],
                "prev": {"mes": str(ult["mes"] - 1), "u": round(float(ult["u_prev"]), 3),
                         "p": round(float(ult["u_prev"] * ult["lf_prev"] / 100), 1)},
            }
            fechas = [d.strftime("%Y-%m-%d") for d in R.index]
            referencia_dia = [round(float(x), 3) for x in R["ref"]]
            mes_ultimo = str(ult["mes"])
            log(f"  {clave}: RMSE {evaluacion[clave][0]:.3f} (sin COVID {evaluacion[clave][1]:.3f}), "
                f"Clark-West p={evaluacion[clave][2]:.3f} (sin COVID {evaluacion[clave][3]:.3f}); hoy {ult['u']:.2f} %")
    log(f"  referencia: RMSE {evaluacion['bench'][0]:.3f} (sin COVID {evaluacion['bench'][1]:.3f})")

    # Combinaciones de términos (evaluación de fin de mes)
    yv = u.reindex(meses_ev)

    def dia(termino, metodo):
        _, diarias, ratio = GT[termino]
        s = diarias[metodo]

        def f(m):
            v = s.loc[:m.end_time].iloc[-VENTANA:].mean()
            if metodo == "kost":
                v *= ratio.loc[:m - 1].median()
            return np.log(v)
        return f

    comb = {}
    for metodo in ("kost", "media"):
        gp, gd = GT["paro"][0], GT["desempleo"][0]
        fp, fd = dia("paro", metodo), dia("desempleo", metodo)
        prev = {
            "indice": prevision_multi(u, [(gp + gd) / 2], [lambda m: (fp(m) + fd(m)) / 2], meses_ev),
            "ambos": prevision_multi(u, [gp, gd], [fp, fd], meses_ev),
            "media_prev": (prevision_multi(u, [gp], [fp], meses_ev) + prevision_multi(u, [gd], [fd], meses_ev)) / 2,
        }
        for nombre, f in prev.items():
            e = f - yv
            comb[f"{nombre}|{metodo}"] = [round(v, 3) for v in (rmse(e), rmse(e[cb]), cw_p(fb, f, yv),
                                                                cw_p(fb[cb], f[cb], yv[cb]))]
            log(f"  combinación {nombre}|{metodo}: RMSE {comb[f'{nombre}|{metodo}'][0]:.3f}")

    obs = [{"d": m.end_time.strftime("%Y-%m-%d"), "u": float(u[m]), "p": float(eu["parados"][m])}
           for m in pd.period_range(inicio, LAST, freq="M")]
    ec = eu.loc[eu["tasa"].first_valid_index():LAST]
    num = lambda v: None if pd.isna(v) else float(v)
    eurostat = [{"m": str(m), "u": num(r.get("tasa")), "p": num(r.get("parados")),
                 "usa": num(r.get("tasa_sa")), "psa": num(r.get("parados_sa"))} for m, r in ec.iterrows()]
    rt = archivo_tiempo_real(series, fechas, mes_ultimo, str(LAST), referencia_dia[-1])
    return {"dates": fechas, "series": series, "obs": obs, "last_obs": str(LAST), "eurostat": eurostat,
            "generado": HOY.strftime("%Y-%m-%d"),
            "eval": {k: [round(v, 3) for v in vals] for k, vals in evaluacion.items()},
            "comb": comb, "info": info, "gdia": gdia, "ref": referencia_dia,
            "rt": rt, "cmp": comparacion_oficial(rt, series, str(LAST), float(u[LAST]), float(lf[LAST]))}


def archivo_tiempo_real(series, fechas, mes, ultimo_eurostat, ref):
    """Añade la estimación de hoy a historico_tiempo_real.csv (una fila por fecha de cálculo)
    y devuelve el archivo completo. Este archivo no debe borrarse: es la única serie sin look-ahead."""
    f = Path("historico_tiempo_real.csv")
    fila = {"fecha_calculo": HOY.strftime("%Y-%m-%d"), "fecha_datos": fechas[-1], "mes_estimado": mes,
            "ultimo_dato_eurostat": ultimo_eurostat, "tasa_referencia_AR": ref}
    for clave, s in series.items():
        v = clave.replace("|", "_")
        fila[f"tasa_{v}"] = s["u"][-1]
        fila[f"parados_{v}"] = round(s["p"][-1]) * 1000
        fila[f"componente_google_{v}"] = s["comp"][-1]
    primera = next(iter(series.values()))
    fila["mes_anterior"] = primera["prev"]["mes"]
    for clave, s in series.items():
        v = clave.replace("|", "_")
        fila[f"tasa_anterior_{v}"] = s["prev"]["u"]
        fila[f"parados_anterior_{v}"] = round(s["prev"]["p"]) * 1000
    h = pd.read_csv(f, dtype=str) if f.exists() else pd.DataFrame()
    if not h.empty:
        h = h[h["fecha_calculo"] != fila["fecha_calculo"]]
    h = pd.concat([h, pd.DataFrame([fila]).astype(str)], ignore_index=True).sort_values("fecha_calculo")
    h.to_csv(f, index=False)
    log(f"  archivo en tiempo real: {len(h)} estimaciones guardadas en {f}")
    return h.to_dict(orient="records")


def comparacion_oficial(rt, series, ultimo_eurostat, tasa_oficial, lf_oficial):
    """Para el último mes publicado por Eurostat, busca la última estimación archivada
    antes de que ese dato estuviera disponible (filas con ultimo_dato_eurostat anterior)."""
    filas = [r for r in rt if str(r.get("ultimo_dato_eurostat", "")) < ultimo_eurostat]
    out = {}
    for clave in series:
        v = clave.replace("|", "_")
        for r in sorted(filas, key=lambda r: r["fecha_calculo"], reverse=True):
            for col_mes, pref in (("mes_estimado", "tasa_"), ("mes_anterior", "tasa_anterior_")):
                val = r.get(pref + v)
                if r.get(col_mes) == ultimo_eurostat and val not in (None, "", "nan"):
                    est = float(val)
                    out[clave] = {"u": round(est, 3), "p": round(est * lf_oficial / 100, 1),
                                  "dif": round(tasa_oficial - est, 3),
                                  "dif_p": round((tasa_oficial - est) * lf_oficial / 100, 1),
                                  "fecha": r["fecha_calculo"], "datos": r["fecha_datos"]}
                    break
            if clave in out:
                break
    if out:
        log(f"  comparación con Eurostat ({ultimo_eurostat}): {len(out)} variantes con estimación archivada")
    return out


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-descarga", action="store_true")
    ap.add_argument("--eurostat-xlsx")
    a = ap.parse_args()

    if not a.sin_descarga:
        log("1. Google Trends")
        descarga_trends()
    for t in TERMINOS:
        if Path(f"gt_cache_{t.replace(' ', '_')}.csv").exists():
            construye_kost(t)
        elif not Path(f"gt2_kost_{t.replace(' ', '_')}.csv").exists():
            raise SystemExit(f"Faltan gt_cache_{t}.csv y gt2_kost_{t}.csv")

    log("2. Eurostat")
    if a.eurostat_xlsx:
        eu = eurostat_xlsx(a.eurostat_xlsx)
    else:
        try:
            eu = eurostat_api()
        except Exception as e:
            raise SystemExit(f"Fallo en la API de Eurostat ({e}). Descarga el Excel y usa --eurostat-xlsx.")
    eu.to_csv("eurostat_es.csv")
    log(f"  último dato: {eu['tasa'].dropna().index[-1]}")

    log("3. Modelo y evaluación")
    datos = calcula(eu)

    html = Path("plantilla_contador.html").read_text(encoding="utf-8")
    Path("contador_desempleo.html").write_text(
        html.replace("__DATA__", json.dumps(datos, separators=(",", ":"))), encoding="utf-8")
    log("4. Hecho: sube contador_desempleo.html al chat para publicarlo.")


if __name__ == "__main__":
    main()
