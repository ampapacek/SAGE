SYSTEM_PROMPT = (
    "You are a strict grading assistant. Return only valid JSON. No extra text. "
    "Always identify mistakes, what is incorrect, and why."
)


def build_grading_prompt(
    assignment_text,
    rubric_text,
    reference_solution_text,
    student_text,
):
    return f"""
Grade the submission using the grading guide and reference solution.

Rules:
- Return only valid JSON that matches the schema exactly.
- Grade parts independently. Award partial credit when reasoning is partly correct.
- If a part is missing, award 0 for that part and explain why.
- Always state where the mistakes are, what is incorrect, and why.
- Provide clear, specific reasons and hints for deductions.
- Use the "notes" field per part to describe mistakes or confirm correctness.

Assignment:
{assignment_text}

Grading Guide:
{rubric_text}

Reference Solution:
{reference_solution_text}

Student Submitted Text (if any):
{student_text}

Output JSON schema:
{{
  "total_points": number,
  "parts": [{{"part_id": "1", "points_awarded": number, "points_possible": number, "notes": string}}],
  "deductions": [{{"part_id":"1", "points_deducted": number, "reason": string, "hint": string}}],
  "final_feedback": string
}}
""".strip()


def build_rubric_draft_prompt(assignment_text):
    return f"""
Create a grading guide and reference solution for the assignment.
Return JSON only with keys rubric_text and reference_solution_text.

Assignment:
{assignment_text}

Output JSON schema:
{{
  "rubric_text": string,
  "reference_solution_text": string
}}
""".strip()
