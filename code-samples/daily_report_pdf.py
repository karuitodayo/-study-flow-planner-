# 元ファイル: services/daily_reports.py（PDF出力に関わる部分の抜粋）
#
# 何をするコード:
#   保存済みのPDCA日報（学習記録）を、A4縦1枚のPDFに書き出す処理。
#   目標のステータス・達成条件の進捗・タスク完了率・予定/実績時間の集計など、
#   自動生成した管理情報は一切含めず、本人が入力した文章と登録済みの目標タイトルを
#   そのまま記載するだけの読み取り専用機能として実装している。
#
#   日本語フォントは、
#     1) Windows同梱のYu Gothic（通常/太字）
#     2) Windows同梱のMeiryo（通常/太字）
#     3) リポジトリに同梱済みの日本語フォント（単一ウェイト）
#     4) reportlab組み込みの日本語CIDフォント（フォントファイル不要）
#   の順に試し、最初に使えたものを使う（Windows以外の環境でも3・4で動作する）。
#
# 抜粋範囲についての注記:
#   元ファイルには日報の保存（save_report等）、実績・予定の集計（aggregate_plan等）、
#   週次振り返り（weekly_summary）、Markdown出力（to_markdown）なども含まれるが、
#   「PDF生成」というテーマに絞り、保存済みデータの取得〜PDF組み立てまでの部分のみを
#   抜粋している。database.db.get_connection は、個人の学習記録を含むSQLiteデータベースへの
#   接続処理のため、接続関数そのものの中身は本リポジトリに含めていない。

import os
from database.db import get_connection, BASE_DIR
from services import goals as goal_service


