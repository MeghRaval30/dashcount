"""Build config/universe.csv: S&P 500 + Nasdaq-100 members with GICS sector.

Source: Wikipedia constituent tables. Re-run occasionally to refresh.
    uv run python scripts/build_universe.py
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "config" / "universe.csv"
UA = {"User-Agent": "DashCount/0.1 (personal research bot)"}


def tables(url: str) -> list[pd.DataFrame]:
    html = httpx.get(url, headers=UA, timeout=30, follow_redirects=True).text
    return pd.read_html(io.StringIO(html))


def main() -> None:
    sp = next(t for t in tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies") if "Symbol" in t.columns)
    sp = pd.DataFrame({"symbol": sp["Symbol"], "name": sp["Security"], "sector": sp["GICS Sector"], "index": "SP500"})
    ndx_tables = tables("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies")
    ndx = next(t for t in ndx_tables if ("Ticker" in t.columns or "Symbol" in t.columns) and len(t) > 50)
    sym_col = "Ticker" if "Ticker" in ndx.columns else "Symbol"
    sec_col = next((c for c in ndx.columns if "Sector" in str(c) or "Industry" in str(c)), None)
    name_col = next((c for c in ndx.columns if str(c) in ("Company", "Security")), sym_col)
    ndx = pd.DataFrame({
        "symbol": ndx[sym_col], "name": ndx[name_col],
        "sector": ndx[sec_col] if sec_col else "Unknown", "index": "NDX100",
    })
    both = pd.concat([sp, ndx], ignore_index=True)
    both["symbol"] = both["symbol"].str.strip().str.replace(".", "/", regex=False).str.upper()
    # Alpaca uses '.' for share classes (BRK.B); keep that form
    both["symbol"] = both["symbol"].str.replace("/", ".", regex=False)
    agg = both.groupby("symbol", as_index=False).agg(
        name=("name", "first"), sector=("sector", "first"), index=("index", lambda s: "+".join(sorted(set(s))))
    )
    agg = agg.sort_values("symbol")
    OUT.write_text(agg.to_csv(index=False), encoding="utf-8")
    print(f"wrote {len(agg)} symbols to {OUT}")


if __name__ == "__main__":
    main()
