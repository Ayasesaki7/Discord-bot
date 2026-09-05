from __future__ import annotations

import io
import unittest

from chat.pdf_parser import PdfDocumentParser, PdfParseError


def make_text_pdf(page_texts: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(420, 595))
    for page_text in page_texts:
        document.drawString(40, 540, page_text)
        document.showPage()
    document.save()
    return output.getvalue()


class PdfDocumentParserTests(unittest.TestCase):
    def test_extracts_page_labelled_text_and_renders_representative_pages(self) -> None:
        parser = PdfDocumentParser(
            max_bytes=2 * 1024 * 1024,
            max_pages=10,
            max_text_chars=20_000,
            rendered_page_limit=2,
        )

        result = parser.parse_bytes(
            make_text_pdf(["alpha page", "middle page", "omega page"]),
            filename="notes.pdf",
        )

        self.assertEqual(result.filename, "notes.pdf")
        self.assertEqual(result.page_count, 3)
        self.assertEqual(result.extracted_page_count, 3)
        self.assertIn("[PDF 第 1 页]", result.text)
        self.assertIn("alpha page", result.text)
        self.assertIn("[PDF 第 3 页]", result.text)
        self.assertIn("omega page", result.text)
        self.assertEqual(
            [page.page_number for page in result.rendered_pages],
            [1, 3],
        )
        self.assertTrue(all(page.data for page in result.rendered_pages))

    def test_long_document_is_bounded_but_keeps_multiple_page_labels(self) -> None:
        parser = PdfDocumentParser(
            max_bytes=2 * 1024 * 1024,
            max_pages=10,
            max_text_chars=4_000,
            rendered_page_limit=0,
        )

        result = parser.parse_bytes(
            make_text_pdf([f"page-{index} " + "x" * 3000 for index in range(1, 5)]),
            filename="large.pdf",
        )

        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text), parser.max_text_chars)
        self.assertIn("[PDF 第 1 页]", result.text)
        self.assertIn("[PDF 第 4 页]", result.text)

    def test_password_protected_pdf_is_rejected_without_leaking_content(self) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=420, height=595)
        writer.encrypt("secret-password")
        encrypted = io.BytesIO()
        writer.write(encrypted)
        parser = PdfDocumentParser(rendered_page_limit=0)

        with self.assertRaisesRegex(PdfParseError, "password-protected"):
            parser.parse_bytes(encrypted.getvalue(), filename="protected.pdf")

    def test_pdf_detection_accepts_mime_or_case_insensitive_suffix(self) -> None:
        self.assertTrue(PdfDocumentParser.is_pdf("document.bin", "application/pdf"))
        self.assertTrue(PdfDocumentParser.is_pdf("REPORT.PDF", None))
        self.assertFalse(PdfDocumentParser.is_pdf("photo.png", "image/png"))


class PdfAttachmentDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_original_cdn_instead_of_pdf_proxy_cache(self) -> None:
        pdf_bytes = make_text_pdf(["Original CDN PDF text"])

        class Attachment:
            filename = "resume.pdf"
            content_type = "application/pdf"
            size = len(pdf_bytes)

            def __init__(self) -> None:
                self.calls: list[bool] = []

            async def read(self, *, use_cached: bool) -> bytes:
                self.calls.append(use_cached)
                if use_cached:
                    raise RuntimeError("HTTP 415 from proxy cache")
                return pdf_bytes

        attachment = Attachment()
        parser = PdfDocumentParser(rendered_page_limit=0)

        result = await parser.parse_attachment(attachment)

        self.assertEqual(attachment.calls, [False])
        self.assertIn("Original CDN PDF text", result.text)

    async def test_falls_back_to_proxy_when_original_cdn_fails(self) -> None:
        pdf_bytes = make_text_pdf(["Cached PDF text"])

        class Attachment:
            filename = "resume.pdf"
            content_type = "application/pdf"
            size = len(pdf_bytes)

            def __init__(self) -> None:
                self.calls: list[bool] = []

            async def read(self, *, use_cached: bool) -> bytes:
                self.calls.append(use_cached)
                if not use_cached:
                    raise RuntimeError("original CDN expired")
                return pdf_bytes

        attachment = Attachment()
        parser = PdfDocumentParser(rendered_page_limit=0)

        result = await parser.parse_attachment(attachment)

        self.assertEqual(attachment.calls, [False, True])
        self.assertIn("Cached PDF text", result.text)

    async def test_both_cdn_failures_are_reported_as_pdf_parse_error(self) -> None:
        class Attachment:
            filename = "resume.pdf"
            content_type = "application/pdf"
            size = 1024

            async def read(self, *, use_cached: bool) -> bytes:
                del use_cached
                raise RuntimeError("CDN unavailable")

        parser = PdfDocumentParser(rendered_page_limit=0)

        with self.assertRaisesRegex(PdfParseError, "both Discord CDN endpoints"):
            await parser.parse_attachment(Attachment())


if __name__ == "__main__":
    unittest.main()
