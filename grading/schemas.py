import json

REQUIRED_GRADE_KEYS = {"total_points", "parts", "deductions", "final_feedback"}


def validate_grade_result(data):
    if not isinstance(data, dict):
        return False, "Grade result is not an object"
    missing = REQUIRED_GRADE_KEYS - set(data.keys())
    if missing:
        return False, f"Missing keys: {', '.join(sorted(missing))}"
    if not isinstance(data.get("parts"), list):
        return False, "parts must be a list"
    if not isinstance(data.get("deductions"), list):
        return False, "deductions must be a list"
    return True, ""


def render_grade_output(data):
    total_points = data.get("total_points")
    parts = data.get("parts", [])
    part_text = ", ".join(
        f"{p.get('points_awarded')}/{p.get('points_possible')}" for p in parts
    )

    lines = [f"TOTAL: {total_points}", f"PARTS: {part_text}", ""]

    deductions = data.get("deductions", [])
    for deduction in deductions:
        reason = deduction.get("reason", "")
        hint = deduction.get("hint", "")
        lines.append(f"- {reason} Hint: {hint}")

    if deductions:
        lines.append("")

    final_feedback = data.get("final_feedback", "")
    lines.append(final_feedback)

    return "\n".join(lines).strip()


def safe_json_loads(text):
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, str(exc)
