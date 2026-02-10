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
# Hafta içi günler (çıktılarda sadece bunlar kullanılacak)
WEEK_DAYS = DAY_NAMES[:5]

# Bazı sayfalarda bozulmuş olarak gelen, öğretmen olmadığı kesin isim(ler).
BLACKLIST_TEACHER_NAMES: Set[str] = {
    "MNİUYHAAZSİ EEBREC ALANB",
}


def abbreviate_lesson_name(name: str) -> str:
    """
    Uzun ders adlarını kısaltır.
    Örn: 'BİLGİSAYARLI TASARIM UYGULAMALARI' ->
         'BİLGİSAYARLI T. U.'
    """
    name = _norm(name)
    if not name:
        return ""
    parts = [p for p in name.split(" ") if p]
    if not parts:
        return ""
    first = parts[0]
    rest = [f"{p[0]}." for p in parts[1:] if p]
    if rest:
        return first + " " + " ".join(rest)
    return first


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


def extract_lesson_teacher_map(
    page: pdfplumber.page.Page,
    catalog: LessonCatalog,
    known_teacher_names: Optional[Set[str]] = None,
) -> Dict[str, Set[str]]:
    """
    Alt tablodan: normalized ders adı -> {öğretmen1, öğretmen2, ...}
    """
    tables = page.extract_tables()
    if not tables:
        return {}

    bottom = tables[-1]
    if not bottom or len(bottom) < 2:
        return {}

    header = [(_norm(c or "")) for c in bottom[0]]
    col_map = {h: i for i, h in enumerate(header) if h}

    def get_col(*candidates: str) -> Optional[int]:
        for c in candidates:
            if c in col_map:
                return col_map[c]
        return None

    idx_lesson = get_col("Dersin Adı")
    idx_teacher = get_col("Dersin Öğretmeni")
    if idx_lesson is None or idx_teacher is None:
        return {}

    lesson_teachers: Dict[str, Set[str]] = {}

    for row in bottom[1:]:
        if not row:
            continue
        if idx_lesson >= len(row) or idx_teacher >= len(row):
            continue
        raw_lesson = _norm(row[idx_lesson] or "")
        if not raw_lesson:
            continue
        lesson_norm = _norm_upper(raw_lesson)

        raw_teachers = str(row[idx_teacher] or "")
        # Virgül veya satır sonu ile ayrılmış olabilir.
        parts = re.split(r"[,\n]", raw_teachers)
        for part in parts:
            name = _norm(part)
            if not name:
                continue

            # Önce bilinen öğretmen listesine göre normalize etmeye çalış.
            if known_teacher_names:
                best = _best_teacher_name_match(name, known_teacher_names)
                if not best:
                    continue
                lesson_teachers.setdefault(lesson_norm, set()).add(best)
            else:
                if not _is_probable_full_teacher_name(name):
                    continue
                lesson_teachers.setdefault(lesson_norm, set()).add(name)

    return lesson_teachers


def _is_probable_full_teacher_name(name: str) -> bool:
    """
    Alt tablodan gelen bir parçanın gerçekten öğretmen adı olup olmadığını
    tahmin etmeye çalışır. Hatalı olarak derslik/kod parçası karışan isimleri
    elemek için kullanılır.
    """
    up = _norm_upper(name)
    if not up:
        return False
    if up in BLACKLIST_TEACHER_NAMES:
        return False
    # Sayı içeren, çok muhtemel kod olan parçaları ele.
    if any(ch.isdigit() for ch in up):
        return False
    # LAB/SINIF vb. derslik kelimeleri içeriyorsa öğretmen değildir.
    if re.search(r"\b(LAB|SINIF|DERSL[İI]K|ATÖLYE)\b", up):
        return False
    # En az bir boşluk olmalı (ad + soyad gibi).
    if " " not in up:
        return False
    # Sadece büyük harf ve boşluklardan oluşsun (noktalama vs. yok).
    if not re.fullmatch(r"[A-ZÇĞİÖŞÜ\s]+", up):
        return False
    # Çok kısa isimleri de ele.
    if len(up) < 5:
        return False
    return True


def _best_teacher_name_match(candidate: str, known_names: Set[str]) -> Optional[str]:
    """
    Verilen aday ismi, bilinen öğretmen listesi içinde en çok benzeyen isme eşler.
    Eşik altındaysa None döner.
    """
    cand_norm = _norm_upper(candidate)
    if not cand_norm:
        return None

    # 1) Önce soyadı üzerinden doğrudan eşleştirmeye çalış.
    cand_words = [w for w in cand_norm.split() if w]
    if cand_words:
        surnames_to_names: Dict[str, Set[str]] = {}
        for name in known_names:
            if name in BLACKLIST_TEACHER_NAMES:
                continue
            parts = _norm_upper(name).split()
            if not parts:
                continue
            surname = parts[-1]
            surnames_to_names.setdefault(surname, set()).add(name)

        candidates_by_surname: Set[str] = set()
        for w in cand_words:
            if w in surnames_to_names:
                candidates_by_surname.update(surnames_to_names[w])

        # Eğer aday soyadlarından sadece tek bir öğretmen çıkıyorsa,
        # aradığımız kişi büyük ihtimalle odur.
        if len(candidates_by_surname) == 1:
            return next(iter(candidates_by_surname))

    # 2) Aksi halde tam isim üzerinden fuzzy eşleştirme yap.
    best_name: Optional[str] = None
    best_score: float = 0.0

    for name in known_names:
        if name in BLACKLIST_TEACHER_NAMES:
            continue
        name_norm = _norm_upper(name)
        score = _similar(cand_norm, name_norm)
        if score > best_score:
            best_score = score
            best_name = name

    # Eşik: %60 benzerlikten fazlasını kabul edelim (bozulmuş yazımlar için biraz esnek).
    if best_name and best_score >= 0.60:
        return best_name
    return None


