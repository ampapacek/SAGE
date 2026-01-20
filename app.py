import csv
import json
import io
import logging
import html
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
import bleach
import markdown
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from config import Config, DATA_DIR, PROCESSED_DIR, UPLOAD_DIR
from db import db
from grading.schemas import safe_json_loads
from grading.schemas import render_grade_output, validate_grade_result
from models import (
    Assignment,
    GradeResult,
    GradingJob,
    JobStatus,
    RubricStatus,
    RubricVersion,
    SubmissionFile,
    Submission,
)
from processing.file_ingest import (
    collect_submission_images,
    collect_submission_text,
    ingest_zip_upload,
    save_submission_files,
)
from processing.job_queue import enqueue_rubric_job, enqueue_submission_job, init_job_queue

logger = logging.getLogger(__name__)

_PRICE_ESTIMATE_RE = re.compile(r"price_estimate=\$([0-9]+(?:\.[0-9]+)?)")
_IMAGE_CAPABLE_MODELS = {
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "o4-mini",
}
_NON_IMAGE_MODELS = {
    "o3-mini",
}
_MODEL_OPTIONS = [
    "gpt-4o-mini",
    "gpt-5-mini",
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.1-mini",
    "o4-mini",
    "o3-mini",
    "gpt-5",
    "gpt-5-nano",
]
_PROVIDER_OPTIONS = [
    "openai",
    "custom1",
    "custom2",
    "custom3",
]
TRANSLATIONS = {
    "en": {
        "nav_back": "Back",
        "nav_home": "Home",
        "assignments_create": "Create Assignment",
        "assignments_toggle_create": "Toggle Create Assignment",
        "assignments_title": "Assignments",
        "assignment_text": "Assignment Text",
        "title": "Title",
        "create": "Create",
        "created": "Created",
        "open": "Open",
        "delete": "Delete",
        "no_assignments": "No assignments yet.",
        "assignments_empty_hint": "Start by creating your first assignment below.",
        "assignments_empty_steps": "To create an assignment, use the form above.",
        "assignment_title_placeholder": "Example: Week 1 homework",
        "assignment_text_placeholder": "Paste the problem statement here. You can include Markdown or LaTeX.",
        "guide_text_placeholder": (
            "Draft a grading guide: parts, points, and criteria.\n"
            "Or choose provider and model to generate the draft and reference solution by LLM."
        ),
        "reference_solution_placeholder": (
            "Write the ideal solution outline for each part.\n"
            "Or choose provider and model to generate the draft and reference solution by LLM."
        ),
        "submission_text_placeholder": "Optional: add student text, clarifications, or notes.",
        "grading_guides": "Grading Guides",
        "guide_number": "Guide #",
        "status": "Status",
        "provider": "Provider",
        "model": "Model",
        "duration_s": "Duration (s)",
        "price": "Price",
        "preview": "Preview",
        "actions": "Actions",
        "view": "View",
        "approve_guide": "Approve Guide",
        "activate_guide": "Activate Guide",
        "cancel": "Cancel",
        "delete_guide": "Delete Guide",
        "criteria_one_per_line": "Criteria (one per line)",
        "add_part": "Add part",
        "reference_line_hint": "One item per line. Use 'key: value' for labeled steps.",
        "add_reference_part": "Add reference part",
        "no_guides": "No grading guides yet.",
        "guide_creation": "Grading Guide Creation",
        "toggle_guide_form": "Toggle Guide Form",
        "create_guide_manual": "Create Grading Guide (Manual)",
        "guide_text": "Grading Guide Text",
        "reference_solution": "Reference Solution",
        "save_draft": "Save Draft",
        "generate_draft_guide": "Generate Draft Guide",
        "choose_model_generation": "Choose model for generation",
        "generate_draft_llm": "Generate Draft via LLM",
        "guide_empty_hint": "Start here -> Create a grading guide below.",
        "guide_empty_note": "After the grading guide is ready, you can upload submissions.",
        "upload_submissions": "Upload Submissions",
        "toggle_uploads": "Toggle Uploads",
        "approve_guide_enable": "Approve a grading guide to enable submissions.",
        "submissions_desc": "Student uploads and feedback results show up here.",
        "show_settings": "Show settings for",
        "hide_settings": "Hide settings for",
        "student_identifier": "Student Identifier (single upload)",
        "model_optional": "Model (optional)",
        "model_selection": "Model selection",
        "submitted_text_optional": "Submitted Text (optional)",
        "files_label": "Files (PDF, images, text)",
        "drop_files_hint": "Drag and drop files here or click to browse.",
        "files_hint": "Upload PDFs, images, or text files with the student solution.",
        "zip_label": "Or ZIP with one file per student",
        "drop_zip_hint": "Drag and drop a ZIP with one file per student, or click to browse.",
        "zip_hint": "To speed things up, upload one ZIP with multiple students at once.",
        "upload": "Upload",
        "submissions": "Submissions",
        "student": "Student",
        "grade": "Grade",
        "jobs_desc": "Background grading jobs and their progress.",
        "no_submissions": "No submissions yet.",
        "no_jobs": "No jobs yet.",
        "export_csv": "Export CSV",
        "jobs": "Jobs",
        "submission_number": "Submission #",
        "total_price_estimate": "Total Price Estimate (guides + jobs):",
        "delete_assignment": "Delete Assignment",
        "edit_assignment": "Edit Assignment",
        "edit": "Edit",
        "save_changes": "Save Changes",
        "edit_guide": "Edit Grading Guide",
        "submission": "Submission",
        "assignment": "Assignment",
        "submitted": "Submitted",
        "submitted_text": "Submitted Text",
        "no_submitted_text": "No submitted text.",
        "files": "Files",
        "no_files": "No files.",
        "images": "Images",
        "no_images": "No images rendered.",
        "grade_result": "Grade Result",
        "error": "Error",
        "raw_llm_response": "Raw LLM Response",
        "raw_json": "Raw JSON",
        "no_grade_result": "No grade result yet.",
        "job": "Job",
        "student_hint": "Click the submission number to open details.",
        "guide_version": "Guide Version",
        "started": "Started",
        "finished": "Finished",
        "price_estimate": "Price Estimate",
        "processing_summary": "Processing Summary",
        "show_processing_summary": "Show Processing Summary",
        "hide_processing_summary": "Hide Processing Summary",
        "terminate_job": "Terminate Job",
        "delete_job": "Delete Job",
        "model_options": "Model options",
        "rerun_job": "Rerun Job",
        "raw_llm_response_error": "Raw LLM Response (Error)",
        "settings": "Settings",
        "settings_helper": "Edit values stored in .env. Some changes require a restart.",
        "save_settings": "Save Settings",
        "restart_required": "Restart required.",
        "guide": "Grading Guide",
        "approved_guide_in_use": "Approved guide in use.",
        "guide_not_ready": "Guide is not ready to approve.",
        "cancel_generation": "Cancel Generation",
        "open_assignments": "Open Assignments",
        "quick_start": "Quick Start",
        "quick_start_brief": "Quick Start",
        "why_sage": "Why SAGE",
        "quick_start_walkthrough": "Quick Start Walkthrough",
        "quick_start_cta_title": "Let’s start using the app!",
        "quick_start_cta_desc": "Create your first assignment and see SAGE move from setup to feedback in minutes.",
        "quick_start_cta_button": "Start creating assignments",
        "top_bar_icons": "Top Bar Icons",
        "responsible_use": "Responsible Use",
        "why_sage1_title": "Automatic grading",
        "why_sage1_text": "Use grading guides with part-level scoring and clear deductions.",
        "why_sage2_title": "Flexible formats",
        "why_sage2_text": "Support PDFs, images, and text, plus ZIP uploads for whole classes.",
        "why_sage3_title": "Structured output",
        "why_sage3_text": "Export all gradings for all students in a clean CSV table.",
        "why_sage4_title": "Control over all steps",
        "why_sage4_text": "All parts of the process are editable, end to end.",
        "sage_home": "SAGE Home",
        "language_currency": "Language",
        "theme": "Theme",
        "back_to_top": "Back to top",
        "total_points": "Total points",
        "max_points": "Max points",
        "criteria": "Criteria",
        "part_label": "Part",
        "edit_grade": "Edit grading and feedback",
        "update_grade": "Save feedback",
        "grade_json": "Grade JSON",
        "rendered_feedback": "Rendered feedback",
        "total_points_override": "Total points",
        "edit_grade_hint": (
            "Edit the feedback text or total points."
        ),
        "delete_submission": "Delete Submission",
        "delete_submission_confirm": "Delete this submission and all related data?",
        "previous_image": "Previous image",
        "next_image": "Next image",
        "close": "Close",
        "custom_model_label": "Custom model name",
        "custom_model_placeholder": "Enter provider model name",
        "other_model_option": "Other",
        "provider_label": "Provider",
        "provider_openai": "OpenAI",
        "provider_other": "Other",
        "show_guide": "Show grading guide",
        "hide_guide": "Hide grading guide",
        "show_reference_solution": "Show reference solution",
        "hide_reference_solution": "Hide reference solution",
        "no_guide_available": "No grading guide available.",
        "show_assignment_text": "Show assignment text",
        "hide_assignment_text": "Hide assignment text",
        "hero_title": "Save time with automatic grading assistant.",
        "sage_acronym": "Smart Automated Grading Engine",
        "hero_subtitle": (
            "A grading workspace for assignments, submissions, and AI-assisted feedback "
            "that helps teachers move faster while staying in control."
        ),
        "hero_assignments_desc": "Manage assignments, grading guides, and submissions.",
        "hero_quickstart_desc": "Jump to a step-by-step walkthrough below.",
        "hero_chip_llm": "LLM-guided feedback",
        "hero_chip_formats": "PDF + image + text",
        "hero_chip_zip": "ZIP uploads supported",
        "quick_step_1": "Create an assignment with the prompt or problem statement.",
        "quick_step_2": "Create or generate a draft grading guide and approve it.",
        "quick_step_3": "Upload submissions (PDFs, images, or text).",
        "quick_step_4": "Review automated results and export grades.",
        "topbar_home_desc": "Click the SAGE logo in the header to return here.",
        "topbar_settings_desc": "Update API keys, model defaults, and limits.",
        "topbar_language_desc": "Switch between English and Czech. Currency follows the language.",
        "topbar_theme_desc": "Toggle light or dark mode.",
        "step_label": "Step",
        "alt_sage_logo": "SAGE logo",
        "alt_settings_icon": "Settings icon",
        "alt_flag_en": "English flag icon",
        "alt_flag_cs": "Czech flag icon",
        "alt_light_mode": "Light mode icon",
        "alt_dark_mode": "Dark mode icon",
        "walk_step1_title": "Step 1 — Create an assignment",
        "walk_step1_desc": "Add the assignment title and problem statement to start a new grading run.",
        "walk_step1a_title": "Step 1a — Example assignment",
        "walk_step1a_desc": "Check a sample assignment layout to keep instructions consistent.",
        "walk_step2_title": "Step 2 — Build a grading guide",
        "walk_step2_desc": "Create a guide manually or generate a draft with the model, then approve it.",
        "walk_step2a_title": "Step 2a — Guide examples",
        "walk_step2a_desc": "See an example grading guide alongside the reference solution.",
        "walk_step3_title": "Step 3 — Upload submissions",
        "walk_step3_desc": "Upload PDFs, images, or text files. ZIPs with all students are supported.",
        "walk_step4_title": "Step 4 — Review results",
        "walk_step4_desc": "Inspect the feedback and export grades after instructor review.",
        "walk_step4a_title": "Step 4a — Detailed submission view",
        "walk_step4a_desc": "Open individual submissions to see detailed feedback and annotated results.",
        "walk_alt_step1": "Create assignment screen",
        "walk_alt_step1a": "Example assignment",
        "walk_alt_step2": "Grading guide creation screen",
        "walk_alt_step2a_guide": "Example grading guide",
        "walk_alt_step2a_reference": "Example reference solution",
        "walk_alt_step3": "Submission upload screen",
        "walk_alt_step4": "Results and export screen",
        "walk_alt_step4a_left": "Submission detail view",
        "walk_alt_step4a_right": "Submission detail feedback",
        "responsible_use_p1": (
            "SAGE is intended to speed up grading and support teacher review. All grading guides, "
            "outputs, and final grades must be verified by the instructor before release. "
            "Do not issue grades based only on model output."
        ),
        "responsible_use_p2": (
            "Confirm with students and your department, school, or university that this tool is permitted "
            "for use. Student data is private and should be handled accordingly."
        ),
        "license_notice": "Licensed under Apache 2.0.",
        "ideas_note": "Have ideas for improvement? Email ales.papacek@seznam.cz or open an issue on GitHub.",
    },
    "cs": {
        "nav_back": "Zpět",
        "nav_home": "Domů",
        "assignments_create": "Vytvořit úkol",
        "assignments_toggle_create": "Zobrazit/skrýt formulář",
        "assignments_title": "Úkoly",
        "assignment_text": "Text úkolu",
        "title": "Název",
        "create": "Vytvořit",
        "created": "Vytvořeno",
        "open": "Otevřít",
        "delete": "Smazat",
        "no_assignments": "Zatím žádné úkoly.",
        "assignments_empty_hint": "Začněte vytvořením prvního úkolu níže.",
        "assignments_empty_steps": "Pro vytvoření úkolu použijte formulář výše.",
        "assignment_title_placeholder": "Příklad: Úkol 1",
        "assignment_text_placeholder": "Sem vložte text úkolu. Můžete použít Markdown nebo LaTeX.",
        "guide_text_placeholder": (
            "Sepište kritéria hodnocení: části, body a kritéria.\n"
            "Nebo vyberte poskytovatele a model pro generování konceptu a řešení přes LLM."
        ),
        "reference_solution_placeholder": (
            "Napište vzorové řešení ke každé části.\n"
            "Nebo vyberte poskytovatele a model pro generování konceptu a řešení přes LLM."
        ),
        "submission_text_placeholder": "Volitelně: přidejte text řešení, upřesnění nebo poznámky.",
        "grading_guides": "Kritéria hodnocení",
        "guide_number": "Kritéria #",
        "status": "Stav",
        "provider": "Poskytovatel",
        "model": "Model",
        "duration_s": "Délka (s)",
        "price": "Cena",
        "preview": "Náhled",
        "actions": "Akce",
        "view": "Zobrazit",
        "approve_guide": "Schválit kritéria",
        "activate_guide": "Aktivovat kritéria",
        "cancel": "Zrušit",
        "delete_guide": "Smazat kritéria",
        "criteria_one_per_line": "Kritéria (jedno na řádek)",
        "add_part": "Přidat část",
        "reference_line_hint": "Jedna položka na řádek. Použijte 'klíč: hodnota' pro popisky.",
        "add_reference_part": "Přidat část řešení",
        "no_guides": "Zatím žádná kritéria hodnocení.",
        "guide_creation": "Vytvoření kritérií hodnocení",
        "toggle_guide_form": "Zobrazit/skrýt formulář",
        "create_guide_manual": "Vytvořit kritéria hodnocení (ručně)",
        "guide_text": "Text kritérií hodnocení",
        "reference_solution": "Vzorové řešení",
        "save_draft": "Uložit koncept",
        "generate_draft_guide": "Vygenerovat koncept kritérií",
        "choose_model_generation": "Vyberte model pro generování",
        "generate_draft_llm": "Vygenerovat koncept přes LLM",
        "guide_empty_hint": "Začněte zde → Vytvořte kritéria hodnocení níže.",
        "guide_empty_note": "Po dokončení kritérií hodnocení můžete nahrát řešení.",
        "upload_submissions": "Nahrát řešení",
        "toggle_uploads": "Zobrazit/skrýt nahrávání",
        "approve_guide_enable": "Schvalte kritéria hodnocení, aby bylo možné nahrávat řešení.",
        "submissions_desc": "Zde se zobrazí nahraná řešení studentů a výsledná zpětná vazba.",
        "show_settings": "Zobrazit nastavení pro",
        "hide_settings": "Skrýt nastavení pro",
        "student_identifier": "Identifikátor studenta (jednotlivě)",
        "model_optional": "Model (volitelný)",
        "model_selection": "Výběr modelu",
        "submitted_text_optional": "Text řešení (volitelný)",
        "files_label": "Soubory (PDF, obrázky, text)",
        "drop_files_hint": "Přetáhněte soubory sem nebo klikněte pro výběr.",
        "files_hint": "Nahrajte PDF, obrázky nebo textové soubory s řešením studenta.",
        "zip_label": "Nebo ZIP s jedním souborem na studenta",
        "drop_zip_hint": "Přetáhněte ZIP s jedním souborem na studenta, nebo klikněte pro výběr.",
        "zip_hint": "Pro urychlení nahrajte jeden ZIP s více studenty najednou.",
        "upload": "Nahrát",
        "submissions": "Řešení",
        "student": "Student",
        "grade": "Body",
        "jobs_desc": "Úlohy na pozadí a jejich aktuální stav.",
        "no_submissions": "Zatím žádná řešení.",
        "no_jobs": "Zatím žádné úlohy.",
        "export_csv": "Export CSV",
        "jobs": "Úlohy",
        "submission_number": "Řešení #",
        "total_price_estimate": "Celkový odhad ceny (průvodci + úlohy):",
        "delete_assignment": "Smazat úkol",
        "edit_assignment": "Upravit úkol",
        "edit": "Upravit",
        "save_changes": "Uložit změny",
        "edit_guide": "Upravit kritéria hodnocení",
        "submission": "Řešení",
        "assignment": "Úkol",
        "submitted": "Nahráno",
        "submitted_text": "Text řešení",
        "no_submitted_text": "Žádný text řešení.",
        "files": "Soubory",
        "no_files": "Žádné soubory.",
        "images": "Obrázky",
        "no_images": "Žádné obrázky.",
        "grade_result": "Výsledek hodnocení",
        "error": "Chyba",
        "raw_llm_response": "Surová odpověď LLM",
        "raw_json": "Surový JSON",
        "no_grade_result": "Zatím žádný výsledek.",
        "job": "Úloha",
        "student_hint": "Kliknutím na číslo řešení otevřete detail.",
        "guide_version": "Verze kritérií",
        "started": "Spuštěno",
        "finished": "Dokončeno",
        "price_estimate": "Odhad ceny",
        "processing_summary": "Souhrn zpracování",
        "show_processing_summary": "Zobrazit souhrn",
        "hide_processing_summary": "Skrýt souhrn",
        "terminate_job": "Ukončit úlohu",
        "delete_job": "Smazat úlohu",
        "model_options": "Možnosti modelu",
        "rerun_job": "Spustit znovu",
        "raw_llm_response_error": "Surová odpověď LLM (chyba)",
        "settings": "Nastavení",
        "settings_helper": "Upravte hodnoty v .env. Některé změny vyžadují restart.",
        "save_settings": "Uložit nastavení",
        "restart_required": "Vyžaduje restart.",
        "guide": "Kritéria hodnocení",
        "approved_guide_in_use": "Schválená kritéria jsou aktivní.",
        "guide_not_ready": "Kritéria nejsou připravena ke schválení.",
        "cancel_generation": "Zrušit generování",
        "open_assignments": "Spravovat úkoly",
        "quick_start": "Návod krok za krokem",
        "quick_start_brief": "Rychlý přehled",
        "why_sage": "Proč SAGE",
        "quick_start_walkthrough": "Návod krok za krokem",
        "quick_start_cta_title": "Pojďme začít s aplikací!",
        "quick_start_cta_desc": "Vytvořte první úkol, nahrajte řešení a sledujte, jak vám SAGE pomůže s vaší prací.",
        "quick_start_cta_button": "Začít vytvářet úkoly",
        "top_bar_icons": "Ikony v horní liště",
        "responsible_use": "Důležité upozornění",
        "why_sage1_title": "Automatické hodnocení",
        "why_sage1_text": "Hodnocení, které se drží vytyčených kritérií.",
        "why_sage2_title": "Flexibilní formáty",
        "why_sage2_text": "Podpora PDF, obrázků i textu, včetně možnosti nahrání ZIP souboru s řešením několika studentů.",
        "why_sage3_title": "Strukturované výstupy",
        "why_sage3_text": "Export všech hodnocení všech studentů do přehledné tabulky CSV.",
        "why_sage4_title": "Kontrola nad všemi kroky",
        "why_sage4_text": "Všechny části procesu jsou upravitelné od začátku do konce.",
        "sage_home": "SAGE domů",
        "language_currency": "Jazyk",
        "theme": "Motiv",
        "back_to_top": "Zpět nahoru",
        "total_points": "Celkem bodů",
        "max_points": "Max. bodů",
        "criteria": "Kritéria",
        "part_label": "Část",
        "edit_grade": "Upravit hodnocení a zpětnou vazbu",
        "update_grade": "Uložit zpětnou vazbu",
        "grade_json": "JSON hodnocení",
        "rendered_feedback": "Zobrazená zpětná vazba",
        "total_points_override": "Celkem bodů",
        "edit_grade_hint": (
            "Upravte text zpětné vazby nebo celkové body."
        ),
        "delete_submission": "Smazat řešení",
        "delete_submission_confirm": "Smazat toto řešení a všechna související data?",
        "previous_image": "Předchozí obrázek",
        "next_image": "Další obrázek",
        "close": "Zavřít",
        "custom_model_label": "Vlastní název modelu",
        "custom_model_placeholder": "Zadejte název modelu poskytovatele",
        "other_model_option": "Jiný",
        "provider_label": "Poskytovatel",
        "provider_openai": "OpenAI",
        "provider_other": "Jiný",
        "show_guide": "Zobrazit kritéria hodnocení",
        "hide_guide": "Skrýt kritéria hodnocení",
        "show_reference_solution": "Zobrazit referenční řešení",
        "hide_reference_solution": "Skrýt referenční řešení",
        "no_guide_available": "Žádná kritéria hodnocení nejsou k dispozici.",
        "show_assignment_text": "Zobrazit text úkolu",
        "hide_assignment_text": "Skrýt text úkolu",
        "hero_title": "Ušetřete čas s automatickým asistentem na hodnocení úkolů.",
        "sage_acronym": "Smart Automated Grading Engine",
        "hero_subtitle": (
            "Pracovní prostor pro úkoly, řešení a automatické hodnocení pomocí AI. "
            "Vše pro pomoc učitelům postupovat rychleji a přitom mít vše pod kontrolou."
        ),
        "hero_assignments_desc": "Vytvořte nové úkoly, prohlížejte všechny vytvořené úkoly.",
        "hero_quickstart_desc": "Přejděte na návod níže.",
        "hero_chip_llm": "LLM zpětná vazba",
        "hero_chip_formats": "PDF + obrázky + text",
        "hero_chip_zip": "Podpora ZIP nahrávek",
        "quick_step_1": "Vytvořte nový úkol se zadáním úlohy.",
        "quick_step_2": "Vytvořte nebo vygenerujte koncept kritérií a schvalte jej.",
        "quick_step_3": "Nahrajte řešení (PDF, obrázky nebo text).",
        "quick_step_4": "Zkontrolujte automatické obodování a zpětnou vazbu. Výsledky můžete exportovat do přehledné tabulky (CSV).",
        "topbar_home_desc": "Klikněte na logo SAGE v horní liště a vraťte se sem.",
        "topbar_settings_desc": "Upravte API klíče, výchozí modely a limity.",
        "topbar_language_desc": "Přepněte mezi angličtinou a češtinou. Měna se řídí jazykem.",
        "topbar_theme_desc": "Přepněte světlý nebo tmavý režim.",
        "step_label": "Krok",
        "alt_sage_logo": "Logo SAGE",
        "alt_settings_icon": "Ikona nastavení",
        "alt_flag_en": "Ikona anglické vlajky",
        "alt_flag_cs": "Ikona české vlajky",
        "alt_light_mode": "Ikona světlého režimu",
        "alt_dark_mode": "Ikona tmavého režimu",
        "walk_step1_title": "Krok 1 — Vytvořte úkol",
        "walk_step1_desc": "Přidejte název úkolu a zadání úlohy pro zahájení hodnocení.",
        "walk_step1a_title": "Krok 1a — Ukázkový úkol",
        "walk_step1a_desc": "Ukázka zadání úkolu",
        "walk_step2_title": "Krok 2 — Sestavte kritéria hodnocení",
        "walk_step2_desc": "Vytvořte kritéria ručně nebo vygenerujte koncept a schvalte jej.",
        "walk_step2a_title": "Krok 2a — Ukázky kritérií",
        "walk_step2a_desc": "Podívejte se na ukázku kritérií vedle vzorového řešení.",
        "walk_step3_title": "Krok 3 — Nahrajte řešení",
        "walk_step3_desc": "Nahrajte PDF, obrázky nebo text. ZIP s více studenty je podporován.",
        "walk_step4_title": "Krok 4 — Zkontrolujte výsledky",
        "walk_step4_desc": "Zkontrolujte automatické obodování a zpětnou vazbu. Výsledky lze exportovat do přehledné tabulky (CSV).",
        "walk_step4a_title": "Krok 4a — Detail řešení",
        "walk_step4a_desc": "Otevřete detailní řešení s komentovanou zpětnou vazbou.",
        "walk_alt_step1": "Obrazovka vytvoření úkolu",
        "walk_alt_step1a": "Ukázkový úkol",
        "walk_alt_step2": "Obrazovka tvorby kritérií",
        "walk_alt_step2a_guide": "Ukázka kritérií hodnocení",
        "walk_alt_step2a_reference": "Ukázka referenčního řešení",
        "walk_alt_step3": "Obrazovka nahrání řešení",
        "walk_alt_step4": "Obrazovka výsledků a exportu",
        "walk_alt_step4a_left": "Detail řešení",
        "walk_alt_step4a_right": "Zpětná vazba k řešení",
        "responsible_use_p1": (
            "SAGE má urychlit hodnocení a podpořit kontrolu učitele. Všechna kritéria, "
            "výstupy i finální známky musí být před zveřejněním ověřeny instruktorem. "
            "Nevydávejte známky pouze na základě výstupu modelu."
        ),
        "responsible_use_p2": (
            "Ověřte se studenty a vedením katedry, školy či univerzity, že je tento nástroj "
            "povolený k použití. Studentská data jsou soukromá a je nutné s nimi tak zacházet."
        ),
        "license_notice": "Licencováno pod Apache 2.0.",
        "ideas_note": "Máte nápad na zlepšení? Napište na ales.papacek@seznam.cz nebo založte issue na GitHubu.",
    },
}
_SETTINGS_FIELDS = [
    {
        "key": "SECRET_KEY",
        "label": "Flask Secret Key",
        "type": "text",
        "help": "Used to sign sessions; change requires restart.",
        "restart": True,
    },
    {
        "key": "LLM_API_KEY",
        "label": "LLM API Key",
        "type": "password",
        "help": "Stored in .env and used for LLM requests.",
        "restart": False,
    },
    {
        "key": "LLM_API_BASE_URL",
        "label": "LLM API Base URL",
        "type": "text",
        "help": "Example: https://api.openai.com/v1",
        "restart": False,
    },
    {
        "key": "LLM_MODEL",
        "label": "Default LLM Model",
        "type": "select",
        "options": _MODEL_OPTIONS,
        "help": "Used when no model is chosen elsewhere.",
        "restart": False,
    },
    {
        "key": "OPENAI_MODEL_OPTIONS",
        "label": "OpenAI Model Options",
        "type": "text",
        "help": "Comma-separated list for the OpenAI model dropdown.",
        "restart": False,
    },
    {
        "key": "LLM_PROVIDER",
        "label": "Default LLM Provider",
        "type": "select",
        "options": _PROVIDER_OPTIONS,
        "help": "Default provider for grading and rubric generation.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_1_NAME",
        "label": "Custom Provider 1 Name",
        "type": "text",
        "help": "Label shown for custom provider 1.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_1_API_KEY",
        "label": "Custom Provider 1 API Key",
        "type": "password",
        "help": "API key for custom provider 1.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_1_API_BASE_URL",
        "label": "Custom Provider 1 Base URL",
        "type": "text",
        "help": "Base URL for custom provider 1.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL",
        "label": "Custom Provider 1 Default Model",
        "type": "text",
        "help": "Default model name for custom provider 1.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_1_MODELS",
        "label": "Custom Provider 1 Model Options",
        "type": "text",
        "help": "Comma-separated list for custom provider 1.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_2_NAME",
        "label": "Custom Provider 2 Name",
        "type": "text",
        "help": "Label shown for custom provider 2.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_2_API_KEY",
        "label": "Custom Provider 2 API Key",
        "type": "password",
        "help": "API key for custom provider 2.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_2_API_BASE_URL",
        "label": "Custom Provider 2 Base URL",
        "type": "text",
        "help": "Base URL for custom provider 2.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL",
        "label": "Custom Provider 2 Default Model",
        "type": "text",
        "help": "Default model name for custom provider 2.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_2_MODELS",
        "label": "Custom Provider 2 Model Options",
        "type": "text",
        "help": "Comma-separated list for custom provider 2.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_3_NAME",
        "label": "Custom Provider 3 Name",
        "type": "text",
        "help": "Label shown for custom provider 3.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_3_API_KEY",
        "label": "Custom Provider 3 API Key",
        "type": "password",
        "help": "API key for custom provider 3.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_3_API_BASE_URL",
        "label": "Custom Provider 3 Base URL",
        "type": "text",
        "help": "Base URL for custom provider 3.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL",
        "label": "Custom Provider 3 Default Model",
        "type": "text",
        "help": "Default model name for custom provider 3.",
        "restart": False,
    },
    {
        "key": "CUSTOM_LLM_PROVIDER_3_MODELS",
        "label": "Custom Provider 3 Model Options",
        "type": "text",
        "help": "Comma-separated list for custom provider 3.",
        "restart": False,
    },
    {
        "key": "LLM_USE_JSON_MODE",
        "label": "LLM JSON Mode",
        "type": "checkbox",
        "help": "Force JSON responses when supported.",
        "restart": False,
    },
    {
        "key": "LLM_MAX_OUTPUT_TOKENS",
        "label": "LLM Max Output Tokens",
        "type": "number",
        "help": "Upper limit for model output.",
        "restart": False,
    },
    {
        "key": "LLM_REQUEST_TIMEOUT",
        "label": "LLM Request Timeout (seconds)",
        "type": "number",
        "help": "HTTP timeout for LLM calls.",
        "restart": False,
    },
    {
        "key": "LLM_PRICE_INPUT_PER_1K",
        "label": "LLM Price Input per 1K Tokens",
        "type": "number",
        "help": "Fallback input pricing when model rate unknown.",
        "restart": False,
    },
    {
        "key": "LLM_PRICE_OUTPUT_PER_1K",
        "label": "LLM Price Output per 1K Tokens",
        "type": "number",
        "help": "Fallback output pricing when model rate unknown.",
        "restart": False,
    },
    {
        "key": "LLM_IMAGE_TOKENS_PER_IMAGE",
        "label": "LLM Image Tokens per Image",
        "type": "number",
        "help": "Estimated token cost per image for pricing.",
        "restart": False,
    },
    {
        "key": "REDIS_URL",
        "label": "Redis URL",
        "type": "text",
        "help": "If blank, fallback worker is used. Restart required.",
        "restart": True,
    },
    {
        "key": "MAX_CONTENT_LENGTH",
        "label": "Upload Size Limit (bytes)",
        "type": "number",
        "help": "Max upload size for Flask.",
        "restart": True,
    },
    {
        "key": "PDF_DPI",
        "label": "PDF Render DPI",
        "type": "number",
        "help": "DPI used for PDF rendering.",
        "restart": False,
    },
]

