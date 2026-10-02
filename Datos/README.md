# Replication package / Paquete de replicación

González-Fernández, M. and Sáez Trujillo, F. J. *A high-frequency nowcast of Spanish unemployment using Google searches.*

## Contents / Contenido

| File | Description |
|---|---|
| `une_rt_m__custom_22953631_spreadsheet.xlsx` | Eurostat monthly unemployment for Spain (une_rt_m), data up to July 2026, as used in the paper. |
| `gt2_mensual2004_paro.csv`, `gt2_mensual2004_desempleo.csv` | Google Trends monthly index for Spain, 2004 onwards, each term downloaded on its own scale. |
| `gt2_mensualP_paro.csv`, `gt2_mensualP_desempleo.csv` | Google Trends monthly index for the daily-data period (2010 onwards), used as monthly weights. |
| `gt2_kost_paro.csv`, `gt2_kost_desempleo.csv` | Daily Google Trends data downloaded month by month and calibrated with the Kostopoulos et al. (2020) procedure. |
| `historico_tiempo_real.csv` | Real-time archive: each estimate as computed and published on its date. |
| `actualizar_contador.py` | Code: model estimation, pseudo-real-time evaluation (Table 1) and generation of the website. |
| `plantilla_contador.html` | Website template used by the code. |
| `contador_desempleo_*.xlsx`, `eurostat_paro_espana_*.xlsx` | Published daily, weekly, real-time and Eurostat series (all variants), with a legend sheet describing every column. |

## Reproducing Table 1 / Reproducir la tabla 1

```
pip install pandas numpy statsmodels scipy openpyxl
python actualizar_contador.py --sin-descarga --eurostat-xlsx une_rt_m__custom_22953631_spreadsheet.xlsx
```

The log prints the RMSE and Clark–West p-values of Table 1 (January 2011 – July 2026, 187 months). Google Trends data are not re-downloaded with `--sin-descarga`; new downloads give slightly different values because Google samples searches differently in each download.

Licence: CC BY 4.0.
