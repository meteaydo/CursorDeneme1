from __future__ import annotations

import base64
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

# Gün adlarının 3 harfli kısaltmaları (mobil uyumlu HTML için)
WEEK_DAYS_SHORT = {
    "Pazartesi": "Paz",
    "Salı": "Sal",
    "Çarşamba": "Çar",
    "Perşembe": "Per",
    "Cuma": "Cum",
}


def _first_upper(s: str) -> str:
    """Sadece ilk karakteri büyük harf yapar."""
    if not s:
        return s
    if len(s) == 1:
        return s.upper()
    return s[0].upper() + s[1:].lower()


def _teacher_schedules_to_html(
    teacher_schedules: TeacherSchedules,
    selected_teachers: List[str] | None = None,
) -> str:
    """Elimizdeki veri setinden modern HTML önizleme üretir (PDF'ye yazmadan)."""
    teachers = sorted(teacher_schedules.keys())
    if selected_teachers is not None:
        teachers = [t for t in teachers if t in selected_teachers]
        teachers.sort()
    if not teachers:
        return "<p>Gösterilecek öğretmen yok.</p>"

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    cards_html: List[str] = []
    for teacher in teachers:
        by_day = teacher_schedules[teacher]
        # Toplam ders saati: dolu (day, period) sayısı
        total_hours = 0
        for day in WEEK_DAYS:
            periods = by_day.get(day, {})
            for p in range(1, 10):
                if periods.get(p, []):
                    total_hours += 1
        header_cells = [f"<th>{esc(_first_upper('Gün'))}</th>"] + [f"<th>{p}</th>" for p in range(1, 10)]
        thead_row = "<tr>" + "".join(header_cells) + "</tr>"
        body_rows: List[str] = []
        for day in WEEK_DAYS:
            periods = by_day.get(day, {})
            short_day = WEEK_DAYS_SHORT.get(day, day[:3])
            cells = [f"<td><strong>{esc(_first_upper(short_day))}</strong></td>"]
            for p in range(1, 10):
                entries = periods.get(p, []) or []
                if entries:
                    parts = [
                        f"{_first_upper(abbreviate_lesson_name(les))} ({esc(_first_upper(cls))})"
                        for les, cls in entries
                    ]
                    content = "<br/>".join(esc(p) for p in parts)
                    cells.append(f"<td>{content}</td>")
                else:
                    cells.append('<td class="empty"></td>')
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        table_body = "\n".join(body_rows)
        colgroup = '<col class="col-gun">' + '<col class="col-saat">' * 9
        title_text = f"{esc(_first_upper(teacher))} – {_first_upper('ders programı')} {total_hours} {_first_upper('saat')}"
        cards_html.append(f"""
        <div class="schedule-card">
            <h3 class="schedule-title">{title_text}</h3>
            <div class="schedule-table-wrap">
                <table class="schedule-table">
                    <colgroup>{colgroup}</colgroup>
                    <thead>{thead_row}</thead>
                    <tbody>{table_body}</tbody>
                </table>
            </div>
        </div>
        """)

    return f"""
    <style>
        .schedule-cards {{ font-family: system-ui, -apple-system, sans-serif; margin: 0.5rem 0; font-size: 11px; }}
        .schedule-card {{
            background: #fff;
            border-radius: 8px;
            box-shadow: 0 1px 6px rgba(0,0,0,0.06);
            margin-bottom: 1rem;
            overflow: hidden;
            border: 1px solid #e5e7eb;
        }}
        .schedule-title {{
            margin: 0;
            padding: 0.4rem 0.5rem;
            font-size: 0.7rem;
            font-weight: 600;
            color: #111827;
            background: #f9fafb;
            border-bottom: 1px solid #e5e7eb;
        }}
        .schedule-table-wrap {{ overflow-x: auto; padding: 0.4rem 0.5rem; -webkit-overflow-scrolling: touch; }}
        .schedule-table {{
            width: 100%;
            table-layout: fixed;
            border-collapse: collapse;
            font-size: 6px;
        }}
        .schedule-table th, .schedule-table td {{
            border: 1px solid #9ca3af;
            padding: 2px 3px;
            text-align: left;
            vertical-align: top;
            word-wrap: break-word;
            overflow-wrap: break-word;
            word-break: break-word;
        }}
        .schedule-table thead tr th {{
            height: 14px;
            min-height: 14px;
            background: #d1d5db;
            font-weight: 600;
            color: #000;
        }}
        .schedule-table tbody tr td {{
            height: 24px;
            min-height: 24px;
            background: #fff;
        }}
        .schedule-table tbody tr td.empty {{ background: #e5e7eb; }}
        .schedule-table tbody tr:hover td:not(.empty) {{ background: #f0f9ff; }}
        .schedule-table col.col-gun {{ width: 28px; }}
        .schedule-table col.col-saat {{ width: auto; }}
    </style>
    <div class="schedule-cards">
        {"".join(cards_html)}
    </div>
    """