_ALLOWED_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "a",
]
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel", "target"],
}


def _utcnow():
    return datetime.now(timezone.utc)


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ensure_data_dirs():
    for path in (DATA_DIR, UPLOAD_DIR, PROCESSED_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _extract_price_estimate(message):
    if not message:
        return None
    match = _PRICE_ESTIMATE_RE.search(message)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _env_file_path():
    return Path(__file__).resolve().parent / ".env"


def _load_env_lines():
    env_path = _env_file_path()
    if not env_path.exists():
        return []
    return env_path.read_text().splitlines()


def _format_env_value(value):
    if value is None:
        return ""
    value = str(value)
    if any(ch in value for ch in [" ", "#", "=", '"', "'"]):
        escaped = value.replace('"', '\\"')
        return f"\"{escaped}\""
    return value


def _update_env_file(values):
    lines = _load_env_lines()
    if not lines:
        lines = []
    keys = set(values.keys())
    seen = set()
    updated_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue
        key, _rest = line.split("=", 1)
        key = key.strip()
        if key in keys:
            updated_lines.append(f"{key}={_format_env_value(values[key])}")
            seen.add(key)
        else:
            updated_lines.append(line)
    for key in keys:
        if key not in seen:
            updated_lines.append(f"{key}={_format_env_value(values[key])}")
    _env_file_path().write_text("\n".join(updated_lines).rstrip() + "\n")


def _current_setting_value(app, key):
    if key in os.environ:
        return os.environ.get(key, "")
    value = app.config.get(key)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _get_locale():
    locale = request.cookies.get("sage_locale", "en")
    if locale not in TRANSLATIONS:
        return "en"
    return locale


def t(key):
    locale = getattr(g, "locale", "en")
    return TRANSLATIONS.get(locale, TRANSLATIONS["en"]).get(key, key)


def _format_points(value):
    if value is None:
        return "--"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}".rstrip("0").rstrip(".")


