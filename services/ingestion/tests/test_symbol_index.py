"""Heading-only API pages should index useful identifiers."""
from services.ingestion.doc_chunker import DocChunker
from services.ingestion.identifiers import extract_code_identifiers
from services.ingestion.models import index_text_for_chunk


def test_reference_heading_indexes_terraform_resource_without_code():
    doc = "# Resources\n\n## aws_instance\n\nLaunch an EC2 instance."
    chunks = DocChunker(token_len=lambda value: len(value.split())).chunk_page(
        "terraform-aws", "6.0.0", "https://docs.example/resource", "hash", doc
    )
    relevant = [c for c in chunks if "aws_instance" in c.headings]
    assert relevant
    assert "aws_instance" in relevant[0].identifiers
    assert "aws_instance" in index_text_for_chunk(
        relevant[0].text, relevant[0].headings, relevant[0].identifiers
    )


def test_backtick_reference_identifier_without_code():
    extracted = extract_code_identifiers("Use `Client.fetch` to retrieve a page.")
    assert "Client.fetch" in extracted