def get_report(report_date: str) -> dict | None:
    """指定日の保存済み日報を取得する。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM daily_reports WHERE report_date = ?", (report_date,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def simplified_goal_lines(report_date: str) -> dict:
    """月間・月間目標を、登録済みの目標タイトルのみの一覧にする（PDF・Markdown出力用）。
    ステータス（進行中・完了等）や達成条件の進捗・タスク完了率などの管理情報は一切含めない
    （それらは「目標管理」画面でのみ確認する）。
    """
    monthly_lines = [g["title"] for g in goal_service.current_monthly_goals(report_date)]
    weekly_lines = [g["title"] for g in goal_service.current_weekly_goals(report_date)]
    return {"monthly": monthly_lines, "weekly": weekly_lines}


# ------------------------------------------------------------------
# PDF出力
#
# 構成（大項目／小項目）:
#   1. 今月の目標
#   2. 今週の目標
#   3. 進捗状況
#      - 今日したこと／1日の振り返り／明日やること／明日意識すること
# ------------------------------------------------------------------
_PDF_FONTS_CACHE: tuple[str, str] | None = None


def _pdf_fonts() -> tuple[str, str]:
    """PDFで使う日本語フォント名(通常, 太字)を返す。初回呼び出し時のみ登録する。"""
    global _PDF_FONTS_CACHE
    if _PDF_FONTS_CACHE:
        return _PDF_FONTS_CACHE

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    windows_pairs = [
        (r"C:\Windows\Fonts\YuGothR.ttc", r"C:\Windows\Fonts\YuGothB.ttc"),
        (r"C:\Windows\Fonts\meiryo.ttc", r"C:\Windows\Fonts\meiryob.ttc"),
    ]
    for regular_path, bold_path in windows_pairs:
        if os.path.exists(regular_path) and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont("SFPJapanese", regular_path, subfontIndex=0))
                pdfmetrics.registerFont(TTFont("SFPJapanese-Bold", bold_path, subfontIndex=0))
                pdfmetrics.registerFontFamily(
                    "SFPJapanese", normal="SFPJapanese", bold="SFPJapanese-Bold"
                )
                _PDF_FONTS_CACHE = ("SFPJapanese", "SFPJapanese-Bold")
                return _PDF_FONTS_CACHE
            except Exception:
                pass

    bundled = BASE_DIR / "data" / "drill_references" / "MED-L1-001" / "NotoSansJP.ttf"
    if bundled.exists():
        try:
            pdfmetrics.registerFont(TTFont("SFPJapanese", str(bundled)))
            pdfmetrics.registerFontFamily("SFPJapanese", normal="SFPJapanese", bold="SFPJapanese")
            _PDF_FONTS_CACHE = ("SFPJapanese", "SFPJapanese")
            return _PDF_FONTS_CACHE
        except Exception:
            pass

    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    _PDF_FONTS_CACHE = ("HeiseiKakuGo-W5", "HeiseiKakuGo-W5")
    return _PDF_FONTS_CACHE


def generate_pdf(report_date: str) -> bytes:
    """日報をPDFに書き出す（A4縦1枚を基本とし、文章量が多い場合だけ自然に改ページする）。

    目標のステータス・達成条件の進捗・タスク完了率・予定時間や実績時間の集計など、
    自動生成した管理情報は一切含めない。本人が入力した文章はそのまま記載し、
    要約・書き換え・補足説明の追加はしない。
    """
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, HRFlowable

    report = get_report(report_date) or {}
    goals = simplified_goal_lines(report_date)

    font, bold = _pdf_fonts()
    navy = colors.HexColor("#243047")
    subtext = colors.HexColor("#667085")
    border = colors.HexColor("#E8EBF0")

    styles = {
        "title": ParagraphStyle("sfp_title", fontName=bold, fontSize=15, leading=18, textColor=navy, spaceAfter=1),
        "meta": ParagraphStyle("sfp_meta", fontName=font, fontSize=8.5, leading=11, textColor=subtext, spaceAfter=8),
        "section": ParagraphStyle("sfp_section", fontName=bold, fontSize=11.5, leading=14, textColor=navy, spaceBefore=8, spaceAfter=2),
        "label": ParagraphStyle("sfp_label", fontName=bold, fontSize=9, leading=12, textColor=navy, leftIndent=10, spaceBefore=3, spaceAfter=1),
        "body": ParagraphStyle("sfp_body", fontName=font, fontSize=9, leading=12.5, textColor=navy, leftIndent=10, spaceAfter=1.5),
        "empty": ParagraphStyle("sfp_empty", fontName=font, fontSize=9, leading=12.5, textColor=subtext, leftIndent=10, spaceAfter=1.5),
    }

    def esc(value) -> str:
        text = str(value if value is not None else "")
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return text.replace("\n", "<br/>")

    story = []

    def section(text):
        story.append(Paragraph(esc(text), styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=border, spaceAfter=3))

    def label(text):
        story.append(Paragraph(esc(text), styles["label"]))

    def body(text, empty=False):
        story.append(Paragraph(esc(text), styles["empty"] if empty else styles["body"]))

    story.append(Paragraph(esc(f"学習日報 {report_date}"), styles["title"]))
    story.append(Paragraph("保存済みの日報をもとに作成（本人の入力内容はそのまま記載）", styles["meta"]))

    section("今月の目標")
    if goals["monthly"]:
        for line in goals["monthly"]:
            body(f"・{line}")
    else:
        body("今月の目標は未登録です。", empty=True)

    section("今週の目標")
    if goals["weekly"]:
        for line in goals["weekly"]:
            body(f"・{line}")
    else:
        body("今週の目標は未登録です。", empty=True)

    section("進捗状況")

    label("今日したこと")
    body(report.get("did_today") or "未記入です。", empty=not report.get("did_today"))

    label("1日の振り返り")
    body(report.get("check_reflection") or "未記入です。", empty=not report.get("check_reflection"))

    label("明日やること")
    body(report.get("action_tomorrow") or "未記入です。", empty=not report.get("action_tomorrow"))

    label("明日意識すること")
    body(report.get("action_mindset") or "未記入です。", empty=not report.get("action_mindset"))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=13 * mm, bottomMargin=13 * mm,
        title=f"学習日報 {report_date}",
    )
    doc.build(story)
    return buffer.getvalue()
