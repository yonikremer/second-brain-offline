from pathlib import Path

STAGES = ["docling", "filtering", "translation", "subdomain", "doc_type", "truthness"]

STAGE_INSTRUCTIONS = {
    "docling": None,
    "filtering": None,
    "translation": Path("instructions/translation.md"),
    "subdomain": Path("instructions/subdomains.md"),
    "doc_type": Path("instructions/document_types.md"),
    "truthness": Path("instructions/truthness.md"),
}