def _extract_math_blocks(text):
    placeholders = {}
    if not text:
        return text, placeholders

    def replace_display(match):
        key = f"@@MATH_BLOCK_{len(placeholders)}@@"
        content = html.escape(match.group(1))
        placeholders[key] = f"$${content}$$"
        return key

    def replace_inline(match):
        key = f"@@MATH_INLINE_{len(placeholders)}@@"
        content = html.escape(match.group(1))
        placeholders[key] = f"${content}$"
        return key

    text = re.sub(r"\$\$(.+?)\$\$", replace_display, text, flags=re.DOTALL)
    text = re.sub(r"\$(.+?)\$", replace_inline, text)
    return text, placeholders


def _render_markdown(text):
    if not text:
        return ""
    prepared, placeholders = _extract_math_blocks(text)
    rendered = markdown.markdown(
        prepared,
        extensions=["extra", "tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )
    cleaned = bleach.clean(
        rendered, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, strip=True
    )
    cleaned = bleach.linkify(cleaned)
    for key, value in placeholders.items():
        cleaned = cleaned.replace(key, value)
    return cleaned


def _build_guide_preview(rubric_text, max_parts=1, max_words=12):
    preview = {
        "total_points": None,
        "parts": None,
        "truncated": False,
        "text": None,
    }
    if not rubric_text:
        return preview
    structured, _error = safe_json_loads(rubric_text)
    if isinstance(structured, dict):
        parts = structured.get("parts")
        if parts:
            preview_parts = []
            if isinstance(parts, dict):
                items = list(parts.items())
                total_parts = len(items)
                for part_id, part in items[:max_parts]:
                    preview_parts.append(
                        _extract_preview_part(str(part_id), part)
                    )
            elif isinstance(parts, list):
                total_parts = len(parts)
                for index, part in enumerate(parts[:max_parts], start=1):
                    part_id = None
                    if isinstance(part, dict):
                        part_id = part.get("part_id")
                    preview_parts.append(
                        _extract_preview_part(str(part_id or index), part)
                    )
            else:
                total_parts = 0
            if preview_parts:
                preview["total_points"] = structured.get("total_points")
                preview["parts"] = preview_parts
                preview["truncated"] = total_parts > max_parts
                return preview
    raw_text = rubric_text.strip()
    if raw_text.startswith("{") or raw_text.startswith("["):
        return preview
    words = raw_text.split()
    if words:
        snippet = " ".join(words[:max_words])
        if len(words) > max_words:
            snippet += "..."
        preview["text"] = snippet
    return preview


def _extract_preview_part(part_id, part):
    max_points = None
    criteria = None
    if isinstance(part, dict):
        max_points = part.get("max_points")
        if max_points is None:
            max_points = part.get("points_possible")
        if max_points is None:
            max_points = part.get("points")
        criteria = part.get("criteria")
        if isinstance(criteria, list):
            criteria = criteria[0] if criteria else None
        elif criteria is not None and not isinstance(criteria, str):
            criteria = str(criteria)
    else:
        criteria = part
        if criteria is not None and not isinstance(criteria, str):
            criteria = str(criteria)
    return {
        "part_id": part_id,
        "max_points": max_points,
        "criteria": criteria,
    }


def _parse_float_field(value, label):
    raw = (value or "").strip()
    if not raw:
        return None, None
    try:
        return float(raw), None
    except ValueError:
        return None, f"{label} must be a number."


def _build_rubric_edit_data(rubric_text):
    if not rubric_text:
        return None
    structured, _error = safe_json_loads(rubric_text)
    if not isinstance(structured, dict) or not structured.get("parts"):
        return None
    parts_raw = structured.get("parts")
    parts = []
    computed_total = 0.0
    has_total = False
    if isinstance(parts_raw, dict):
        items = list(parts_raw.items())
    elif isinstance(parts_raw, list):
        items = [(str(index), part) for index, part in enumerate(parts_raw, start=1)]
    else:
        items = []
    for part_id, part in items:
        max_points = None
        criteria = []
        if isinstance(part, dict):
            max_points = part.get("max_points")
            if max_points is None:
                max_points = part.get("points_possible")
            if max_points is None:
                max_points = part.get("points")
            criteria_raw = part.get("criteria")
        else:
            criteria_raw = part
        if isinstance(criteria_raw, list):
            criteria = [str(item) for item in criteria_raw if str(item).strip()]
        elif criteria_raw:
            criteria = [str(criteria_raw)]
        parts.append(
            {
                "part_id": str(part_id),
                "max_points": max_points,
                "criteria": criteria,
            }
        )
        if max_points is not None:
            try:
                computed_total += float(max_points)
                has_total = True
            except (TypeError, ValueError):
                pass
    return {
        "total_points": structured.get("total_points"),
        "computed_total": computed_total if has_total else None,
        "parts": parts,
    }


def _build_reference_edit_data(reference_text):
    if not reference_text:
        return None
    structured, _error = safe_json_loads(reference_text)
    if not isinstance(structured, dict):
        return None
    parts = []
    for part_id, part in structured.items():
        lines = []
        if isinstance(part, dict):
            for key, value in part.items():
                if isinstance(value, list):
                    value_text = ", ".join(str(item) for item in value)
                else:
                    value_text = str(value)
                lines.append(f"{key}: {value_text}")
        elif isinstance(part, list):
            lines = [str(item) for item in part]
        else:
            lines = [str(part)]
        parts.append({"part_id": str(part_id), "content_lines": lines})
    return {"parts": parts}


def _parse_reference_editor(form):
    part_ids = form.getlist("reference_part_id")
    contents = form.getlist("reference_content")
    total_count = max(len(part_ids), len(contents))
    parts = {}
    errors = []
    for index in range(total_count):
        part_id = part_ids[index].strip() if index < len(part_ids) else ""
        content_raw = contents[index] if index < len(contents) else ""
        if not part_id and not content_raw.strip():
            continue
        if not part_id:
            errors.append("Reference solution part id is required.")
            part_id = str(index + 1)
        lines = [line.strip() for line in content_raw.splitlines() if line.strip()]
        if not lines:
            parts[part_id] = ""
            continue
        has_keyed = any(":" in line for line in lines)
        if has_keyed:
            part_obj = {}
            notes = []
            for line in lines:
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    if value.startswith("- "):
                        value = value[2:].strip()
                    if key:
                        part_obj[key] = value
                else:
                    notes.append(line)
            if notes:
                part_obj["notes"] = notes if len(notes) > 1 else notes[0]
            parts[part_id] = part_obj
        else:
            parts[part_id] = lines[0] if len(lines) == 1 else lines
    if errors:
        return None, errors
    return parts, None


def _model_supports_images(model_name):
    if not model_name:
        return True
    name = model_name.strip().lower()
    for model in _NON_IMAGE_MODELS:
        if name == model or name.startswith(f"{model}-"):
            return False
    for model in _IMAGE_CAPABLE_MODELS:
        if name == model or name.startswith(f"{model}-"):
            return True
    return True


def _resolve_model_from_form(form, default_model):
    selected_model = (form.get("llm_model") or "").strip()
    custom_model = (form.get("custom_llm_model") or "").strip()
    if selected_model == "other":
        if custom_model:
            return custom_model, True
        return default_model, False
    if not selected_model:
        selected_model = default_model
    return selected_model, False


def _resolve_provider_from_form(form, default_provider):
    selected = (form.get("llm_provider") or "").strip()
    if not selected:
        selected = default_provider
    return _normalize_provider_key(selected)


def _provider_config(provider_key):
    provider_key = _normalize_provider_key(provider_key)
    if provider_key == "custom1":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_1_NAME or "Other 1",
            "api_key": Config.CUSTOM_LLM_PROVIDER_1_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_1_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL or Config.LLM_MODEL,
        }
    if provider_key == "custom2":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_2_NAME or "Other 2",
            "api_key": Config.CUSTOM_LLM_PROVIDER_2_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_2_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL or Config.LLM_MODEL,
        }
    if provider_key == "custom3":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_3_NAME or "Other 3",
            "api_key": Config.CUSTOM_LLM_PROVIDER_3_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_3_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL or Config.LLM_MODEL,
        }
    return {
        "name": "OpenAI",
        "api_key": Config.LLM_API_KEY,
        "base_url": Config.LLM_API_BASE_URL,
        "default_model": Config.LLM_MODEL,
    }


