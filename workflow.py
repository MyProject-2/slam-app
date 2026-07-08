"""
workflow.py — the SLAM automation definitions.

Each lifecycle event type (starter / mover / leaver) maps to an ordered
list of steps. Each step optionally touches a downstream "system" and/or
is flagged as a GDPR-relevant checkpoint.
"""

SYSTEMS = [
    {"key": "corehr", "name": "Core HR", "role": "system of record"},
    {"key": "accessit", "name": "Access / IT", "role": "okta-style provisioning"},
    {"key": "payroll", "name": "Payroll", "role": "comp & banking"},
    {"key": "talent", "name": "Talent", "role": "recruiting handoff"},
    {"key": "comms", "name": "Comms", "role": "slack / email groups"},
    {"key": "snowflake", "name": "Snowflake", "role": "people analytics"},
]

PIPELINES = {
    "starter": [
        {"label": "Core HR record created", "system": "corehr"},
        {"label": "Welcome message sent", "system": None, "welcome_message": True},
        {"label": "GDPR consent check", "system": None, "gdpr": True},
        {"label": "Access + accounts provisioned", "system": "accessit"},
        {"label": "Payroll & banking linked", "system": "payroll"},
        {"label": "Comms groups added", "system": "comms"},
        {"label": "Synced to Snowflake", "system": "snowflake"},
    ],
    "mover": [
        {"label": "Core HR record updated", "system": "corehr"},
        {"label": "Comp band re-evaluated", "system": "payroll"},
        {"label": "Access rights re-scoped", "system": "accessit"},
        {"label": "Reporting line reassigned", "system": "talent"},
        {"label": "Synced to Snowflake", "system": "snowflake"},
    ],
    "leaver": [
        {"label": "Last day confirmed", "system": "corehr"},
        {"label": "GDPR retention rule applied", "system": None, "gdpr": True},
        {"label": "Access revoked on schedule", "system": "accessit"},
        {"label": "Final payroll run flagged", "system": "payroll"},
        {"label": "Comms access removed", "system": "comms"},
        {"label": "Synced to Snowflake", "system": "snowflake"},
    ],
}
