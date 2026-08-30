import os
import re
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_CENTER
from reportlab.pdfgen import canvas

def md_to_html(text: str) -> str:
    """Converts markdown to HTML with strict XML escaping for safe ReportLab paragraph parsing."""
    if not text:
        return ""
    
    text = str(text)
    # 1. XML Escape special control characters
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    
    # 2. Convert links: [label](url) -> <a href="\2" color="#2563EB"><b>\1</b></a>
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" color="#2563EB"><b>\1</b></a>', text)
    
    # 3. Convert bold: **text** -> <b>text</b>
    parts = text.split("**")
    result = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            result.append(f"<b>{part}</b>")
        else:
            result.append(part)
    return "".join(result)

# Color Palette
PRIMARY_COLOR = colors.HexColor("#0B1938")    # Obsidian navy
SECONDARY_COLOR = colors.HexColor("#2563EB")  # Vivid cobalt blue
ACCENT_COLOR = colors.HexColor("#0D9488")     # Teal accent
TEXT_COLOR = colors.HexColor("#1E293B")       # Slate text
BG_LIGHT = colors.HexColor("#F8FAFC")         # Clean slate off-white
BG_CARD = colors.HexColor("#F1F5F9")          # Slate card
BORDER_COLOR = colors.HexColor("#CBD5E1")     # Clean border

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_elements(num_pages)
            super().showPage()
        super().save()

    def draw_page_elements(self, page_count):
        self.saveState()
        
        # Header on later pages
        if self._pageNumber > 1:
            self.setFont("Helvetica-Bold", 8)
            self.setFillColor(SECONDARY_COLOR)
            self.drawString(54, 750, "EXECUTIVE LEAD INTELLIGENCE & STRATEGIC MATCH DOSSIER")
            self.setFont("Helvetica", 8)
            self.setFillColor(TEXT_COLOR)
            self.drawRightString(letter[0] - 54, 750, datetime.now().strftime("%B %d, %Y"))
            self.setStrokeColor(BORDER_COLOR)
            self.setLineWidth(0.5)
            self.line(54, 742, letter[0] - 54, 742)

        # Footer on all pages
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(SECONDARY_COLOR)
        self.drawString(54, 38, "CONFIDENTIAL & PROPRIETARY")
        
        self.setFont("Helvetica", 8)
        self.setFillColor(TEXT_COLOR)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.drawString(200, 38, f"Generated: {timestamp}")
        
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(letter[0] - 54, 38, page_text)
        
        self.setStrokeColor(BORDER_COLOR)
        self.setLineWidth(0.5)
        self.line(54, 48, letter[0] - 54, 48)
        
        self.restoreState()

def safe_cell_text(val: str, max_chars: int = 250) -> str:
    if not val:
        return "N/A"
    s = str(val).strip()
    if not s or s.upper() == "N/A":
        return "N/A"
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "..."
    return md_to_html(s)