def _provider_default_models():
    return {
        "openai": _provider_config("openai")["default_model"],
        "custom1": _provider_config("custom1")["default_model"],
        "custom2": _provider_config("custom2")["default_model"],
        "custom3": _provider_config("custom3")["default_model"],
    }


def _provider_display(provider_key):
    provider_key = _normalize_provider_key(provider_key)
    if provider_key == "custom1":
        return Config.CUSTOM_LLM_PROVIDER_1_NAME or "Other 1"
    if provider_key == "custom2":
        return Config.CUSTOM_LLM_PROVIDER_2_NAME or "Other 2"
    if provider_key == "custom3":
        return Config.CUSTOM_LLM_PROVIDER_3_NAME or "Other 3"
    return "OpenAI"


def _normalize_provider_key(provider_key):
    if not provider_key:
        return "openai"
    if provider_key == "other":
        return "custom1"
    return provider_key


def _provider_option_items(include_unconfigured=False):
    options = [{"value": "openai", "label": t("provider_openai")}]

    def add_custom(value, name, base_url, fallback):
        if include_unconfigured or (base_url and base_url.strip()):
            options.append({"value": value, "label": name or fallback})

    add_custom(
        "custom1",
        Config.CUSTOM_LLM_PROVIDER_1_NAME,
        Config.CUSTOM_LLM_PROVIDER_1_API_BASE_URL,
        "Other 1",
    )
    add_custom(
        "custom2",
        Config.CUSTOM_LLM_PROVIDER_2_NAME,
        Config.CUSTOM_LLM_PROVIDER_2_API_BASE_URL,
        "Other 2",
    )
    add_custom(
        "custom3",
        Config.CUSTOM_LLM_PROVIDER_3_NAME,
        Config.CUSTOM_LLM_PROVIDER_3_API_BASE_URL,
        "Other 3",
    )
    return options