def _loading_overlay_html(message: str) -> str:
    """Tam ekran, her şeyin üstünde büyük loading katmanı (HTML)."""
    return f"""
    <div id="loading-overlay" style="
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0,0,0,0.75);
        z-index: 99999;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        font-family: system-ui, sans-serif;
    ">
        <div style="
            width: 120px;
            height: 120px;
            border: 8px solid rgba(255,255,255,0.3);
            border-top-color: #fff;
            border-radius: 50%;
            animation: spin 0.9s linear infinite;
        "></div>
        <p style="
            color: #fff;
            font-size: 2rem;
            font-weight: 700;
            margin-top: 2rem;
            text-align: center;
            padding: 0 2rem;
            line-height: 1.4;
        ">{message}</p>
    </div>
    <style>
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
    </style>
    """


def parse_class_name(page_text: str, fallback: str) -> str:
    """
    Sayfa metninden '9/A' gibi sınıf adını ayıklar, bulunamazsa fallback döner.
    """
    m = re.search(r"\b([0-9]+/[A-ZÇĞİÖŞÜ])\b", page_text)
    return m.group(1) if m else fallback


def _process_uploaded_pdf_impl(file_bytes: bytes) -> Tuple[Dict[str, List[Tuple[str, List[str]]]], TeacherSchedules]:
    """
    Verilen PDF baytlarından sınıf ve öğretmen programlarını çıkarır (önbelleksiz).
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


@st.cache_data(show_spinner=False)
def process_uploaded_pdf(file_bytes: bytes) -> Tuple[Dict[str, List[Tuple[str, List[str]]]], TeacherSchedules]:
    """
    Aynı PDF tekrar yüklendiğinde veya sadece çıktı tipi değiştiğinde
    yeniden işlem yapılmaması için önbellekli sarmalayıcı.
    """
    return _process_uploaded_pdf_impl(file_bytes)


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
        total_hours = sum(
            1 for day in WEEK_DAYS for p in range(1, 10) if by_day.get(day, {}).get(p, [])
        )
        story.append(Paragraph(f"{teacher} - Ders Programı {total_hours} Saat", title_style))
        story.append(Spacer(1, 2 * mm))

        # Başlık satırı: Gün, 1..9
        header_row = [Paragraph("Gün", body_style)]
        for p in range(1, 10):
            header_row.append(Paragraph(str(p), body_style))

        data: List[List[Paragraph]] = [header_row]
        empty_cells: List[Tuple[int, int]] = []
        for day in WEEK_DAYS:
            periods = by_day.get(day, {})
            row: List[Paragraph] = [Paragraph(day, body_style)]
            for p in range(1, 10):
                entries = periods.get(p, []) or []
                if entries:
                    lines = [
                        f"{abbreviate_lesson_name(lesson)} ({cls})"
                        for lesson, cls in entries
                    ]
                    cell_html = "<br/>".join(lines)
                    row.append(Paragraph(cell_html, body_style))
                else:
                    row.append(Paragraph("", body_style))
                    empty_cells.append((len(data), len(row) - 1))
            data.append(row)

        header_h = 6 * mm
        day_h = 10 * mm
        row_heights = [header_h] + [day_h] * (len(data) - 1)

        style_commands: List[Tuple] = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), font_name),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 1), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("BACKGROUND", (0, 1), (-1, -1), colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        for r, c in empty_cells:
            style_commands.append(("BACKGROUND", (c, r), (c, r), colors.lightgrey))

        tbl = Table(
            data,
            colWidths=[20 * mm] + [17 * mm] * 9,
            rowHeights=row_heights,
        )
        tbl.setStyle(TableStyle(style_commands))

        story.append(tbl)

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def main() -> None:
    st.set_page_config(page_title="Ders Programı Oluşturucu", layout="wide")

    st.markdown(
        "1. **Sınıf Ders Programlarını Yükle (PDF)**\n"
        "2. **Öğretmen Ders Programlarına Dönüştür**"
    )

    uploaded_files = st.file_uploader(
        "Sınıf ders programı PDF dosyalarını seçin (bir veya birden fazla)",
        type=["pdf"],
        accept_multiple_files=True,
    )

    if not uploaded_files:
        st.info("En az bir PDF yükle.")
        return

    all_class_schedules: Dict[str, List[Tuple[str, List[str]]]] = {}
    all_teacher_schedules: TeacherSchedules = {}

    # Tam ekran loading katmanı (iş bitince kaldırılacak)
    loading_placeholder = st.empty()
    loading_placeholder.markdown(
        _loading_overlay_html("PDF dosyaları okunuyor ve analiz ediliyor… Lütfen bekleyin."),
        unsafe_allow_html=True,
    )
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
    loading_placeholder.empty()

    if not all_teacher_schedules:
        st.error("Yüklenen PDF'lerden öğretmen programı çıkarılamadı.")
        return

    teachers = sorted(all_teacher_schedules.keys())

    output_mode = st.radio(
        "Çıktı tipi",
        options=["Tüm öğretmenler (tek PDF)", "Öğretmen seçimi"],
        horizontal=True,
    )

    if output_mode == "Tüm öğretmenler (tek PDF)":
        if st.button("Tüm öğretmenler için programı göster"):
            # Önce veri setinden anında HTML önizleme (PDF yok, engel yok)
            st.success("Aşağıda öğretmen programları veri setinden üretilmiş önizlemedir.")
            
            # PDF hazırlanırken loading overlay
            pdf_loading = st.empty()
            pdf_loading.markdown(
                _loading_overlay_html("PDF dosyası hazırlanıyor… Lütfen bekleyin."),
                unsafe_allow_html=True,
            )
            pdf_bytes = build_teacher_pdf_bytes(all_teacher_schedules, selected_teachers=None)
            pdf_loading.empty()
            
            if pdf_bytes:
                # Üstte: Önizleme başlığı ve sağda PDF indir butonu
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown("**Önizleme**")
                with col2:
                    st.download_button(
                        label="PDF olarak indir",
                        data=pdf_bytes,
                        file_name="tum_ogretmenler_ders_programi.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
                
                html_preview = _teacher_schedules_to_html(all_teacher_schedules, selected_teachers=None)
                st.components.v1.html(html_preview, height=800, scrolling=True)
                
                # Altta da PDF indir butonu
                st.markdown("---")
                st.download_button(
                    label="PDF olarak indir",
                    data=pdf_bytes,
                    file_name="tum_ogretmenler_ders_programi.pdf",
                    mime="application/pdf",
                )
    else:
        selected = st.multiselect(
            "Öğretmen seçin (birden fazla seçebilirsiniz)",
            options=teachers,
            help="Listeden öğretmenleri seçin. Birden fazla öğretmen seçerek hepsinin programını tek PDF'de indirebilirsiniz."
        )
        if selected and st.button("Seçili öğretmenler için programı göster"):
            # Önce veri setinden anında HTML önizleme
            st.success("Aşağıda seçtiğiniz öğretmenlerin programları veri setinden üretilmiş önizlemedir.")
            
            # PDF hazırlanırken loading overlay
            pdf_loading = st.empty()
            pdf_loading.markdown(
                _loading_overlay_html("PDF dosyası hazırlanıyor… Lütfen bekleyin."),
                unsafe_allow_html=True,
            )
            pdf_bytes = build_teacher_pdf_bytes(all_teacher_schedules, selected_teachers=selected)
            pdf_loading.empty()
            
            if pdf_bytes:
                # Üstte: Önizleme başlığı ve sağda PDF indir butonu
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown("**Önizleme**")
                with col2:
                    st.download_button(
                        label="PDF olarak indir",
                        data=pdf_bytes,
                        file_name="secili_ogretmenler_ders_programi.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
                
                html_preview = _teacher_schedules_to_html(all_teacher_schedules, selected_teachers=selected)
                st.components.v1.html(html_preview, height=800, scrolling=True)
                
                # Altta da PDF indir butonu
                st.markdown("---")
                st.download_button(
                    label="PDF olarak indir",
                    data=pdf_bytes,
                    file_name="secili_ogretmenler_ders_programi.pdf",
                    mime="application/pdf",
                )


if __name__ == "__main__":
    main()

