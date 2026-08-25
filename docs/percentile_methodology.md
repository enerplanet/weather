# COSMO-REA6 Percentile Representative Year Methodology

## 1. Overview

For each of the 824 × 848 spatial cells, this algorithm identifies
which calendar year from the COSMO-REA6 archive best represents the
10th-percentile (P10), median (P50), and 90th-percentile (P90) of the
long-term solar radiation climate. The algorithm works over whatever
years are actually present — the real production run used the full
1995–2019 archive (298 monthly files, not a clean 24-year boundary;
DWD's real coverage stops partway through 2019).

The ranking metric is GHI (Global Horizontal Irradiance), the primary
solar energy resource variable.  Once the representative year is
selected per cell per percentile, **all variables** from that year's
file are carried into the output mosaic — not just GHI.

---

## 2. Why GHI as the Ranking Metric

GHI integrates both the direct-beam and diffuse solar components and
is the single best indicator of PV/solar-thermal yield, building
cooling load from solar gain, and daylight availability.  This aligns
with IEC 61724-1 and the ASHRAE TMY3 methodology, which ranks years
by cumulative monthly global radiation.

---

## 3. Algorithm (per spatial cell)

### 3.1 Inputs

| Input | Shape | Description |
| --- | --- | --- |
| Monthly NetCDF files | real production run: 298 | `COSMO_REA6_YYYY_MM_all_attrs.nc` |
| Analysis period | 1995–2019 | ~24.8 years (real archive; see note above) |
| Spatial grid | 824 × 848 | COSMO-REA6 rotated-pole |
| Ranking metric | daily GHI sum | Summed per calendar day, per cell |

Leap-year days (29 Feb) are removed before any calculation.

### 3.2 Steps

For each cell `(i, j)` and each month `m`:

```text
1. Compute daily total GHI for every day in month m, for all N years.

2. Sum each year's daily totals into one cumulative monthly
   radiation figure:
       total[y,i,j] = sum of year y's daily GHI totals for month m

3. Take the target radiation level for each percentile ACROSS years:
       tgt_P10[i,j] = 10th percentile of total[:,i,j]
       tgt_P50[i,j] = 50th percentile (median)
       tgt_P90[i,j] = 90th percentile

4. Select the year sitting nearest each target level:
       best_P10[i,j] = year  with  min |total[y,i,j] - tgt_P10[i,j]|
       best_P50[i,j] = year  with  min |total[y,i,j] - tgt_P50[i,j]|
       best_P90[i,j] = year  with  min |total[y,i,j] - tgt_P90[i,j]|
```

This ranks candidate years by cumulative monthly global radiation, as
in ASHRAE TMY3 (see section 2).  Because `total` is a real-valued sum
rather than a day count, the selection is effectively tie-free and the
chosen year's brightness rank comes out at approximately the requested
percentile.

### 3.2.1 Superseded selection rule (fixed 2026-08-19)

Until 2026-08-19 steps 2-4 instead pooled every year's daily values
together, took the pooled P10/P50/P90 as **thresholds**, and picked the
year minimising `|fraction of that year's days below the threshold - q|`.

That rule cannot produce the levels section 3.3 promises.  A *typical*
year has ~10% of its days below the pooled P10 — that is what the 10th
percentile means — so `min |cdf_P10 - 0.10|` selected the typical year
and actively rejected genuinely cloudy ones; `min |cdf_P90 - 0.90|`
rejected sunny ones the same way.  All three levels therefore chased
the same target.  Measured against the real 76-year ERA5-Land archive,
the selected years' brightness ranks were 0.273 / 0.240 / 0.268 for
P10 / P50 / P90 instead of 0.10 / 0.50 / 0.90 — P90 was returning
years *cloudier* than the median.

The statistic was also a day count divided by `max_days`, so it could
only take `max_days + 1` distinct values.  With 31-day months and 76
candidate years every cell had ties (median 12-20 years, mean ~42), and
`np.argmin` awarded each tie to whichever year sorted first — handing
the earliest year in the archive 52-59% of all cells at every level.

All three providers shared the defect; all three were fixed together,
so any percentile output generated before 2026-08-19 should be
regenerated.

### 3.3 Physical Interpretation

| Output | GHI level | Interpretation |
| --- | --- | --- |
| **P10** | 10th percentile | Extreme cloudy / low-solar year |
| **P50** | Median | Typical Meteorological Year (TMY) |
| **P90** | 90th percentile | Extreme sunny / high-solar year |

Adjacent cells can and do select **different years** — each cell
optimises independently.

### 3.4 Mosaic Output

Because each cell independently selects its representative year,
the output files are spatial mosaics:

```text
P50 output (8760 h × 824 × 848):
  cell(0,0)     → all variables from year 2007
  cell(0,1)     → all variables from year 2003
  cell(823,847) → all variables from year 2011
  ...
```

The `source_year(rlat, rlon)` variable in each output file records
the origin year for every cell.

---

## 4. Time Axis

All output files use a standard **8760-hour axis** (365 days × 24 h).
Leap-year files (8784 h) have their last 24 h (31 Dec hours 00–23)
truncated to match.

---

## 5. Output Files

36 files total: 12 months × 3 percentile levels.

| Pattern | Percentile | Content |
| --- | --- | --- |
| `cosmo_rea6_p10_MM_all_attrs.nc` | P10 | Extreme low-GHI (cloudy) year |
| `cosmo_rea6_p50_MM_all_attrs.nc` | P50 | Median / typical year |
| `cosmo_rea6_p90_MM_all_attrs.nc` | P90 | Extreme high-GHI (sunny) year |

**Format:** NetCDF-4 / HDF5, zlib compression level 1, float32.  
**Dimensions:** `time=8760, rlat=824, rlon=848`.  
**Variables:** `T`, `GHI`, `DHI`, `WS_10M`, `PS`, `H_SNOW`,
`SNOW_GSP`, `SNOW_CON` (+ `DNI` if present), `source_year`.

---

## 6. Practical Notes

- **Re-runs:** Existing valid output files are skipped automatically.
  Run with `--clean` to remove all output and force a full re-run.
- **Sample size:** N = 24 years gives moderate percentile uncertainty,
  particularly at P10/P90, where the target level is interpolated from
  only two or three bracketing years.  Interpret the tails with
  caution; ERA5-Land's 76-year archive is correspondingly tighter.
- **Time axis:** monthly source files are not guaranteed to share one
  time axis — ERA5-Land's 1950-01 has no 00:00 stamp and so carries 743
  hours starting at 01:00.  The mosaic is sized from the longest axis
  across the winning years and each year is written at its own hour
  offset, so a short year lands in the right slots instead of shifting
  an hour early.
- **GHI-only ranking:** Cells with uniformly low GHI (heavily clouded)
  may show inconsistent temperature or wind rankings relative to the
  selected P-level.  Multi-variable ranking is a planned extension.
