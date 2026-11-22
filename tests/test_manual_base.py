from preorderproof.manual import resolve_number


def test_old_number_points_at_new_different_section():
    sections = [
        {"language": "en", "number": "7", "title": "Warranty"},
        {"language": "en", "number": "8", "title": "Battery safety"},
    ]
    assert resolve_number(sections, "en", "7")["title"] == "Warranty"