def _resolve_default_provider(preferred, provider_options):
    values = [option["value"] for option in provider_options]
    if preferred in values:
        return preferred
    if "openai" in values:
        return "openai"
    return values[0] if values else "openai"


def _parse_model_options(raw, fallback):
    if raw is None:
        return list(fallback)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(fallback)


def _build_model_option_items(model_list, include_supports_images=True):
    items = []
    seen = set()
    for model in model_list:
        if not model:
            continue
        key = model.strip()
        if not key or key.lower() == "other" or key in seen:
            continue
        seen.add(key)
        item = {"value": key}
        if include_supports_images:
            item["supports_images"] = _model_supports_images(key)
        items.append(item)
    return items


def _provider_model_option_items():
    openai_models = _parse_model_options(Config.OPENAI_MODEL_OPTIONS, _MODEL_OPTIONS)
    if Config.LLM_MODEL and Config.LLM_MODEL not in openai_models:
        openai_models = [Config.LLM_MODEL] + openai_models
    custom1_models = _parse_model_options(Config.CUSTOM_LLM_PROVIDER_1_MODELS, [])
    if Config.CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL and Config.CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL not in custom1_models:
        custom1_models = [Config.CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL] + custom1_models
    custom2_models = _parse_model_options(Config.CUSTOM_LLM_PROVIDER_2_MODELS, [])
    if Config.CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL and Config.CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL not in custom2_models:
        custom2_models = [Config.CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL] + custom2_models
    custom3_models = _parse_model_options(Config.CUSTOM_LLM_PROVIDER_3_MODELS, [])
    if Config.CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL and Config.CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL not in custom3_models:
        custom3_models = [Config.CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL] + custom3_models
    return {
        "openai": _build_model_option_items(openai_models, include_supports_images=True),
        "custom1": _build_model_option_items(custom1_models, include_supports_images=False),
        "custom2": _build_model_option_items(custom2_models, include_supports_images=False),
        "custom3": _build_model_option_items(custom3_models, include_supports_images=False),
    }


def _submission_requires_images(submission):
    if not submission:
        return False
    for file_record in submission.files:
        if file_record.file_type in {"pdf", "image"}:
            return True
    return False


def _ensure_schema_updates():
    if db.engine.dialect.name != "sqlite":
        return
    try:
        result = db.session.execute(text("PRAGMA table_info(grading_job)"))
        columns = {row[1] for row in result.fetchall()}
        if "llm_model" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN llm_model VARCHAR(128)")
            )
            db.session.commit()
            logger.info("Added llm_model column to grading_job table")
        if "prompt_tokens" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN prompt_tokens INTEGER")
            )
            db.session.commit()
        if "completion_tokens" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN completion_tokens INTEGER")
            )
            db.session.commit()
        if "total_tokens" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN total_tokens INTEGER")
            )
            db.session.commit()
        if "price_estimate" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN price_estimate REAL")
            )
            db.session.commit()
        if "llm_provider" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN llm_provider VARCHAR(64)")
            )
            db.session.commit()
            logger.info("Added llm_provider column to grading_job table")
        result = db.session.execute(text("PRAGMA table_info(rubric_version)"))
        rubric_columns = {row[1] for row in result.fetchall()}
        if "llm_model" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN llm_model VARCHAR(128)")
            )
            db.session.commit()
            logger.info("Added llm_model column to rubric_version table")
        if "llm_provider" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN llm_provider VARCHAR(64)")
            )
            db.session.commit()
            logger.info("Added llm_provider column to rubric_version table")
        if "error_message" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN error_message TEXT DEFAULT ''")
            )
            db.session.commit()
            logger.info("Added error_message column to rubric_version table")
        if "raw_response" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN raw_response TEXT DEFAULT ''")
            )
            db.session.commit()
            logger.info("Added raw_response column to rubric_version table")
        if "prompt_tokens" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN prompt_tokens INTEGER")
            )
            db.session.commit()
        if "completion_tokens" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN completion_tokens INTEGER")
            )
            db.session.commit()
        if "total_tokens" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN total_tokens INTEGER")
            )
            db.session.commit()
        if "price_estimate" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN price_estimate REAL")
            )
            db.session.commit()
        if "finished_at" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN finished_at DATETIME")
            )
            db.session.commit()
    except Exception:
        logger.exception("Failed to apply schema updates")
        db.session.rollback()