def collect_known_teacher_names(pdf: pdfplumber.PDF) -> Set[str]:
    """
    Tüm sayfalardaki alt tablolardan temiz öğretmen isimlerini toplar.
    """
    names: Set[str] = set()

    for page in pdf.pages:
        tables = page.extract_tables()
        if not tables:
            continue
        bottom = tables[-1]
        if not bottom or len(bottom) < 2:
            continue
        header = [_norm(c or "") for c in bottom[0]]
        col_map = {h: idx for idx, h in enumerate(header) if h}
        idx_teacher = col_map.get("Dersin Öğretmeni")
        if idx_teacher is None:
            continue

        for row in bottom[1:]:
            if not row or idx_teacher >= len(row):
                continue
            raw_teachers = str(row[idx_teacher] or "")
            parts = [_norm(p) for p in re.split(r"[,\\n]", raw_teachers)]
            for name in parts:
                if not name:
                    continue
                if not _is_probable_full_teacher_name(name):
                    continue
                names.add(name)

    # Açıkça kara listeye alınmış bozulmuş isimleri at.
    names -= BLACKLIST_TEACHER_NAMES
    return names


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
        # Hafta sonu (Cumartesi/Pazar) satırlarını plana dahil etme.
        if day not in WEEK_DAYS:
            continue
        cells = row[1:]  # periods
        lessons = [extract_lesson_name_from_cell(c or "", catalog) for c in cells]
        # Keep blanks out, but preserve order.
        lessons = [l for l in lessons if _norm(l)]
        simplified.append((day, lessons))
    return simplified


def build_teacher_schedules(
    week_table: List[List[str]],
    catalog: LessonCatalog,
    lesson_teachers: Dict[str, Set[str]],
    class_name: str,
    max_periods: int = 9,
) -> Dict[str, Dict[str, Dict[int, List[Tuple[str, str]]]]]:
    """
    teacher -> day -> period_index (1..max_periods) -> [(lesson name, class_name), ...]
    Boş dersler için değer boş liste.
    """
    teacher_schedules: Dict[str, Dict[str, Dict[int, List[Tuple[str, str]]]]] = {}

    # Tüm öğretmenler için iskelet oluşturmak yerine, yalnızca kullanılanlarda oluşturacağız.
    def ensure_teacher(teacher: str) -> None:
        if teacher in teacher_schedules:
            return
        teacher_schedules[teacher] = {
            day: {p: [] for p in range(1, max_periods + 1)} for day in WEEK_DAYS
        }

    for row in week_table:
        if not row or not row[0]:
            continue
        day = _norm(row[0])
        if day not in WEEK_DAYS:
            continue

        # 1'den max_periods'e kadar olan sütunları dikkate al.
        cells = row[1 : 1 + max_periods]
        for idx, cell in enumerate(cells, start=1):
            lesson_name = extract_lesson_name_from_cell(cell or "", catalog)
            if not _norm(lesson_name):
                continue

            # Alt tablodaki ders adına eşleyelim.
            matched = catalog.best_lesson_match(lesson_name) or lesson_name
            lesson_key = _norm_upper(matched)
            teachers = lesson_teachers.get(lesson_key, set())
            if not teachers:
                continue

            for t in teachers:
                ensure_teacher(t)
                teacher_schedules[t][day][idx].append((matched, class_name))

    return teacher_schedules


def write_simple_pdf(
    out_path: Path,
    title: str,
    schedule: List[Tuple[str, List[str]]],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    font_name = pick_tr_font()

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
        # Uzun ders isimleri için kısaltma + otomatik kaydırma: her dersi satır sonu ile ayır.
        short_lessons = [abbreviate_lesson_name(l) for l in lessons]
        lessons_html = "<br/>".join(short_lessons)
        data.append(
            [
                Paragraph(day, body_style),
                Paragraph(lessons_html, body_style),
            ]
        )

    # Satır yüksekliklerini de sabitleyerek boş / dolu satırlar arasında
    # görsel bütünlüğü koruyalım.
    row_heights = [10 * mm] * len(data)

    tbl = Table(data, colWidths=[35 * mm, 140 * mm], rowHeights=row_heights)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, 0), font_name),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 1), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
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


def pick_tr_font() -> str:
    """
    Türkçe karakterleri destekleyen bir font bulup register eder; bulunamazsa Helvetica döner.
    """
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
    return font_name


