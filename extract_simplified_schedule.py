from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pdfplumber
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


DAY_NAMES = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def _norm(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_upper(s: str) -> str:
    # Turkish-aware uppercasing is tricky; PDF text here is already uppercase-ish.
    return _norm(s).upper()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class LessonCatalog:
    # normalized upper lesson names
    lesson_names: Tuple[str, ...]
    # normalized upper -> original lesson name (as in bottom table, with Turkish chars)
    raw_by_norm: Dict[str, str]
    # normalized upper words from all teachers
    teacher_words: Set[str]
    # normalized upper words from all locations
    location_words: Set[str]

    def best_lesson_match(self, candidate: str) -> Optional[str]:
        """
        Fuzzy eşleştirme ile en yakın ders adını döndürür (orijinal yazılışıyla).
        """
        cand = _norm_upper(candidate)
        if not cand:
            return None
        best_name = None
        best_score = 0.0
        for name in self.lesson_names:
            score = _similar(cand, name)
            if score > best_score:
                best_score = score
                best_name = name
        # Eşik: kırpılmış / hafif bozulmuş yazımları da yakalasın.
        if best_name and best_score >= 0.60:
            return self.raw_by_norm.get(best_name, best_name)
        return None


def extract_catalog_from_bottom_table(page: pdfplumber.page.Page) -> LessonCatalog:
    """
    Builds a catalog from the bottom 'S.No ... Dersin Adı ... Dersin Öğretmeni ... Yer' table.
    """
    tables = page.extract_tables()
    if not tables:
        return LessonCatalog(lesson_names=tuple(), teacher_words=set(), location_words=set())

    # Bottom table is typically the last extracted table on this PDF.
    bottom = tables[-1]
    if not bottom or len(bottom) < 2:
        return LessonCatalog(lesson_names=tuple(), teacher_words=set(), location_words=set())

    header = [(_norm(c or "")) for c in bottom[0]]
    col_map = {h: i for i, h in enumerate(header) if h}

    def get_col(*candidates: str) -> Optional[int]:
        for c in candidates:
            if c in col_map:
                return col_map[c]
        return None

    idx_lesson = get_col("Dersin Adı")
    idx_teacher = get_col("Dersin Öğretmeni")
    idx_loc = get_col("Yer")

    lesson_norms: Set[str] = set()
    raw_by_norm: Dict[str, str] = {}
    teacher_words: Set[str] = set()
    location_words: Set[str] = set()

    for row in bottom[1:]:
        if not row:
            continue
        if idx_lesson is not None and idx_lesson < len(row):
            raw_lesson = _norm(row[idx_lesson] or "")
            ln = _norm_upper(raw_lesson)
            if ln:
                lesson_norms.add(ln)
                # Aynı normalized isim birden çok kez gelirse ilkini tutmak yeterli.
                raw_by_norm.setdefault(ln, raw_lesson)
        if idx_teacher is not None and idx_teacher < len(row):
            t = _norm_upper(row[idx_teacher] or "")
            # Teachers can be comma-separated.
            t = t.replace(",", " ")
            for w in t.split():
                # Ignore tiny tokens.
                if len(w) >= 2:
                    teacher_words.add(w)
        if idx_loc is not None and idx_loc < len(row):
            loc = _norm_upper(row[idx_loc] or "")
            for w in loc.split():
                if len(w) >= 2:
                    location_words.add(w)

    # Add common schedule-location words
    for w in ["LAB", "SINIF", "DERSLİK", "ATÖLYE"]:
        location_words.add(w)

    return LessonCatalog(
        lesson_names=tuple(sorted(lesson_norms)),
        raw_by_norm=raw_by_norm,
        teacher_words=teacher_words,
        location_words=location_words,
    )


def _is_probably_teacher_line(line: str, catalog: LessonCatalog) -> bool:
    up = _norm_upper(line)
    if not up:
        return False
    words = [w for w in up.split() if w]
    if not words:
        return False
    # If all words look like teacher words, treat as teacher line.
    known = sum(1 for w in words if w in catalog.teacher_words)
    return known == len(words) and len(words) <= 4


def _is_probably_location_line(line: str, catalog: LessonCatalog) -> bool:
    up = _norm_upper(line)
    if not up:
        return False
    if re.search(r"\bLAB\b", up):
        return True
    words = [w for w in up.split() if w]
    if not words:
        return False
    known = sum(1 for w in words if w in catalog.location_words)
    return known >= max(1, len(words) - 1) and len(words) <= 6


def extract_lesson_name_from_cell(cell_text: str, catalog: LessonCatalog) -> str:
    """
    Takes the messy cell content and returns a clean lesson name.
    Strategy:
    - Split into lines.
    - Take top lines until we hit teacher/location lines.
    - Fuzzy-match to known lesson names from bottom table.
    """
    if not cell_text:
        return ""
    lines = [_norm(l) for l in str(cell_text).splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return ""

    kept: List[str] = []
    for line in lines:
        if _is_probably_teacher_line(line, catalog) or _is_probably_location_line(line, catalog):
            break
        kept.append(line)

    # If heuristic kept nothing (rare), try first 1-2 lines as fallback
    if not kept:
        kept = lines[:2]

    candidate = _norm(" ".join(kept))

    # Önce alt tablodan bilinen ders adlarıyla fuzzy eşleştirme yap.
    match = catalog.best_lesson_match(candidate)
    if match:
        # Alt tabloda zaten temiz ders adı var, onu doğrudan kullan.
        return match

    # Eşleşme yoksa, aday metinden öğretmen + mekan kelimelerini temizleyelim.
    up = _norm_upper(candidate)
    words = up.split()
    cleaned_words: List[str] = []
    for w in words:
        if w in catalog.teacher_words:
            continue
        if w in catalog.location_words:
            continue
        # Basit sayıları (saat vb.) da at.
        if re.fullmatch(r"[0-9]+", w):
            continue
        cleaned_words.append(w)

    if cleaned_words:
        return " ".join(cleaned_words)

    # Hâlâ bir şey kalmadıysa, son çare olarak orijinal adayı döndür.
    return candidate


def extract_week_table(page: pdfplumber.page.Page) -> List[List[str]]:
    """
    Returns the extracted 'week grid' as a table.
    Expected shape for this PDF: header rows + day rows.
    """
    tables = page.extract_tables()
    if not tables:
        return []
    # In this PDF, the first extracted table is the week grid.
    return tables[0]


def make_simplified_schedule(
    week_table: List[List[str]],
    catalog: LessonCatalog,
) -> List[Tuple[str, List[str]]]:
    """
    Output format: [(day, [lesson1, lesson2, ...]), ...]
    """
    simplified: List[Tuple[str, List[str]]] = []
    for row in week_table:
        if not row or not row[0]:
            continue
        day = _norm(row[0])
        if day not in DAY_NAMES:
            continue
        cells = row[1:]  # periods
        lessons = [extract_lesson_name_from_cell(c or "", catalog) for c in cells]
        # Keep blanks out, but preserve order.
        lessons = [l for l in lessons if _norm(l)]
        simplified.append((day, lessons))
    return simplified


def write_simple_pdf(
    out_path: Path,
    title: str,
    schedule: List[Tuple[str, List[str]]],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Türkçe karakterler için uygun bir TTF font bulmaya çalış.
    # Bulamazsak varsayılan Helvetica'ya düşer.
    font_candidates = [
        # macOS yaygın fontları
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
        # Linux için yaygın
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Windows için olası
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    font_name = "Helvetica"
    for fp in font_candidates:
        if Path(fp).is_file():
            try:
                pdfmetrics.registerFont(TTFont("TRFont", fp))
                font_name = "TRFont"
                break
            except Exception:
                continue

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    # Başlık ve tablo için Türkçe destekli font kullan.
    styles["Title"].fontName = font_name
    body_style = ParagraphStyle(
        "BodyTR",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=12,
    )
    story = []
    story.append(Paragraph(title, styles["Title"]))
    story.append(Spacer(1, 6 * mm))

    data: List[List[Paragraph]] = [
        [
            Paragraph("Gün", body_style),
            Paragraph("Dersler (sırayla)", body_style),
        ]
    ]
    for day, lessons in schedule:
        # Uzun ders isimleri için otomatik kaydırma: her dersi satır sonu ile ayır.
        lessons_html = "<br/>".join(lessons)
        data.append(
            [
                Paragraph(day, body_style),
                Paragraph(lessons_html, body_style),
            ]
        )

    tbl = Table(data, colWidths=[35 * mm, 140 * mm])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, 0), font_name),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 1), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(tbl)
    doc.build(story)


def main() -> None:
    pdf_path = Path(__file__).parent / "SnfProgram2li.pdf"
    out_dir = Path(__file__).parent / "output"

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            # Build catalog for that page (each page is a different class here)
            catalog = extract_catalog_from_bottom_table(page)
            week_table = extract_week_table(page)
            schedule = make_simplified_schedule(week_table, catalog)

            # Title: try to pick class name from text (ör: '9/A')
            page_text = page.extract_text() or ""
            m = re.search(r"\b([0-9]+/[A-ZÇĞİÖŞÜ])\b", page_text)
            class_name = m.group(1) if m else f"Sayfa {i+1}"
            title = f"{class_name} - Sade Ders Programı"

            out_pdf = out_dir / f"{class_name.replace('/', '_')}_sade_program.pdf"
            write_simple_pdf(out_pdf, title=title, schedule=schedule)
            print(f"Wrote: {out_pdf}")


if __name__ == "__main__":
    main()

