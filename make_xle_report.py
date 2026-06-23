"""Generate XLE RICH Analysis Word document."""
import datetime
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

doc = Document()

# ── Page margins ──────────────────────────────────────────────────────────────
for section in doc.sections:
    section.top_margin    = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin   = Inches(1.2)
    section.right_margin  = Inches(1.2)

BLUE  = RGBColor(0x1F, 0x49, 0x7D)
RED   = RGBColor(0xC0, 0x00, 0x00)
GREY  = RGBColor(0x70, 0x70, 0x70)


# ── Helpers ───────────────────────────────────────────────────────────────────

def heading(text, level=1):
    p = doc.add_heading(text, level=level)
    for run in p.runs:
        run.font.color.rgb = BLUE
    return p


def body(text, size=11):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(size)
    return p


def bullet(text):
    p = doc.add_paragraph(style="List Bullet")
    run = p.add_run(text)
    run.font.size = Pt(11)
    return p


def add_table(headers, rows):
    t = doc.add_table(rows=1 + len(rows), cols=len(headers))
    t.style = "Light List Accent 1"
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = h
        for para in cell.paragraphs:
            for run in para.runs:
                run.bold = True
                run.font.size = Pt(10)
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = t.rows[ri + 1].cells[ci]
            cell.text = str(val)
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(10)
    return t


def hline():
    p = doc.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "1F497D")
    pBdr.append(bottom)
    pPr.append(pBdr)
    return p


# ══════════════════════════════════════════════════════════════════════════════
# TITLE
# ══════════════════════════════════════════════════════════════════════════════
t = doc.add_heading("XLE — Energy Select Sector SPDR ETF", 0)
t.alignment = WD_ALIGN_PARAGRAPH.CENTER
for run in t.runs:
    run.font.color.rgb = BLUE

sub = doc.add_paragraph("RICH Framework Analysis  |  As of 2026-05-12")
sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
for run in sub.runs:
    run.font.size = Pt(11)
    run.italic = True
    run.font.color.rgb = GREY

hline()
doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# 1. IMMEDIATE VERDICT
# ══════════════════════════════════════════════════════════════════════════════
heading("1. Immediate Verdict from RICH")
body(
    "RICH would not score XLE at all. Two hard gates eliminate it before the "
    "model even starts. An ETF is a basket of stocks — scoring a basket as if "
    "it were a company produces meaningless numbers. RICH's edge comes from "
    "earnings quality signals that do not exist for ETFs."
)
doc.add_paragraph()

add_table(
    ["Hard Gate", "Result"],
    [
        ["Sector exclusion",
         'FMP classifies XLE as "Financial Services" → excluded in universe.py'],
        ["Financial statements",
         "0 rows from income statement, balance sheet, cash flow, ratios, key metrics"],
        ["ETF flag",
         "isEtf: true — no earnings, no Beneish M-Score, no Piotroski F-Score possible"],
    ],
)
doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# 2. PRICE SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════════
heading("2. Price Snapshot  (FMP data, 2026-05-12)")
hline()
add_table(
    ["Metric", "Value"],
    [
        ["Current price",             "$57.57"],
        ["52-week range",             "$40.36 – $63.46"],
        ["Position in 52w range",     "74%  (upper third)"],
        ["vs 50-day avg ($57.96)",    "-0.7%  — just below"],
        ["vs 200-day avg ($49.04)",   "+17.4%  — extended"],
        ["Drawdown from 52w high",    "-9.3%"],
        ["Upside to 52w high",        "+10.2%"],
        ["Return 1 week",             "+1.0%"],
        ["Return 1 month",            "+0.8%"],
        ["Return 3 months",           "+7.5%"],
        ["Avg daily volume (5d)",     "42.0M shares"],
        ["Avg daily volume (21d)",    "41.3M shares"],
        ["Last dividend",             "$1.49 / share  (~2.6% yield)"],
    ],
)
doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# 3. RICH PRINCIPLES APPLIED
# ══════════════════════════════════════════════════════════════════════════════
heading("3. RICH Principles Applied to XLE")
hline()

heading("Quality Sleeve — N/A quantitatively", level=2)
body(
    "XLE holds the S&P 500 energy names (XOM ~23%, CVX ~18%, COP, EOG, SLB, etc.). "
    "These individually have strong ROE and FCF, but XLE blends them with no earnings "
    "quality filter — you get the good and the mediocre together. Piotroski F-Score "
    "and Beneish M-Score cannot be computed on a basket."
)
doc.add_paragraph()

