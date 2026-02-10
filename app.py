from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pdfplumber
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from extract_simplified_schedule import (
    DAY_NAMES,
    WEEK_DAYS,
    LessonCatalog,
    abbreviate_lesson_name,
    build_teacher_schedules,
    collect_known_teacher_names,
    extract_catalog_from_bottom_table,
    extract_lesson_teacher_map,
    extract_week_table,
    make_simplified_schedule,
    pick_tr_font,
)


TeacherSchedules = Dict[str, Dict[str, Dict[int, List[Tuple[str, str]]]]]


def parse_class_name(page_text: str, fallback: str) -> str:
    """
    Sayfa metninden '9/A' gibi sınıf adını ayıklar, bulunamazsa fallback döner.
    """
    m = re.search(r"\b([0-9]+/[A-ZÇĞİÖŞÜ])\b", page_text)
    return m.group(1) if m else fallback


def process_uploaded_pdf(file_bytes: bytes) -> Tuple[Dict[str, List[Tuple[str, List[str]]]], TeacherSchedules]:
    """
    Verilen PDF baytlarından:
    - sınıf bazlı sade ders programları
    - öğretmen bazlı ders programları
    çıkarır.

    class_schedules: {class_name: [(day, [lesson1,...]), ...]}
    teacher_schedules: TeacherSchedules
    """
    class_schedules: Dict[str, List[Tuple[str, List[str]]]] = {}
    teacher_schedules_all: TeacherSchedules = {}

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        known_teacher_names = collect_known_teacher_names(pdf)

        for i, page in enumerate(pdf.pages):
            catalog: LessonCatalog = extract_catalog_from_bottom_table(page)
            week_table = extract_week_table(page)
            if not week_table:
                continue

            schedule = make_simplified_schedule(week_table, catalog)
            page_text = page.extract_text() or ""
            class_name = parse_class_name(page_text, fallback=f"Sayfa {i+1}")
            class_schedules[class_name] = schedule

            lesson_teachers = extract_lesson_teacher_map(page, catalog, known_teacher_names)
            teacher_schedules_page = build_teacher_schedules(
                week_table, catalog, lesson_teachers, class_name
            )

            # Merge this page's teacher schedules into global.
            for teacher, by_day in teacher_schedules_page.items():
                if teacher not in teacher_schedules_all:
                    teacher_schedules_all[teacher] = by_day
                else:
                    existing = teacher_schedules_all[teacher]
                    for day, periods in by_day.items():
                        ex_periods = existing.setdefault(day, {})
                        for p, entries in periods.items():
                            if not entries:
                                continue
                            ex_list = ex_periods.setdefault(p, [])
                            ex_list.extend(entries)

    return class_schedules, teacher_schedules_all


def build_teacher_pdf_bytes(
    teacher_schedules: TeacherSchedules,
    selected_teachers: List[str] | None = None,
) -> bytes:
    """
    Seçili öğretmenler için (veya None ise tümü için) tek bir PDF üretir.
    Her öğretmenin tablosu ayrı sayfada.
    """
    if not teacher_schedules:
        return b""

    if selected_teachers is None:
        teachers = sorted(teacher_schedules.keys())
    else:
        teachers = [t for t in selected_teachers if t in teacher_schedules]
        teachers.sort()

    if not teachers:
        return b""

    buffer = io.BytesIO()

    font_name = pick_tr_font()
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.fontName = font_name
    # Öğretmen adı başlığını daha da küçült
    title_style.fontSize = 12
    title_style.leading = 14

    body_style = ParagraphStyle(
        "BodyTR",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=7,
        leading=9,
    )

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Öğretmen Ders Programları",
    )

    story: List = []

    for idx, teacher in enumerate(teachers):
        # Her sayfaya 3 öğretmen: 0-1-2, 3-4-5, ...
        if idx > 0 and idx % 3 == 0:
            story.append(PageBreak())
        elif idx % 3 != 0:
            # Aynı sayfadaki tablolar arasında biraz boşluk
            story.append(Spacer(1, 6 * mm))

        by_day = teacher_schedules[teacher]

        story.append(Paragraph(f"{teacher} - Ders Programı", title_style))
        story.append(Spacer(1, 6 * mm))

        # Başlık satırı: Gün, 1..9
        header_row = [Paragraph("Gün", body_style)]
        for p in range(1, 10):
            header_row.append(Paragraph(str(p), body_style))

        data: List[List[Paragraph]] = [header_row]
        for day in WEEK_DAYS:
            periods = by_day.get(day, {})
            row: List[Paragraph] = [Paragraph(day, body_style)]
            for p in range(1, 10):
                entries = periods.get(p, []) or []
                if entries:
                    # Uzun ders adları için kısaltma + çoklu sınıf desteği.
                    lines = [
                        f"{abbreviate_lesson_name(lesson)} ({cls})"
                        for lesson, cls in entries
                    ]
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
    buffer.seek(0)
    return buffer.read()


