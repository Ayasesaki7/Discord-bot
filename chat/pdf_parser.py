from __future__ import annotations

import asyncio
import io
import os
import re
from dataclasses import dataclass, field


PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_MAX_PAGES = 120
DEFAULT_MAX_TEXT_CHARS = 80_000
DEFAULT_RENDERED_PAGES = 3
DEFAULT_PARSE_TIMEOUT_SECONDS = 30.0


class PdfParseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PdfRenderedPage:
    page_number: int
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class PdfParseResult:
    filename: str
    page_count: int
    text: str
    extracted_page_count: int
    rendered_pages: tuple[PdfRenderedPage, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class PdfDocumentParser:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_pages: int = DEFAULT_MAX_PAGES
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    rendered_page_limit: int = DEFAULT_RENDERED_PAGES
    timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS
    render_max_side: int = 1600
    _render_available: bool = field(default=True, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "PdfDocumentParser":
        return cls(
            max_bytes=_bounded_env_int(
                "CHAT_PDF_MAX_BYTES",
                DEFAULT_MAX_BYTES,
                minimum=1024 * 1024,
                maximum=25 * 1024 * 1024,
            ),
            max_pages=_bounded_env_int(
                "CHAT_PDF_MAX_PAGES",
                DEFAULT_MAX_PAGES,
                minimum=1,
                maximum=500,
            ),
            max_text_chars=_bounded_env_int(
                "CHAT_PDF_MAX_TEXT_CHARS",
                DEFAULT_MAX_TEXT_CHARS,
                minimum=4_000,
                maximum=200_000,
            ),
            rendered_page_limit=_bounded_env_int(
                "CHAT_PDF_RENDERED_PAGES",
                DEFAULT_RENDERED_PAGES,
                minimum=0,
                maximum=6,
            ),
            timeout_seconds=_bounded_env_float(
                "CHAT_PDF_PARSE_TIMEOUT",
                DEFAULT_PARSE_TIMEOUT_SECONDS,
                minimum=5.0,
                maximum=120.0,
            ),
        )

    @staticmethod
    def is_pdf(filename: str, content_type: str | None) -> bool:
        normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
        return normalized_type in PDF_MIME_TYPES or filename.strip().casefold().endswith(".pdf")

    async def parse_attachment(self, attachment: object) -> PdfParseResult:
        filename = str(getattr(attachment, "filename", "document.pdf") or "document.pdf")
        content_type = getattr(attachment, "content_type", None)
        if not self.is_pdf(filename, str(content_type or "")):
            raise PdfParseError("attachment is not a PDF")
        declared_size = int(getattr(attachment, "size", 0) or 0)
        if declared_size > self.max_bytes:
            raise PdfParseError(
                f"PDF exceeds the {self.max_bytes}-byte parsing limit"
            )
        raw_bytes = await self._read_attachment_bytes(attachment)
        if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
            raise PdfParseError("PDF attachment was empty")
        if len(raw_bytes) > self.max_bytes:
            raise PdfParseError(
                f"PDF exceeds the {self.max_bytes}-byte parsing limit"
            )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await asyncio.to_thread(
                    self.parse_bytes,
                    bytes(raw_bytes),
                    filename=filename,
                )
        except TimeoutError as exc:
            raise PdfParseError(
                f"PDF parsing timed out after {self.timeout_seconds:g} seconds"
            ) from exc

    @staticmethod
    async def _read_attachment_bytes(attachment: object) -> object:
        """Read a Discord attachment without depending on one CDN endpoint.

        ``use_cached=True`` selects Discord's proxy URL.  That is useful after
        a source message is deleted, but the proxy can return HTTP 415 for PDF
        assets even while the original attachment URL is healthy.  Prefer the
        original CDN for a live message and retain the proxy as a fallback.
        """

        read = getattr(attachment, "read", None)
        if not callable(read):
            raise PdfParseError("PDF attachment cannot be downloaded")

        failures: list[BaseException] = []
        for use_cached in (False, True):
            try:
                return await read(use_cached=use_cached)
            except TypeError:
                # Compatibility with attachment-like test/adaptor objects that
                # predate discord.py's use_cached keyword.
                try:
                    return await read()
                except Exception as exc:
                    failures.append(exc)
                    break
            except Exception as exc:
                failures.append(exc)

        raise PdfParseError(
            "PDF attachment download failed from both Discord CDN endpoints"
        ) from (failures[-1] if failures else None)

    def parse_bytes(self, raw_bytes: bytes, *, filename: str = "document.pdf") -> PdfParseResult:
        if not raw_bytes:
            raise PdfParseError("PDF was empty")
        if len(raw_bytes) > self.max_bytes:
            raise PdfParseError(
                f"PDF exceeds the {self.max_bytes}-byte parsing limit"
            )
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise PdfParseError("pypdf is required for PDF text extraction") from exc

        try:
            reader = PdfReader(io.BytesIO(raw_bytes), strict=False)
        except Exception as exc:
            raise PdfParseError("PDF structure could not be opened") from exc

        if reader.is_encrypted:
            try:
                unlocked = bool(reader.decrypt(""))
            except Exception:
                unlocked = False
            if not unlocked:
                raise PdfParseError("password-protected PDFs are not supported")

        try:
            page_count = len(reader.pages)
        except Exception as exc:
            raise PdfParseError("PDF page tree could not be read") from exc
        if page_count <= 0:
            raise PdfParseError("PDF has no readable pages")

        read_count = min(page_count, self.max_pages)
        warnings: list[str] = []
        if page_count > read_count:
            warnings.append(
                f"only the first {read_count} of {page_count} pages were parsed"
            )

        page_texts: list[str] = []
        failed_pages: list[int] = []
        for index in range(read_count):
            try:
                extracted = reader.pages[index].extract_text() or ""
            except Exception:
                extracted = ""
                failed_pages.append(index + 1)
            page_texts.append(_normalize_pdf_text(extracted))

        if failed_pages:
            preview = ", ".join(str(value) for value in failed_pages[:8])
            suffix = "..." if len(failed_pages) > 8 else ""
            warnings.append(f"text extraction failed on page(s) {preview}{suffix}")

        text, text_truncated = _build_bounded_page_text(
            page_texts,
            max_chars=self.max_text_chars,
        )
        if not text.strip():
            warnings.append("no embedded text was found; rendered pages are used for OCR")

        rendered_pages: tuple[PdfRenderedPage, ...] = ()
        if self.rendered_page_limit > 0:
            try:
                rendered_pages = tuple(
                    self._render_representative_pages(
                        raw_bytes,
                        page_count=page_count,
                    )
                )
            except Exception:
                warnings.append("representative page rendering was unavailable")

        return PdfParseResult(
            filename=filename,
            page_count=page_count,
            text=text,
            extracted_page_count=sum(bool(value) for value in page_texts),
            rendered_pages=rendered_pages,
            truncated=(page_count > read_count or text_truncated),
            warnings=tuple(warnings),
        )

    def _render_representative_pages(
        self,
        raw_bytes: bytes,
        *,
        page_count: int,
    ) -> list[PdfRenderedPage]:
        try:
            import pypdfium2 as pdfium
            from PIL import Image
        except ImportError as exc:
            self._render_available = False
            raise PdfParseError("pypdfium2 and Pillow are required for PDF rendering") from exc

        indexes = _representative_page_indexes(page_count, self.rendered_page_limit)
        document = pdfium.PdfDocument(raw_bytes)
        rendered: list[PdfRenderedPage] = []
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        try:
            for index in indexes:
                page = document[index]
                bitmap = None
                try:
                    bitmap = page.render(scale=1.35)
                    image = bitmap.to_pil().convert("RGB")
                    image.thumbnail(
                        (self.render_max_side, self.render_max_side),
                        resample=resampling,
                    )
                    output = io.BytesIO()
                    image.save(output, format="PNG", optimize=True, compress_level=7)
                    data = output.getvalue()
                    mime_type = "image/png"
                    if len(data) > 2 * 1024 * 1024:
                        output = io.BytesIO()
                        image.save(output, format="JPEG", quality=82, optimize=True)
                        data = output.getvalue()
                        mime_type = "image/jpeg"
                    if data:
                        rendered.append(
                            PdfRenderedPage(
                                page_number=index + 1,
                                data=data,
                                mime_type=mime_type,
                            )
                        )
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    page.close()
        finally:
            document.close()
        return rendered


def _normalize_pdf_text(value: str) -> str:
    normalized = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t\f\v]+", " ", normalized)
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    return normalized.strip()


def _build_bounded_page_text(
    page_texts: list[str],
    *,
    max_chars: int,
) -> tuple[str, bool]:
    nonempty_count = sum(bool(value) for value in page_texts)
    if nonempty_count == 0:
        return "", False
    total_source_chars = sum(len(value) for value in page_texts)
    fair_share = max(250, (max_chars - len(page_texts) * 24) // nonempty_count)
    blocks: list[str] = []
    used = 0
    truncated = False
    for index, text in enumerate(page_texts, start=1):
        if not text:
            continue
        available = max(max_chars - used - 32, 0)
        if available <= 0:
            truncated = True
            break
        excerpt_limit = min(fair_share, available)
        excerpt = text[:excerpt_limit]
        if len(excerpt) < len(text):
            truncated = True
            excerpt = excerpt.rstrip() + "\n[本页文本已截断]"
        block = f"[PDF 第 {index} 页]\n{excerpt}"
        if used + len(block) > max_chars:
            truncated = True
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks), truncated or total_source_chars > max_chars


def _representative_page_indexes(page_count: int, limit: int) -> tuple[int, ...]:
    if page_count <= 0 or limit <= 0:
        return ()
    if page_count <= limit:
        return tuple(range(page_count))
    if limit == 1:
        return (0,)
    values = {
        min(page_count - 1, round((page_count - 1) * index / (limit - 1)))
        for index in range(limit)
    }
    return tuple(sorted(values))


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _bounded_env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)
