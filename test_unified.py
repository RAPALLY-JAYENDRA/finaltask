import sys
import os
import json

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

print("=== 1. Testing Service Catalog & Vector Embeddings ===")
from service_catalog import catalog
print(f"Loaded sectors count: {len(catalog.sectors)}")
assert len(catalog.sectors) == 462, "Catalog should have 462 canonical offerings!"
print("Service Catalog vector matrix shape:", catalog.vectors.shape)

# Test candidate ranking
sample_ranked = catalog.rank_candidates("battery storage interconnect ERCOT BESS", top_k=3)
print(f"Top matches for battery storage: {[c.canonical_name for c in sample_ranked]}")
assert len(sample_ranked) > 0, "Should return ranked candidates"
print("[PASS] Service Catalog Test Passed!")

print("\n=== 2. Testing Email & Field Validator ===")
from validator import validate_email
valid_res = validate_email("test@google.com")
print(f"Validation for test@google.com: {valid_res}")
assert valid_res in ["Valid", "N/A"], "Valid domain check failed"
print("[PASS] Validator Test Passed!")

print("\n=== 3. Testing Database CRUD ===")
from database import init_db, insert_lead, get_all_leads, delete_lead
init_db()

dummy_lead = {
    "name": "Integration Test Executive",
    "email": "test_exec@enterprise.com",
    "phone": "+1 555-0199",
    "company": "Enterprise Global",
    "country": "United States",
    "email_validity": "Valid",
    "professional_summary": "Chief Technology Officer leading infrastructure modernization.",
    "company_profile": "Global enterprise provider of digital solutions.",
    "buying_role": "Decision Maker",
    "use_case": "Cloud & Edge Migration",
    "budget": "$100K - $500K",
    "timeline": "Immediate (30 days)",
    "referred_product": "Data Center Energy Solutions",
    "message": "Interested in power efficiency reports for colocation sites."
}

insert_lead(dummy_lead)
all_leads = get_all_leads()
found = any(l.get("email") == "test_exec@enterprise.com" for l in all_leads)
assert found, "Lead should be inserted into database"
print(f"Total leads in database: {len(all_leads)}")
print("[PASS] Database Test Passed!")

print("\n=== 4. Testing PDF Report Generation ===")
from pdf_generator import generate_lead_pdf
test_pdf_path = "test_dossier.pdf"

test_dossier_data = {
    **dummy_lead,
    "projects": {
        "delivered_projects": [
            {
                "project_name": "500MW Hyperscale Power Substation",
                "client_partner": "Cloud Tier-1 Provider",
                "details": "High voltage interconnection and switchyard delivery.",
                "evidence_quote": "Delivered ahead of schedule in Q3.",
                "source_url": "https://example.com/project-1"
            }
        ],
        "active_operations": [
            {
                "operation_name": "Regional Data Hub Operations",
                "scope": "12 facilities across North America",
                "details": "24/7 uptime monitoring with liquid cooling retrofits."
            }
        ],
        "future_roadmaps": [
            {
                "initiative_name": "Net-Zero Microgrid Deployment",
                "target_timeline": "2027-2028",
                "strategic_focus": "Hydrogen fuel cell backup and BESS integration."
            }
        ]
    },
    "strategic_offerings": [
        {
            "product_name": "Data Center Infrastructure & Power Intelligence",
            "vector_cosine": 0.885,
            "confidence": "High",
            "relevance_summary": "Direct alignment with facility capex tracking and power substation dockets."
        }
    ],
    "sales_strategy": {
        "pitch_hook": "Accelerate your 500MW substation roadmap with verified stage-gate capex dockets.",
        "email_draft": "Hi Integration Test Executive,\n\nI noticed Enterprise Global's recent expansion in regional hubs..."
    }
}

generate_lead_pdf(test_dossier_data, test_pdf_path)
assert os.path.exists(test_pdf_path), "PDF file should be created"
print(f"Generated PDF size: {os.path.getsize(test_pdf_path)} bytes")
print("[PASS] PDF Generation Test Passed!")

print("\n=== 5. Testing Lead Enrichment Pipeline Integration ===")
from enricher import enrich_lead_dossier
sample_dossier = enrich_lead_dossier(
    lead_input={
        "name": "Jane Doe",
        "email": "jane@vertiv.com",
        "company": "Vertiv",
        "website": "https://www.vertiv.com",
        "interests": "Thermal Management & Liquid Cooling",
        "message": "Seeking data center cooling infrastructure reports and supplier dockets."
    }
)

assert sample_dossier["name"] == "Jane Doe"
assert sample_dossier["company"] == "Vertiv"
assert "strategic_offerings" in sample_dossier
print(f"Generated dossier keys: {list(sample_dossier.keys())}")
print(f"Top matched offering: {sample_dossier['strategic_offerings'][0]['product_name'] if sample_dossier['strategic_offerings'] else 'None'}")
print("[PASS] Lead Enrichment Pipeline Test Passed!")

print("\nALL UNIFIED SYSTEM TESTS PASSED SUCCESSFULLY!")
