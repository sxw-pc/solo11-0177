"""Provisional printed-manual section reference lookup."""


def resolve_number(sections, language, number):
    return next((row for row in sections
                 if row["language"] == language and row["number"] == number), None)