def write_teacher_pdfs(
    out_dir: Path,
    teacher_schedules: Dict[str, Dict[str, Dict[int, List[Tuple[str, str]]]]],
) -> None:
    """
    Her öğretmen için: satırlar günler, sütunlar 1–9. Hücrede ders adı (yoksa boş).
    """
    if not teacher_schedules:
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    font_name = pick_tr_font()
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.fontName = font_name
    # Başlık (öğretmen adı) daha da küçük olsun
    title_style.fontSize = 12
    title_style.leading = 14
    body_style = ParagraphStyle(
        "BodyTR",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=7,
        leading=9,
    )

    for teacher, by_day in sorted(teacher_schedules.items(), key=lambda x: x[0]):
        safe_name = teacher.replace("/", "-").replace(" ", "_")
        out_path = out_dir / f"{safe_name}_ders_programi.pdf"

        doc = SimpleDocTemplate(
            str(out_path),
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,
            title=f"{teacher} - Ders Programı",
        )

        story: List = []
        story.append(Paragraph(f"{teacher} - Ders Programı", title_style))
        story.append(Spacer(1, 6 * mm))

        # Başlık satırı: Gün, 1..9
        header_row: List[Paragraph] = [Paragraph("Gün", body_style)]
        for p in range(1, 10):
            header_row.append(Paragraph(str(p), body_style))

        data: List[List[Paragraph]] = [header_row]
        for day in WEEK_DAYS:
            periods = by_day.get(day, {})
            row: List[Paragraph] = [Paragraph(day, body_style)]
            for p in range(1, 10):
                entries = periods.get(p, []) or []
                if entries:
                    # Aynı saate birden fazla sınıf varsa hepsini alt alta yaz.
                    lines = [f"{abbreviate_lesson_name(lesson)} ({cls})" for lesson, cls in entries]
                    cell_html = "<br/>".join(lines)
                else:
                    cell_html = ""
                row.append(Paragraph(cell_html, body_style))
            data.append(row)

        # Satır yükseklikleri:
        # - İlk satır (Gün, 1..9 başlığı) daha düşük
        # - Diğer satırlar (günler) daha yüksek
        header_h = 6 * mm
        day_h = 10 * mm
        row_heights = [header_h] + [day_h] * (len(data) - 1)

        tbl = Table(
            data,
            colWidths=[25 * mm] + [16 * mm] * 9,
            rowHeights=row_heights,
        )
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("FONTNAME", (0, 0), (-1, 0), font_name),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("FONTNAME", (0, 1), (-1, -1), font_name),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )

        story.append(tbl)
        doc.build(story)


def main() -> None:
    # Test için varsayılan: 2 sayfalı örnek PDF. Tüm kitapçığı test etmek
    # istediğinde aşağıdaki satırı 'SnfProgram.pdf' olarak değiştirebilirsin.
    pdf_path = Path(__file__).parent / "SnfProgram2li.pdf"
    out_dir = Path(__file__).parent / "output"

    with pdfplumber.open(str(pdf_path)) as pdf:
        # Tüm öğretmenler için temiz isim havuzu oluştur.
        known_teacher_names = collect_known_teacher_names(pdf)
        print(f"[INFO] PDF toplam sayfa sayısı: {len(pdf.pages)}")
        print(f"[INFO] Tespit edilen öğretmen sayısı: {len(known_teacher_names)}")

        all_teacher_schedules: Dict[str, Dict[str, Dict[int, List[Tuple[str, str]]]]] = {}

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
            print(f"[PAGE {i+1}] Sınıf: {class_name}, sade program PDF yazıldı: {out_pdf}")

            # Öğretmen ders programları için de bu sayfadan veri topla.
            lesson_teachers = extract_lesson_teacher_map(page, catalog, known_teacher_names)
            teacher_count = len({t for s in lesson_teachers.values() for t in s})
            print(f"[PAGE {i+1}] Ders sayısı (alt tablo): {len(lesson_teachers)}, öğretmen sayısı: {teacher_count}")
            teacher_schedules_page = build_teacher_schedules(
                week_table, catalog, lesson_teachers, class_name
            )

            # Aynı öğretmen farklı sınıflara giriyorsa, programları birleştiriyoruz.
            for teacher, by_day in teacher_schedules_page.items():
                if teacher not in all_teacher_schedules:
                    all_teacher_schedules[teacher] = by_day
                else:
                    existing = all_teacher_schedules[teacher]
                    for day, periods in by_day.items():
                        ex_periods = existing.setdefault(day, {})
                        for p, entries in periods.items():
                            if not entries:
                                continue
                            ex_list = ex_periods.setdefault(p, [])
                            ex_list.extend(entries)

        # Tüm sayfalardan topladığımız programlarla öğretmen PDF'lerini üret.
        teacher_out_dir = out_dir / "teachers"
        write_teacher_pdfs(teacher_out_dir, all_teacher_schedules)
        if all_teacher_schedules:
            print(f"[INFO] Toplam öğretmen için program üretildi: {len(all_teacher_schedules)}")
            print(f"[INFO] Öğretmen programları klasörü: {teacher_out_dir}")


if __name__ == "__main__":
    main()