def main() -> None:
    st.set_page_config(page_title="Ders Programı Oluşturucu", layout="wide")
    st.title("Sade Ders Programı ve Öğretmen Çıktıları")

    st.markdown(
        "1. **Sınıf ders programı PDF'lerini yükle** (senin örnekteki gibi üstte sınıf programı, altta ders listesi olan PDF).\n"
        "2. Sistem bu PDF'lerden **sade sınıf programlarını** ve **öğretmen ders programlarını** çıkarır.\n"
        "3. Çıktı olarak tüm öğretmenler veya seçili öğretmenler için PDF indirebilirsin."
    )

    uploaded_files = st.file_uploader(
        "Sınıf ders programı PDF'lerini seç (bir veya birden fazla)",
        type=["pdf"],
        accept_multiple_files=True,
    )

    if not uploaded_files:
        st.info("En az bir PDF yükle.")
        return

    all_class_schedules: Dict[str, List[Tuple[str, List[str]]]] = {}
    all_teacher_schedules: TeacherSchedules = {}

    for f in uploaded_files:
        bytes_data = f.read()
        class_schedules, teacher_schedules = process_uploaded_pdf(bytes_data)

        # Sınıf programlarını birleştir (aynı sınıf adı tekrar gelirse sonuncusu geçerli olsun).
        all_class_schedules.update(class_schedules)

        # Öğretmen programlarını birleştir.
        for teacher, by_day in teacher_schedules.items():
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

    if not all_teacher_schedules:
        st.error("Yüklenen PDF'lerden öğretmen programı çıkarılamadı.")
        return

    with st.expander("Tespit edilen sınıf programları", expanded=False):
        for cls, schedule in sorted(all_class_schedules.items(), key=lambda x: x[0]):
            st.markdown(f"**{cls}**")
            for day, lessons in schedule:
                st.write(f"- {day}: {', '.join(lessons)}")

    st.markdown("---")
    st.subheader("Öğretmen çıktıları")

    teachers = sorted(all_teacher_schedules.keys())
    st.write(f"Toplam öğretmen sayısı: **{len(teachers)}**")

    output_mode = st.radio(
        "Çıktı tipi",
        options=["Tüm öğretmenler (tek PDF)", "Öğretmen seçimi"],
        horizontal=True,
    )

    if output_mode == "Tüm öğretmenler (tek PDF)":
        if st.button("Tüm öğretmenler için PDF oluştur"):
            pdf_bytes = build_teacher_pdf_bytes(all_teacher_schedules, selected_teachers=None)
            if pdf_bytes:
                st.download_button(
                    label="PDF'yi indir",
                    data=pdf_bytes,
                    file_name="tum_ogretmenler_ders_programi.pdf",
                    mime="application/pdf",
                )
    else:
        selected = st.multiselect("Öğretmen seç", options=teachers)
        if selected and st.button("Seçili öğretmenler için PDF oluştur"):
            pdf_bytes = build_teacher_pdf_bytes(all_teacher_schedules, selected_teachers=selected)
            if pdf_bytes:
                st.download_button(
                    label="PDF'yi indir",
                    data=pdf_bytes,
                    file_name="secili_ogretmenler_ders_programi.pdf",
                    mime="application/pdf",
                )


if __name__ == "__main__":
    main()

