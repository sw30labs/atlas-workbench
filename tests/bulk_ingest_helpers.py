"""Synthetic fixture builders for the bulk-ingest tests. Every name is fictional."""

from __future__ import annotations

import json
from pathlib import Path

SERVICES = [
    ("SVC-001", "Card Authorisation Platform (UK)", "Head of Cards (synthetic)", "Tier 1", "4h", "15 mins"),
    ("SVC-002", "Retail Payments Hub", "Head of Payments (synthetic)", "Tier 2", "240 mins", "1h"),
    ("SVC-003", "Mortgage Origination Service", "Head of Lending (synthetic)", "Tier 3", "2 days", ""),
]


class FakeClient:
    """chat_json stand-in; ``responses`` is a list of dicts/exceptions consumed in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.responses:
            return {"records": []}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def write_services_xlsx(path: Path, rows=SERVICES, sheet="Services") -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["Fictional bank service catalogue v3.2 (synthetic)"])
    ws.merge_cells("A1:F1")
    ws.append([])
    ws.append(["Service ID", "Business Service", "Service Owner", "Criticality", "Recovery objectives", None])
    ws.merge_cells("E3:F3")
    ws.append([None, None, None, None, "RTO", "RPO"])
    for row in rows:
        ws.append(list(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_vendors_csv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("Vendor Name;Service Provided;Supported Services;Contract Ref\n"
            "Northwind Cloud Ltd;Cloud hosting;CAP; Retail Payments Hub;CR-100\n"
            "Northwind Cloud Limited;Cloud hosting;Mortgage Origination Service;CR-101\n"
            "Contoso Processing plc;Card processing;Card Auth;CR-102\n")
    # The semicolon-separated service list is quoted so it stays one cell.
    text = text.replace("CAP; Retail Payments Hub", '"CAP| Retail Payments Hub"')
    path.write_text(text, encoding="utf-8")
    return path


def write_controls_csv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Control ID,Control Name,Control Owner,Service,Test Result,Last Tested,Evidence\n"
        "CTL-1,Daily failover check,Ops lead (synthetic),Card Auth,Untested,,\n"
        "CTL-2,Payment reconciliation,Finance lead (synthetic),Retail Payments Hub,Effective,,\n",
        encoding="cp1252")
    return path


def write_losses_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Loss Data"
    ws.append(["Loss ID", "Basel Event Type", "Occurrence Date", "Discovery Date", "Gross Loss (GBP)",
               "Recoveries (GBP)", "Net Loss (GBP)", "Status", "Affected Service", "Description"])
    ws.append(["L-001", "ET7 Execution, Delivery", "03/02/2025", "14/02/2025", "1,200,000", "200,000",
               "1,000,000", "Closed", "CAP", "Duplicate batch posted (synthetic)"])
    ws.append(["L-002", "External fraud", "2025-04-01", "2025-04-03", "£0.5m", "0", "£0.5m", "Open",
               "Retail Payments Hub", "Card testing attack (synthetic)"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_scenarios_csv(path: Path, p99="£180m") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Scenario ID,Scenario Title,Owner,Frequency,P50,P90,P99,Last Workshop,Linked Services\n"
        f"SCN-07,Prolonged card authorisation outage,CRO office (synthetic),1 in 20 years,£20m,£80m,{p99},"
        "12 Mar 2025,CAP\n", encoding="utf-8")
    return path


def write_board_pptx(path: Path, p99="£210m") -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Board risk report (synthetic)"
    shape = slide.shapes.add_table(2, 3, Inches(1), Inches(2), Inches(6), Inches(1))
    for c, text in enumerate(["Scenario", "P99", "Frequency"]):
        shape.table.cell(0, c).text = text
    for c, text in enumerate(["Prolonged card authorisation outage", p99, "1 in 20 years"]):
        shape.table.cell(1, c).text = text
    path.parent.mkdir(parents=True, exist_ok=True)
    deck.save(path)
    return path


def write_actions_docx(path: Path, extra_paragraphs=()) -> Path:
    from docx import Document

    doc = Document()
    doc.add_heading("Audit findings follow-up (synthetic)", level=1)
    doc.add_paragraph("Action AF-1: Document the card authorisation failover runbook. "
                      "Owner: Ops lead (synthetic); Due: 31/03/2026")
    doc.add_paragraph("Action AF-2: Retest payment reconciliation. Owner: Finance lead (synthetic); Due: 2026-06-30")
    for text in extra_paragraphs:
        doc.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def write_pdf(path: Path, lines: list[str]) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in lines:
        pdf.drawString(50, y, line)
        y -= 18
    pdf.showPage()
    pdf.save()
    return path


def write_eml(path: Path, body: str, attachment: bytes | None = None) -> Path:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Vendor Desk <desk@vendor.example>"
    msg["To"] = "resilience@bank.example"
    msg["Subject"] = "Re: hosting incident follow-up (synthetic)"
    msg["Date"] = "Mon, 03 Mar 2025 10:00:00 +0000"
    msg.set_content(body)
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="octet-stream", filename="tool.exe")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(msg))
    return path


def write_taxonomy_json(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": "Risk taxonomy (synthetic)", "nodes": [
        {"code": "R1", "name": "Third-party dependency", "level": 1, "parent": ""},
        {"code": "R1.2", "name": "Cloud vendor outage", "level": 2, "parent": "R1"},
        {"code": "R9", "name": "Canteen hygiene", "level": 1, "parent": ""},
    ]}), encoding="utf-8")
    return path


def write_watchlist_csv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Headline,Organisation,Date,Source URL,Summary,Loss (USD)\n"
        "Example outage at a fictional bank,Example Bank (fictional),2024-05-01,"
        "https://example.com/news/outage,A fictional outage summary written for tests.,USD 12m\n",
        encoding="utf-8")
    return path


def build_mini_pack(root: Path) -> Path:
    write_services_xlsx(root / "01_services" / "service_catalogue_v3.2.xlsx")
    write_vendors_csv(root / "02_vendors" / "vendor_register.csv")
    write_controls_csv(root / "03_controls" / "controls.csv")
    write_losses_xlsx(root / "04_losses" / "internal_loss_data.xlsx")
    write_scenarios_csv(root / "05_scenarios" / "scenario_register.csv")
    write_board_pptx(root / "06_board" / "board_risk_report.pptx")
    write_actions_docx(root / "07_misc" / "audit_actions.docx")
    write_taxonomy_json(root / "05_scenarios" / "risk_taxonomy.json")
    write_watchlist_csv(root / "08_external" / "external_loss_watchlist.csv")
    (root / "07_misc" / "canteen_menu.md").write_text("# Canteen menu\n\nSoup of the day and sandwiches.\n")
    return root


def private_settings(settings, tmp_path: Path):
    """Conftest settings plus an isolated workspace and private organization store."""
    return settings.model_copy(update={"workspace": tmp_path / "ws", "context_enabled": True,
                                       "context_db_path": tmp_path / "private" / "org-context.db"})


def run(settings, root: Path, tmp_path: Path, **kwargs):
    from workbench_atlas.bulk_ingest import run_batch

    kwargs.setdefault("out_dir", tmp_path / "out")
    return run_batch(settings, root, actor="Synthetic operator", **kwargs)


def doc(result, path: str) -> dict:
    return next(d for d in result.documents if d["path"] == path)


def records(result, kind: str, path: str | None = None) -> list[dict]:
    return [c for c in result.candidates if c["kind"] == kind
            and (path is None or any(p["file"] == path for p in c["provenance"]))]
