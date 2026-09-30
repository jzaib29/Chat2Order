# Build verification

Verified on 2026-09-30, Python 3.12 / Linux.

- Direct and transitive dependency versions resolved and installed together.
- Python files compiled successfully.
- Small offline smoke check passed: quantity replacement, capacity conflicts, missing pickup details, cancellation, approval checks, and customer answers.
- Streamlit AppTest smoke passed: startup, navigation to Order desk, review, and approval.
- Targeted checks passed for retaining existing approvals after an inbox replacement and avoiding inherited quantities after cancellation.
- Actual Groq requests during verification: **0**.

The live Groq path was not called with a real API key. No automatic live-LLM tests are included. This is a single-owner, session-based hackathon MVP; see README.md for its exact scope and deployment steps.