def _init_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _get_approved_rubric(assignment_id):
    # "rubric" here refers to the grading guide shown in the UI.
    return (
        RubricVersion.query.filter_by(
            assignment_id=assignment_id, status=RubricStatus.APPROVED
        )
        .order_by(RubricVersion.created_at.desc())
        .first()
    )


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.jinja_env.globals["t"] = t

    _init_logging()
    db.init_app(app)

    with app.app_context():
        _ensure_data_dirs()
        db.create_all()
        _ensure_schema_updates()

    init_job_queue(app)

    @app.errorhandler(413)
    def too_large(_error):
        flash("Upload too large. Adjust MAX_CONTENT_LENGTH.")
        return redirect(request.referrer or url_for("list_assignments"))

    @app.before_request
    def set_locale():
        g.locale = _get_locale()

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/assignments", methods=["GET", "POST"])
    def list_assignments():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            assignment_text = request.form.get("assignment_text", "").strip()
            if not title or not assignment_text:
                flash("Title and assignment text are required.")
                return redirect(url_for("list_assignments"))

            assignment = Assignment(title=title, assignment_text=assignment_text)
            db.session.add(assignment)
            db.session.commit()
            return redirect(url_for("assignment_detail", assignment_id=assignment.id))

        assignments = Assignment.query.order_by(Assignment.created_at.desc()).all()
        return render_template("assignments.html", assignments=assignments)

    @app.route("/assignments/<int:assignment_id>")
    def assignment_detail(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        assignment_html = _render_markdown(assignment.assignment_text)
        rubrics = (
            RubricVersion.query.filter_by(assignment_id=assignment_id)
            .order_by(RubricVersion.created_at.desc())
            .all()
        )
        submissions = (
            Submission.query.filter_by(assignment_id=assignment_id)
            .order_by(Submission.created_at.desc())
            .all()
        )
        jobs = (
            GradingJob.query.filter_by(assignment_id=assignment_id)
            .order_by(GradingJob.created_at.desc())
            .all()
        )
        has_pending_rubrics = any(
            rubric.status == RubricStatus.GENERATING for rubric in rubrics
        )
        for rubric in rubrics:
            if rubric.llm_model:
                rubric.provider_display = _provider_display(
                    rubric.llm_provider or Config.LLM_PROVIDER
                )
            else:
                rubric.provider_display = "manual"
            preview = _build_guide_preview(rubric.rubric_text)
            rubric.preview_total_points = preview["total_points"]
            rubric.preview_parts = preview["parts"]
            rubric.preview_truncated = preview["truncated"]
            rubric.preview_text = preview["text"]
            if rubric.finished_at:
                finished_at = _as_utc(rubric.finished_at)
                created_at = _as_utc(rubric.created_at)
                if finished_at and created_at:
                    rubric.duration_seconds = (finished_at - created_at).total_seconds()
                else:
                    rubric.duration_seconds = None
            elif rubric.status == RubricStatus.GENERATING:
                created_at = _as_utc(rubric.created_at)
                if created_at:
                    rubric.duration_seconds = (_utcnow() - created_at).total_seconds()
                else:
                    rubric.duration_seconds = None
            else:
                rubric.duration_seconds = None
        for job in jobs:
            job.provider_display = _provider_display(
                job.llm_provider or Config.LLM_PROVIDER
            )
            if job.started_at and job.finished_at:
                started_at = _as_utc(job.started_at)
                finished_at = _as_utc(job.finished_at)
                if started_at and finished_at:
                    job.duration_seconds = (finished_at - started_at).total_seconds()
                else:
                    job.duration_seconds = None
            elif job.started_at:
                started_at = _as_utc(job.started_at)
                if started_at:
                    job.duration_seconds = (_utcnow() - started_at).total_seconds()
                else:
                    job.duration_seconds = None
            else:
                job.duration_seconds = None
            job.price_estimate_display = job.price_estimate
            if job.price_estimate_display is None:
                job.price_estimate_display = _extract_price_estimate(job.message)
        has_active_jobs = any(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in jobs
        )
        approved_rubric = _get_approved_rubric(assignment_id)
        for submission in submissions:
            latest_result = None
            if submission.grade_results:
                latest_result = submission.grade_results[-1]
            if not latest_result:
                submission.grade_display = "--"
                continue
            data, _error = safe_json_loads(latest_result.json_result)
            if not data:
                submission.grade_display = _format_points(latest_result.total_points)
                continue
            parts = data.get("parts", [])
            total_possible = 0.0
            has_possible = False
            for part in parts:
                try:
                    value = float(part.get("points_possible"))
                except (TypeError, ValueError):
                    value = None
                if value is None:
                    continue
                total_possible += value
                has_possible = True
            total_points = data.get("total_points", latest_result.total_points)
            if has_possible:
                submission.grade_display = (
                    f"{_format_points(total_points)}/{_format_points(total_possible)}"
                )
            else:
                submission.grade_display = _format_points(total_points)
        total_price_estimate = 0.0
        has_price_estimate = False
        for rubric in rubrics:
            if rubric.price_estimate is not None:
                total_price_estimate += rubric.price_estimate
                has_price_estimate = True
        for job in jobs:
            if job.price_estimate_display is not None:
                total_price_estimate += job.price_estimate_display
                has_price_estimate = True
        if not has_price_estimate:
            total_price_estimate = None

        provider_options = _provider_option_items()
        default_provider = _resolve_default_provider(
            _normalize_provider_key(Config.LLM_PROVIDER), provider_options
        )
        default_provider_cfg = _provider_config(default_provider)
        return render_template(
            "assignment_detail.html",
            assignment=assignment,
            assignment_html=assignment_html,
            rubrics=rubrics,
            submissions=submissions,
            jobs=jobs,
            has_active_jobs=has_active_jobs,
            has_pending_rubrics=has_pending_rubrics,
            approved_rubric=approved_rubric,
            default_model=default_provider_cfg["default_model"],
            total_price_estimate=total_price_estimate,
            provider_options=provider_options,
            provider_model_options=_provider_model_option_items(),
            provider_default_models=_provider_default_models(),
            default_provider=default_provider,
        )

    @app.route("/assignments/<int:assignment_id>/edit", methods=["GET", "POST"])
    def edit_assignment(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            assignment_text = request.form.get("assignment_text", "").strip()
            if not title or not assignment_text:
                flash("Title and assignment text are required.")
                return redirect(url_for("edit_assignment", assignment_id=assignment_id))

            assignment.title = title
            assignment.assignment_text = assignment_text
            db.session.commit()
            flash("Assignment updated.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        return render_template("assignment_edit.html", assignment=assignment)

    @app.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
    def delete_assignment(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        has_active_jobs = (
            GradingJob.query.filter(
                GradingJob.assignment_id == assignment_id,
                GradingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            ).first()
            is not None
        )
        if has_active_jobs:
            flash("Cancel running jobs before deleting the assignment.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        submissions = Submission.query.filter_by(assignment_id=assignment_id).all()
        submission_ids = [s.id for s in submissions]

        if submission_ids:
            GradeResult.query.filter(
                GradeResult.submission_id.in_(submission_ids)
            ).delete(synchronize_session=False)
            SubmissionFile.query.filter(
                SubmissionFile.submission_id.in_(submission_ids)
            ).delete(synchronize_session=False)
            Submission.query.filter(
                Submission.id.in_(submission_ids)
            ).delete(synchronize_session=False)

        GradingJob.query.filter_by(assignment_id=assignment_id).delete(
            synchronize_session=False
        )
        RubricVersion.query.filter_by(assignment_id=assignment_id).delete(
            synchronize_session=False
        )
        db.session.delete(assignment)
        db.session.commit()

        shutil.rmtree(UPLOAD_DIR / f"assignment_{assignment_id}", ignore_errors=True)
        shutil.rmtree(PROCESSED_DIR / f"assignment_{assignment_id}", ignore_errors=True)

        flash("Assignment deleted.")
        return redirect(url_for("list_assignments"))

    @app.route("/assignments/<int:assignment_id>/status.json")
    def assignment_status(assignment_id):
        rubrics = RubricVersion.query.filter_by(assignment_id=assignment_id).all()
        jobs = GradingJob.query.filter_by(assignment_id=assignment_id).all()
        has_active_jobs = any(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in jobs
        )
        has_pending_rubrics = any(
            rubric.status == RubricStatus.GENERATING for rubric in rubrics
        )
        return jsonify(
            {
                "has_active_jobs": has_active_jobs,
                "has_pending_rubrics": has_pending_rubrics,
            }
        )

    @app.route("/assignments/<int:assignment_id>/rubrics/create", methods=["POST"])
    def create_rubric(assignment_id):
        rubric_text = request.form.get("rubric_text", "").strip()
        reference_solution_text = request.form.get("reference_solution_text", "").strip()
        if not rubric_text or not reference_solution_text:
            flash("Grading guide text and reference solution are required.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        rubric = RubricVersion(
            assignment_id=assignment_id,
            rubric_text=rubric_text,
            reference_solution_text=reference_solution_text,
            status=RubricStatus.DRAFT,
        )
        db.session.add(rubric)
        db.session.commit()
        flash("Grading guide saved as DRAFT.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/assignments/<int:assignment_id>/rubrics/generate_draft", methods=["POST"])
    def generate_rubric(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        provider_key = _resolve_provider_from_form(
            request.form, app.config.get("LLM_PROVIDER")
        )
        provider_cfg = _provider_config(provider_key)
        selected_model, _custom_used = _resolve_model_from_form(
            request.form, provider_cfg["default_model"]
        )
        rubric = RubricVersion(
            assignment_id=assignment_id,
            rubric_text="",
            reference_solution_text="",
            status=RubricStatus.GENERATING,
            llm_provider=provider_key,
            llm_model=selected_model,
            error_message="",
            raw_response="",
        )
        db.session.add(rubric)
        db.session.commit()

        enqueue_rubric_job(rubric.id)
        flash("Grading guide generation queued. It will appear when ready.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/rubrics/<int:rubric_id>/approve", methods=["POST"])
    def approve_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status not in {RubricStatus.DRAFT, RubricStatus.ARCHIVED}:
            flash("Only DRAFT or ARCHIVED guides can be activated.")
            return redirect(url_for("assignment_detail", assignment_id=rubric.assignment_id))

        RubricVersion.query.filter(
            RubricVersion.assignment_id == rubric.assignment_id,
            RubricVersion.id != rubric_id,
            RubricVersion.status == RubricStatus.APPROVED,
        ).update({"status": RubricStatus.ARCHIVED})

        rubric.status = RubricStatus.APPROVED
        db.session.commit()
        flash("Grading guide activated.")
        return redirect(url_for("assignment_detail", assignment_id=rubric.assignment_id))

    @app.route("/rubrics/<int:rubric_id>/cancel", methods=["POST"])
    def cancel_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status != RubricStatus.GENERATING:
            flash("Grading guide is not generating.")
            return redirect(url_for("rubric_detail", rubric_id=rubric.id))
        rubric.status = RubricStatus.CANCELLED
        if "Cancelled by user." not in rubric.error_message:
            rubric.error_message = "Cancelled by user."
        if not rubric.finished_at:
            rubric.finished_at = _utcnow()
        db.session.commit()
        flash("Grading guide generation cancelled.")
        return redirect(url_for("rubric_detail", rubric_id=rubric.id))

    @app.route("/rubrics/<int:rubric_id>/delete", methods=["POST"])
    def delete_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status == RubricStatus.GENERATING:
            flash("Cancel guide generation before deleting.")
            return redirect(url_for("rubric_detail", rubric_id=rubric.id))

        GradingJob.query.filter_by(rubric_version_id=rubric_id).delete(
            synchronize_session=False
        )
        GradeResult.query.filter_by(rubric_version_id=rubric_id).delete(
            synchronize_session=False
        )
        db.session.delete(rubric)
        db.session.commit()
        flash("Grading guide deleted.")
        return redirect(url_for("assignment_detail", assignment_id=rubric.assignment_id))

    @app.route("/rubrics/<int:rubric_id>")
    def rubric_detail(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        assignment = Assignment.query.get_or_404(rubric.assignment_id)
        duration_seconds = None
        if rubric.finished_at:
            finished_at = _as_utc(rubric.finished_at)
            created_at = _as_utc(rubric.created_at)
            if finished_at and created_at:
                duration_seconds = (finished_at - created_at).total_seconds()
        elif rubric.status == RubricStatus.GENERATING:
            created_at = _as_utc(rubric.created_at)
            if created_at:
                duration_seconds = (_utcnow() - created_at).total_seconds()
        rubric_structured = None
        if rubric.rubric_text:
            structured, _error = safe_json_loads(rubric.rubric_text)
            if isinstance(structured, dict) and structured.get("parts"):
                rubric_structured = structured
        reference_structured = None
        if rubric.reference_solution_text:
            structured, _error = safe_json_loads(rubric.reference_solution_text)
            if isinstance(structured, dict):
                reference_structured = structured

        provider_display = "manual"
        if rubric.llm_model:
            provider_display = _provider_display(rubric.llm_provider or Config.LLM_PROVIDER)
        return render_template(
            "rubric_detail.html",
            rubric=rubric,
            assignment=assignment,
            duration_seconds=duration_seconds,
            rubric_structured=rubric_structured,
            reference_structured=reference_structured,
            provider_display=provider_display,
        )

    @app.route("/rubrics/<int:rubric_id>/edit", methods=["GET", "POST"])
    def edit_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status not in {RubricStatus.DRAFT, RubricStatus.APPROVED}:
            flash("Only DRAFT or APPROVED grading guides can be edited.")
            return redirect(url_for("rubric_detail", rubric_id=rubric.id))

        if request.method == "POST":
            rubric_text = ""
            structured_editor = request.form.get("structured_editor") == "1"
            reference_structured = request.form.get("reference_structured") == "1"
            if structured_editor:
                part_ids = request.form.getlist("part_id")
                part_points = request.form.getlist("part_max_points")
                part_criteria = request.form.getlist("part_criteria")
                parts = []
                errors = []
                total_count = max(
                    len(part_ids), len(part_points), len(part_criteria)
                )
                for index in range(total_count):
                    part_id = part_ids[index].strip() if index < len(part_ids) else ""
                    points_raw = (
                        part_points[index].strip()
                        if index < len(part_points)
                        else ""
                    )
                    criteria_raw = (
                        part_criteria[index]
                        if index < len(part_criteria)
                        else ""
                    )
                    if not part_id and not points_raw and not criteria_raw.strip():
                        continue
                    max_points, max_error = _parse_float_field(
                        points_raw, f"Part {part_id or index + 1} max points"
                    )
                    if max_error:
                        errors.append(max_error)
                    criteria = [
                        line.strip()
                        for line in criteria_raw.splitlines()
                        if line.strip()
                    ]
                    parts.append(
                        {
                            "part_id": part_id or str(index + 1),
                            "max_points": max_points,
                            "criteria": criteria,
                        }
                    )
                if not parts:
                    errors.append("At least one part is required.")
                if errors:
                    flash(" ".join(errors))
                    return redirect(url_for("edit_rubric", rubric_id=rubric.id))

                total_points = 0.0
                has_total = False
                for part in parts:
                    value = part.get("max_points")
                    if value is None:
                        continue
                    try:
                        total_points += float(value)
                        has_total = True
                    except (TypeError, ValueError):
                        pass
                rubric_structured = {"parts": parts}
                if has_total:
                    rubric_structured["total_points"] = total_points
                rubric_text = json.dumps(rubric_structured, ensure_ascii=True, indent=2)
            else:
                rubric_text = request.form.get("rubric_text", "").strip()

            if reference_structured:
                reference_parts, ref_errors = _parse_reference_editor(request.form)
                if ref_errors:
                    flash(" ".join(ref_errors))
                    return redirect(url_for("edit_rubric", rubric_id=rubric.id))
                reference_solution_text = json.dumps(
                    reference_parts, ensure_ascii=True, indent=2
                )
            else:
                reference_solution_text = request.form.get(
                    "reference_solution_text", ""
                ).strip()

            if not rubric_text or not reference_solution_text:
                flash("Grading guide text and reference solution are required.")
                return redirect(url_for("edit_rubric", rubric_id=rubric.id))

            rubric.rubric_text = rubric_text
            rubric.reference_solution_text = reference_solution_text
            db.session.commit()
            flash("Grading guide updated.")
            return redirect(url_for("rubric_detail", rubric_id=rubric.id))

        edit_data = _build_rubric_edit_data(rubric.rubric_text)
        reference_edit_data = _build_reference_edit_data(
            rubric.reference_solution_text
        )
        return render_template(
            "rubric_edit.html",
            rubric=rubric,
            rubric_edit_data=edit_data,
            reference_edit_data=reference_edit_data,
        )

    @app.route("/assignments/<int:assignment_id>/submissions/upload", methods=["POST"])
    def upload_submission(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        approved_rubric = _get_approved_rubric(assignment_id)
        if not approved_rubric:
            flash("Approve a grading guide before uploading submissions.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))
        zip_file = request.files.get("zip_file")
        provider_key = _resolve_provider_from_form(
            request.form, app.config.get("LLM_PROVIDER")
        )
        provider_cfg = _provider_config(provider_key)
        selected_model, custom_used = _resolve_model_from_form(
            request.form, provider_cfg["default_model"]
        )

        submissions = []
        if zip_file and zip_file.filename:
            submissions = ingest_zip_upload(assignment_id, zip_file)
            db.session.commit()
        else:
            student_identifier = request.form.get("student_identifier", "").strip()
            submitted_text = request.form.get("submitted_text", "").strip()
            if not student_identifier:
                flash("Student identifier is required for single upload.")
                return redirect(url_for("assignment_detail", assignment_id=assignment_id))

            submission = Submission(
                assignment_id=assignment_id,
                student_identifier=student_identifier,
                submitted_text=submitted_text,
            )
            db.session.add(submission)
            db.session.commit()

            file_storages = request.files.getlist("files")
            save_submission_files(submission, file_storages)
            db.session.commit()
            submissions = [submission]

        if not submissions:
            flash("No submissions found in upload.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        requires_images = any(
            _submission_requires_images(submission) for submission in submissions
        )
        if (
            requires_images
            and provider_key != "other"
            and not custom_used
            and not _model_supports_images(selected_model)
        ):
            flash("Selected model does not support images. Choose an image-capable model.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        for submission in submissions:
            job = GradingJob(
                assignment_id=assignment_id,
                submission_id=submission.id,
                rubric_version_id=approved_rubric.id,
                status=JobStatus.QUEUED,
                llm_provider=provider_key,
                llm_model=selected_model,
            )
            db.session.add(job)
            db.session.commit()
            queue_id = enqueue_submission_job(job.id)
            job.queue_job_id = queue_id
            db.session.commit()

        flash(f"Queued {len(submissions)} submission(s) for grading.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/submissions/<int:submission_id>")
    def submission_detail(submission_id):
        submission = Submission.query.get_or_404(submission_id)
        assignment = db.session.get(Assignment, submission.assignment_id)
        grade_result = (
            GradeResult.query.filter_by(submission_id=submission.id)
            .order_by(GradeResult.created_at.desc())
            .first()
        )
        rubric = None
        rubric_structured = None
        reference_structured = None
        if grade_result and grade_result.rubric_version:
            rubric = grade_result.rubric_version
        else:
            rubric = _get_approved_rubric(submission.assignment_id)
        if rubric and rubric.rubric_text:
            structured, _error = safe_json_loads(rubric.rubric_text)
            if isinstance(structured, dict) and structured.get("parts"):
                rubric_structured = structured
        if rubric and rubric.reference_solution_text:
            structured, _error = safe_json_loads(rubric.reference_solution_text)
            if isinstance(structured, dict):
                reference_structured = structured
        images = collect_submission_images(submission)
        image_rel_paths = []
        for path in images:
            try:
                image_rel_paths.append(str(Path(path).relative_to(DATA_DIR)))
            except ValueError:
                image_rel_paths.append(path)
        student_text = collect_submission_text(submission)
        student_text_html = _render_markdown(student_text)
        assignment_text_html = _render_markdown(assignment.assignment_text or "")

        return render_template(
            "submission_detail.html",
            submission=submission,
            assignment=assignment,
            grade_result=grade_result,
            rubric=rubric,
            rubric_structured=rubric_structured,
            reference_structured=reference_structured,
            images=image_rel_paths,
            student_text=student_text,
            student_text_html=student_text_html,
            assignment_text_html=assignment_text_html,
        )

    @app.route("/submissions/<int:submission_id>/grade/edit", methods=["POST"])
    def edit_submission_grade(submission_id):
        submission = Submission.query.get_or_404(submission_id)
        grade_result = (
            GradeResult.query.filter_by(submission_id=submission.id)
            .order_by(GradeResult.created_at.desc())
            .first()
        )
        if not grade_result:
            flash("No grade result to edit.")
            return redirect(url_for("submission_detail", submission_id=submission_id))

        rendered_text = request.form.get("rendered_text", "").strip()
        total_points_input = request.form.get("total_points", "").strip()
        data, error = safe_json_loads(grade_result.json_result)
        if not data:
            flash("Stored grading data is invalid.")
            return redirect(url_for("submission_detail", submission_id=submission_id))
        valid, msg = validate_grade_result(data)
        if not valid:
            flash(f"Stored grading data is invalid: {msg}")
            return redirect(url_for("submission_detail", submission_id=submission_id))

        if total_points_input:
            try:
                data["total_points"] = float(total_points_input)
            except ValueError:
                flash("Total points must be a number.")
                return redirect(url_for("submission_detail", submission_id=submission_id))
        grade_result.total_points = data.get("total_points")
        grade_result.json_result = json.dumps(data, ensure_ascii=True, indent=2)
        if rendered_text:
            grade_result.rendered_text = rendered_text
        else:
            grade_result.rendered_text = render_grade_output(data)
        grade_result.error_message = ""
        db.session.commit()

        flash("Feedback updated.")
        return redirect(url_for("submission_detail", submission_id=submission_id))

    @app.route("/submissions/<int:submission_id>/delete", methods=["POST"])
    def delete_submission(submission_id):
        submission = Submission.query.get_or_404(submission_id)
        assignment_id = submission.assignment_id
        active_job = (
            GradingJob.query.filter_by(submission_id=submission.id)
            .filter(GradingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .first()
        )
        if active_job:
            flash("Cancel running jobs before deleting the submission.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        GradeResult.query.filter_by(submission_id=submission.id).delete(
            synchronize_session=False
        )
        SubmissionFile.query.filter_by(submission_id=submission.id).delete(
            synchronize_session=False
        )
        GradingJob.query.filter_by(submission_id=submission.id).delete(
            synchronize_session=False
        )
        db.session.delete(submission)
        db.session.commit()

        shutil.rmtree(
            UPLOAD_DIR / f"assignment_{assignment_id}" / f"submission_{submission.id}",
            ignore_errors=True,
        )
        shutil.rmtree(
            PROCESSED_DIR / f"assignment_{assignment_id}" / f"submission_{submission.id}",
            ignore_errors=True,
        )

        flash("Submission deleted.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/assignments/<int:assignment_id>/export.csv")
    def export_csv(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        submissions = (
            Submission.query.filter_by(assignment_id=assignment_id)
            .order_by(Submission.created_at.asc())
            .all()
        )

        results_by_submission = {
            result.submission_id: result
            for result in GradeResult.query.join(Submission)
            .filter(Submission.assignment_id == assignment_id)
            .all()
        }

        max_parts = 0
        parsed_results = {}
        for submission in submissions:
            result = results_by_submission.get(submission.id)
            if not result:
                continue
            data, _error = safe_json_loads(result.json_result)
            if data:
                parts = data.get("parts", [])
                parsed_results[submission.id] = data
                max_parts = max(max_parts, len(parts))

        headers = ["student_identifier", "total_points"]
        for idx in range(1, max_parts + 1):
            headers.append(f"part{idx}_points")
        headers.append("rendered_text")

        def generate_rows():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

            for submission in submissions:
                result = results_by_submission.get(submission.id)
                row = [submission.student_identifier, ""]
                parts_values = ["" for _ in range(max_parts)]
                rendered_text = ""

                if result:
                    data = parsed_results.get(submission.id)
                    if data:
                        row[1] = data.get("total_points", "")
                        parts = data.get("parts", [])
                        for idx, part in enumerate(parts[:max_parts]):
                            parts_values[idx] = part.get("points_awarded", "")
                        rendered_text = result.rendered_text or ""
                row.extend(parts_values)
                row.append(rendered_text)

                writer.writerow(row)
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

        filename = f"assignment_{assignment.id}_grades.csv"
        return Response(
            generate_rows(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/jobs/<int:job_id>")
    def job_detail(job_id):
        job = GradingJob.query.get_or_404(job_id)
        auto_refresh = job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        submission_requires_images = _submission_requires_images(job.submission)
        job_provider_display = _provider_display(job.llm_provider or Config.LLM_PROVIDER)
        job_price_display = job.price_estimate
        if job_price_display is None:
            job_price_display = _extract_price_estimate(job.message)
        grade_result = (
            GradeResult.query.filter_by(
                submission_id=job.submission_id, rubric_version_id=job.rubric_version_id
            )
            .order_by(GradeResult.created_at.desc())
            .first()
        )
        duration_seconds = None
        if job.started_at and job.finished_at:
            started_at = _as_utc(job.started_at)
            finished_at = _as_utc(job.finished_at)
            if started_at and finished_at:
                duration_seconds = (finished_at - started_at).total_seconds()
        elif job.started_at:
            started_at = _as_utc(job.started_at)
            if started_at:
                duration_seconds = (_utcnow() - started_at).total_seconds()
        provider_options = _provider_option_items()
        default_provider = _resolve_default_provider(
            _normalize_provider_key(Config.LLM_PROVIDER), provider_options
        )
        default_provider_cfg = _provider_config(default_provider)
        rerun_provider = _resolve_default_provider(
            _normalize_provider_key(job.llm_provider or default_provider), provider_options
        )
        return render_template(
            "job_detail.html",
            job=job,
            duration_seconds=duration_seconds,
            grade_result=grade_result,
            auto_refresh=auto_refresh,
            default_model=default_provider_cfg["default_model"],
            submission_requires_images=submission_requires_images,
            job_price_display=job_price_display,
            provider_options=provider_options,
            provider_model_options=_provider_model_option_items(),
            provider_default_models=_provider_default_models(),
            default_provider=default_provider,
            job_provider_display=job_provider_display,
            rerun_provider=rerun_provider,
        )

    @app.route("/jobs/<int:job_id>/status.json")
    def job_status(job_id):
        job = GradingJob.query.get_or_404(job_id)
        duration_seconds = None
        if job.started_at and job.finished_at:
            started_at = _as_utc(job.started_at)
            finished_at = _as_utc(job.finished_at)
            if started_at and finished_at:
                duration_seconds = (finished_at - started_at).total_seconds()
        elif job.started_at:
            started_at = _as_utc(job.started_at)
            if started_at:
                duration_seconds = (_utcnow() - started_at).total_seconds()
        return jsonify(
            {
                "status": job.status,
                "duration_seconds": duration_seconds,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }
        )

    @app.route("/jobs/<int:job_id>/terminate", methods=["POST"])
    def terminate_job(job_id):
        job = GradingJob.query.get_or_404(job_id)
        if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            flash("Job is not running or queued.")
            return redirect(url_for("job_detail", job_id=job.id))
        job.status = JobStatus.CANCELLED
        if not job.finished_at:
            job.finished_at = _utcnow()
        if "Cancelled by user." not in job.message:
            job.message = (job.message + "\n" if job.message else "") + "Cancelled by user."
        db.session.commit()
        flash("Job cancelled.")
        return redirect(url_for("job_detail", job_id=job.id))

    @app.route("/jobs/<int:job_id>/delete", methods=["POST"])
    def delete_job(job_id):
        job = GradingJob.query.get_or_404(job_id)
        if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            flash("Cancel the job before deleting.")
            return redirect(url_for("job_detail", job_id=job.id))
        db.session.delete(job)
        db.session.commit()
        flash("Job deleted.")
        return redirect(url_for("assignment_detail", assignment_id=job.assignment_id))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            updates = {}
            for field in _SETTINGS_FIELDS:
                key = field["key"]
                field_type = field.get("type")
                if field_type == "checkbox":
                    updates[key] = "1" if request.form.get(key) else "0"
                elif key == "LLM_MODEL":
                    selected = request.form.get(key, "").strip()
                    if selected == "other":
                        custom_model = request.form.get("LLM_MODEL_CUSTOM", "").strip()
                        if not custom_model:
                            flash("Custom model name is required when selecting Other.")
                            return redirect(url_for("settings"))
                        updates[key] = custom_model
                    else:
                        updates[key] = selected or app.config.get(key, "")
                else:
                    updates[key] = request.form.get(key, "").strip()
            _update_env_file(updates)
            for key, value in updates.items():
                os.environ[key] = value
                app.config[key] = value
                if key in {"LLM_USE_JSON_MODE"}:
                    app.config[key] = value.lower() in {"1", "true", "yes", "on"}
                if key in {
                    "LLM_MAX_OUTPUT_TOKENS",
                    "LLM_REQUEST_TIMEOUT",
                    "LLM_IMAGE_TOKENS_PER_IMAGE",
                    "MAX_CONTENT_LENGTH",
                    "PDF_DPI",
                    "PDF_TEXT_MIN_CHARS",
                }:
                    try:
                        app.config[key] = int(value) if value else app.config.get(key)
                    except ValueError:
                        pass
                if key in {"LLM_PRICE_INPUT_PER_1K", "LLM_PRICE_OUTPUT_PER_1K", "PDF_TEXT_MIN_RATIO"}:
                    try:
                        app.config[key] = float(value) if value else app.config.get(key)
                    except ValueError:
                        pass
            flash("Settings saved to .env. Restart may be required for some changes.")
            return redirect(url_for("settings"))

        field_values = {
            field["key"]: _current_setting_value(app, field["key"])
            for field in _SETTINGS_FIELDS
        }
        provider_options = _provider_option_items(include_unconfigured=True)
        default_provider = _resolve_default_provider(
            _normalize_provider_key(Config.LLM_PROVIDER), provider_options
        )
        base_fields = []
        provider_field_map = {"1": [], "2": [], "3": []}
        for field in _SETTINGS_FIELDS:
            key = field["key"]
            if key.startswith("CUSTOM_LLM_PROVIDER_1_"):
                provider_field_map["1"].append(field)
            elif key.startswith("CUSTOM_LLM_PROVIDER_2_"):
                provider_field_map["2"].append(field)
            elif key.startswith("CUSTOM_LLM_PROVIDER_3_"):
                provider_field_map["3"].append(field)
            else:
                base_fields.append(field)

        def clean_value(value):
            return (value or "").strip().strip('"')

        def has_provider_info(index):
            default_name = f"Other {index}"
            name_key = f"CUSTOM_LLM_PROVIDER_{index}_NAME"
            api_key_key = f"CUSTOM_LLM_PROVIDER_{index}_API_KEY"
            base_url_key = f"CUSTOM_LLM_PROVIDER_{index}_API_BASE_URL"
            model_key = f"CUSTOM_LLM_PROVIDER_{index}_DEFAULT_MODEL"
            models_key = f"CUSTOM_LLM_PROVIDER_{index}_MODELS"
            name = clean_value(field_values.get(name_key))
            if name and name != default_name:
                return True
            return any(
                clean_value(field_values.get(key))
                for key in [api_key_key, base_url_key, model_key, models_key]
            )

        provider_groups = []
        for index in ["1", "2", "3"]:
            name = clean_value(field_values.get(f"CUSTOM_LLM_PROVIDER_{index}_NAME"))
            label = name or f"Other {index}"
            provider_groups.append(
                {
                    "id": index,
                    "label": label,
                    "fields": provider_field_map[index],
                    "has_info": has_provider_info(index),
                }
            )
        return render_template(
            "settings.html",
            base_fields=base_fields,
            provider_groups=provider_groups,
            values=field_values,
            provider_options=provider_options,
            provider_model_options=_provider_model_option_items(),
            provider_default_models=_provider_default_models(),
            default_provider=default_provider,
        )

    @app.route("/jobs/<int:job_id>/rerun", methods=["POST"])
    def rerun_job(job_id):
        job = GradingJob.query.get_or_404(job_id)
        rubric = db.session.get(RubricVersion, job.rubric_version_id)
        if not rubric or rubric.status != RubricStatus.APPROVED:
            flash("Approved grading guide required to rerun job.")
            return redirect(url_for("job_detail", job_id=job.id))

        provider_key = _resolve_provider_from_form(
            request.form, app.config.get("LLM_PROVIDER")
        )
        provider_cfg = _provider_config(provider_key)
        selected_model, _custom_used = _resolve_model_from_form(
            request.form, provider_cfg["default_model"]
        )

        grade_result = GradeResult(
            submission_id=job.submission_id,
            rubric_version_id=job.rubric_version_id,
            total_points=None,
            json_result="{}",
            rendered_text="",
            raw_response="",
            error_message="",
        )
        db.session.add(grade_result)
        db.session.commit()

        new_job = GradingJob(
            assignment_id=job.assignment_id,
            submission_id=job.submission_id,
            rubric_version_id=job.rubric_version_id,
            status=JobStatus.QUEUED,
            llm_provider=provider_key,
            llm_model=selected_model,
        )
        db.session.add(new_job)
        db.session.commit()
        queue_id = enqueue_submission_job(new_job.id)
        new_job.queue_job_id = queue_id
        db.session.commit()
        flash(f"Queued rerun as job {new_job.id}.")
        return redirect(url_for("job_detail", job_id=new_job.id))

    @app.route("/data/<path:filepath>")
    def data_file(filepath):
        return send_from_directory(DATA_DIR, filepath)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