def generate_lead_pdf(data: dict, filepath: str):
    """
    Generates an executive-level PDF dossier combining contact intelligence,
    company analysis, delivered/active/future projects, and vector-matched offerings.
    """
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=64,
        bottomMargin=64
    )

    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=PRIMARY_COLOR,
        spaceAfter=3
    )

    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=SECONDARY_COLOR,
        spaceAfter=12
    )

    section_heading = ParagraphStyle(
        'SectionHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=PRIMARY_COLOR,
        spaceBefore=10,
        spaceAfter=6,
        keepWithNext=True
    )

    sub_heading = ParagraphStyle(
        'SubHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=SECONDARY_COLOR,
        spaceBefore=6,
        spaceAfter=4,
        keepWithNext=True
    )

    label_style = ParagraphStyle(
        'GridLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=SECONDARY_COLOR
    )

    value_style = ParagraphStyle(
        'GridValue',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=TEXT_COLOR
    )

    body_style = ParagraphStyle(
        'BodyTextCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=TEXT_COLOR,
        alignment=TA_LEFT
    )

    quote_style = ParagraphStyle(
        'QuoteStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155")
    )

    story = []

    # 1. Header
    lead_name = data.get('name') or data.get('lead_name') or "Executive Contact"
    company_name = data.get('company') or data.get('company_name') or "Target Enterprise"
    
    story.append(Paragraph("Strategic Lead Intelligence & Offering Dossier", title_style))
    story.append(Paragraph(f"<b>Target:</b> {lead_name} &bull; <b>Enterprise:</b> {company_name}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=SECONDARY_COLOR, spaceAfter=8))

    # 2. Key Details Grid
    story.append(Paragraph("1. Core Contact & Inbound Profile", section_heading))
    grid_data = [
        [
            Paragraph("Lead Name", label_style),
            Paragraph(safe_cell_text(data.get('name') or data.get('lead_name')), value_style),
            Paragraph("Company", label_style),
            Paragraph(safe_cell_text(data.get('company') or data.get('company_name')), value_style)
        ],
        [
            Paragraph("Email", label_style),
            Paragraph(safe_cell_text(data.get('email') or data.get('lead_email')), value_style),
            Paragraph("Country", label_style),
            Paragraph(safe_cell_text(data.get('country')), value_style)
        ],
        [
            Paragraph("Phone", label_style),
            Paragraph(safe_cell_text(data.get('phone')), value_style),
            Paragraph("Email Validity", label_style),
            Paragraph(safe_cell_text(data.get('email_validity')), value_style)
        ],
        [
            Paragraph("LinkedIn URL", label_style),
            Paragraph(safe_cell_text(data.get('linkedin_url')), value_style),
            Paragraph("Buying Role", label_style),
            Paragraph(safe_cell_text(data.get('buying_role')), value_style)
        ],
        [
            Paragraph("Referred Product", label_style),
            Paragraph(safe_cell_text(data.get('referred_product') or (data.get('lead_intent', {}) if isinstance(data.get('lead_intent'), dict) else {}).get('referred_product_or_service')), value_style),
            Paragraph("Use Case", label_style),
            Paragraph(safe_cell_text(data.get('use_case')), value_style)
        ]
    ]

    t = Table(grid_data, colWidths=[90, 160, 90, 164])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_LIGHT),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('PADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))

    # 3. Executive Professional Summary
    summary = data.get('professional_summary') or data.get('summary')
    if summary and summary.strip().upper() != "N/A":
        story.append(Paragraph("2. Executive Summary & Senior Analysis", section_heading))
        story.append(Paragraph(md_to_html(summary), body_style))
        story.append(Spacer(1, 8))

    # 4. Work Experience & Background
    exp = data.get('work_experience') or data.get('experience')
    if exp:
        exp_text = ""
        if isinstance(exp, str) and exp.strip().upper() != "N/A":
            exp_text = exp
        elif isinstance(exp, list):
            items = []
            for e in exp:
                if isinstance(e, dict):
                    t_val = e.get('title', '')
                    c_val = e.get('company', '')
                    p_val = e.get('period', '')
                    d_val = e.get('description', '')
                    line = f"&bull; <b>{t_val}</b> at <b>{c_val}</b> ({p_val})" if p_val and p_val != "N/A" else f"&bull; <b>{t_val}</b> at <b>{c_val}</b>"
                    if d_val and d_val != "N/A":
                        line += f"<br/>&nbsp;&nbsp;<i>{d_val}</i>"
                    items.append(line)
            exp_text = "<br/>".join(items)
        if exp_text:
            story.append(Paragraph("3. Professional Background & Track Record", section_heading))
            story.append(Paragraph(md_to_html(exp_text), body_style))
            story.append(Spacer(1, 8))

    # 5. Company Intelligence & Web Signals
    comp_prof = data.get('company_profile')
    if comp_prof and comp_prof.strip().upper() != "N/A":
        story.append(Paragraph("4. Enterprise Intelligence & Operational Profile", section_heading))
        story.append(Paragraph(md_to_html(comp_prof), body_style))
        story.append(Spacer(1, 8))

    # 6. Strategic Projects & Operational Pipeline
    raw_projects = data.get('projects') or {}
    if isinstance(raw_projects, dict) and any(raw_projects.values()):
        story.append(Paragraph("5. Verified Projects & Operational Initiatives", section_heading))
        
        # Delivered Projects
        deliv = raw_projects.get('delivered_projects') or []
        if deliv:
            story.append(Paragraph("Delivered Projects & Completed Deployments", sub_heading))
            for p in deliv[:4]:
                p_name = p.get('project_name') or p.get('name') or "Project"
                p_client = p.get('client_partner') or p.get('client') or ""
                p_details = p.get('details') or p.get('description') or ""
                p_quote = p.get('evidence_quote') or ""
                p_line = f"&bull; <b>{p_name}</b>"
                if p_client and p_client != "N/A":
                    p_line += f" | Partner: <i>{p_client}</i>"
                if p_details:
                    p_line += f"<br/>&nbsp;&nbsp;{p_details}"
                if p_quote:
                    p_line += f"<br/>&nbsp;&nbsp;<i>Evidence: \"{p_quote[:160]}...\"</i>"
                story.append(Paragraph(md_to_html(p_line), body_style))
                story.append(Spacer(1, 3))
        
        # Active Operations
        active = raw_projects.get('active_operations') or []
        if active:
            story.append(Spacer(1, 4))
            story.append(Paragraph("Active Operations & Ongoing Programs", sub_heading))
            for op in active[:4]:
                op_name = op.get('operation_name') or op.get('name') or "Operation"
                op_details = op.get('details') or op.get('scope') or ""
                op_line = f"&bull; <b>{op_name}</b>: {op_details}"
                story.append(Paragraph(md_to_html(op_line), body_style))
                story.append(Spacer(1, 3))

        # Future Pipeline
        future = raw_projects.get('future_roadmaps') or []
        if future:
            story.append(Spacer(1, 4))
            story.append(Paragraph("Future Strategic Roadmap & Target Initiatives", sub_heading))
            for f_item in future[:4]:
                f_name = f_item.get('initiative_name') or f_item.get('name') or "Initiative"
                f_details = f_item.get('strategic_focus') or f_item.get('details') or ""
                f_line = f"&bull; <b>{f_name}</b>: {f_details}"
                story.append(Paragraph(md_to_html(f_line), body_style))
                story.append(Spacer(1, 3))
        story.append(Spacer(1, 8))

    # 7. Strategic Offerings Match (462-Catalog Vector Matching)
    offerings = data.get('strategic_offerings') or []
    if not offerings and isinstance(data.get('lead_intent'), dict):
        offerings = data.get('lead_intent', {}).get('matched_offerings', [])

    if offerings:
        story.append(Paragraph("6. Tailored Offering Matches (1024-Dim Vector Matcher)", section_heading))
        off_table_data = [
            [
                Paragraph("<b>Matched Offering</b>", label_style),
                Paragraph("<b>Match Score / Confidence</b>", label_style),
                Paragraph("<b>Relevance & Fit Rationale</b>", label_style)
            ]
        ]
        for off in offerings[:5]:
            p_name = off.get('offering_name') or off.get('product_name') or off.get('canonical_name') or "Strategic Service"
            score = off.get('vector_cosine') or off.get('final_score') or 0.0
            conf = off.get('confidence') or ("High" if score > 0.6 else "Medium")
            score_str = f"{score:.3f} ({conf})" if isinstance(score, float) and score > 0 else f"{conf}"
            rel = off.get('relevance_summary') or off.get('definition') or "Aligned with operational requirements."
            off_table_data.append([
                Paragraph(f"<b>{p_name}</b>", value_style),
                Paragraph(score_str, value_style),
                Paragraph(safe_cell_text(rel, 180), value_style)
            ])
        
        ot = Table(off_table_data, colWidths=[150, 110, 244])
        ot.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), BG_CARD),
            ('GRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ('PADDING', (0, 0), (-1, -1), 4),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(ot)
        story.append(Spacer(1, 8))

    # 8. Sales Pitch & Strategic Playbook
    pitch = data.get('sales_pitch_hook') or (data.get('lead_intent', {}) if isinstance(data.get('lead_intent'), dict) else {}).get('sales_pitch_hook')
    sales_strat = data.get('sales_strategy') or {}
    if pitch or sales_strat:
        story.append(Paragraph("7. Strategic Outreach Playbook & Pitch Hook", section_heading))
        if pitch:
            story.append(Paragraph("<b>Tailored Sales Pitch Hook:</b>", sub_heading))
            story.append(Paragraph(md_to_html(pitch), body_style))
            story.append(Spacer(1, 4))
        if isinstance(sales_strat, dict) and sales_strat.get('email_draft'):
            story.append(Paragraph("<b>Personalized Executive Email Draft:</b>", sub_heading))
            story.append(Paragraph(md_to_html(sales_strat.get('email_draft')), quote_style))

    doc.build(story, canvasmaker=NumberedCanvas)
    return filepath