heading("Growth Sleeve — Driven by oil, not earnings", level=2)
body(
    "XLE's revenue growth is crude oil price x production volume. It has nothing "
    "to do with the compounding EPS and FCF growth RICH looks for. The 3-month +7.5% "
    "gain came from oil price recovery after the April tariff shock, not from "
    "fundamental improvement. That bounce is largely already in the price."
)
doc.add_paragraph()

heading("Value/Risk Sleeve — Not cheap", level=2)
body(
    "XLE is sitting 17.4% above its 200-day moving average. RICH penalises stocks "
    "trading above their own historical valuation baseline. Key observations:"
)
bullet(
    "The 52w low of $40.36 was hit in April (tariff panic). Recovery to $57.57 is "
    "+42.6% from that low — the margin of safety has already compressed significantly."
)
bullet(
    "The 50-day average at $57.96 is acting as resistance — XLE has failed to close "
    "above it on three consecutive sessions."
)
bullet(
    "Volume is average, not expanding on up days — no institutional conviction in the rally."
)
doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# 4. BUYING RECOMMENDATION
# ══════════════════════════════════════════════════════════════════════════════
heading("4. Buying Recommendation")
hline()

verdict = doc.add_paragraph()
r = verdict.add_run("AVOID at current price ($57.57).  Watch for pullback to $52–54.")
r.bold = True
r.font.size = Pt(13)
r.font.color.rgb = RED

doc.add_paragraph()
add_table(
    ["Factor", "Assessment"],
    [
        ["RICH methodology compatibility",
         "None — wrong instrument type (ETF, no financials)"],
        ["Value",
         "Extended (+17% above 200d avg), upper 74% of 52w range"],
        ["Momentum",
         "Stalling — three closes below 50d avg ($57.96)"],
        ["Risk / Reward at $57.57",
         "Upside to 52w high = +10.2%;  downside to 200d avg = -14.8%"],
        ["Macro dependency",
         "Entirely driven by crude oil — RICH avoids macro bets"],
    ],
)
doc.add_paragraph()

heading("Better entry level", level=2)
body(
    "A pullback toward $52–53 is where the risk/reward flips to roughly 2:1 in "
    "your favour (upside to 52w high ~+23%, downside to 200d avg ~-6%). "
    "At $57.57 today the ratio is approximately 1:1.5 against you."
)
doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# 5. ENERGY EXPOSURE THE RICH WAY
# ══════════════════════════════════════════════════════════════════════════════
heading("5. Energy Exposure the RICH Way")
hline()
body(
    "Run the top XLE holdings individually through the RICH screener. RICH can score "
    "their earnings quality, FCF compounding, Beneish manipulation risk, and Piotroski "
    "financial strength — the analysis that actually differentiates a good energy "
    "company from a mediocre one."
)
doc.add_paragraph()
add_table(
    ["Ticker", "Company", "XLE Weight (approx)", "RICH-eligible?"],
    [
        ["XOM", "Exxon Mobil",        "~23%", "Yes — run through RICH"],
        ["CVX", "Chevron",            "~18%", "Yes — run through RICH"],
        ["COP", "ConocoPhillips",     "~8%",  "Yes — run through RICH"],
        ["EOG", "EOG Resources",      "~6%",  "Yes — run through RICH"],
        ["SLB", "SLB (Schlumberger)", "~5%",  "Yes — run through RICH"],
    ],
)
doc.add_paragraph()

# ══════════════════════════════════════════════════════════════════════════════
# FOOTER
# ══════════════════════════════════════════════════════════════════════════════
hline()
footer_p = doc.add_paragraph()
footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
fr = footer_p.add_run(
    "Generated by RICH  |  Data source: Financial Modeling Prep API  |  "
    + datetime.date.today().strftime("%Y-%m-%d")
)
fr.font.size = Pt(9)
fr.italic = True
fr.font.color.rgb = GREY

# ── Save ──────────────────────────────────────────────────────────────────────
out = r"C:\Users\chaim\Desktop\XLE_RICH_Analysis.docx"
doc.save(out)
print("Saved:", out)
