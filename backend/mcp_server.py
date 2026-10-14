from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from document_analysis import analyze_job_description, compare_resume_to_job


mcp = FastMCP("InterviewAI")


@mcp.tool()
def analyze_job_requirements(job_description: str) -> dict[str, Any]:
    """Extract skills, experience requirements, and responsibilities from a job description."""
    return analyze_job_description(job_description)


@mcp.tool()
def compare_candidate_to_job(resume_text: str, job_description: str) -> dict[str, Any]:
    """Compare a resume with a job description and return matching and missing skills."""
    return compare_resume_to_job(resume_text, job_description)


if __name__ == "__main__":
    mcp.run(transport="stdio")
