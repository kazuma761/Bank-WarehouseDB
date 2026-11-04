"""SQL explanation prompt — translates generated SQL into plain English.

Returned to the user alongside the SQL so they can verify what the
query will do before (or after) execution.
"""

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You explain SQL queries in plain English for non-technical users.
Be concise. Maximum 3 sentences. No technical jargon."""

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(question: str, sql: str) -> str:
    """Return a prompt asking for a plain-English explanation of the SQL.

    Provides the user's original question as context so the explanation
    can frame what the query answers, not just what it does mechanically.
    """
    return (
        f"The user asked: \"{question}\"\n\n"
        f"This SQL query was generated to answer it:\n\n"
        f"{sql}\n\n"
        "Explain in plain English what this query does and how it answers "
        "the user's question. Maximum 3 sentences. No technical jargon."
    )
