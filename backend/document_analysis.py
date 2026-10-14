from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, cast

import spacy
from spacy.language import Language
from spacy.pipeline import EntityRuler


SKILL_TERMS = (
    "Python",
    "JavaScript",
    "TypeScript",
    "Java",
    "C++",
    "C#",
    "SQL",
    "PostgreSQL",
    "MySQL",
    "FastAPI",
    "Django",
    "Flask",
    "React",
    "REST API",
    "AWS",
    "Azure",
    "Docker",
    "Kubernetes",
    "Git",
    "unit testing",
    "Pytest",
    "Excel",
    "Power BI",
    "Tableau",
    "Bloomberg",
    "GAAP",
    "IFRS",
    "financial modeling",
    "financial analysis",
    "valuation",
    "budgeting",
    "forecasting",
    "risk management",
    "audit",
    "investment research",
    "portfolio management",
    "regulatory compliance",
    "AML",
    "KYC",
    "cash flow",
    "accounting",
)

SECTION_ALIASES = {
    "summary": "summary",
    "professional summary": "summary",
    "profile": "summary",
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "employment": "experience",
    "education": "education",
    "skills": "skills",
    "technical skills": "skills",
    "projects": "projects",
    "certifications": "certifications",
    "licenses": "certifications",
}

EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
LINK_PATTERN = re.compile(r"(?:https?://|www\.)\S+|linkedin\.com/\S+", re.IGNORECASE)
ADDRESS_PATTERN = re.compile(
    r"(?im)^\s*\d{1,6}\s+[\w .'-]+\s+(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd)\b[^\n]*$"
)
YEARS_PATTERN = re.compile(
    r"\b(?:(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?\s+experience|"
    r"experience\s+of\s+(\d{1,2})\s*\+?\s*(?:years?|yrs?))\b",
    re.IGNORECASE,
)
RESPONSIBILITY_VERBS = re.compile(
    r"\b(?:build|create|design|develop|maintain|implement|analy[sz]e|manage|"
    r"prepare|report|lead|support|collaborate|review|debug|test|monitor|"
    r"reconcile|forecast|advise|investigate|ensure|deliver|own)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _nlp() -> Language:
    pipeline: Language = spacy.blank("en")
    ruler = cast(EntityRuler, pipeline.add_pipe("entity_ruler"))
    ruler.add_patterns(
        [{"label": "SKILL", "pattern": term} for term in SKILL_TERMS]
    )
    return pipeline


def extract_skills(text: str) -> list[str]:
    matches: dict[str, tuple[int, str]] = {}
    document = _nlp()(text[:30_000])
    for entity in document.ents:
        if entity.label_ == "SKILL":
            canonical = next(
                (term for term in SKILL_TERMS if term.casefold() == entity.text.casefold()),
                entity.text,
            )
            matches.setdefault(canonical.casefold(), (entity.start_char, canonical))
    return [term for _, term in sorted(matches.values())]


def extract_resume_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_section = "other"
    for line in text.splitlines():
        normalized_heading = re.sub(r"[:\s]+$", "", line.strip()).casefold()
        heading = SECTION_ALIASES.get(normalized_heading)
        if heading is not None:
            current_section = heading
            sections.setdefault(current_section, [])
        elif line.strip():
            sections.setdefault(current_section, []).append(line.strip())
    return {
        section: "\n".join(lines).strip()
        for section, lines in sections.items()
        if lines
    }


def extract_contact_details(text: str) -> dict[str, str | None]:
    email = EMAIL_PATTERN.search(text)
    phone = PHONE_PATTERN.search(text)
    linkedin = re.search(
        r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Z0-9_%/-]+",
        text,
        re.IGNORECASE,
    )
    return {
        "email": email.group(0) if email else None,
        "phone": phone.group(0).strip() if phone else None,
        "linkedin": linkedin.group(0).rstrip(".,;") if linkedin else None,
    }


def privacy_filtered_resume_context(text: str) -> dict[str, Any]:
    sections = extract_resume_sections(text)
    first_content_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    relevant = [
        f"{section}: {content}"
        for section, content in sections.items()
        if section in {"summary", "experience", "skills", "projects", "education", "certifications"}
    ]
    if not relevant:
        filtered = EMAIL_PATTERN.sub("[email removed]", text)
        filtered = PHONE_PATTERN.sub("[phone removed]", filtered)
        filtered = LINK_PATTERN.sub("[link removed]", filtered)
        filtered = ADDRESS_PATTERN.sub("[address removed]", filtered)
        relevant = [filtered]

    if _is_probable_name(first_content_line):
        relevant = [
            re.sub(
                rf"(?im)^\s*{re.escape(first_content_line)}\s*$",
                "",
                section,
            ).strip()
            for section in relevant
        ]

    filtered_sections = "\n".join(relevant)
    filtered_sections = EMAIL_PATTERN.sub("[email removed]", filtered_sections)
    filtered_sections = PHONE_PATTERN.sub("[phone removed]", filtered_sections)
    filtered_sections = LINK_PATTERN.sub("[link removed]", filtered_sections)
    filtered_sections = ADDRESS_PATTERN.sub("[address removed]", filtered_sections)
    skills = extract_skills(filtered_sections)
    return {
        "skills": skills,
        "relevant_background": filtered_sections[:5_000],
        "experience_years": _extract_years(filtered_sections),
    }


def analyze_job_description(job_description: str) -> dict[str, Any]:
    text = job_description[:15_000]
    skills = extract_skills(text)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    responsibilities: list[str] = []
    for sentence in sentences:
        normalized = sentence.strip(" \t-*•")
        if (
            len(normalized) >= 12
            and RESPONSIBILITY_VERBS.search(normalized)
            and normalized not in responsibilities
        ):
            responsibilities.append(normalized[:300])
        if len(responsibilities) == 8:
            break
    experience_years = _extract_years(text)
    return {
        "required_skills": skills,
        "minimum_experience_years": experience_years,
        "responsibilities": responsibilities,
    }


def compare_resume_to_job(resume_text: str, job_description: str) -> dict[str, Any]:
    resume = privacy_filtered_resume_context(resume_text)
    requirements = analyze_job_description(job_description)
    resume_skills = {skill.casefold(): skill for skill in resume["skills"]}
    job_skills = requirements["required_skills"]
    matched = [skill for skill in job_skills if skill.casefold() in resume_skills]
    missing = [skill for skill in job_skills if skill.casefold() not in resume_skills]
    return {
        "resume_skills": resume["skills"],
        "required_skills": job_skills,
        "matched_skills": matched,
        "missing_skills": missing,
        "minimum_experience_years": requirements["minimum_experience_years"],
        "candidate_experience_years": resume["experience_years"],
        "responsibilities": requirements["responsibilities"],
    }


def _extract_years(text: str) -> int | None:
    match = YEARS_PATTERN.search(text)
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    return int(value) if value else None


def _is_probable_name(line: str) -> bool:
    return (
        1 < len(line.split()) <= 4
        and len(line) <= 50
        and bool(re.fullmatch(r"[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){1,3}", line))
        and not re.search(
            r"\b(?:engineer|developer|analyst|accountant|manager|skills|experience)\b",
            line,
            re.IGNORECASE,
        )
    )
